"""Connection manager for one or more MCP servers.

Aggregates the tools of every connected MCP server into one namespace and
dispatches tool calls to the right server. Everything here is async; the CLI
drives it.

Each server connection runs in its own dedicated asyncio task that opens the
stdio subprocess + ``ClientSession`` with ``async with`` and then parks on a
shutdown event. Opening and closing the connection in the *same* task is what
lets :meth:`connect` be called safely from any task — including from inside an
MCP request handler when we're running as a subagent (see
:mod:`mcp_client_seb.server`) — without tripping anyio's cancel-scope rules.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .config import PermissionConfig, ServerConfig

logger = logging.getLogger("mcp_client_seb.manager")

#: A local tool handler: ``(arguments) -> (content_parts, is_error)``.
#: ``content_parts`` is the same normalized list shape ``call_tool`` returns.
LocalHandler = Callable[[dict[str, Any]], Awaitable[tuple[list[dict[str, Any]], bool]]]

#: Asks a human to approve a tool call: ``(ToolRef, arguments) -> allowed?``.
#: Injected by the REPL: a keyboard prompt in front, an escalation to the parent
#: in back. ``None`` means no approver is attached — such tools are denied.
PermissionPrompt = Callable[["ToolRef", dict[str, Any]], Awaitable[bool]]

#: Answers an elicitation a connected server sent mid-tool-call:
#: ``(server_name, ElicitRequestParams) -> ElicitResult``. This is how a
#: subagent's permission request reaches us. Injected by the REPL; ``None``
#: declines every elicitation.
ElicitHandler = Callable[[str, Any], Awaitable[types.ElicitResult]]


@dataclass
class ToolRef:
    """A tool exposed to the user, mapped back to its owning server.

    Most tools are backed by an MCP server (``server`` set, ``handler`` None).
    A *local* tool is implemented in-process by a Python coroutine (``handler``
    set, ``server`` None) — this is how ``spawn_subagent`` is offered to the LLM
    without any MCP server providing it.
    """

    exposed_name: str            # unique name shown to the user / sent to the LLM
    server: str | None           # which server owns it (None for a local tool)
    original_name: str           # the tool's real name on that server
    description: str
    input_schema: dict[str, Any]
    handler: LocalHandler | None = None  # set for local tools; dispatched in-process
    #: Resolved at registration: does calling this tool need human approval? True
    #: if the server declared it (tool ``_meta``/annotations) OR the client's
    #: config asks for it. Enforced in :meth:`MCPManager.call_tool`.
    requires_permission: bool = False


class MCPManager:
    """Launches MCP servers and provides a unified tool interface."""

    def __init__(self, servers: dict[str, ServerConfig],
                 permissions: PermissionConfig | None = None):
        self._servers = servers
        self._permissions = permissions or PermissionConfig()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, ToolRef] = {}
        self.connect_errors: dict[str, str] = {}
        self._conn_tasks: dict[str, asyncio.Task] = {}
        self._shutdown: dict[str, asyncio.Event] = {}
        #: Set by the REPL (see :data:`PermissionPrompt`); ``None`` denies.
        self.permission_prompt: PermissionPrompt | None = None
        #: Set by the REPL (see :data:`ElicitHandler`); ``None`` declines.
        self.elicit_handler: ElicitHandler | None = None
        #: Tools the human chose to "always allow" this session (exposed names).
        self.always_allow: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "MCPManager":
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def connect_all(self) -> None:
        for name, cfg in list(self._servers.items()):
            try:
                await self.connect(cfg)
            except Exception as e:  # noqa: BLE001 — surface, don't crash the REPL
                self.connect_errors[name] = f"{type(e).__name__}: {e}"
                logger.warning("Failed to connect to %s: %s", name, e)

    async def connect(self, cfg: ServerConfig, prefix: str | None = None) -> list[str]:
        """Connect to a new MCP server at runtime and register its tools.

        Unlike :meth:`connect_all` this raises on failure (so the caller — e.g.
        the ``spawn_subagent`` tool — can report it) and returns the list of
        newly-exposed tool names. ``prefix`` forces every tool from this server
        to be namespaced ``<prefix>__<tool>`` (used for subagents so each one's
        ``ask``/``connect_server``/… stay distinct and predictable).
        """
        if cfg.name in self.sessions or cfg.name in self._conn_tasks:
            raise ValueError(f"a server named {cfg.name!r} is already connected")
        # An explicit prefix (e.g. from spawn_subagent) wins; otherwise honour the
        # server's own configured prefix so static entries can namespace too.
        prefix = prefix or cfg.prefix
        before = set(self.tools)
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._shutdown[cfg.name] = asyncio.Event()
        self._conn_tasks[cfg.name] = asyncio.ensure_future(
            self._run_connection(cfg, prefix, ready))
        try:
            await ready  # resolves once the session is initialized (or raises)
        except Exception:
            # Startup failed; tear down the bookkeeping so the name can be reused.
            self._conn_tasks.pop(cfg.name, None)
            self._shutdown.pop(cfg.name, None)
            raise
        self._servers.setdefault(cfg.name, cfg)
        self.connect_errors.pop(cfg.name, None)
        logger.info("Connected to %s (%d tools)", cfg.name,
                    sum(1 for t in self.tools.values() if t.server == cfg.name))
        return [name for name in self.tools if name not in before]

    async def _run_connection(self, cfg: ServerConfig, prefix: str | None,
                              ready: asyncio.Future) -> None:
        """Own one server connection for its whole life, in a single task.

        Opens the subprocess + session, registers its tools, signals ``ready``,
        then parks until :meth:`aclose` sets the shutdown event — at which point
        the ``async with`` blocks unwind here, in this same task.
        """
        params = StdioServerParameters(
            command=cfg.command, args=cfg.args, env=cfg.merged_env(), cwd=cfg.cwd)
        try:
            async with stdio_client(params) as (read, write), \
                    ClientSession(read, write,
                                  elicitation_callback=self._elicitation_callback(cfg.name)
                                  ) as session:
                await session.initialize()
                self.sessions[cfg.name] = session
                await self._register_tools(cfg.name, session, prefix=prefix)
                if not ready.done():
                    ready.set_result(None)
                await self._shutdown[cfg.name].wait()
        except Exception as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e)  # surface startup failure to connect()
            else:
                logger.warning("Connection to %s dropped: %s", cfg.name, e)
        finally:
            self.sessions.pop(cfg.name, None)

    def _elicitation_callback(self, server: str):
        """Route a server's elicitation to :attr:`elicit_handler`, tagged with its name.

        Looked up per call, not bound at connect time, so the REPL can install
        its handler after the static servers are already connected. Any failure
        declines — an elicitation is how permission requests travel, so a broken
        handler must never read as consent.
        """
        async def callback(_context: Any, params: Any) -> types.ElicitResult:
            handler = self.elicit_handler
            if handler is None:
                return types.ElicitResult(action="decline")
            try:
                return await handler(server, params)
            except Exception as e:  # noqa: BLE001
                logger.warning("elicitation from %s failed: %s", server, e)
                return types.ElicitResult(action="decline")
        return callback

    async def _register_tools(self, server: str, session: ClientSession,
                              prefix: str | None = None) -> None:
        result = await session.list_tools()
        for tool in result.tools:
            if prefix:
                exposed = f"{prefix}__{tool.name}"
            else:
                exposed = tool.name
                # Namespace on collision so two servers can both expose e.g. "read".
                if exposed in self.tools:
                    exposed = f"{server}__{tool.name}"
            declared = _server_declares_permission(tool)
            self.tools[exposed] = ToolRef(
                exposed_name=exposed,
                server=server,
                original_name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                requires_permission=self._resolve_permission(
                    server, exposed, tool.name, declared),
            )

    def register_local_tool(self, name: str, description: str,
                            input_schema: dict[str, Any], handler: LocalHandler) -> None:
        """Register an in-process tool, dispatched to ``handler`` (not a server).

        Appears in the tool namespace alongside MCP tools — the LLM and ``/call``
        treat it identically. Used to expose ``spawn_subagent``.
        """
        self.tools[name] = ToolRef(
            exposed_name=name,
            server=None,
            original_name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            requires_permission=self._resolve_permission(None, name, name, False),
        )

    def _resolve_permission(self, server: str | None, exposed: str,
                            original: str, server_declared: bool) -> bool:
        """Does this tool need approval? server-declared OR any client-side rule.

        The OR is the whole point: if *either* the server or the client asks for
        permission, we ask. A tool is gated unless *both* sides stay silent.
        """
        if server_declared:
            return True
        perms = self._permissions
        if perms.all_tools:                                   # global: gate all
            return True
        if exposed in perms.tools or original in perms.tools:  # global: named
            return True
        cfg = self._servers.get(server) if server else None
        rp = getattr(cfg, "require_permission", False) if cfg else False
        if rp is True:                                        # per-server: all
            return True
        if isinstance(rp, set) and (original in rp or exposed in rp):  # per-server: named
            return True
        return False

    async def disconnect(self, name: str) -> list[str]:
        """Tear down one server connection: stop its process and drop its tools.

        Signals just this connection's shutdown event and awaits its owning task
        (so the ``async with`` unwinds in the same task that opened it, per
        :meth:`_run_connection`), then removes its tools and bookkeeping so the
        name is free to reuse. Returns the exposed names that were dropped.
        Raises :class:`KeyError` if no such connection exists.
        """
        task = self._conn_tasks.get(name)
        if task is None:
            raise KeyError(name)
        event = self._shutdown.get(name)
        if event is not None:
            event.set()
        await asyncio.gather(task, return_exceptions=True)
        self._conn_tasks.pop(name, None)
        self._shutdown.pop(name, None)
        self.sessions.pop(name, None)
        self.connect_errors.pop(name, None)
        self._servers.pop(name, None)
        removed = [n for n, ref in list(self.tools.items()) if ref.server == name]
        for n in removed:
            self.tools.pop(n, None)
        return removed

    async def aclose(self) -> None:
        """Signal every connection task to unwind, then wait for them all."""
        for event in self._shutdown.values():
            event.set()
        tasks = list(self._conn_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._conn_tasks.clear()
        self._shutdown.clear()

    # -- dispatch ----------------------------------------------------------

    async def call_tool(self, exposed_name: str, arguments: dict[str, Any] | None):
        """Call a tool by its exposed name.

        Returns ``(content, is_error)`` where ``content`` is a list of normalized
        parts: ``{"type": "text", "text": ...}`` and/or
        ``{"type": "image", "data": <base64>, "media_type": ...}``. Image blocks
        are preserved (not stringified) so vision-capable backends can forward
        screenshots to the model — e.g. the chrome-agent-screenshot MCP server.
        """
        ref = self.tools.get(exposed_name)
        if ref is None:
            return [{"type": "text", "text": f"Unknown tool: {exposed_name!r}"}], True

        denied = await self._check_permission(ref, arguments or {})
        if denied is not None:
            return denied

        if ref.handler is not None:  # local (in-process) tool
            try:
                return await ref.handler(arguments or {})
            except Exception as e:  # noqa: BLE001
                return [{"type": "text",
                         "text": f"Local tool raised {type(e).__name__}: {e}"}], True

        session = self.sessions[ref.server]
        try:
            result = await session.call_tool(ref.original_name, arguments or {})
        except Exception as e:  # noqa: BLE001
            return [{"type": "text", "text": f"Tool call raised {type(e).__name__}: {e}"}], True

        return self._result_content(result), bool(getattr(result, "isError", False))

    async def _check_permission(self, ref: ToolRef, arguments: dict[str, Any]):
        """Gate a tool call. Returns a denial ``(content, True)`` or ``None`` to proceed.

        Generic across every server: enforced here so both the agent loop and a
        manual ``/call`` go through it. ``always_allow`` lets the human whitelist
        a tool for the rest of the session.
        """
        if not ref.requires_permission or ref.exposed_name in self.always_allow:
            return None
        if self.permission_prompt is None:
            # Headless (e.g. subagent server): nobody to ask → refuse, safely.
            return ([{"type": "text",
                      "text": f"Tool {ref.exposed_name!r} requires permission, but no "
                              "approver is attached — denied."}], True)
        try:
            allowed = await self.permission_prompt(ref, arguments)
        except Exception as e:  # noqa: BLE001 — a broken prompter must not auto-allow
            return ([{"type": "text",
                      "text": f"Permission check failed ({type(e).__name__}: {e}) — denied."}],
                    True)
        if not allowed:
            return ([{"type": "text",
                      "text": f"Permission denied by the user for {ref.exposed_name!r}."}], True)
        return None

    @staticmethod
    def _result_content(result: Any) -> list[dict[str, Any]]:
        """Normalize a CallToolResult's content blocks into text/image parts."""
        parts: list[dict[str, Any]] = []
        for block in getattr(result, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "image":
                data = getattr(block, "data", None)
                if data:
                    parts.append({
                        "type": "image",
                        "data": data,
                        "media_type": getattr(block, "mimeType", None) or "image/png",
                    })
            elif btype == "resource":
                res = getattr(block, "resource", None)
                txt = getattr(res, "text", None)
                parts.append({"type": "text", "text": txt if txt is not None else str(res)})
            else:
                parts.append({"type": "text", "text": str(block)})
        return parts or [{"type": "text", "text": "(no content returned)"}]


def _server_declares_permission(tool: Any) -> bool:
    """Did the MCP server itself mark this tool as needing approval?

    Read from two spec-legal places so servers can use whichever is convenient:
    the tool's ``_meta`` object (``{"requiresPermission": true}``) or a custom
    ``requiresPermission`` field on its ``annotations`` (both allow extra keys).
    """
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict) and meta.get("requiresPermission") is True:
        return True
    ann = getattr(tool, "annotations", None)
    if ann is not None and getattr(ann, "requiresPermission", None) is True:
        return True
    return False


def parts_to_text(parts: list[dict[str, Any]]) -> str:
    """Render normalized tool-result parts as plain text (images become a marker).

    Used for the manual REPL, event previews, and any backend/display that only
    handles text — an image part shows as ``[image <media_type>, ~N bytes]``
    instead of dumping base64.
    """
    out: list[str] = []
    for p in parts:
        if p.get("type") == "image":
            approx = (len(p.get("data", "")) * 3) // 4
            out.append(f"[image {p.get('media_type', 'image/png')}, ~{approx} bytes]")
        else:
            out.append(p.get("text", ""))
    return "\n".join(out).strip() or "(no textual content returned)"
