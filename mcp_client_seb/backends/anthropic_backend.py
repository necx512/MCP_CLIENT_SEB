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
    jsonify,  # re-exported by base so the optional-import lives in one place
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

        turn = self._parse({
            "stop_reason": getattr(response, "stop_reason", None),
            "content": jsonify(response.content),
        })
        # Untouched path: replay the SDK's own block objects, exactly as this did
        # before interception existed. Only an edited response swaps in the dict
        # form (via _parse), so a serialization quirk can only ever surface on a
        # turn a human deliberately edited — never on a normal one.
        turn.raw = response.content
        return turn

    def _parse(self, wire: dict[str, Any]) -> Turn:
        """A Message-shaped dict -> Turn. Mirrors the wire format, not the SDK.

        Note for editing: ``thinking`` blocks must be replayed to the API
        byte-for-byte — they carry a signature the API verifies, and a modified
        block is rejected. Their text is over the eliding threshold in practice,
        so the interceptor hands the human a placeholder rather than the real
        signature, which keeps an edit elsewhere in the response from
        accidentally corrupting one.
        """
        blocks = wire.get("content") or []
        turn = Turn(raw=blocks, wire=wire)
        if wire.get("stop_reason") == "refusal":
            turn.refused = True
            turn.refusal_message = "Claude declined to respond to this request."
            return turn

        texts: list[str] = []
        for block in blocks:
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "thinking":
                turn.thinking += block.get("thinking", "") or ""
            elif btype == "tool_use":
                turn.tool_calls.append(ToolCall(
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                    id=block.get("id"),
                ))
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
