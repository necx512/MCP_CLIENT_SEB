"""Server mode — expose this client as an MCP server (a controllable subagent).

Run via ``python -m mcp_client_seb --serve``. Instead of a REPL, this speaks the
MCP protocol over stdio, so a *parent* instance can connect to it dynamically
(see :mod:`mcp_client_seb.subagent`) and drive it through the tools below.

A subagent is a full agent in its own right: it owns an :class:`MCPManager`
(its own MCP servers, added on demand) and its own LLM backend. The exposed
tools let the parent configure it and delegate work to it:

    ask(prompt)                       run the subagent's own agent loop, return
                                      its answer (it uses its own tools + LLM)
    connect_server(name, command, …)  give the subagent a new MCP server
    list_tools()                      list the tools the subagent currently has
    reset()                           start the subagent on a fresh conversation

The subagent's system prompt is fixed at launch (from its config file's
``systemPrompt``); there is no runtime re-steering tool.

Nothing here writes to stdout except the MCP protocol itself (FastMCP owns the
stream); logs go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .backends import BackendUnavailable, BaseAgent, ChatSettings, build_agent
from .config import ServerConfig, load_servers
from .manager import MCPManager
from .subagent import SpawnSettings, install_spawn_tool

logger = logging.getLogger("mcp_client_seb.server")


class _Subagent:
    """Holds the subagent's manager + (lazily built) LLM agent and its config."""

    def __init__(self, manager: MCPManager, backend: str, settings: ChatSettings):
        self.manager = manager
        self._backend = backend
        self._settings = settings
        self.agent: BaseAgent | None = None
        self.agent_error: str | None = None
        self._lock = asyncio.Lock()  # serialize ask() — one conversation, one turn at a time

    def ensure_agent(self) -> BaseAgent | None:
        """Build the LLM backend on first use; cache the failure if unavailable.

        The system prompt (if any) comes from the config file the subagent was
        launched with, applied by :func:`build_agent` — there's no runtime
        re-steering.
        """
        if self.agent is None and self.agent_error is None:
            try:
                self.agent = build_agent(self._backend, self.manager, self._settings)
            except BackendUnavailable as e:
                self.agent_error = str(e)
                logger.warning("subagent has no LLM backend: %s", e)
        return self.agent


def _build_mcp(state: _Subagent):
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415 — only needed in serve mode

    mcp = FastMCP("mcp_client_seb")

    async def ask(prompt: str) -> str:
        """Delegate a task to this subagent; it reasons and uses its own tools, then answers."""
        agent = state.ensure_agent()
        if agent is None:
            return f"Subagent has no LLM backend available: {state.agent_error}"
        texts: list[str] = []
        trace: list[str] = []

        def on_event(kind: str, payload) -> None:
            if kind == "text":
                texts.append(payload)
            elif kind == "tool_call":
                trace.append(f"[tool] {payload['name']}({json.dumps(payload['input'])})")
            elif kind == "error":
                texts.append(f"[error] {payload}")

        async with state._lock:
            await agent.send(prompt, on_event)

        answer = "\n".join(t for t in texts if t).strip()
        if not answer:
            answer = "(subagent produced no text)"
            if trace:
                answer += "\nTool activity:\n" + "\n".join(trace)
        return answer

    async def connect_server(
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        """Give the subagent a new MCP server (stdio). Its tools become available to ask()."""
        cfg = ServerConfig(name=name, command=command,
                           args=list(args or []), env=dict(env or {}), cwd=cwd)
        try:
            new_tools = await state.manager.connect(cfg)
        except Exception as e:  # noqa: BLE001
            return f"Failed to connect {name!r}: {type(e).__name__}: {e}"
        return f"Connected {name!r}. New tools: " + (", ".join(new_tools) or "(none)")

    async def list_tools() -> str:
        """List the MCP tools the subagent currently has available."""
        refs = sorted(state.manager.tools.values(), key=lambda r: r.exposed_name)
        if not refs:
            return "(no tools yet — use connect_server to add an MCP server)"
        out = []
        for r in refs:
            summary = r.description.strip().splitlines()[0] if r.description.strip() else ""
            out.append(f"{r.exposed_name}: {summary}" if summary else r.exposed_name)
        return "\n".join(out)

    async def reset() -> str:
        """Start the subagent on a fresh conversation (keeps servers and system prompt)."""
        if state.agent is not None:
            state.agent.reset()
        return "Subagent conversation reset."

    mcp.add_tool(ask, name="ask",
                 description="Delegate a task to this subagent; returns its answer.")
    mcp.add_tool(connect_server, name="connect_server",
                 description="Connect the subagent to a new stdio MCP server.")
    mcp.add_tool(list_tools, name="list_tools",
                 description="List the tools the subagent currently has.")
    mcp.add_tool(reset, name="reset",
                 description="Reset the subagent's conversation.")
    return mcp


async def serve(
    *,
    config_path: str | None,
    backend: str,
    settings: ChatSettings,
    spawn_settings: SpawnSettings,
) -> int:
    """Run this instance as an MCP server over stdio until the parent disconnects."""
    servers = load_servers(config_path) if config_path else {}
    async with MCPManager(servers) as manager:
        # A subagent can itself spawn sub-subagents, so it also gets the local tool.
        install_spawn_tool(manager, spawn_settings)
        state = _Subagent(manager, backend, settings)
        mcp = _build_mcp(state)
        logger.info("subagent serving over stdio (%d preconfigured server(s))", len(manager.sessions))
        await mcp.run_stdio_async()
    return 0
