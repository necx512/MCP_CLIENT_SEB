"""MCP_CLIENT_SEB — a small, self-contained Model Context Protocol client.

Connects to one or more MCP servers over stdio (declared in a config file in
the standard ``mcpServers`` shape) and exposes them two ways:

* a **manual REPL** (``/tools``, ``/call`` …) that needs no LLM at all, and
* a **Claude-driven agent loop** that lets Claude Opus 4.8 call the MCP tools
  to answer your questions (requires the ``anthropic`` package + an API key).

Entry point: ``python -m mcp_client_seb`` (see ``cli.main``).
"""

__version__ = "0.1.0"
