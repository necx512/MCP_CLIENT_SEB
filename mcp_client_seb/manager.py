"""Connection manager for one or more MCP servers.

Owns the lifetime of every stdio subprocess + ``ClientSession`` behind a single
``AsyncExitStack``, aggregates their tools into one namespace, and dispatches
tool calls to the right server. Everything here is async; the CLI drives it.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ServerConfig

logger = logging.getLogger("mcp_client_seb.manager")


@dataclass
class ToolRef:
    """A tool exposed to the user, mapped back to its owning server."""

    exposed_name: str      # unique name shown to the user / sent to Claude
    server: str            # which server owns it
    original_name: str     # the tool's real name on that server
    description: str
    input_schema: dict[str, Any]


class MCPManager:
    """Launches MCP servers and provides a unified tool interface."""

    def __init__(self, servers: dict[str, ServerConfig]):
        self._servers = servers
        self._stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, ToolRef] = {}
        self.connect_errors: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "MCPManager":
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def connect_all(self) -> None:
        for name, cfg in self._servers.items():
            try:
                await self._connect(cfg)
            except Exception as e:  # noqa: BLE001 — surface, don't crash the REPL
                self.connect_errors[name] = f"{type(e).__name__}: {e}"
                logger.warning("Failed to connect to %s: %s", name, e)

    async def _connect(self, cfg: ServerConfig) -> None:
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=cfg.merged_env(),
            cwd=cfg.cwd,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.sessions[cfg.name] = session
        await self._register_tools(cfg.name, session)
        logger.info("Connected to %s (%d tools)", cfg.name,
                    sum(1 for t in self.tools.values() if t.server == cfg.name))

    async def _register_tools(self, server: str, session: ClientSession) -> None:
        result = await session.list_tools()
        for tool in result.tools:
            exposed = tool.name
            # Namespace on collision so two servers can both expose e.g. "read".
            if exposed in self.tools:
                exposed = f"{server}__{tool.name}"
            self.tools[exposed] = ToolRef(
                exposed_name=exposed,
                server=server,
                original_name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
            )

    async def aclose(self) -> None:
        await self._stack.aclose()

    # -- dispatch ----------------------------------------------------------

    async def call_tool(self, exposed_name: str, arguments: dict[str, Any] | None):
        """Call a tool by its exposed name.

        Returns ``(content, is_error)`` where ``content`` is a list of normalized
        parts: ``{"type": "text", "text": ...}`` and/or
        ``{"type": "image", "data": <base64>, "media_type": ...}``. Image blocks
        are preserved (not stringified) so vision-capable backends can forward
        screenshots to the model — e.g. the chrome-agent-screenshot MCP server.
        """
        ref = self.tools.get(exposed_name)
        if ref is None:
            return [{"type": "text", "text": f"Unknown tool: {exposed_name!r}"}], True

        session = self.sessions[ref.server]
        try:
            result = await session.call_tool(ref.original_name, arguments or {})
        except Exception as e:  # noqa: BLE001
            return [{"type": "text", "text": f"Tool call raised {type(e).__name__}: {e}"}], True

        return self._result_content(result), bool(getattr(result, "isError", False))

    @staticmethod
    def _result_content(result: Any) -> list[dict[str, Any]]:
        """Normalize a CallToolResult's content blocks into text/image parts."""
        parts: list[dict[str, Any]] = []
        for block in getattr(result, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "image":
                data = getattr(block, "data", None)
                if data:
                    parts.append({
                        "type": "image",
                        "data": data,
                        "media_type": getattr(block, "mimeType", None) or "image/png",
                    })
            elif btype == "resource":
                res = getattr(block, "resource", None)
                txt = getattr(res, "text", None)
                parts.append({"type": "text", "text": txt if txt is not None else str(res)})
            else:
                parts.append({"type": "text", "text": str(block)})
        return parts or [{"type": "text", "text": "(no content returned)"}]


def parts_to_text(parts: list[dict[str, Any]]) -> str:
    """Render normalized tool-result parts as plain text (images become a marker).

    Used for the manual REPL, event previews, and any backend/display that only
    handles text — an image part shows as ``[image <media_type>, ~N bytes]``
    instead of dumping base64.
    """
    out: list[str] = []
    for p in parts:
        if p.get("type") == "image":
            approx = (len(p.get("data", "")) * 3) // 4
            out.append(f"[image {p.get('media_type', 'image/png')}, ~{approx} bytes]")
        else:
            out.append(p.get("text", ""))
    return "\n".join(out).strip() or "(no textual content returned)"
