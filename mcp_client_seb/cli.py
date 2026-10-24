"""REPL entry point — every agent, root or subagent, runs this same REPL.

Front or back is detected, not configured: the one process whose stdin is a
terminal is **front** and reads its prompts from the keyboard. Every other one
(a subagent, whose stdin is its parent's MCP pipe) is **back**: it serves MCP
over stdio and its prompts arrive through the ``ask`` tool (see
:mod:`mcp_client_seb.server`). Either way a prompt goes through the same
:meth:`Repl.handle`, so ``ask`` accepts slash commands too.

What differs is who decides. A permission-gated tool call is approved at the
keyboard in front; in back it's escalated to the parent as an MCP elicitation,
which relays it up until it reaches the front. Output works the same way:
while an ``ask`` runs, back streams every line it would have printed up to its
parent, and the front prints them live under the subagent's path.

The front can redirect its keyboard: ``/focus worker/scout`` makes every line
typed go to that subagent (via ``/ask``), so you talk to it as if it were front.
The focus lives only in the front — the subagents themselves don't change, so
an LLM delegating to ``worker__ask`` meanwhile still reaches ``worker``.

Two kinds of input, always available in the same session:

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
import os
import sys

from mcp import types

from . import __version__
from .backends import BackendUnavailable, BaseAgent, ChatSettings, build_agent
from .config import ClientConfig, default_config_path, load_config
from .manager import OUTPUT_LOGGER, MCPManager, ToolRef, parts_to_text
from .subagent import (
    SpawnSettings, create_agent_config, install_spawn_tool, remove_subagent, spawn_subagent,
)

logger = logging.getLogger("mcp_client_seb.cli")

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


def _flush_stdin() -> None:
    """Drop any unread terminal input (POSIX tty only; a no-op elsewhere)."""
    try:
        import termios  # noqa: PLC0415 — POSIX-only, imported lazily
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 — not a tty / not POSIX / redirected: skip
        pass


# -- permission requests (shared by front and back) ------------------------

#: A permission request travels up the subagent chain as an MCP elicitation
#: with this schema. The message's first line names the tool; each agent that
#: relays it prefixes its child's name, so the front sees ``worker/scout/tool``.
DECISIONS = ("yes", "no", "always")
PERMISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": list(DECISIONS),
            "description": "yes = allow once, no = deny, always = allow for the session",
        },
    },
    "required": ["decision"],
}


def _is_permission_request(params: object) -> bool:
    schema = getattr(params, "requestedSchema", None) or {}
    try:
        return schema["properties"]["decision"]["enum"] == list(DECISIONS)
    except (KeyError, TypeError):
        return False


def _permission_message(ref: ToolRef, arguments: dict) -> str:
    preview = json.dumps(arguments)
    if len(preview) > 300:
        preview = preview[:300] + "…"
    return f"{ref.exposed_name} ({ref.server or 'local'})\nargs: {preview}"


#: Commands the front always runs itself, even while focused on a subagent —
#: otherwise there would be no way back.
_LOCAL_COMMANDS = ("focus", "unfocus", "quit", "exit", "q")


def _is_local(line: str) -> bool:
    return line.startswith("/") and line[1:].split(" ", 1)[0].lower() in _LOCAL_COMMANDS


HELP = f"""
{c('MCP_CLIENT_SEB', 'bold')} v{__version__} — commands:

  {c('/help', 'cyan')}                 show this help
  {c('/servers', 'cyan')}             list connected servers (and any errors)
  {c('/tools', 'cyan')} [filter]      list available tools, optionally filtered
  {c('/schema', 'cyan')} <tool>       print a tool's JSON input schema
  {c('/call', 'cyan')} <tool> [json]  call a tool directly, e.g.
                          /call file_info {{"path": "/bin/ls"}}
  {c('/mkconfig', 'cyan')} <path> [json]
                          write a subagent config file; json takes the same
                          keys as the create_agent_config tool, e.g.
                          /mkconfig w.json {{"system_prompt": "...", "mcp_servers": {{}}}}
  {c('/spawn', 'cyan')} <name> <cfg.json>
                          launch a subagent (this project in server mode) from
                          its config file and connect it. Drive it via its
                          <name>__ask tool.
  {c('/kill', 'cyan')} <name>         stop a spawned subagent and drop its tools
  {c('/ask', 'cyan')} <path> <prompt>  send one prompt to a subagent, e.g.
                          /ask worker/scout /tools — its output streams live
  {c('/focus', 'cyan')} <path>        send every prompt to that subagent until
                          {c('/unfocus', 'cyan')} (talk to it as if it were front)
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
        front: bool = True,
        backend: str = "auto",
        model: str | None = None,
        host: str | None = None,
        api_key: str | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
        system_prompt: str | None = None,
        spawn_settings: SpawnSettings | None = None,
    ):
        self.manager = manager
        #: Front reads the keyboard and prints; back is driven by ``ask`` and
        #: collects its output into the tool's answer (see :meth:`submit`).
        self.front = front
        self._sink: list[str] | None = None   # back: output of the current ask
        self._trace: list[str] = []           # back: tool activity of the current ask
        self._ask_ctx = None                  # back: MCP context of the current ask
        self._lock = asyncio.Lock()           # back: one ask at a time
        self._stream: asyncio.Queue | None = None  # back: live output of the current ask
        self._streamed = 0                    # lines received from subagents (see cmd_ask)
        #: Front only: subagent path (``worker/scout``) the keyboard is redirected to.
        self.focus: str | None = None
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
        self._system_prompt = system_prompt
        self._spawn_settings = spawn_settings or SpawnSettings()
        # Offer create_agent_config / spawn_subagent / remove_subagent to the
        # LLM as local tools (mirror /mkconfig, /spawn, /kill).
        install_spawn_tool(self.manager, self._spawn_settings)
        # Who approves permission-gated calls — ours and our subagents'.
        if front:
            self.manager.permission_prompt = self._ask_permission
            self.manager.elicit_handler = self._ask_elicitation
        else:
            self.manager.permission_prompt = self._escalate_permission
            self.manager.elicit_handler = self._relay_elicitation
        self.manager.output_handler = self._on_subagent_output
        self._init_agent()

    def out(self, text: str = "") -> None:
        """Print in front. In back, stdout is the MCP pipe: the line goes into
        the current ``ask``'s answer *and* streams live to the parent (see
        :meth:`submit`) — or to the log if no ask is running."""
        if self.front:
            print(text)
        elif self._sink is not None:
            self._sink.append(text)
            self._send_up(text)
        elif text.strip():
            logger.info(text)

    def _send_up(self, text: str, path: list[str] | None = None) -> None:
        """Back: queue one line for the parent's screen. ``path`` names the
        nested subagent it came from, if it isn't ours."""
        if self._stream is not None:
            self._stream.put_nowait({"from": path or [], "text": text})

    async def _on_subagent_output(self, server: str, data: dict) -> None:
        """A subagent streamed a line. Front prints it under its path; back
        relays it up, prepending the child's name."""
        self._streamed += 1
        path = [server, *data.get("from", [])]
        if not self.front:
            self._send_up(data["text"], path)
            return
        tag = c(f"  {'/'.join(path)} │ ", "cyan")
        for line in str(data["text"]).splitlines() or [""]:
            print(tag + line)

    def _init_agent(self) -> None:
        """Select a chat backend per --backend (or auto-detect)."""
        settings = ChatSettings(model=self._model, host=self._host, api_key=self._api_key,
                                think=self._think, keep_alive=self._keep_alive,
                                system_prompt=self._system_prompt)
        try:
            self.agent = build_agent(self._backend, self.manager, settings)
        except BackendUnavailable as e:
            self.agent_error = str(e)
            return
        self.agent.show_thinking = self.show_thinking
        self.backend_label = self.agent.label
        if self.agent.warning:
            self.out(c(f"  Note: {self.agent.warning}", "yellow"))

    # -- command handlers --------------------------------------------------

    def cmd_servers(self, _arg: str) -> None:
        for name, cfg in self.manager._servers.items():
            connected = name in self.manager.sessions
            n_tools = sum(1 for t in self.manager.tools.values() if t.server == name)
            status = c("connected", "green") if connected else c("FAILED", "red")
            self.out(f"  {c(name, 'bold')}  [{status}]  {n_tools} tools  "
                  f"{c(cfg.command + ' ' + ' '.join(cfg.args), 'dim')}")
            if name in self.manager.connect_errors:
                self.out(f"      {c(self.manager.connect_errors[name], 'red')}")

    def cmd_tools(self, arg: str) -> None:
        flt = arg.strip().lower()
        shown = 0
        for t in sorted(self.manager.tools.values(), key=lambda x: x.exposed_name):
            if flt and flt not in t.exposed_name.lower() and flt not in t.description.lower():
                continue
            summary = t.description.strip().splitlines()[0] if t.description.strip() else ""
            origin = t.server or "local"
            lock = c(" 🔒", "yellow") if t.requires_permission else ""
            self.out(f"  {c(t.exposed_name, 'cyan')} {c('(' + origin + ')', 'dim')}{lock}")
            if summary:
                self.out(f"      {summary}")
            shown += 1
        self.out(c(f"  {shown} tool(s)", "dim"))

    def cmd_schema(self, arg: str) -> None:
        name = arg.strip()
        ref = self.manager.tools.get(name)
        if not ref:
            self.out(c(f"  Unknown tool: {name!r} (try /tools)", "red"))
            return
        self.out(c(f"  {ref.exposed_name}  —  {ref.description.strip()}", "bold"))
        self.out(json.dumps(ref.input_schema, indent=2))

    async def cmd_call(self, arg: str) -> None:
        parts = arg.strip().split(maxsplit=1)
        if not parts:
            self.out(c("  Usage: /call <tool> [json-args]", "yellow"))
            return
        name = parts[0]
        args_json = parts[1] if len(parts) > 1 else "{}"
        try:
            arguments = json.loads(args_json)
        except json.JSONDecodeError as e:
            self.out(c(f"  Invalid JSON arguments: {e}", "red"))
            return
        self.out(c(f"  → calling {name} …", "dim"))
        content, is_error = await self.manager.call_tool(name, arguments)
        style = "red" if is_error else "green"
        self.out(c(f"  [{'error' if is_error else 'ok'}]", style))
        self.out(parts_to_text(content))

    async def cmd_mkconfig(self, arg: str) -> None:
        path, _, args_json = arg.strip().partition(" ")
        if not path:
            self.out(c("  Usage: /mkconfig <path> [json]", "yellow"))
            return
        try:
            fields = json.loads(args_json) if args_json.strip() else {}
        except json.JSONDecodeError as e:
            self.out(c(f"  Invalid JSON arguments: {e}", "red"))
            return
        if not isinstance(fields, dict):
            self.out(c("  JSON arguments must be an object", "red"))
            return
        content, is_error = create_agent_config(
            path,
            system_prompt=fields.get("system_prompt"),
            description=fields.get("description"),
            mcp_servers=fields.get("mcp_servers"),
            no_required_permission=fields.get("no_required_permission"),
            overwrite=bool(fields.get("overwrite", False)),
        )
        self.out(c(parts_to_text(content), "red" if is_error else "green"))

    async def cmd_spawn(self, arg: str) -> None:
        tokens = arg.strip().split()
        if len(tokens) != 2:
            self.out(c("  Usage: /spawn <name> <config.json>", "yellow"))
            return
        name, config = tokens
        self.out(c(f"  → spawning subagent {name!r} …", "dim"))
        content, is_error = await spawn_subagent(
            self.manager, name, self._spawn_settings, config)
        self.out(c(parts_to_text(content), "red" if is_error else "green"))

    async def cmd_kill(self, arg: str) -> None:
        name = arg.strip().split()[0] if arg.strip() else ""
        if not name:
            self.out(c("  Usage: /kill <name>", "yellow"))
            return
        if self.focus and self.focus.split("/")[0] == name:
            self.cmd_focus("")
        self.out(c(f"  → removing subagent {name!r} …", "dim"))
        content, is_error = await remove_subagent(self.manager, name)
        self.out(c(parts_to_text(content), "red" if is_error else "green"))

    async def cmd_ask(self, arg: str) -> None:
        """Send one prompt to a subagent, routed hop by hop along ``path``.

        Each hop is the next agent's own ``/ask``, so ``worker/scout`` works at
        any depth with no routing tables. Its output streams back live (see
        :meth:`_on_subagent_output`); the returned answer is only printed if
        nothing streamed, so it isn't shown twice.
        """
        path, _, prompt = arg.strip().partition(" ")
        prompt = prompt.strip()
        if not path or not prompt:
            self.out(c("  Usage: /ask <subagent[/sub…]> <prompt>", "yellow"))
            return
        first, _, rest = path.strip("/").partition("/")
        tool = f"{first}__ask"
        if tool not in self.manager.tools:
            self.out(c(f"  No subagent named {first!r} here (see /tools).", "red"))
            return
        if rest:
            prompt = f"/ask {rest} {prompt}"
        before = self._streamed
        content, is_error = await self.manager.call_tool(tool, {"prompt": prompt})
        text = parts_to_text(content)
        if is_error or self._streamed == before:
            self.out(c(text, "red") if is_error else text)
        elif self._sink is not None:
            # Already streamed to the human, but still this ask's answer.
            self._sink.append(text)

    def cmd_focus(self, arg: str) -> None:
        path = arg.strip().strip("/")
        if not self.front:
            self.out("  /focus only applies to the front (the agent at the keyboard).")
            return
        if not path:
            if self.focus:
                self.out(c(f"  back to this agent (was focused on {self.focus}).", "dim"))
            self.focus = None
            return
        first = path.split("/")[0]
        if f"{first}__ask" not in self.manager.tools:
            self.out(c(f"  No subagent named {first!r} here (see /tools).", "red"))
            return
        self.focus = path
        self.out(c(f"  prompts now go to {path} — /unfocus to come back.", "dim"))

    async def cmd_chat(self, text: str) -> None:
        if self.agent is None:
            self.out(c(f"  Chat unavailable: {self.agent_error}", "yellow"))
            self.out(c("  You can still use /call to invoke tools directly.", "dim"))
            return
        self.agent.show_thinking = self.show_thinking
        try:
            await self.agent.send(text, self._on_event)
        except Exception as e:  # noqa: BLE001
            self.out(c(f"\n  Chat error: {type(e).__name__}: {e}", "red"))

    # -- permissions: front decides at the keyboard ------------------------

    async def _ask_permission(self, ref: ToolRef, arguments: dict) -> bool:
        """Approve/deny one of our own gated tool calls. Backs manager.permission_prompt."""
        decision = await self._prompt_decision(_permission_message(ref, arguments))
        if decision == "always":
            self.manager.always_allow.add(ref.exposed_name)
        return decision != "no"

    async def _ask_elicitation(self, server: str, params) -> types.ElicitResult:
        """A subagent escalated a permission request: let the human decide it.

        The subagent records an "always" itself, against its own tool — so
        nothing is stored here. Any other kind of elicitation is declined: this
        client only knows how to render permission requests.
        """
        if not _is_permission_request(params):
            self.out(c(f"  ⚠ {server} asked for input this client can't render — declined.",
                       "yellow"))
            return types.ElicitResult(action="decline")
        decision = await self._prompt_decision(f"{server}/{params.message}")
        return types.ElicitResult(action="accept", content={"decision": decision})

    async def _prompt_decision(self, message: str) -> str:
        """Ask y/n/a at the keyboard. Returns one of :data:`DECISIONS`.

        Runs the blocking prompt in a thread (like the main REPL input) so the
        event loop keeps turning. Default (empty answer) is **no** — fail safe.
        """
        title, _, detail = message.partition("\n")
        # Discard any type-ahead buffered while the model was generating: a
        # security decision must be a fresh, deliberate keystroke, never a stray
        # newline that would otherwise read as "" and auto-deny.
        _flush_stdin()
        self.out(c(f"\n  🔒 permission required — {title}", "yellow", "bold"))
        for line in detail.splitlines():
            self.out(c(f"     {line}", "dim"))
        while True:
            ans = await asyncio.to_thread(
                input, c("     allow? [y]es once / [n]o / [a]lways this session: ", "yellow"))
            ans = ans.strip().lower()
            if ans in ("y", "yes"):
                return "yes"
            if ans in ("", "n", "no"):
                self.out(c("     denied.", "dim"))
                return "no"
            if ans in ("a", "always"):
                self.out(c(f"     always allowing {title} this session.", "dim"))
                return "always"
            self.out(c("     please answer y, n, or a.", "dim"))

    # -- permissions: back escalates to whoever sent the ask ---------------

    async def _escalate_permission(self, ref: ToolRef, arguments: dict) -> bool:
        """Back's manager.permission_prompt: ask our parent, who asks the front."""
        decision = await self._escalate(_permission_message(ref, arguments))
        if decision == "always":
            self.manager.always_allow.add(ref.exposed_name)
        return decision != "no"

    async def _relay_elicitation(self, server: str, params) -> types.ElicitResult:
        """A subagent of ours escalated a request: pass it on up, naming it."""
        if not _is_permission_request(params):
            return types.ElicitResult(action="decline")
        decision = await self._escalate(f"{server}/{params.message}")
        return types.ElicitResult(action="accept", content={"decision": decision})

    async def _escalate(self, message: str) -> str:
        """Send a permission request to our parent, as part of the running ask.

        No ask running means nobody to ask, and a parent that can't answer
        elicitations (or errors, or declines) means no — never an implicit yes.
        """
        ctx = self._ask_ctx
        if ctx is None:
            return "no"
        if self._stream is not None:
            # Flush queued output first, so the front shows what led up to the
            # request (e.g. "→ calling run_bash …") before asking about it.
            await self._stream.join()
        try:
            result = await ctx.session.elicit_form(
                message, PERMISSION_SCHEMA, related_request_id=ctx.request_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("permission escalation failed: %s", e)
            return "no"
        decision = (result.content or {}).get("decision") if result.action == "accept" else None
        return decision if decision in DECISIONS else "no"

    def _on_event(self, kind: str, payload) -> None:
        if kind == "thinking":
            self.out(c(f"\n[thinking] {payload}", "dim", "magenta"))
        elif kind == "text":
            self.out(payload)
        elif kind == "tool_call":
            args = json.dumps(payload["input"])
            if not self.front:
                # The parent's LLM wants an answer, not a log: tool activity only
                # streams to the human, and is kept as a fallback for when no
                # text comes (see submit).
                self._trace.append(f"[tool] {payload['name']}({args})")
            self._activity(c(f"  ⚙ {payload['name']}({args})", "dim", "cyan"))
        elif kind == "tool_result":
            preview = payload["text"].replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            tag = "error" if payload["is_error"] else "ok"
            style = "red" if payload["is_error"] else "dim"
            self._activity(c(f"    ↳ [{tag}] {preview}", style))
        elif kind == "error":
            # Backend/loop failures (bad request, refusal, iteration cap) arrive
            # here; without this branch they'd be silently swallowed.
            self.out(c(f"  ⚠ {payload}", "red"))

    def _activity(self, text: str) -> None:
        """A line for the human only: printed in front, streamed (but kept out
        of the answer) in back."""
        if self.front:
            print(text)
        else:
            self._send_up(text)

    # -- loop --------------------------------------------------------------

    async def run(self) -> None:
        self.out(HELP)
        self.cmd_servers("")
        if self.agent_error:
            self.out(c(f"\n  Chat disabled — {self.agent_error}", "yellow"))
            self.out(c("  Manual tool access via /call still works.\n", "dim"))
        else:
            self.out(c(f"\n  Chat enabled via {self.backend_label}. "
                    "Type a message or a /command.\n", "green"))

        while True:
            try:
                prompt = f"mcp:{self.focus}> " if self.focus else "mcp> "
                line = await asyncio.to_thread(input, c(prompt, "bold", "green"))
            except (EOFError, KeyboardInterrupt):
                self.out()
                break
            if await self.handle(line):
                break

    async def submit(self, line: str, ctx) -> str:
        """Back's input: one prompt from the ``ask`` tool, its output as the answer.

        ``ctx`` is the MCP request context of that ask — the channel permission
        requests are escalated over while it runs.
        """
        async with self._lock:
            self._sink, self._trace, self._ask_ctx = [], [], ctx
            self._stream = asyncio.Queue()
            pump = asyncio.create_task(self._pump(self._stream, ctx))
            try:
                await self.handle(line)
                answer = "\n".join(t for t in self._sink if t).strip()
                trace = self._trace
            finally:
                # Drain before answering: the parent then receives every live
                # line before the result, so nothing prints after the answer.
                self._stream.put_nowait(None)
                await pump
                self._sink, self._ask_ctx, self._stream = None, None, None
        if not answer:
            answer = "(subagent produced no text)"
            if trace:
                answer += "\nTool activity:\n" + "\n".join(trace)
        return answer

    @staticmethod
    async def _pump(queue: asyncio.Queue, ctx) -> None:
        """Send queued output lines to the parent, in order, as MCP log
        notifications tied to the running ask. Display only: a failed send is
        dropped, never allowed to fail the ask."""
        while (item := await queue.get()) is not None:
            try:
                await ctx.session.send_log_message(
                    "info", item, logger=OUTPUT_LOGGER, related_request_id=ctx.request_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("output line not delivered: %s", e)
            finally:
                queue.task_done()  # lets _escalate wait for the backlog

    async def handle(self, line: str) -> bool:
        """Run one prompt — keyboard or ``ask``, it's the same. Returns True to quit."""
        line = line.strip()
        if not line:
            return False
        if self.focus and not _is_local(line):
            line = f"/ask {self.focus} {line}"
        if line.startswith("/"):
            return await self._dispatch(line)
        await self.cmd_chat(line)
        return False

    async def _dispatch(self, line: str) -> bool:
        """Handle a slash command. Returns True to quit."""
        cmd, _, arg = line[1:].partition(" ")
        cmd = cmd.lower()
        if cmd in ("quit", "exit", "q"):
            if not self.front:
                # A subagent's lifetime belongs to its parent, not to a prompt.
                self.out("  /quit is ignored in a subagent — the parent removes it "
                         "(remove_subagent / /kill).")
                return False
            return True
        elif cmd == "help":
            self.out(HELP)
        elif cmd == "servers":
            self.cmd_servers(arg)
        elif cmd == "tools":
            self.cmd_tools(arg)
        elif cmd == "schema":
            self.cmd_schema(arg)
        elif cmd == "call":
            await self.cmd_call(arg)
        elif cmd == "mkconfig":
            await self.cmd_mkconfig(arg)
        elif cmd == "spawn":
            await self.cmd_spawn(arg)
        elif cmd == "kill":
            await self.cmd_kill(arg)
        elif cmd == "ask":
            await self.cmd_ask(arg)
        elif cmd == "focus":
            self.cmd_focus(arg)
        elif cmd == "unfocus":
            self.cmd_focus("")
        elif cmd == "thinking":
            self.show_thinking = not self.show_thinking
            self.out(c(f"  show reasoning: {self.show_thinking}", "dim"))
        elif cmd == "reset":
            if self.agent:
                self.agent.reset()
            self.out(c("  conversation reset", "dim"))
        else:
            self.out(c(f"  Unknown command: /{cmd} (try /help)", "yellow"))
        return False


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_client_seb",
        description="A custom MCP client: manual tool REPL + Claude agent loop.",
    )
    parser.add_argument(
        "--config", "-c", default=None,
        help="Path to a config file with an 'mcpServers' object "
             "(default: servers.json next to the package). For a subagent (stdin "
             "not a terminal), omitting this starts it with no servers (add them "
             "dynamically via its connect_server tool).",
    )
    parser.add_argument(
        "--backend", "-b", choices=["auto", "anthropic", "ollama", "vllm", "copilot"],
        default="auto",
        help="LLM backend for chat mode. 'auto' prefers Anthropic (if a key is "
             "set), then a local Ollama, then a vLLM server (default: auto). "
             "'copilot' (GitHub Copilot via its SDK) must be chosen explicitly.",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help="Model to use for chat (Ollama, vLLM and Copilot backends). Ollama defaults "
             "to $OLLAMA_MODEL or 'llama3.1'; vLLM to $VLLM_MODEL or the first model "
             "the server advertises; Copilot to $COPILOT_MODEL or 'claude-sonnet-5.5'. "
             "(The Anthropic model is fixed in its backend.)",
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
    parser.add_argument(
        "--vllm-host", default=None,
        help="vLLM server URL (default: $VLLM_HOST or http://localhost:8000). vLLM speaks "
             "the OpenAI-compatible API at /v1.",
    )
    parser.add_argument(
        "--vllm-api-key", default=None,
        help="Bearer token for the vLLM server (default: $VLLM_API_KEY or $OPENAI_API_KEY). "
             "Optional; matches the server's --api-key when it was started with one.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s | %(levelname)s | %(message)s",
    )

    think = {"on": True, "off": False}.get(args.ollama_think)  # "auto" -> None
    # host/api_key are generic ChatSettings fields; route the right per-backend
    # flags into them. In 'auto' each backend still falls back to its own env
    # var / default when the flag is unset (host stays None here).
    if args.backend == "vllm":
        chat_host, chat_api_key = args.vllm_host, args.vllm_api_key
    else:
        chat_host, chat_api_key = args.ollama_host, args.ollama_api_key
    # Subagents are child processes that inherit this environment. A key given on
    # the CLI isn't in it yet, so publish it here — via env, never argv (argv is
    # visible in ps) — so spawned subagents can reach the same backend. Inherited
    # by children to any depth. Only sets what was actually passed.
    for var, val in (("VLLM_API_KEY", args.vllm_api_key),
                     ("OLLAMA_API_KEY", args.ollama_api_key)):
        if val:
            os.environ[var] = val
    # How subagents are launched: same interpreter + backend choice, no --config
    # (they start empty and are configured dynamically). Keys flow via env.
    spawn_settings = SpawnSettings(serve_args=_serve_arg_tail(args))

    # Front = the one process at a terminal. A subagent's stdin is its parent's
    # MCP pipe, so it lands in back with nothing to configure — and since only
    # one process can own the terminal, there is only ever one front.
    front = sys.stdin.isatty()

    # A subagent with no --config starts empty; the front defaults to servers.json.
    config = args.config or (str(default_config_path()) if front else None)
    try:
        client_cfg = load_config(config) if config else ClientConfig()
    except (FileNotFoundError, ValueError) as e:
        print(c(f"Config error: {e}", "red"), file=sys.stderr)
        return 2
    servers = client_cfg.servers

    if front:
        print(c(f"Connecting to {len(servers)} MCP server(s) from {config} …", "dim"))
    async with MCPManager(servers, permissions=client_cfg.permissions) as manager:
        if front and not manager.sessions:
            if servers:
                print(c("No servers connected. Check /servers output and your config.", "red"))
            else:
                print(c("No MCP servers configured — chat and /spawn still work.", "dim"))
        repl = Repl(manager, front=front, backend=args.backend, model=args.model,
                    host=chat_host, api_key=chat_api_key,
                    think=think, keep_alive=args.ollama_keep_alive,
                    system_prompt=client_cfg.system_prompt,
                    spawn_settings=spawn_settings)
        try:
            if front:
                await repl.run()
            else:
                # Back: no stdout chatter — the MCP protocol owns stdout.
                from .server import serve  # noqa: PLC0415 — pulls in FastMCP only when serving
                await serve(repl, client_cfg)
        finally:
            if repl.agent is not None:
                await repl.agent.aclose()
    if front:
        print(c("bye.", "dim"))
    return 0


def _serve_arg_tail(args) -> list[str]:
    """Argv (after the interpreter) used to launch a subagent.

    Forwards the parent's backend selection so subagents use the same LLM.
    ``--config`` is added per subagent by :meth:`SpawnSettings.config_for`.
    API keys are inherited from the environment, not passed here.
    """
    tail = ["-m", "mcp_client_seb", "--backend", args.backend]
    if args.model:
        tail += ["--model", args.model]
    if args.ollama_host:
        tail += ["--ollama-host", args.ollama_host]
    if args.ollama_think and args.ollama_think != "auto":
        tail += ["--ollama-think", args.ollama_think]
    if args.ollama_keep_alive:
        tail += ["--ollama-keep-alive", args.ollama_keep_alive]
    if args.vllm_host:
        tail += ["--vllm-host", args.vllm_host]
    return tail


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
