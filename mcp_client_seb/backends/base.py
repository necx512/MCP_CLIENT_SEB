"""Backend-agnostic agent scaffolding.

Every LLM backend is a :class:`BaseAgent` subclass. The agentic tool-use loop
is identical across providers — only the wire format differs — so that loop
lives here once, and each backend implements a handful of small hooks:

    create()               classmethod: build an instance, or raise
                           BackendUnavailable (this is the unified equivalent
                           of the old make_client() + constructor split).
    label                  human-readable name incl. the model.
    _tool_specs()          the MCP tools in this provider's schema shape.
    _complete()            one blocking API call -> a normalized Turn.
    _record_assistant()    append the assistant turn to history.
    _record_tool_results() append this turn's tool results to history.
    _initial_messages()    the starting message list (e.g. a system turn).

The loop, refusal handling, thinking/text emission, and the tool fan-out are
shared. See ``anthropic_backend`` and ``ollama_backend`` for the two ~90-line
implementations.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..manager import MCPManager, ToolRef, parts_to_text

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools provided by one or more "
    "Model Context Protocol (MCP) servers. Use the tools when they help answer "
    "the user's request, and explain your findings clearly. When a tool returns "
    "structured data, interpret it for the user rather than dumping it verbatim."
)

MAX_TOOL_ITERATIONS = 25


class BackendUnavailable(RuntimeError):
    """Raised by ``create()`` when a backend can't be used (missing key, no server…)."""


class BackendError(RuntimeError):
    """A recoverable per-request failure (network error, bad response…)."""


@dataclass
class ChatSettings:
    """User-supplied knobs common to every backend."""

    model: str | None = None
    host: str | None = None
    api_key: str | None = None  # Ollama Bearer token (optional); Anthropic uses the SDK/env
    think: bool | None = None       # Ollama: per-request reasoning toggle (None = model default)
    keep_alive: str | None = None   # Ollama: how long to keep the model resident in VRAM


@dataclass
class ToolCall:
    """A single tool invocation requested by the model (provider-neutral)."""

    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass
class Turn:
    """One assistant turn, normalized across providers."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    refused: bool = False
    refusal_message: str = ""
    raw: Any = None  # provider-native payload, replayed verbatim into history


class BaseAgent(ABC):
    """Shared agent loop. Subclasses supply the provider-specific hooks."""

    #: short backend id, e.g. "anthropic" / "ollama"
    name: str = "base"

    def __init__(self, manager: MCPManager):
        self._manager = manager
        self.show_thinking = False
        self.warning: str | None = None
        self._messages: list[Any] = []
        self.reset()

    # -- construction / identity (subclass) --------------------------------

    @classmethod
    @abstractmethod
    def create(cls, manager: MCPManager, settings: ChatSettings) -> "BaseAgent":
        """Build a ready agent, or raise :class:`BackendUnavailable`."""

    @property
    @abstractmethod
    def label(self) -> str:
        """Human-readable backend + model, e.g. ``Ollama (llama3.1)``."""

    # -- provider-specific hooks -------------------------------------------

    @abstractmethod
    def _complete(self) -> Turn:
        """Make one blocking model call over ``self._messages`` -> Turn.

        May raise :class:`BackendError` for recoverable request failures.
        """

    @abstractmethod
    def _record_assistant(self, turn: Turn) -> None:
        """Append the assistant turn to ``self._messages``."""

    @abstractmethod
    def _record_tool_results(self, results: list[tuple[ToolCall, list, bool]]) -> None:
        """Append this turn's ``(call, content_parts, is_error)`` results to history.

        ``content_parts`` is the normalized list from ``MCPManager.call_tool`` —
        text and/or image parts. Each backend renders it into its own wire shape.
        """

    def _initial_messages(self) -> list[Any]:
        """Starting conversation state (overridden if a system turn is needed)."""
        return []

    # -- shared helpers ----------------------------------------------------

    def _tool_refs(self) -> list[ToolRef]:
        return list(self._manager.tools.values())

    def _add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def reset(self) -> None:
        """Start a fresh conversation."""
        self._messages = self._initial_messages()

    # -- the loop (shared across every backend) ----------------------------

    async def send(self, user_text: str, on_event) -> None:
        """Run one user turn to completion, streaming UI updates via on_event.

        ``on_event(kind, payload)`` kinds:
        {"thinking", "text", "tool_call", "tool_result", "error"}.
        """
        self._add_user(user_text)

        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                turn = await asyncio.to_thread(self._complete)
            except BackendError as e:
                on_event("error", str(e))
                return

            if turn.refused:
                on_event("error", turn.refusal_message or "The model declined to respond.")
                return
            if self.show_thinking and turn.thinking:
                on_event("thinking", turn.thinking)
            if turn.text:
                on_event("text", turn.text)

            self._record_assistant(turn)

            if not turn.tool_calls:
                return

            results: list[tuple[ToolCall, list, bool]] = []
            for call in turn.tool_calls:
                on_event("tool_call", {"name": call.name, "input": call.arguments})
                content, is_error = await self._manager.call_tool(call.name, call.arguments)
                on_event("tool_result",
                         {"name": call.name, "text": parts_to_text(content), "is_error": is_error})
                results.append((call, content, is_error))
            self._record_tool_results(results)

        on_event("error", f"Stopped after {MAX_TOOL_ITERATIONS} tool iterations.")
