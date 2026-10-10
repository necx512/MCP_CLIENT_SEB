"""Back mode — the MCP face of a subagent's REPL.

A process whose stdin isn't a terminal is a subagent (see :mod:`mcp_client_seb.cli`):
its parent drives it over stdio, so instead of reading the keyboard, its
:class:`~mcp_client_seb.cli.Repl` takes prompts from the tools below:

    ask(prompt)                       one REPL prompt — chat or a /command —
                                      answered with the REPL's output
    connect_server(name, command, …)  give the subagent a new MCP server
    list_tools()                      list the tools the subagent currently has
    reset()                           start the subagent on a fresh conversation

While an ``ask`` runs, the REPL escalates permission requests back over it as
MCP elicitations (see ``Repl._escalate``), so they reach the front.

The subagent's system prompt is fixed at launch (from its config file's
``systemPrompt``); there is no runtime re-steering tool.

Nothing here writes to stdout except the MCP protocol itself (FastMCP owns the
stream); logs go to stderr.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .config import ClientConfig, ServerConfig

if TYPE_CHECKING:
    from .cli import Repl

logger = logging.getLogger("mcp_client_seb.server")


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


def _build_mcp(repl: Repl, descriptions: dict[str, str]):
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415 — only needed in back mode

    mcp = FastMCP("mcp_client_seb")

    async def ask(prompt: str) -> str:
        """Delegate a task to this subagent; it reasons and uses its own tools, then answers."""
        return await repl.submit(prompt, mcp.get_context())

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
            new_tools = await repl.manager.connect(cfg)
        except Exception as e:  # noqa: BLE001
            return f"Failed to connect {name!r}: {type(e).__name__}: {e}"
        return f"Connected {name!r}. New tools: " + (", ".join(new_tools) or "(none)")

    async def list_tools() -> str:
        """List the MCP tools the subagent currently has available."""
        refs = sorted(repl.manager.tools.values(), key=lambda r: r.exposed_name)
        if not refs:
            return "(no tools yet — use connect_server to add an MCP server)"
        out = []
        for r in refs:
            summary = r.description.strip().splitlines()[0] if r.description.strip() else ""
            out.append(f"{r.exposed_name}: {summary}" if summary else r.exposed_name)
        return "\n".join(out)

    async def reset() -> str:
        """Start the subagent on a fresh conversation (keeps servers and system prompt)."""
        if repl.agent is not None:
            repl.agent.reset()
        return "Subagent conversation reset."

    mcp.add_tool(ask, name="ask", description=descriptions["ask"])
    mcp.add_tool(connect_server, name="connect_server",
                 description=descriptions["connect_server"])
    mcp.add_tool(list_tools, name="list_tools",
                 description=descriptions["list_tools"])
    mcp.add_tool(reset, name="reset", description=descriptions["reset"])
    return mcp


async def serve(repl: Repl, client_cfg: ClientConfig) -> None:
    """Serve ``repl`` over stdio until the parent disconnects."""
    specialty = _agent_specialty(client_cfg.description, client_cfg.system_prompt)
    mcp = _build_mcp(repl, _tool_descriptions(specialty))
    logger.info("subagent serving over stdio (%d preconfigured server(s))",
                len(repl.manager.sessions))
    await mcp.run_stdio_async()
