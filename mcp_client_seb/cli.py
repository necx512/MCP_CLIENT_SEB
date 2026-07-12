"""Interactive REPL entry point.

Two modes, always available in the same session:

* **Slash commands** — inspect and drive the MCP servers with no LLM:
  ``/servers``, ``/tools``, ``/schema``, ``/call``. These work even without the
  anthropic package or an API key.
* **Chat** — type anything that isn't a slash command and Claude will answer,
  calling MCP tools as needed. Requires ``anthropic`` + ``ANTHROPIC_API_KEY``.

Run: ``python -m mcp_client_seb [--config servers.json]``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import __version__
from .backends import BackendUnavailable, BaseAgent, ChatSettings, build_agent
from .config import default_config_path, load_servers
from .manager import MCPManager, parts_to_text

# -- tiny ANSI helpers (no third-party deps) -------------------------------

_C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "magenta": "\033[35m",
}


def c(text: str, *styles: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(_C[s] for s in styles) + text + _C["reset"]


HELP = f"""
{c('MCP_CLIENT_SEB', 'bold')} v{__version__} — commands:

  {c('/help', 'cyan')}                 show this help
  {c('/servers', 'cyan')}             list connected servers (and any errors)
  {c('/tools', 'cyan')} [filter]      list available tools, optionally filtered
  {c('/schema', 'cyan')} <tool>       print a tool's JSON input schema
  {c('/call', 'cyan')} <tool> [json]  call a tool directly, e.g.
                          /call file_info {{"path": "/bin/ls"}}
  {c('/thinking', 'cyan')}            toggle showing Claude's reasoning summary
  {c('/reset', 'cyan')}               start a fresh chat conversation
  {c('/quit', 'cyan')}                exit (also: /exit, Ctrl-D)

Type anything else to chat with Claude, which will call the MCP tools for you.
"""


class Repl:
    def __init__(
        self,
        manager: MCPManager,
        *,
        backend: str = "auto",
        model: str | None = None,
        host: str | None = None,
        api_key: str | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
    ):
        self.manager = manager
        self.agent: BaseAgent | None = None
        self.agent_error: str | None = None
        self.backend_label: str | None = None
        self.show_thinking = False
        self._backend = backend
        self._model = model
        self._host = host
        self._api_key = api_key
        self._think = think
        self._keep_alive = keep_alive
        self._init_agent()

    def _init_agent(self) -> None:
        """Select a chat backend per --backend (or auto-detect)."""
        settings = ChatSettings(model=self._model, host=self._host, api_key=self._api_key,
                                think=self._think, keep_alive=self._keep_alive)
        try:
            self.agent = build_agent(self._backend, self.manager, settings)
        except BackendUnavailable as e:
            self.agent_error = str(e)
            return
        self.agent.show_thinking = self.show_thinking
        self.backend_label = self.agent.label
        if self.agent.warning:
            print(c(f"  Note: {self.agent.warning}", "yellow"))

    # -- command handlers --------------------------------------------------

    def cmd_servers(self, _arg: str) -> None:
        for name, cfg in self.manager._servers.items():
            connected = name in self.manager.sessions
            n_tools = sum(1 for t in self.manager.tools.values() if t.server == name)
            status = c("connected", "green") if connected else c("FAILED", "red")
            print(f"  {c(name, 'bold')}  [{status}]  {n_tools} tools  "
                  f"{c(cfg.command + ' ' + ' '.join(cfg.args), 'dim')}")
            if name in self.manager.connect_errors:
                print(f"      {c(self.manager.connect_errors[name], 'red')}")

    def cmd_tools(self, arg: str) -> None:
        flt = arg.strip().lower()
        shown = 0
        for t in sorted(self.manager.tools.values(), key=lambda x: x.exposed_name):
            if flt and flt not in t.exposed_name.lower() and flt not in t.description.lower():
                continue
            summary = t.description.strip().splitlines()[0] if t.description.strip() else ""
            print(f"  {c(t.exposed_name, 'cyan')} {c('(' + t.server + ')', 'dim')}")
            if summary:
                print(f"      {summary}")
            shown += 1
        print(c(f"  {shown} tool(s)", "dim"))

    def cmd_schema(self, arg: str) -> None:
        name = arg.strip()
        ref = self.manager.tools.get(name)
        if not ref:
            print(c(f"  Unknown tool: {name!r} (try /tools)", "red"))
            return
        print(c(f"  {ref.exposed_name}  —  {ref.description.strip()}", "bold"))
        print(json.dumps(ref.input_schema, indent=2))

    async def cmd_call(self, arg: str) -> None:
        parts = arg.strip().split(maxsplit=1)
        if not parts:
            print(c("  Usage: /call <tool> [json-args]", "yellow"))
            return
        name = parts[0]
        args_json = parts[1] if len(parts) > 1 else "{}"
        try:
            arguments = json.loads(args_json)
        except json.JSONDecodeError as e:
            print(c(f"  Invalid JSON arguments: {e}", "red"))
            return
        print(c(f"  → calling {name} …", "dim"))
        content, is_error = await self.manager.call_tool(name, arguments)
        style = "red" if is_error else "green"
        print(c(f"  [{'error' if is_error else 'ok'}]", style))
        print(parts_to_text(content))

    async def cmd_chat(self, text: str) -> None:
        if self.agent is None:
            print(c(f"  Chat unavailable: {self.agent_error}", "yellow"))
            print(c("  You can still use /call to invoke tools directly.", "dim"))
            return
        self.agent.show_thinking = self.show_thinking
        try:
            await self.agent.send(text, self._on_event)
        except Exception as e:  # noqa: BLE001
            print(c(f"\n  Chat error: {type(e).__name__}: {e}", "red"))

    def _on_event(self, kind: str, payload) -> None:
        if kind == "thinking":
            print(c(f"\n[thinking] {payload}", "dim", "magenta"))
        elif kind == "text":
            print(payload)
        elif kind == "tool_call":
            args = json.dumps(payload["input"])
            print(c(f"  ⚙ {payload['name']}({args})", "dim", "cyan"))
        elif kind == "tool_result":
            preview = payload["text"].replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            tag = "error" if payload["is_error"] else "ok"
            style = "red" if payload["is_error"] else "dim"
            print(c(f"    ↳ [{tag}] {preview}", style))

    # -- loop --------------------------------------------------------------

    async def run(self) -> None:
        print(HELP)
        self.cmd_servers("")
        if self.agent_error:
            print(c(f"\n  Chat disabled — {self.agent_error}", "yellow"))
            print(c("  Manual tool access via /call still works.\n", "dim"))
        else:
            print(c(f"\n  Chat enabled via {self.backend_label}. "
                    "Type a message or a /command.\n", "green"))

        while True:
            try:
                line = await asyncio.to_thread(input, c("mcp> ", "bold", "green"))
            except (EOFError, KeyboardInterrupt):
                print()
                break

            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if await self._dispatch(line):
                    break
            else:
                await self.cmd_chat(line)

    async def _dispatch(self, line: str) -> bool:
        """Handle a slash command. Returns True to quit."""
        cmd, _, arg = line[1:].partition(" ")
        cmd = cmd.lower()
        if cmd in ("quit", "exit", "q"):
            return True
        elif cmd == "help":
            print(HELP)
        elif cmd == "servers":
            self.cmd_servers(arg)
        elif cmd == "tools":
            self.cmd_tools(arg)
        elif cmd == "schema":
            self.cmd_schema(arg)
        elif cmd == "call":
            await self.cmd_call(arg)
        elif cmd == "thinking":
            self.show_thinking = not self.show_thinking
            print(c(f"  show reasoning: {self.show_thinking}", "dim"))
        elif cmd == "reset":
            if self.agent:
                self.agent.reset()
            print(c("  conversation reset", "dim"))
        else:
            print(c(f"  Unknown command: /{cmd} (try /help)", "yellow"))
        return False


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_client_seb",
        description="A custom MCP client: manual tool REPL + Claude agent loop.",
    )
    parser.add_argument(
        "--config", "-c", default=str(default_config_path()),
        help="Path to a config file with an 'mcpServers' object "
             "(default: servers.json next to the package).",
    )
    parser.add_argument(
        "--backend", "-b", choices=["auto", "anthropic", "ollama"], default="auto",
        help="LLM backend for chat mode. 'auto' prefers Anthropic (if a key is "
             "set), else Ollama (default: auto).",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help="Model to use for chat. Ollama backend only; defaults to "
             "$OLLAMA_MODEL or 'llama3.1'. (Anthropic model is set in agent.py.)",
    )
    parser.add_argument(
        "--ollama-host", default=None,
        help="Ollama server URL (default: $OLLAMA_HOST or http://localhost:11434).",
    )
    parser.add_argument(
        "--ollama-api-key", default=None,
        help="Bearer token for the Ollama server (default: $OLLAMA_API_KEY). Optional; "
             "sent as 'Authorization: Bearer ...' on every Ollama request.",
    )
    parser.add_argument(
        "--ollama-think", choices=["auto", "on", "off"], default="auto",
        help="Ollama reasoning phase: 'off' skips the model's <think> step — much faster "
             "for tool-driving agents like this one (default: auto = the model's default).",
    )
    parser.add_argument(
        "--ollama-keep-alive", default=None,
        help="How long Ollama keeps the model in VRAM between steps, e.g. 30m, -1 (forever), "
             "0 (unload now). Default: unset (Ollama's own default, ~5m). Use -1 to keep a "
             "remote model resident and avoid reloads. Also reads $OLLAMA_KEEP_ALIVE.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s | %(levelname)s | %(message)s",
    )

    try:
        servers = load_servers(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(c(f"Config error: {e}", "red"), file=sys.stderr)
        return 2

    print(c(f"Connecting to {len(servers)} MCP server(s) from {args.config} …", "dim"))
    async with MCPManager(servers) as manager:
        if not manager.sessions:
            print(c("No servers connected. Check /servers output and your config.", "red"))
        think = {"on": True, "off": False}.get(args.ollama_think)  # "auto" -> None
        repl = Repl(manager, backend=args.backend, model=args.model,
                    host=args.ollama_host, api_key=args.ollama_api_key,
                    think=think, keep_alive=args.ollama_keep_alive)
        await repl.run()
    print(c("bye.", "dim"))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
