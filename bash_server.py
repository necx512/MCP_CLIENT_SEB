#!/usr/bin/env python3
"""A tiny MCP server exposing one tool: run a bash command on the host.

The ``run_bash`` tool is **declared as requiring permission** — both via the
tool's standard ``_meta`` (``{"requiresPermission": true}``) and a matching
``requiresPermission`` annotation. A client that honours that flag (like
MCP_CLIENT_SEB) will ask a human before every call, regardless of the client's
own config. See the "Tool permissions" section of that project's README.

Run standalone:  python3 bash_server.py     (speaks MCP over stdio)
Usually it's launched for you by the client via servers.json.
"""

from __future__ import annotations

import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("bash")


def run_bash(command: str, timeout: float = 30.0) -> str:
    """Execute a bash command and return its exit code plus stdout/stderr."""
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]  command: {command!r}"
    except Exception as e:  # noqa: BLE001 — report, the client shows it as a tool error
        return f"[failed to run: {type(e).__name__}: {e}]"

    body = proc.stdout or ""
    if proc.stderr:
        body += ("\n" if body else "") + "[stderr]\n" + proc.stderr
    return f"[exit {proc.returncode}]\n{body}".strip()


mcp.add_tool(
    run_bash,
    name="run_bash",
    description="Execute a bash command on the host and return its output. Use this tool only if no other tool allow you to do what you want",
    # Two ways to say the same thing; a client only needs to read one:
    annotations=ToolAnnotations(destructiveHint=True, requiresPermission=True),
    meta={"requiresPermission": True},
)


if __name__ == "__main__":
    mcp.run()  # stdio transport
