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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ServerConfig

logger = logging.getLogger("mcp_client_seb.manager")

#: A local tool handler: ``(arguments) -> (content_parts, is_error)``.
#: ``content_parts`` is the same normalized list shape ``call_tool`` returns.
LocalHandler = Callable[[dict[str, Any]], Awaitable[tuple[list[dict[str, Any]], bool]]]


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


class MCPManager:
    """Launches MCP servers and provides a unified tool interface."""

    def __init__(self, servers: dict[str, ServerConfig]):
        self._servers = servers
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, ToolRef] = {}
        self.connect_errors: dict[str, str] = {}
        self._conn_tasks: dict[str, asyncio.Task] = {}
        self._shutdown: dict[str, asyncio.Event] = {}

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
                    ClientSession(read, write) as session:
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
            self.tools[exposed] = ToolRef(
                exposed_name=exposed,
                server=server,
                original_name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
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
        )

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
