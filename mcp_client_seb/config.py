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


def load_servers(config_path: str | os.PathLike) -> dict[str, ServerConfig]:
    """Parse ``config_path`` and return {name: ServerConfig}."""
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = json.loads(path.read_text())
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError(
            f"{path} has no 'mcpServers' object — expected "
            '{"mcpServers": {"<name>": {"command": "...", ...}}}'
        )

    return {name: ServerConfig.from_dict(name, cfg) for name, cfg in servers.items()}


def default_config_path() -> Path:
    """The ``servers.json`` shipped next to the package, by convention."""
    return Path(__file__).resolve().parents[1] / "servers.json"
