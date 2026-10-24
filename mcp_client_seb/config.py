"""Server configuration loading.

The config file uses the same ``mcpServers`` shape as Claude Desktop's
``claude_desktop_config.json`` and this repo's ``.mcp.json`` — so a config that
works here works there, and vice-versa. Each server entry supports:

    {
      "command": "python3",          # required — the executable to spawn
      "args": ["-m", "src.server"],  # optional — argv for the server
      "env":  {"FOO": "bar"},        # optional — extra env vars
      "cwd":  "/path/to/project"     # optional — working directory
    }

Only stdio transport is supported (the transport every local MCP server uses).

The file may also carry an optional top-level ``systemPrompt`` that overrides the
agent's built-in system prompt — a plain string, or a list of strings joined by
newlines for a readable multi-line prompt in JSON::

    {
      "systemPrompt": ["You are a pentest assistant.", "Only touch in-scope hosts."],
      "mcpServers": { ... }
    }

Claude Desktop ignores unknown top-level keys, so a config with ``systemPrompt``
still works there — the extra key is simply this client's own.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A namespace prefix must make a clean tool name: ``<prefix>__<tool>``.
_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass
class ServerConfig:
    """One MCP server the client can launch and talk to."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    #: Client-side permission exemption for *this server's* tools. Every tool
    #: needs approval by default; ``True`` = none of this server's tools do,
    #: ``False`` = no exemption, or a set of tool names (original,
    #: un-namespaced) that run without asking. A tool the server itself declares
    #: as needing permission can't be exempted — see :class:`PermissionConfig`.
    no_required_permission: bool | set[str] = False
    #: Optional namespace for this server's tools: every tool is exposed as
    #: ``<prefix>__<tool>``. Opt-in — most servers keep their clean bare names;
    #: set it to disambiguate a subagent (``scout__ask``) or two servers that
    #: share tool names. ``None`` = no prefix (bare, namespaced only on collision).
    prefix: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "ServerConfig":
        if "command" not in data:
            raise ValueError(f"Server {name!r} is missing the required 'command' field")
        _reject_legacy_key(data, f"server {name!r}")
        return cls(
            name=name,
            command=str(data["command"]),
            args=[str(a) for a in data.get("args", [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            cwd=data.get("cwd"),
            no_required_permission=_parse_no_required_permission(
                data.get("noRequiredPermission"), f"server {name!r}"),
            prefix=_parse_prefix(data.get("prefix"), f"server {name!r}"),
        )

    def merged_env(self) -> dict[str, str]:
        """Server env layered on top of the current process environment.

        The MCP stdio helper otherwise starts the child with a minimal env,
        which usually breaks interpreters that need PATH/PYTHONPATH. Inheriting
        keeps ``python3 -m ...`` working out of the box.
        """
        return {**os.environ, **self.env}


@dataclass
class PermissionConfig:
    """Client-wide (global) permission exemptions, applied across all servers.

    Every tool needs human approval before it runs, unless the client exempts
    it (here / per-server config) **and** the server that owns it doesn't
    declare it as needing permission (via its MCP metadata). This holds only the
    client's *global* exemptions; per-server ones live on each
    :class:`ServerConfig.no_required_permission`.
    """

    all_tools: bool = False                    # noRequiredPermission: true    → exempt everything
    tools: set[str] = field(default_factory=set)  # noRequiredPermission: [names] → exempt these


@dataclass
class ClientConfig:
    """Everything read from a client config file."""

    servers: dict[str, ServerConfig] = field(default_factory=dict)
    system_prompt: str | None = None  # overrides the built-in SYSTEM_PROMPT when set
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    #: A one-line summary of what this (sub)agent is for. In ``--serve`` mode it's
    #: woven into the ``ask`` tool's description so a *parent* agent can see what
    #: the subagent specialises in and route work to it. Ignored in REPL mode.
    description: str | None = None


def load_config(config_path: str | os.PathLike) -> ClientConfig:
    """Parse ``config_path`` into a :class:`ClientConfig` (servers + system prompt)."""
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = json.loads(path.read_text())
    servers = raw.get("mcpServers")
    # An *empty* mcpServers object is allowed — that's a deliberate "no fixed
    # servers" run (chat + /spawn still work). Only a missing/malformed key is
    # an error.
    if not isinstance(servers, dict):
        raise ValueError(
            f"{path} has no 'mcpServers' object — expected "
            '{"mcpServers": {"<name>": {"command": "...", ...}}}  '
            '(use {"mcpServers": {}} for no servers)'
        )

    _reject_legacy_key(raw, "the client")
    return ClientConfig(
        servers={name: ServerConfig.from_dict(name, cfg) for name, cfg in servers.items()},
        system_prompt=_parse_system_prompt(raw.get("systemPrompt")),
        permissions=_parse_permissions(raw.get("noRequiredPermission")),
        description=_parse_description(raw.get("description")),
    )


def _parse_prefix(value: Any, where: str) -> str | None:
    """Normalize/validate an optional tool-namespace ``prefix`` for a server."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"'prefix' for {where} must be a string")
    value = value.strip()
    if not value:
        return None
    if not _PREFIX_RE.match(value):
        raise ValueError(f"'prefix' for {where} must be letters/digits/_/- "
                         "and start with a letter")
    return value


def _parse_description(value: Any) -> str | None:
    """Normalize the optional top-level ``description`` (a plain one-liner)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    raise ValueError("'description' must be a string")


def _reject_legacy_key(data: dict[str, Any], where: str) -> None:
    """Fail loudly on the old ``requirePermission`` key.

    Its meaning was inverted (it listed tools to *gate*), so silently ignoring it
    would leave a config doing something other than what its author wrote.
    """
    if "requirePermission" in data:
        raise ValueError(
            f"'requirePermission' for {where} is no longer supported: every tool now "
            "needs approval by default. Use 'noRequiredPermission' (true, or a list "
            "of tool names) to exempt tools instead")


def _parse_no_required_permission(value: Any, where: str) -> bool | set[str]:
    """Normalize a ``noRequiredPermission`` value: bool, or a list of tool names.

    ``True``/``False`` exempt all/none of the relevant tools; a list names the
    tools (by their un-namespaced name) that run without approval. Anything else
    is an error.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return {str(v) for v in value}
    raise ValueError(
        f"'noRequiredPermission' for {where} must be true/false or a list of tool names")


def _parse_permissions(value: Any) -> PermissionConfig:
    """Parse the top-level (global) ``noRequiredPermission`` into a PermissionConfig."""
    rp = _parse_no_required_permission(value, "the client")
    if rp is True:
        return PermissionConfig(all_tools=True)
    if isinstance(rp, set):
        return PermissionConfig(tools=rp)
    return PermissionConfig()


def load_servers(config_path: str | os.PathLike) -> dict[str, ServerConfig]:
    """Parse ``config_path`` and return {name: ServerConfig}."""
    return load_config(config_path).servers


def _parse_system_prompt(value: Any) -> str | None:
    """Normalize the optional ``systemPrompt`` key (string or list of lines)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):  # list of lines → readable multi-line prompt in JSON
        return "\n".join(str(line) for line in value).strip() or None
    raise ValueError("'systemPrompt' must be a string or a list of strings")


def default_config_path() -> Path:
    """The ``servers.json`` shipped next to the package, by convention."""
    return Path(__file__).resolve().parents[1] / "servers.json"
