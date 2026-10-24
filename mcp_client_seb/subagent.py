"""Spawning and controlling subagents.

A *subagent* is another instance of this very project (``python -m
mcp_client_seb``), which serves MCP because its stdin is our pipe rather than a
terminal (see :mod:`mcp_client_seb.cli`), and is connected to over stdio like any
other MCP server — but it is never listed in ``servers.json``; it is wired up
dynamically at runtime via :meth:`MCPManager.connect`.

A subagent is defined **only by its config file** (``systemPrompt``,
``description``, ``mcpServers``, ``noRequiredPermission``): spawning takes a name
and a config path, nothing else. Two functions are shared by the ``/mkconfig``
and ``/spawn`` REPL commands and by the LLM's local tools of the same purpose:

* :func:`create_agent_config` — write a subagent config file;
* :func:`spawn_subagent` — launch a subagent from a config file.

The subagent exposes a handful of tools (``ask``, ``connect_server``,
``list_tools``, ``reset``) namespaced under its handle, e.g. ``worker__ask``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ServerConfig, load_config
from .manager import MCPManager

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

CONFIG_TOOL_NAME = "create_agent_config"
CONFIG_TOOL_DESCRIPTION = (
    "Write a subagent config file (JSON) that spawn_subagent can then launch. "
    "It holds the subagent's system prompt, a one-line description of what it "
    "is for (shown to the parent so it can route work to it), and the MCP "
    "servers it connects to at startup. Refuses to replace an existing file "
    "unless overwrite is true."
)
CONFIG_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Where to write the config file (e.g. agents/worker.json).",
        },
        "system_prompt": {
            "type": "string",
            "description": "System prompt steering the subagent's own LLM.",
        },
        "description": {
            "type": "string",
            "description": "One-line summary of what the subagent specialises in.",
        },
        "mcp_servers": {
            "type": "object",
            "description": "MCP servers the subagent connects to at startup, in "
                           "the mcpServers shape: {\"<name>\": {\"command\": \"...\", "
                           "\"args\": [...], \"env\": {...}, \"cwd\": \"...\"}}.",
        },
        "no_required_permission": {
            "description": "Tools exempt from human approval (every other tool needs "
                           "it): true (all) or a list of tool names.",
            "anyOf": [{"type": "boolean"}, {"type": "array", "items": {"type": "string"}}],
        },
        "overwrite": {
            "type": "boolean",
            "description": "Replace the file if it already exists (default false).",
        },
    },
    "required": ["path"],
}

SPAWN_TOOL_NAME = "spawn_subagent"
SPAWN_TOOL_DESCRIPTION = (
    "Spawn a subagent from a config file (write one first with "
    "create_agent_config): a fresh instance of this MCP client with its own LLM, "
    "system prompt and MCP servers, all taken from that file. Returns the new "
    "tools it exposes, namespaced under <name>__: use <name>__ask to delegate a "
    "task to it."
)
SPAWN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Unique handle for the subagent (letters, digits, _ , -). "
                           "Its tools are exposed as <name>__ask, <name>__connect_server, …",
        },
        "config": {
            "type": "string",
            "description": "Path to the subagent's JSON config file.",
        },
    },
    "required": ["name", "config"],
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
    ``["-m", "mcp_client_seb", "--backend", "anthropic"]``; the subagent's
    ``--config`` is appended by :meth:`config_for`.
    """

    python: str = field(default_factory=lambda: sys.executable)
    serve_args: list[str] = field(default_factory=lambda: ["-m", "mcp_client_seb"])

    def config_for(self, name: str, config: str) -> ServerConfig:
        args = list(self.serve_args) + ["--config", config]
        # Name this child for an attached interceptor (an optional external tool
        # — see llm_interceptor), so it can tell "root/worker/scout" from
        # "root/worker". The parent's own path travels in the env, so this
        # composes to arbitrary depth with no extra plumbing, and a child
        # overrides the value it inherited rather than reusing it. Harmless when
        # nothing is listening, and a no-op when the package isn't installed.
        env = {}
        try:
            from llm_interceptor import child_agent_path  # noqa: PLC0415
        except ImportError:
            pass
        else:
            env["INTERCEPT_AGENT_PATH"] = child_agent_path(name)
        return ServerConfig(name=name, command=self.python, args=args, env=env)


def create_agent_config(
    path: str,
    system_prompt: str | None = None,
    description: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
    no_required_permission: bool | list[str] | None = None,
    overwrite: bool = False,
) -> tuple[list[dict[str, str]], bool]:
    """Write a subagent config file. Returns ``(content_parts, is_error)``.

    The written file is re-read with :func:`load_config` so a malformed server
    entry is reported now rather than at spawn time (the file is then removed).
    Backs both ``/mkconfig`` and the ``create_agent_config`` local tool.
    """
    if not (path or "").strip():
        return _err("missing config path")
    target = Path(path).expanduser()
    if target.exists() and not overwrite:
        return _err(f"config file already exists: {target} (pass overwrite to replace it)")
    if mcp_servers is not None and not isinstance(mcp_servers, dict):
        return _err("'mcp_servers' must be an object {name: {command, args, …}}")

    data: dict[str, Any] = {}
    if description:
        data["description"] = description
    if system_prompt:
        data["systemPrompt"] = system_prompt
    if no_required_permission is not None:
        data["noRequiredPermission"] = no_required_permission
    data["mcpServers"] = mcp_servers or {}

    existed = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        load_config(target)
    except ValueError as e:
        if not existed:
            target.unlink(missing_ok=True)
        return _err(f"invalid subagent config: {e}")
    except OSError as e:
        return _err(f"could not write config: {type(e).__name__}: {e}")

    servers = ", ".join(data["mcpServers"]) or "none"
    return [{"type": "text",
             "text": f"Wrote subagent config {target.resolve()} (MCP servers: {servers}). "
                     f"Launch it with spawn_subagent."}], False


async def spawn_subagent(
    manager: MCPManager,
    name: str,
    settings: SpawnSettings,
    config: str,
) -> tuple[list[dict[str, str]], bool]:
    """Launch a subagent from its config file and connect it.

    Returns ``(content_parts, is_error)``, the shape of
    :meth:`MCPManager.call_tool`, so this backs both ``/spawn`` and the
    ``spawn_subagent`` local tool directly. The config is validated here first
    so a bad file gives a clear error instead of a dead child process.
    """
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return _err(f"invalid subagent name {name!r} "
                    "(use letters/digits/_/-, starting with a letter)")
    if not (config or "").strip():
        return _err("missing config path (write one with create_agent_config)")
    config_path = Path(config).expanduser()
    try:
        load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        return _err(f"bad subagent config: {e}")

    cfg = settings.config_for(name, config=str(config_path.resolve()))
    try:
        new_tools = await manager.connect(cfg, prefix=name)
    except Exception as e:  # noqa: BLE001 — report, don't crash the caller
        return _err(f"failed to spawn subagent {name!r}: {type(e).__name__}: {e}")

    lines = [f"Spawned subagent {name!r} from {config_path}. New tools:"]
    for tool_name in sorted(new_tools):
        ref = manager.tools.get(tool_name)
        desc = ref.description.strip().splitlines()[0] if ref and ref.description.strip() else ""
        lines.append(f"  - {tool_name}: {desc}" if desc else f"  - {tool_name}")
    lines.append(f"Delegate work with {name}__ask.")
    return [{"type": "text", "text": "\n".join(lines)}], False


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
    """Register the ``create_agent_config``, ``spawn_subagent`` and
    ``remove_subagent`` local tools (the LLM's side of ``/mkconfig``,
    ``/spawn`` and ``/kill``)."""

    async def config_handler(args: dict) -> tuple[list[dict[str, str]], bool]:
        return create_agent_config(
            path=str(args.get("path", "")),
            system_prompt=args.get("system_prompt"),
            description=args.get("description"),
            mcp_servers=args.get("mcp_servers"),
            no_required_permission=args.get("no_required_permission"),
            overwrite=bool(args.get("overwrite", False)),
        )

    async def spawn_handler(args: dict) -> tuple[list[dict[str, str]], bool]:
        return await spawn_subagent(
            manager,
            name=str(args.get("name", "")),
            settings=settings,
            config=str(args.get("config", "")),
        )

    async def remove_handler(args: dict) -> tuple[list[dict[str, str]], bool]:
        return await remove_subagent(manager, name=str(args.get("name", "")))

    manager.register_local_tool(
        CONFIG_TOOL_NAME, CONFIG_TOOL_DESCRIPTION, CONFIG_TOOL_SCHEMA, config_handler
    )
    manager.register_local_tool(
        SPAWN_TOOL_NAME, SPAWN_TOOL_DESCRIPTION, SPAWN_TOOL_SCHEMA, spawn_handler
    )
    manager.register_local_tool(
        REMOVE_TOOL_NAME, REMOVE_TOOL_DESCRIPTION, REMOVE_TOOL_SCHEMA, remove_handler
    )


def _err(msg: str) -> tuple[list[dict[str, str]], bool]:
    return [{"type": "text", "text": msg}], True
