#!/usr/bin/env bash
# Convenience launcher for MCP_CLIENT_SEB.
# Usage: ./run.sh [--config path] [--verbose]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m mcp_client_seb "$@"
