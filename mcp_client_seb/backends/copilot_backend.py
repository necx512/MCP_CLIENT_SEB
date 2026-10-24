"""GitHub Copilot backend — the official Copilot SDK, driving our MCP tools.

Unlike the other backends this one doesn't implement the shared tool-use loop
in :class:`~mcp_client_seb.backends.base.BaseAgent`: the Copilot runtime runs
the loop itself. We give it every tool in the :class:`MCPManager` registry as
an SDK *custom tool* whose handler calls back into ``BaseAgent._run_tool`` — so
each call still goes through ``MCPManager.call_tool`` (and its permission
check), still emits the usual ``tool_call``/``tool_result`` events, and still
pauses at the interceptor's ``tool_call``/``tool_result`` points.

The model traffic itself happens inside the Copilot runtime. When the optional
``llm_interceptor`` package is installed, the client is given an SDK
``request_handler`` through which the runtime routes every model HTTP call, and
the ``request``/``response`` points pause there — on the provider's own wire
format (Anthropic Messages for Claude, OpenAI for GPT…), not ours. Without the
package no handler is installed and the runtime talks to Copilot directly.

The session is created with the client in ``mode="empty"`` and an
``available_tools`` allowlist of custom tools only, so Copilot's own built-in
tools (shell, file edits, its GitHub MCP server…) are never offered to the
model — our tools and our permission system are the only way it acts.

Requires ``pip install github-copilot-sdk`` and a Copilot subscription.

Model:  ``--model`` / ``$COPILOT_MODEL`` (default ``claude-sonnet-5.5``).
Auth:   the runtime's own lookup — ``$COPILOT_GITHUB_TOKEN`` / ``$GH_TOKEN`` /
        ``$GITHUB_TOKEN`` (a fine-grained PAT with the *Copilot Requests*
        permission), else the login stored by ``copilot`` → ``/login``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from ..manager import MCPManager, parts_to_text
from .base import BackendUnavailable, BaseAgent, ChatSettings, ToolCall, Turn

logger = logging.getLogger("mcp_client_seb.copilot")

DEFAULT_MODEL = "claude-sonnet-5.5"
#: How long one user turn may run. Generous: a turn can wait on a human at a
#: permission prompt, or on a subagent's own long task. On expiry the turn is
#: aborted rather than left running in the runtime.
TURN_TIMEOUT = 3600  # seconds

#: Copilot tool names must match this; MCP names (e.g. ``fs.read``) may not.
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class CopilotAgent(BaseAgent):
    name = "copilot"

    def __init__(self, manager: MCPManager, model: str, sdk: Any):
        # Set before super().__init__, which calls reset().
        self.model = model
        self._sdk = sdk
        self._client: Any = None
        self._session: Any = None
        #: Signature of the tool list the session currently has (see _sync_tools).
        self._tools_sig: tuple | None = None
        #: Copilot-safe tool name -> exposed MCP name.
        self._names: dict[str, str] = {}
        #: The UI callback of the turn in progress (tool handlers report through it).
        self._on_event = None
        super().__init__(manager)
        #: Route the runtime's model traffic through us only if a hub can attach.
        self._intercept_model = _interceptor_installed(self._intercept)
        # The hub takes one event at a time per connection, but Copilot may run
        # tool calls concurrently, alongside its own model requests.
        self._intercept = _Serialized(self._intercept)

    # -- construction / identity -------------------------------------------

    @classmethod
    def create(cls, manager: MCPManager, settings: ChatSettings) -> "CopilotAgent":
        try:
            import copilot  # noqa: PLC0415 — lazy so this backend is optional
        except ImportError as e:
            raise BackendUnavailable(
                "the 'github-copilot-sdk' package is not installed "
                "(pip install github-copilot-sdk)"
            ) from e
        # Auth can only be checked by starting the runtime (async), so it's
        # verified on the first message instead; a failure is reported there.
        model = settings.model or os.environ.get("COPILOT_MODEL") or DEFAULT_MODEL
        return cls(manager, model=model, sdk=copilot)

    @property
    def label(self) -> str:
        return f"GitHub Copilot ({self.model})"

    # -- conversation lifecycle --------------------------------------------

    def reset(self) -> None:
        """Start a fresh conversation: the next message opens a new session."""
        self._messages = []
        old, self._session, self._tools_sig = getattr(self, "_session", None), None, None
        if old is not None:
            _spawn(old.disconnect(), "disconnect old Copilot session")

    async def aclose(self) -> None:
        """Disconnect the session and stop the Copilot runtime."""
        session, self._session = self._session, None
        client, self._client = self._client, None
        for what, coro in (("session", session and session.disconnect()),
                           ("client", client and client.stop())):
            if coro is None:
                continue
            try:
                await coro
            except Exception as e:  # noqa: BLE001 — shutting down anyway
                logger.debug("Copilot %s close failed: %s", what, e)

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        sdk = self._sdk
        if self._client is None:
            token = os.environ.get("COPILOT_GITHUB_TOKEN")
            handler = _make_request_handler(sdk, self) if self._intercept_model else None
            client = sdk.CopilotClient(mode="empty", github_token=token or None,
                                       request_handler=handler)
            await client.start()
            self._client = client
        tools = self._build_tools()
        self._session = await self._client.create_session(
            model=self.model,
            tools=tools,
            # Custom tools only: no built-in shell/edit tools, no GitHub MCP server.
            # The wildcard also admits tools added later via set_tools.
            available_tools=sdk.ToolSet().add_custom("*"),
            # Our prompt *is* the agent's identity, as with every other backend.
            system_message={"mode": "replace", "content": self.system_prompt},
            on_permission_request=_deny_permission,
        )
        self._tools_sig = self._signature()
        return self._session

    # -- the turn (replaces BaseAgent's loop) -------------------------------

    async def send(self, user_text: str, on_event) -> None:
        try:
            session = await self._ensure_session()
            await self._sync_tools()
        except Exception as e:  # noqa: BLE001 — auth, network, bad model…
            on_event("error", f"Copilot unavailable: {type(e).__name__}: {e}")
            return

        reported: list[str] = []

        def handle(event: Any) -> None:
            kind = getattr(getattr(event, "type", None), "value", None)
            data = getattr(event, "data", None)
            if kind == "assistant.message" and getattr(data, "content", ""):
                on_event("text", data.content)
            elif kind == "assistant.reasoning" and self.show_thinking:
                if getattr(data, "content", ""):
                    on_event("thinking", data.content)
            elif kind == "session.error":
                msg = getattr(data, "message", None) or str(data)
                reported.append(msg)
                on_event("error", f"Copilot: {msg}")

        self._on_event = on_event
        unsubscribe = session.on(handle)
        try:
            await session.send_and_wait(user_text, timeout=TURN_TIMEOUT)
        except TimeoutError:
            _spawn(session.abort(), "abort timed-out Copilot turn")
            on_event("error", f"Copilot turn aborted after {TURN_TIMEOUT}s.")
        except Exception as e:  # noqa: BLE001
            if not reported:
                on_event("error", f"Copilot request failed: {type(e).__name__}: {e}")
        finally:
            unsubscribe()
            self._on_event = None

    # -- tools ---------------------------------------------------------------

    def _signature(self) -> tuple:
        return tuple(sorted((t.exposed_name, t.description, repr(t.input_schema))
                            for t in self._tool_refs()))

    def _build_tools(self) -> list[Any]:
        """Every tool in the registry as an SDK Tool dispatching to _run_tool."""
        tools, names = [], {}
        for ref in self._tool_refs():
            safe = _TOOL_NAME_RE.sub("_", ref.exposed_name)
            while safe in names:  # two names that sanitize alike
                safe += "_"
            names[safe] = ref.exposed_name
            tools.append(self._sdk.Tool(
                name=safe,
                description=ref.description,
                parameters=ref.input_schema,
                handler=self._make_handler(ref.exposed_name),
                # MCPManager.call_tool owns permissions (noRequiredPermission).
                skip_permission=True,
            ))
        self._names = names
        return tools

    def _make_handler(self, exposed: str):
        async def handler(invocation: Any) -> Any:
            args = invocation.arguments if isinstance(invocation.arguments, dict) else {}
            call = ToolCall(name=exposed, arguments=args, id=invocation.tool_call_id)
            _call, parts, is_error = await self._run_tool(call, self._on_event or _drop)
            # A tool may have changed the registry (spawn_subagent, remove_subagent):
            # hand Copilot the new list so its next model call within this turn
            # sees it. Not awaited here — the runtime is waiting on this result.
            if self._signature() != self._tools_sig:
                _spawn(self._sync_tools(), "update Copilot tools")
            return self._to_result(parts, is_error)
        return handler

    def _report(self, kind: str, payload: Any) -> None:
        """Send a UI event for the turn in progress (dropped outside a turn)."""
        (self._on_event or _drop)(kind, payload)

    async def _sync_tools(self) -> None:
        """Push the registry to the session if it changed since last time."""
        sig = self._signature()
        if self._session is None or sig == self._tools_sig:
            return
        self._tools_sig = sig
        await self._session.set_tools(self._build_tools())

    def _to_result(self, parts: list[dict[str, Any]], is_error: bool) -> Any:
        """Normalized parts -> an SDK ToolResult (images as binary results)."""
        sdk = self._sdk
        images = [sdk.ToolBinaryResult(data=p["data"], type="image",
                                       mime_type=p.get("media_type") or "image/png")
                  for p in parts if p.get("type") == "image"]
        return sdk.ToolResult(
            text_result_for_llm=parts_to_text(parts),
            result_type="failure" if is_error else "success",
            binary_results_for_llm=images or None,
        )

    # -- BaseAgent hooks: the loop runs inside the Copilot runtime ----------

    def _tool_specs(self) -> list[Any]:
        return self._build_tools()

    def _complete(self) -> Turn:
        raise NotImplementedError("the Copilot runtime runs the model loop")

    def _parse(self, wire: Any) -> Turn:
        raise NotImplementedError("the Copilot runtime runs the model loop")

    def _record_assistant(self, turn: Turn) -> None:
        raise NotImplementedError("the Copilot runtime keeps the history")

    def _record_tool_results(self, results: list[tuple[ToolCall, list, bool]]) -> None:
        raise NotImplementedError("the Copilot runtime keeps the history")


# -- interception of the runtime's model traffic ------------------------------

class _Serialized:
    """An interceptor that lets one event through at a time."""

    def __init__(self, inner: Any):
        self._inner = inner
        self._lock = asyncio.Lock()

    async def intercept(self, point: str, payload: Any) -> Any:
        async with self._lock:
            return await self._inner.intercept(point, payload)


def _interceptor_installed(interceptor: Any) -> bool:
    """Is this a live llm_interceptor (package installed, not ``INTERCEPT=off``)?"""
    try:
        from llm_interceptor import SocketInterceptor  # noqa: PLC0415
    except ImportError:
        return False
    return isinstance(interceptor, SocketInterceptor)


def _make_request_handler(sdk: Any, agent: CopilotAgent) -> Any:
    """An SDK request handler pausing model calls at ``request``/``response``.

    Only model calls are paused — a POST whose JSON body has ``messages``
    (Anthropic Messages, OpenAI Chat Completions) or ``input`` (OpenAI
    Responses). Anything else (the model catalog, …) passes straight through.
    Forwarding without an edit sends the original bytes, untouched.
    """
    import httpx  # noqa: PLC0415 — a dependency of the SDK

    class InterceptingHandler(sdk.CopilotRequestHandler):
        async def send_request(self, request: httpx.Request, ctx: Any) -> httpx.Response:
            body = _model_call_body(request)
            if body is None:
                return await super().send_request(request, ctx)

            verdict = await agent._intercept.intercept(
                "request", {"url": str(request.url), "body": body})
            if verdict.dropped:
                agent._report("error", "Request dropped at the interceptor.")
                return _error_response(request, "Request dropped at the interceptor.")
            if verdict.edited:
                request = _with_content(request, json.dumps(
                    verdict.payload.get("body", body)).encode())

            response = await super().send_request(request, ctx)
            try:
                raw = await response.aread()
            finally:
                await response.aclose()
            view = _response_view(response, raw)
            if view is None:  # not a format we can show: pass it on as is
                return _rebuild(response, raw, request)

            verdict = await agent._intercept.intercept("response", view)
            if verdict.dropped:
                agent._report("error", "Response dropped at the interceptor.")
                return _error_response(request, "Response dropped at the interceptor.")
            if verdict.edited:
                raw = _render_response(verdict.payload, view)
            return _rebuild(response, raw, request)

    return InterceptingHandler()


def _model_call_body(request: Any) -> dict | None:
    """The JSON body of a model call, or None for any other request."""
    if request.method != "POST":
        return None
    try:
        body = json.loads(request.content or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(body, dict) and ("messages" in body or "input" in body):
        return body
    return None


def _with_content(request: Any, content: bytes) -> Any:
    """``request`` with a new body (httpx recomputes Content-Length)."""
    import httpx  # noqa: PLC0415
    headers = [(k, v) for k, v in request.headers.multi_items()
               if k.lower() != "content-length"]
    return httpx.Request(request.method, request.url, headers=headers, content=content)


def _response_view(response: Any, raw: bytes) -> dict | None:
    """A response as the human sees it: JSON body, or SSE as a list of events."""
    ctype = response.headers.get("content-type", "")
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        return None
    if "text/event-stream" in ctype:
        return {"status": response.status_code, "events": _parse_sse(text)}
    if "json" in ctype:
        try:
            return {"status": response.status_code, "body": json.loads(text)}
        except ValueError:
            return None
    return None


def _render_response(edited: dict, view: dict) -> bytes:
    """An edited view back to bytes, in the shape it was shown in."""
    if "events" in view:
        return _render_sse(edited.get("events", view["events"])).encode()
    return json.dumps(edited.get("body", view["body"])).encode()


def _parse_sse(text: str) -> list[dict]:
    """``event:``/``data:`` blocks -> ``[{"event": …, "data": <json or str>}]``."""
    events = []
    for block in re.split(r"\r?\n\r?\n", text):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
        if name is None and not data:
            continue
        joined = "\n".join(data)
        try:
            payload: Any = json.loads(joined)
        except ValueError:
            payload = joined  # e.g. OpenAI's terminal "[DONE]"
        events.append({"event": name, "data": payload} if name else {"data": payload})
    return events


def _render_sse(events: list[dict]) -> str:
    out = []
    for ev in events:
        data = ev.get("data", "")
        text = data if isinstance(data, str) else json.dumps(data)
        lines = ([f"event: {ev['event']}"] if ev.get("event") else []) + \
                [f"data: {line}" for line in text.split("\n")]
        out.append("\n".join(lines) + "\n\n")
    return "".join(out)


def _rebuild(response: Any, raw: bytes, request: Any) -> Any:
    """A fresh response carrying ``raw``, already decoded (so no encoding headers)."""
    import httpx  # noqa: PLC0415
    drop = {"content-length", "content-encoding", "transfer-encoding"}
    headers = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in drop]
    return httpx.Response(response.status_code, headers=headers, content=raw, request=request)


def _error_response(request: Any, message: str) -> Any:
    """A client-error reply: the runtime ends the turn instead of retrying."""
    import httpx  # noqa: PLC0415
    return httpx.Response(400, json={"error": {"type": "dropped", "message": message}},
                          request=request)


def _deny_permission(request: Any, invocation: Any = None) -> Any:
    """Copilot's own permission requests: refuse.

    Our tools skip them (``skip_permission``) and built-ins aren't available, so
    this only fires if something unexpected asks — and then the answer is no.
    """
    from copilot import PermissionDecisionReject  # noqa: PLC0415
    logger.warning("unexpected Copilot permission request denied: %r", request)
    return PermissionDecisionReject(
        feedback="Denied: only this client's own tools may be used.")


def _drop(_kind: str, _payload: Any) -> None:
    """on_event for a tool call arriving outside a turn (shouldn't happen)."""


def _spawn(coro: Any, what: str) -> None:
    """Run ``coro`` in the background, logging (not raising) its failure."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no running loop (e.g. reset() at construction)
        coro.close()
        return

    def done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.warning("failed to %s: %s", what, t.exception())

    task.add_done_callback(done)
