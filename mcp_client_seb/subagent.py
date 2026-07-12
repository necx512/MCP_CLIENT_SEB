"""Spawning and controlling subagents.

A *subagent* is another instance of this very project launched in server mode
(``python -m mcp_client_seb --serve``) and connected to over stdio like any
other MCP server — but it is never listed in ``servers.json``; it is wired up
dynamically at runtime via :meth:`MCPManager.connect`.

The subagent exposes a handful of tools (``ask``, ``connect_server``,
``set_system_prompt``, ``list_tools``, ``reset``) namespaced under its handle,
e.g. ``worker__ask``. Spawning it registers a local ``spawn_subagent`` tool so
the parent's LLM can create and drive subagents on its own; the same helper
backs the ``/spawn`` REPL command.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from .config import ServerConfig
from .manager import MCPManager

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

SPAWN_TOOL_NAME = "spawn_subagent"
SPAWN_TOOL_DESCRIPTION = (
    "Spawn a subagent: a fresh, separately-configurable instance of this MCP "
    "client with its own LLM and its own (initially empty) set of MCP servers. "
    "Returns the new tools it exposes, namespaced under <name>__: use "
    "<name>__ask to delegate a task to it, <name>__connect_server to give it "
    "access to an MCP server, and <name>__set_system_prompt to steer it."
)
SPAWN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Unique handle for the subagent (letters, digits, _ , -). "
                           "Its tools are exposed as <name>__ask, <name>__connect_server, …",
        },
        "system_prompt": {
            "type": "string",
            "description": "Optional initial system prompt steering the subagent's own LLM.",
        },
    },
    "required": ["name"],
}


@dataclass
class SpawnSettings:
    """How to launch a subagent process — the parent's backend choice forwarded on.

    ``serve_args`` is the argv tail after the interpreter, e.g.
    ``["-m", "mcp_client_seb", "--serve", "--backend", "anthropic"]``. It carries
    no ``--config`` so every subagent starts with zero MCP servers and is
    configured dynamically through its exposed tools.
    """

    python: str = field(default_factory=lambda: sys.executable)
    serve_args: list[str] = field(default_factory=lambda: ["-m", "mcp_client_seb", "--serve"])

    def config_for(self, name: str) -> ServerConfig:
        return ServerConfig(name=name, command=self.python, args=list(self.serve_args))


async def spawn_subagent(
    manager: MCPManager,
    name: str,
    settings: SpawnSettings,
    system_prompt: str | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """Launch a subagent and connect it. Returns ``(content_parts, is_error)``.

    The return shape matches :meth:`MCPManager.call_tool` so this can back both
    the ``spawn_subagent`` local tool and the ``/spawn`` command directly.
    """
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return _err(f"invalid subagent name {name!r} "
                    "(use letters/digits/_/-, starting with a letter)")

    cfg = settings.config_for(name)
    try:
        new_tools = await manager.connect(cfg, prefix=name)
    except Exception as e:  # noqa: BLE001 — report, don't crash the caller
        return _err(f"failed to spawn subagent {name!r}: {type(e).__name__}: {e}")

    lines = [f"Spawned subagent {name!r}. New tools:"]
    for tool_name in sorted(new_tools):
        ref = manager.tools.get(tool_name)
        desc = ref.description.strip().splitlines()[0] if ref and ref.description.strip() else ""
        lines.append(f"  - {tool_name}: {desc}" if desc else f"  - {tool_name}")

    if system_prompt:
        _, err = await manager.call_tool(f"{name}__set_system_prompt", {"text": system_prompt})
        lines.append("  system prompt: " + ("FAILED to set" if err else "set"))

    lines.append(f"Delegate work with {name}__ask.")
    return [{"type": "text", "text": "\n".join(lines)}], False


def install_spawn_tool(manager: MCPManager, settings: SpawnSettings) -> None:
    """Register the local ``spawn_subagent`` tool on ``manager``."""

    async def handler(args: dict) -> tuple[list[dict[str, str]], bool]:
        return await spawn_subagent(
            manager,
            name=str(args.get("name", "")),
            settings=settings,
            system_prompt=args.get("system_prompt"),
        )

    manager.register_local_tool(
        SPAWN_TOOL_NAME, SPAWN_TOOL_DESCRIPTION, SPAWN_TOOL_SCHEMA, handler
    )


def _err(msg: str) -> tuple[list[dict[str, str]], bool]:
    return [{"type": "text", "text": msg}], True
