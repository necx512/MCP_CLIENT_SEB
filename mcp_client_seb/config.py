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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ServerConfig:
    """One MCP server the client can launch and talk to."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "ServerConfig":
        if "command" not in data:
            raise ValueError(f"Server {name!r} is missing the required 'command' field")
        return cls(
            name=name,
            command=str(data["command"]),
            args=[str(a) for a in data.get("args", [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            cwd=data.get("cwd"),
        )

    def merged_env(self) -> dict[str, str]:
        """Server env layered on top of the current process environment.

        The MCP stdio helper otherwise starts the child with a minimal env,
        which usually breaks interpreters that need PATH/PYTHONPATH. Inheriting
        keeps ``python3 -m ...`` working out of the box.
        """
        return {**os.environ, **self.env}


@dataclass
class ClientConfig:
    """Everything read from a client config file."""

    servers: dict[str, ServerConfig] = field(default_factory=dict)
    system_prompt: str | None = None  # overrides the built-in SYSTEM_PROMPT when set


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

    return ClientConfig(
        servers={name: ServerConfig.from_dict(name, cfg) for name, cfg in servers.items()},
        system_prompt=_parse_system_prompt(raw.get("systemPrompt")),
    )


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
