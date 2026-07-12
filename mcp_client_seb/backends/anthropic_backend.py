"""Anthropic (Claude) backend — a manual tool-use loop via the anthropic SDK.

Implements the hooks in :class:`~mcp_client_seb.backends.base.BaseAgent`. The
``anthropic`` package is imported lazily inside ``create()`` so the rest of the
client (and the Ollama backend) works without it installed.
"""

from __future__ import annotations

import os
from typing import Any

from ..manager import MCPManager
from .base import (
    BackendError,
    BackendUnavailable,
    BaseAgent,
    ChatSettings,
    ToolCall,
    Turn,
)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 8192  # comfortably under the SDK's non-streaming timeout guard


class AnthropicAgent(BaseAgent):
    name = "anthropic"

    def __init__(self, manager: MCPManager, client: Any):
        super().__init__(manager)
        self._client = client

    # -- construction / identity -------------------------------------------

    @classmethod
    def create(cls, manager: MCPManager, settings: ChatSettings) -> "AnthropicAgent":
        try:
            import anthropic  # noqa: PLC0415 — lazy so this backend is optional
        except ImportError as e:
            raise BackendUnavailable(
                "the 'anthropic' package is not installed (pip install anthropic)"
            ) from e
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise BackendUnavailable("no ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) set")
        # settings.model is Ollama-only; the Claude model is MODEL above.
        return cls(manager, anthropic.Anthropic())

    @property
    def label(self) -> str:
        return f"Anthropic ({MODEL})"

    # -- provider hooks ----------------------------------------------------

    def _tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.exposed_name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tool_refs()
        ]

    def _complete(self) -> Turn:
        try:
            # Recomputed each turn so tools added mid-conversation (e.g. a freshly
            # spawned subagent's tools) are offered on the very next model call.
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self.system_prompt,
                thinking={"type": "adaptive", "display": "summarized"},
                tools=self._tool_specs(),
                messages=self._messages,
            )
        except Exception as e:  # noqa: BLE001 — surface SDK/network errors to the loop
            raise BackendError(f"Anthropic request failed: {type(e).__name__}: {e}") from e

        turn = Turn(raw=response.content)
        if getattr(response, "stop_reason", None) == "refusal":
            turn.refused = True
            turn.refusal_message = "Claude declined to respond to this request."
            return turn

        texts: list[str] = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "thinking":
                turn.thinking += getattr(block, "thinking", "") or ""
            elif block.type == "tool_use":
                turn.tool_calls.append(
                    ToolCall(name=block.name, arguments=block.input or {}, id=block.id)
                )
        turn.text = "".join(texts)
        return turn

    def _record_assistant(self, turn: Turn) -> None:
        # Append the native content blocks so thinking/tool_use replay correctly.
        self._messages.append({"role": "assistant", "content": turn.raw})

    def _record_tool_results(self, results: list[tuple[ToolCall, list, bool]]) -> None:
        # All tool_result blocks for one turn go in a single user message.
        # tool_result content may itself hold image blocks (e.g. a screenshot),
        # which Claude reads as vision.
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": _to_tool_content(parts),
                "is_error": is_error,
            }
            for call, parts, is_error in results
        ]
        self._messages.append({"role": "user", "content": blocks})


def _to_tool_content(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalized parts -> Anthropic tool_result content blocks (text + image)."""
    blocks: list[dict[str, Any]] = []
    for p in parts:
        if p.get("type") == "image":
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": p.get("media_type") or "image/png",
                    "data": p["data"],
                },
            })
        else:
            blocks.append({"type": "text", "text": p.get("text", "")})
    return blocks or [{"type": "text", "text": "(no content)"}]
