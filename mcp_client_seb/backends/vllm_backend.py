"""vLLM backend — a manual tool-use loop via vLLM's OpenAI-compatible API.

Implements the hooks in :class:`~mcp_client_seb.backends.base.BaseAgent` using
only the standard library (no extra dependency). vLLM exposes an
OpenAI-compatible server, so this talks to ``/v1/chat/completions`` and
``/v1/models`` — the same wire format the OpenAI SDK uses. Tool calling requires
the server to be started with a tool-capable model **and** a tool-call parser,
e.g.::

    vllm serve Qwen/Qwen2.5-7B-Instruct --enable-auto-tool-choice \\
        --tool-call-parser hermes

Model:  ``--model`` / ``$VLLM_MODEL`` (default: the first model the server
        reports at ``/v1/models``).
Host:   ``--vllm-host`` / ``$VLLM_HOST`` (default ``http://localhost:8000``).
Key:    ``--vllm-api-key`` / ``$VLLM_API_KEY`` / ``$OPENAI_API_KEY`` (optional;
        matches the server's ``--api-key`` when it was started with one).
"""

from __future__ import annotations

import json
import os
import re
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

DEFAULT_HOST = "http://localhost:8000"
CHAT_TIMEOUT = 600  # seconds — generation can be slow for large contexts
PING_TIMEOUT = 5    # seconds — allow for remote hosts / proxy cold starts


# -- HTTP helpers (stdlib only) --------------------------------------------

def resolve_host(host: str | None) -> str:
    """Normalise a host string. ``$VLLM_HOST`` is often bare (``host:port``)."""
    host = host or os.environ.get("VLLM_HOST") or DEFAULT_HOST
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host.rstrip("/")


def resolve_api_key(api_key: str | None) -> str | None:
    """The Bearer token for the server, if any (falls back to OpenAI's env var)."""
    return (api_key or os.environ.get("VLLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY"))


def _headers(api_key: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Common request headers.

    A non-default ``User-Agent`` avoids the HTTP 403 some reverse proxies / WAFs
    return for urllib's default (``Python-urllib/x.y``). Optional Bearer auth for
    a server started with ``--api-key`` (or an authenticating proxy).
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


def vllm_models(host: str, api_key: str | None = None) -> list[str]:
    """The model ids the server advertises at ``/v1/models`` (empty on failure)."""
    try:
        data = _get_json(f"{host}/v1/models", PING_TIMEOUT, api_key)
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except (urllib.error.URLError, OSError, ValueError):
        return []


def vllm_available(host: str, api_key: str | None = None) -> bool:
    try:
        _get_json(f"{host}/v1/models", PING_TIMEOUT, api_key)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


#: A closed ``<think>…</think>`` block, as reasoning models (e.g. Qwen3) emit
#: inline in ``content`` when the server has no ``--reasoning-parser`` loaded.
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
#: A dangling, *unclosed* ``<think>`` to end of content — happens when the reply
#: is cut short or the model omits the closing tag. Without this the raw tag
#: leaks and the answer is lost.
_THINK_OPEN_RE = re.compile(r"<think>(.*)$", re.DOTALL)


def _split_inline_think(content: str) -> tuple[str, str]:
    """Split inline ``<think>`` reasoning off ``content``.

    Returns ``(visible_text, thinking)``. Handles closed blocks anywhere and a
    trailing unclosed block. When the server runs a reasoning parser the thinking
    arrives in its own field and this is a no-op (no ``<think>`` tag to find).
    """
    thinking: list[str] = []
    text = _THINK_RE.sub(lambda m: thinking.append(m.group(1).strip()) or "", content)
    m = _THINK_OPEN_RE.search(text)  # a leftover unclosed <think>…
    if m:
        thinking.append(m.group(1).strip())
        text = text[:m.start()]
    return text.strip(), "\n".join(t for t in thinking if t).strip()


def _image_data_url(part: dict[str, Any]) -> str:
    media = part.get("media_type") or "image/png"
    return f"data:{media};base64,{part.get('data', '')}"


def _tool_text(parts: list[dict[str, Any]], pending_images: list[str]) -> str:
    """Normalized parts -> text for a ``tool`` message; images are queued.

    OpenAI-style ``tool`` messages carry only text, so an image part is replaced
    with a placeholder and its data-URL queued for a following ``user`` message.
    """
    out: list[str] = []
    for p in parts:
        if p.get("type") == "image":
            pending_images.append(_image_data_url(p))
            out.append("[image sent in the following message]")
        else:
            out.append(p.get("text", ""))
    return " ".join(out).strip() or "(no content)"


class VLLMAgent(BaseAgent):
    name = "vllm"

    def __init__(self, manager: MCPManager, host: str, model: str,
                 api_key: str | None = None):
        self._host = host
        self.model = model
        self._api_key = api_key
        super().__init__(manager)

    # -- construction / identity -------------------------------------------

    @classmethod
    def create(cls, manager: MCPManager, settings: ChatSettings) -> "VLLMAgent":
        host = resolve_host(settings.host)
        api_key = resolve_api_key(settings.api_key)
        served = vllm_models(host, api_key)
        if not served and not vllm_available(host, api_key):
            raise BackendUnavailable(
                f"no vLLM server reachable at {host} "
                "(start it with: vllm serve <model> --enable-auto-tool-choice "
                "--tool-call-parser <parser>)"
            )
        model = settings.model or os.environ.get("VLLM_MODEL")
        if not model:
            # vLLM needs the request's model to match a served one; without an
            # explicit choice, use the first the server advertises.
            if not served:
                raise BackendUnavailable(
                    f"vLLM server at {host} advertises no models — pass --model")
            model = served[0]
        agent = cls(manager, host=host, model=model, api_key=api_key)
        if served and model not in served:
            agent.warning = (
                f"model {model!r} is not served by {host} "
                f"(available: {', '.join(served) or 'none'})"
            )
        return agent

    @property
    def label(self) -> str:
        return f"vLLM ({self.model})"

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
        tools = self._request_tools()  # recomputed each turn; interceptor may override
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            resp = _post_json(f"{self._host}/v1/chat/completions", payload,
                              CHAT_TIMEOUT, self._api_key)
        except urllib.error.HTTPError as e:
            detail = _http_error_detail(e)
            raise BackendError(f"vLLM request failed (HTTP {e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise BackendError(f"vLLM request failed: {e}") from e
        except (OSError, ValueError) as e:
            raise BackendError(f"vLLM error: {type(e).__name__}: {e}") from e

        choices = resp.get("choices") or []
        if not choices:
            raise BackendError("vLLM returned no choices")
        return self._parse(choices[0].get("message", {}) or {})

    def _parse(self, wire: dict[str, Any]) -> Turn:
        """An OpenAI ``message`` object -> Turn.

        The response is already plain JSON, so ``raw`` and ``wire`` are the same
        object. Tool-call arguments arrive as a JSON *string* (OpenAI's format),
        so they're decoded here.
        """
        text = wire.get("content") or ""
        # A reasoning parser (--reasoning-parser) puts thinking in its own field;
        # without one, models like Qwen3 emit it inline as <think>…</think> in
        # content. Handle both so the visible answer is always clean.
        thinking = wire.get("reasoning_content") or wire.get("reasoning") or ""
        if not thinking and "<think>" in text:
            text, thinking = _split_inline_think(text)
        turn = Turn(raw=wire, wire=wire, text=text, thinking=thinking)
        for call in wire.get("tool_calls") or []:
            fn = call.get("function", {}) or {}
            args = fn.get("arguments", {}) or {}
            if isinstance(args, str):  # OpenAI encodes arguments as a JSON string
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            turn.tool_calls.append(ToolCall(
                name=fn.get("name", ""), arguments=args, id=call.get("id")))
        return turn

    def _record_assistant(self, turn: Turn) -> None:
        message = dict(turn.raw)
        # The API rejects a null content on replay; normalise to "".
        message["content"] = message.get("content") or ""
        self._messages.append(message)

    def _record_tool_results(self, results: list[tuple[ToolCall, list, bool]]) -> None:
        # Each result is a "tool" message keyed by the model's own tool_call_id.
        # Images can't ride inside a tool message, so they follow in a user
        # message (needs a vision-capable model to actually be seen).
        pending_images: list[str] = []
        for call, parts, _is_error in results:
            text = _tool_text(parts, pending_images)
            self._messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": text})
        for url in pending_images:
            self._messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "Attached image (tool result)."},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            })


def _http_error_detail(e: urllib.error.HTTPError) -> str:
    """Best-effort extraction of vLLM's JSON error body for a clearer message."""
    try:
        body = json.loads(e.read().decode())
    except (ValueError, OSError):
        return e.reason or "unknown error"
    err = body.get("error") or body.get("message") or body
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)
