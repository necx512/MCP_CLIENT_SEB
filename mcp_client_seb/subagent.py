"""Spawning and controlling subagents.

A *subagent* is another instance of this very project launched in server mode
(``python -m mcp_client_seb --serve``) and connected to over stdio like any
other MCP server — but it is never listed in ``servers.json``; it is wired up
dynamically at runtime via :meth:`MCPManager.connect`.

The subagent exposes a handful of tools (``ask``, ``connect_server``,
``list_tools``, ``reset``) namespaced under its handle, e.g. ``worker__ask``.
Spawning it registers a local ``spawn_subagent`` tool so the parent's LLM can
create and drive subagents on its own; the same helper backs the ``/spawn`` REPL
command. A subagent's system prompt is fixed at launch via its config file's
``systemPrompt`` (created on the fly when a prompt is given) — there is no
runtime ``set_system_prompt``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ServerConfig
from .manager import MCPManager

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

SPAWN_TOOL_NAME = "spawn_subagent"
SPAWN_TOOL_DESCRIPTION = (
    "Spawn a subagent: a fresh, separately-configurable instance of this MCP "
    "client with its own LLM and its own (initially empty) set of MCP servers. "
    "Returns the new tools it exposes, namespaced under <name>__: use "
    "<name>__ask to delegate a task to it and <name>__connect_server to give it "
    "access to an MCP server. Its system prompt (optional 'system_prompt' arg) is "
    "fixed at spawn time and cannot be changed afterwards."
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
        "config": {
            "type": "string",
            "description": "Optional path to a JSON config file with an 'mcpServers' object; "
                           "the subagent connects to those servers at startup (it starts with "
                           "no servers if omitted).",
        },
    },
    "required": ["name"],
}

REMOVE_TOOL_NAME = "remove_subagent"
REMOVE_TOOL_DESCRIPTION = (
    "Remove a subagent previously created with spawn_subagent: stop its process "
    "and drop its <name>__ tools. Pass the same name it was spawned with. Only "
    "works on spawned subagents, not on statically configured MCP servers."
)
REMOVE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Handle of the subagent to remove (the name it was spawned with).",
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

    def config_for(self, name: str, config: str | None = None) -> ServerConfig:
        args = list(self.serve_args)
        if config:
            args += ["--config", config]
        return ServerConfig(name=name, command=self.python, args=args)


async def spawn_subagent(
    manager: MCPManager,
    name: str,
    settings: SpawnSettings,
    system_prompt: str | None = None,
    config: str | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """Launch a subagent and connect it. Returns ``(content_parts, is_error)``.

    ``config`` is an optional path to a JSON config file (``mcpServers`` shape)
    the subagent connects to at startup; without it the subagent starts empty.

    ``system_prompt`` is delivered to the subagent **through its config file's**
    ``systemPrompt`` key — read once at startup — not via a ``set_system_prompt``
    call. If a prompt is supplied but the target config doesn't exist, it's
    created dynamically just before the spawn (a throwaway temp file when no path
    was given). See :func:`_prepare_config`.

    The return shape matches :meth:`MCPManager.call_tool` so this can back both
    the ``spawn_subagent`` local tool and the ``/spawn`` command directly.
    """
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return _err(f"invalid subagent name {name!r} "
                    "(use letters/digits/_/-, starting with a letter)")

    try:
        config_path, temp_cfg = _prepare_config(name, config, system_prompt)
    except FileNotFoundError as e:
        return _err(str(e))
    except (OSError, ValueError) as e:  # noqa: BLE001 — bad/unwritable config
        return _err(f"could not prepare subagent config: {type(e).__name__}: {e}")

    cfg = settings.config_for(name, config=config_path)
    try:
        try:
            new_tools = await manager.connect(cfg, prefix=name)
        except Exception as e:  # noqa: BLE001 — report, don't crash the caller
            return _err(f"failed to spawn subagent {name!r}: {type(e).__name__}: {e}")
    finally:
        # The subagent reads its config at startup, so it's already been loaded
        # by the time connect() returns (or the child died) — safe to remove.
        if temp_cfg:
            try:
                os.unlink(temp_cfg)
            except OSError:
                pass

    lines = [f"Spawned subagent {name!r}. New tools:"]
    for tool_name in sorted(new_tools):
        ref = manager.tools.get(tool_name)
        desc = ref.description.strip().splitlines()[0] if ref and ref.description.strip() else ""
        lines.append(f"  - {tool_name}: {desc}" if desc else f"  - {tool_name}")

    if system_prompt:
        lines.append("  system prompt: baked into the subagent's config")

    lines.append(f"Delegate work with {name}__ask.")
    return [{"type": "text", "text": "\n".join(lines)}], False


def _prepare_config(
    name: str, config: str | None, system_prompt: str | None,
) -> tuple[str | None, str | None]:
    """Resolve the ``--config`` path for a subagent, creating it if needed.

    Returns ``(config_path, temp_to_cleanup)``. The system prompt is carried by
    the config file's ``systemPrompt`` key (read at startup), never a runtime
    ``set_system_prompt`` call. Behaviour:

    * no config path, no prompt → ``(None, None)`` (subagent starts empty).
    * config path exists → used as-is; if a prompt is also given it's merged over
      the file's servers into a **throwaway** copy (the user's file is untouched).
    * config path missing + prompt → the file is created at that path.
    * no config path + prompt → a throwaway temp file is created.

    ``temp_to_cleanup`` is a path the caller must delete after the spawn (or
    ``None`` for a persistent, caller-chosen path).
    """
    if not config and not system_prompt:
        return None, None

    data: dict[str, Any] = {"mcpServers": {}}
    existing = Path(config).expanduser() if config else None
    if existing and existing.is_file():
        data = json.loads(existing.read_text())  # start from the given config
    elif config and not system_prompt:
        # A path was named but doesn't exist and there's nothing to synthesize.
        raise FileNotFoundError(f"config file not found: {existing}")

    if system_prompt:
        data["systemPrompt"] = system_prompt  # spawn-time prompt wins over the file's

    if existing and not existing.is_file():
        # Named path missing → create it there (persistent; caller's choice).
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text(json.dumps(data, indent=2))
        return str(existing.resolve()), None
    if existing and not system_prompt:
        return str(existing.resolve()), None  # existing file, nothing to inject

    # Need a throwaway file: no path given, or existing file + prompt override.
    fd, tmp = tempfile.mkstemp(prefix=f"subagent-{name}-", suffix=".json")
    os.close(fd)
    Path(tmp).write_text(json.dumps(data, indent=2))
    return tmp, tmp


async def remove_subagent(
    manager: MCPManager, name: str,
) -> tuple[list[dict[str, str]], bool]:
    """Stop a spawned subagent and drop its tools. Returns ``(content, is_error)``.

    Refuses names that aren't spawned subagents — identified by the presence of
    their ``<name>__ask`` tool — so a statically configured MCP server can't be
    torn down by accident.
    """
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return _err(f"invalid subagent name {name!r}")
    if f"{name}__ask" not in manager.tools:
        return _err(f"no subagent named {name!r} is connected "
                    "(spawn one with spawn_subagent first)")
    try:
        removed = await manager.disconnect(name)
    except Exception as e:  # noqa: BLE001 — report, don't crash the caller
        return _err(f"failed to remove subagent {name!r}: {type(e).__name__}: {e}")
    return [{"type": "text",
             "text": f"Removed subagent {name!r} ({len(removed)} tool(s) dropped)."}], False


def install_spawn_tool(manager: MCPManager, settings: SpawnSettings) -> None:
    """Register the local ``spawn_subagent`` and ``remove_subagent`` tools."""

    async def spawn_handler(args: dict) -> tuple[list[dict[str, str]], bool]:
        return await spawn_subagent(
            manager,
            name=str(args.get("name", "")),
            settings=settings,
            system_prompt=args.get("system_prompt"),
            config=args.get("config"),
        )

    async def remove_handler(args: dict) -> tuple[list[dict[str, str]], bool]:
        return await remove_subagent(manager, name=str(args.get("name", "")))

    manager.register_local_tool(
        SPAWN_TOOL_NAME, SPAWN_TOOL_DESCRIPTION, SPAWN_TOOL_SCHEMA, spawn_handler
    )
    manager.register_local_tool(
        REMOVE_TOOL_NAME, REMOVE_TOOL_DESCRIPTION, REMOVE_TOOL_SCHEMA, remove_handler
    )


def _err(msg: str) -> tuple[list[dict[str, str]], bool]:
    return [{"type": "text", "text": msg}], True
