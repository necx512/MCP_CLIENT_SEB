"""Ollama backend — a manual tool-use loop via Ollama's native /api/chat.

Implements the hooks in :class:`~mcp_client_seb.backends.base.BaseAgent` using
only the standard library (no extra dependency). Tool calling requires a
tool-capable model to be pulled, e.g. ``llama3.1``, ``qwen2.5``, ``qwen3``,
``mistral-nemo``, ``llama3.2``.

Model:  ``--model`` / ``$OLLAMA_MODEL`` (default ``llama3.1``).
Host:   ``--ollama-host`` / ``$OLLAMA_HOST`` (default ``http://localhost:11434``).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .. import __version__
from ..manager import MCPManager
from .base import (
    BackendError,
    BackendUnavailable,
    BaseAgent,
    ChatSettings,
    ToolCall,
    Turn,
)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1"
CHAT_TIMEOUT = 600  # seconds — local generation can be slow
PING_TIMEOUT = 5    # seconds — allow for remote hosts / proxy cold starts


# -- HTTP helpers (stdlib only) --------------------------------------------

def resolve_host(host: str | None) -> str:
    """Normalise a host string. ``$OLLAMA_HOST`` is often bare (``host:port``)."""
    host = host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host.rstrip("/")


def _headers(api_key: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Common request headers.

    A non-default ``User-Agent`` is required: urllib's default
    (``Python-urllib/x.y``) is rejected with HTTP 403 by some reverse proxies /
    WAFs sitting in front of a remote Ollama (e.g. RunPod). Optional Bearer auth
    for an authenticating proxy.
    """
    headers = {"User-Agent": f"mcp_client_seb/{__version__}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra:
        headers.update(extra)
    return headers


def _get_json(url: str, timeout: float, api_key: str | None = None) -> Any:
    req = urllib.request.Request(url, headers=_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — trusted host
        return json.loads(resp.read().decode())


def _post_json(url: str, payload: dict[str, Any], timeout: float,
               api_key: str | None = None) -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers=_headers(api_key, {"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def ollama_available(host: str, api_key: str | None = None) -> bool:
    try:
        _get_json(f"{host}/api/tags", PING_TIMEOUT, api_key)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ollama_models(host: str, api_key: str | None = None) -> list[str]:
    try:
        data = _get_json(f"{host}/api/tags", PING_TIMEOUT, api_key)
        return [m.get("name", "") for m in data.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return []


def _tool_text(parts: list[dict[str, Any]], pending_images: list[str]) -> str:
    """Normalized parts -> text for an Ollama tool message; images are queued."""
    out: list[str] = []
    for p in parts:
        if p.get("type") == "image":
            pending_images.append(p["data"])
            out.append("[image sent in the following message]")
        else:
            out.append(p.get("text", ""))
    return " ".join(out).strip() or "(no content)"


class OllamaAgent(BaseAgent):
    name = "ollama"

    def __init__(self, manager: MCPManager, host: str, model: str, api_key: str | None = None,
                 think: bool | None = None, keep_alive: str | None = None):
        self._host = host
        self.model = model
        self._api_key = api_key
        self._think = think
        self._keep_alive = keep_alive
        super().__init__(manager)
        pulled = ollama_models(host, api_key)
        if pulled and not any(m == model or m.split(":")[0] == model for m in pulled):
            self.warning = (
                f"model {model!r} is not pulled on {host} "
                f"(available: {', '.join(pulled) or 'none'}) — pull it: ollama pull {model}"
            )

    # -- construction / identity -------------------------------------------

    @classmethod
    def create(cls, manager: MCPManager, settings: ChatSettings) -> "OllamaAgent":
        host = resolve_host(settings.host)
        api_key = settings.api_key or os.environ.get("OLLAMA_API_KEY")
        if not ollama_available(host, api_key):
            raise BackendUnavailable(
                f"no Ollama server reachable at {host} (start it with: ollama serve)"
            )
        model = settings.model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL

        think = settings.think
        if think is None:
            env_think = os.environ.get("OLLAMA_THINK")
            if env_think is not None:
                think = env_think.strip().lower() in ("1", "true", "on", "yes")
        # Only sent when explicitly set (flag or env); otherwise Ollama uses its own default.
        keep_alive = settings.keep_alive or os.environ.get("OLLAMA_KEEP_ALIVE")
        return cls(manager, host=host, model=model, api_key=api_key,
                   think=think, keep_alive=keep_alive)

    @property
    def label(self) -> str:
        return f"Ollama ({self.model})"

    # -- provider hooks ----------------------------------------------------

    def _initial_messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system_prompt}]

    def _tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.exposed_name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in self._tool_refs()
        ]

    def _complete(self) -> Turn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages,
            "stream": False,
        }
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive
        if self._think is not None:
            payload["think"] = self._think  # False = skip the model's <think> phase (much faster)
        tools = self._tool_specs()  # recomputed each turn (see AnthropicAgent._complete)
        if tools:
            payload["tools"] = tools
        try:
            resp = _post_json(f"{self._host}/api/chat", payload, CHAT_TIMEOUT, self._api_key)
        except urllib.error.URLError as e:
            raise BackendError(f"Ollama request failed: {e}") from e
        except (OSError, ValueError) as e:
            raise BackendError(f"Ollama error: {type(e).__name__}: {e}") from e

        return self._parse(resp.get("message", {}) or {})

    def _parse(self, wire: dict[str, Any]) -> Turn:
        """An Ollama ``message`` object -> Turn.

        Ollama's response is already plain JSON, so ``raw`` and ``wire`` are the
        same object — an edited response needs no special handling here.
        """
        turn = Turn(
            raw=wire,
            wire=wire,
            text=wire.get("content", "") or "",
            thinking=wire.get("thinking", "") or "",
        )
        for call in wire.get("tool_calls") or []:
            fn = call.get("function", {}) or {}
            args = fn.get("arguments", {}) or {}
            if isinstance(args, str):  # some models emit a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            turn.tool_calls.append(ToolCall(name=fn.get("name", ""), arguments=args))
        return turn

    def _record_assistant(self, turn: Turn) -> None:
        message = turn.raw
        record: dict[str, Any] = {"role": "assistant", "content": message.get("content", "")}
        if message.get("tool_calls"):
            record["tool_calls"] = message["tool_calls"]
        self._messages.append(record)

    def _record_tool_results(self, results: list[tuple[ToolCall, list, bool]]) -> None:
        # Ollama can't carry an image inside a "tool" message: answer each tool in
        # text (with a placeholder for images), then send the images in a following
        # user message. Needs a vision-capable model to actually see them.
        pending_images: list[str] = []
        for call, parts, _is_error in results:
            text = _tool_text(parts, pending_images)
            self._messages.append({"role": "tool", "tool_name": call.name, "content": text})
        for img in pending_images:
            self._messages.append({
                "role": "user",
                "content": "Attached image (tool result).",
                "images": [img],
            })
