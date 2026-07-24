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
from .config import ClientConfig, ServerConfig, load_config
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


def _agent_specialty(description: str | None, system_prompt: str | None) -> str:
    """What this subagent is *for*, as the parent should understand it.

    A config ``description`` if given (the clean path), else a trimmed
    ``systemPrompt`` (works for ``/spawn`` too, since that bakes the prompt into
    the config), else empty.
    """
    return (description or "").strip() or _trim(system_prompt, 400)


def _tool_descriptions(specialty: str) -> dict[str, str]:
    """Descriptions for the four subagent tools, all carrying its identity.

    The parent only sees these tools — and when several subagents are connected
    (bare, un-prefixed names via static config), the descriptions are the only
    way to tell whose ``ask`` / ``connect_server`` / … is whose. So every tool
    names the subagent's role, not just ``ask``.
    """
    tag = f" (This subagent's role: {_trim(specialty, 140)})" if specialty else ""
    if specialty:
        ask = (f"Delegate a task to this subagent. Its role: {specialty} "
               "It reasons with its own LLM and tools, then returns its answer.")
    else:
        ask = ("Delegate a task to this subagent; it reasons with its own LLM "
               "and tools, then returns its answer.")
    return {
        "ask": ask,
        "connect_server": ("Connect this subagent to a new stdio MCP server; its "
                           "tools then become available to this subagent's ask." + tag),
        "list_tools": "List the MCP tools this subagent currently has." + tag,
        "reset": ("Start this subagent on a fresh conversation "
                  "(keeps its servers and system prompt)." + tag),
    }


def _trim(text: str | None, limit: int) -> str:
    """Collapse whitespace and cap ``text`` at ``limit`` chars (word boundary)."""
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


def _build_mcp(state: _Subagent, descriptions: dict[str, str]):
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

    mcp.add_tool(ask, name="ask", description=descriptions["ask"])
    mcp.add_tool(connect_server, name="connect_server",
                 description=descriptions["connect_server"])
    mcp.add_tool(list_tools, name="list_tools",
                 description=descriptions["list_tools"])
    mcp.add_tool(reset, name="reset", description=descriptions["reset"])
    return mcp


async def serve(
    *,
    config_path: str | None,
    backend: str,
    settings: ChatSettings,
    spawn_settings: SpawnSettings,
) -> int:
    """Run this instance as an MCP server over stdio until the parent disconnects."""
    client_cfg = load_config(config_path) if config_path else ClientConfig()
    # No human is attached to a subagent, so any permission-gated tool is denied
    # by default (see MCPManager._check_permission). The config's rules are still
    # loaded so the subagent gates the same tools its parent would.
    async with MCPManager(client_cfg.servers, permissions=client_cfg.permissions) as manager:
        # A subagent can itself spawn sub-subagents, so it also gets the local tool.
        install_spawn_tool(manager, spawn_settings)
        state = _Subagent(manager, backend, settings)
        specialty = _agent_specialty(client_cfg.description, client_cfg.system_prompt)
        mcp = _build_mcp(state, _tool_descriptions(specialty))
        logger.info("subagent serving over stdio (%d preconfigured server(s))", len(manager.sessions))
        await mcp.run_stdio_async()
    return 0
