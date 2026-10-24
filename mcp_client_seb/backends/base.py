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
    _parse()               provider-native response payload -> a Turn.
    _record_assistant()    append the assistant turn to history.
    _record_tool_results() append this turn's tool results to history.
    _initial_messages()    the starting message list (e.g. a system turn).

The loop, refusal handling, thinking/text emission, the tool fan-out, and the
interception points are shared. See ``anthropic_backend`` and ``ollama_backend``
for the two ~100-line implementations.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..manager import MCPManager, ToolRef, parts_to_text

try:  # Optional, like `anthropic`: an external tool, not a feature of this client.
    from llm_interceptor import build_interceptor
except ImportError:  # not installed -> the pauses below are a no-op, and free
    class _Forward:
        dropped = False
        edited = False
        payload = None

    _FORWARD = _Forward()

    class _NoInterceptor:
        async def intercept(self, point: str, payload: Any) -> Any:
            return _FORWARD

    def build_interceptor(label: str = "") -> Any:
        return _NoInterceptor()


def jsonify(value: Any) -> Any:
    """Provider SDK objects -> plain JSON data.

    Ours, not the interceptor's, even though llm_interceptor exports an
    identical helper. This is on the *parse* path — ``AnthropicAgent._complete``
    needs its response as dicts to read it at all — so it has to work with no
    optional package installed. Importing it from llm_interceptor once meant the
    Anthropic backend broke outright whenever that package was absent.
    """
    for attr in ("to_dict", "model_dump"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return jsonify(fn())
            except Exception:  # noqa: BLE001 — never break a turn over rendering
                break
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

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
    system_prompt: str | None = None  # overrides SYSTEM_PROMPT (e.g. from the config file)


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
    raw: Any = None   # provider-native payload, replayed verbatim into history
    #: JSON-safe view of the provider's response, shown to (and editable by) the
    #: interceptor. Distinct from ``raw`` because for Anthropic ``raw`` holds the
    #: SDK's own block objects: replaying those keeps the untouched path exactly
    #: as it was before interception existed. Only an actual edit swaps ``raw``
    #: for plain dicts, via ``_parse``.
    wire: Any = None


class BaseAgent(ABC):
    """Shared agent loop. Subclasses supply the provider-specific hooks."""

    #: short backend id, e.g. "anthropic" / "ollama"
    name: str = "base"

    def __init__(self, manager: MCPManager):
        self._manager = manager
        self.show_thinking = False
        self.warning: str | None = None
        #: Overridden by ``build_agent`` from a config file's ``systemPrompt``.
        self.system_prompt = SYSTEM_PROMPT
        self._messages: list[Any] = []
        #: A one-shot tool-list override set by the interceptor when a human edits
        #: the ``tools`` of a paused request. Consumed by the next ``_complete``
        #: (see ``_request_tools``); ``None`` means "use the live tool registry".
        self._tools_override: list[Any] | None = None
        try:
            label = self.label
        except Exception:  # noqa: BLE001 — identity is cosmetic; never block startup
            label = ""
        #: Pauses ``send`` for a human when the optional ``llm_interceptor``
        #: package is installed and its console is attached; a no-op (and free)
        #: otherwise. See the fallback stub at the top of this module.
        self._intercept = build_interceptor(label)
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
    def _tool_specs(self) -> list[Any]:
        """The MCP tools in this provider's wire schema, sent with each request.

        Recomputed per turn from the live tool registry so tools added
        mid-conversation (e.g. a freshly spawned subagent's) appear on the next
        call. Also shown to the interceptor at the ``request`` point.
        """

    @abstractmethod
    def _complete(self) -> Turn:
        """Make one blocking model call over ``self._messages`` -> Turn.

        May raise :class:`BackendError` for recoverable request failures.
        """

    @abstractmethod
    def _parse(self, wire: Any) -> Turn:
        """Provider-native response payload -> Turn, with ``raw`` and ``wire`` set.

        ``_complete`` uses this so there's one parser, and the interceptor
        re-runs it on an edited payload — the human edits the provider's own
        response shape and the client re-reads it, exactly as a proxy does.
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

    def _request_tools(self) -> list[Any]:
        """Tool specs for the next model call — honouring an interceptor edit.

        Normally the live tool list (``_tool_specs``). If a human edited the
        ``tools`` of a paused request, use that instead for this one call, then
        fall back to the live list (tools are derived state, not stored history,
        so an override can't sensibly persist across turns).
        """
        if self._tools_override is not None:
            tools = self._tools_override
            self._tools_override = None
            return tools
        return self._tool_specs()

    def _add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def reset(self) -> None:
        """Start a fresh conversation."""
        self._messages = self._initial_messages()

    async def aclose(self) -> None:
        """Release backend resources at exit (e.g. a runtime process). Default: none."""

    # -- the loop (shared across every backend) ----------------------------

    async def send(self, user_text: str, on_event) -> None:
        """Run one user turn to completion, streaming UI updates via on_event.

        ``on_event(kind, payload)`` kinds:
        {"thinking", "text", "tool_call", "tool_result", "error"}.

        Four points here can pause for a human if the optional ``llm_interceptor``
        package is installed and its console is attached; otherwise they're a
        no-op. ``_complete`` runs in a worker thread, so this loop is free to
        await a decision on the event loop — pausing costs nothing structurally.
        """
        self._add_user(user_text)

        for _ in range(MAX_TOOL_ITERATIONS):
            if not await self._intercept_request(on_event):
                return

            try:
                turn = await asyncio.to_thread(self._complete)
            except BackendError as e:
                on_event("error", str(e))
                return

            turn = await self._intercept_response(turn, on_event)
            if turn is None:
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
                results.append(await self._run_tool(call, on_event))
            self._record_tool_results(results)

        on_event("error", f"Stopped after {MAX_TOOL_ITERATIONS} tool iterations.")

    async def _run_tool(self, call: ToolCall, on_event) -> tuple[ToolCall, list, bool]:
        """One tool call, with the call and its result each interceptable."""
        verdict = await self._intercept.intercept(
            "tool_call", {"name": call.name, "arguments": call.arguments})
        if verdict.dropped:
            # A dropped call still needs a result: Anthropic rejects a turn whose
            # tool_use block has no matching tool_result, so silently skipping it
            # would 400 on the next request rather than doing nothing.
            return self._synthetic_result(call, "[dropped at the interceptor]", on_event)
        if verdict.edited:
            call.name = verdict.payload.get("name", call.name)
            call.arguments = verdict.payload.get("arguments", call.arguments)

        on_event("tool_call", {"name": call.name, "input": call.arguments})
        content, is_error = await self._manager.call_tool(call.name, call.arguments)

        verdict = await self._intercept.intercept(
            "tool_result",
            {"name": call.name, "is_error": is_error, "parts": jsonify(content)})
        if verdict.dropped:
            return self._synthetic_result(call, "[result dropped at the interceptor]",
                                          on_event)
        if verdict.edited:
            content = verdict.payload.get("parts", content)
            is_error = bool(verdict.payload.get("is_error", is_error))

        text = parts_to_text(content)
        on_event("tool_result", {"name": call.name, "text": text, "is_error": is_error})
        return (call, content, is_error)

    def _synthetic_result(self, call: ToolCall, text: str,
                          on_event) -> tuple[ToolCall, list, bool]:
        parts = [{"type": "text", "text": text}]
        on_event("tool_result", {"name": call.name, "text": text, "is_error": True})
        return (call, parts, True)

    # -- interception points -----------------------------------------------

    async def _intercept_request(self, on_event) -> bool:
        """Pause before the model call. Returns False if the turn was dropped."""
        # Include the tools actually sent this turn so the interceptor shows the
        # whole outgoing request (system + tools + messages), as a proxy would.
        tool_specs = self._tool_specs()
        verdict = await self._intercept.intercept(
            "request",
            {"system": self.system_prompt,
             "tools": tool_specs,
             "messages": jsonify(self._messages)})
        if verdict.dropped:
            on_event("error", "Request dropped at the interceptor.")
            return False
        if verdict.edited:
            # Edits land in history, not just this call: ``_messages`` *is* the
            # conversation. That differs from Burp, where editing a request
            # leaves the browser's own state untouched — here it's how you steer.
            # ``tools`` are derived per turn, so an edit applies to this call only
            # (see ``_request_tools``), while system/messages edits persist.
            self.system_prompt = verdict.payload.get("system", self.system_prompt)
            self._messages = verdict.payload.get("messages", self._messages)
            self._tools_override = verdict.payload.get("tools", tool_specs)
        return True

    async def _intercept_response(self, turn: Turn, on_event) -> Turn | None:
        """Pause on the model's reply. Returns None if the turn was dropped."""
        verdict = await self._intercept.intercept("response", turn.wire)
        if verdict.dropped:
            on_event("error", "Response dropped at the interceptor.")
            return None
        if verdict.edited:
            return self._parse(verdict.payload)
        return turn
