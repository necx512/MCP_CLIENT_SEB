# MCP_CLIENT_SEB

A small, self-contained **Model Context Protocol (MCP) client**. It launches one
or more MCP servers (declared in a config file), connects to them over stdio, and
lets you use their tools two ways:

1. **Manual REPL** — inspect and call tools by hand. No LLM, no API key needed.
2. **Agent loop** — chat in plain English and an LLM calls the MCP tools for you
   to answer. Two interchangeable backends: **Claude** (Anthropic API) or a
   local **Ollama** model.

It comes pre-wired to the **`re-assistant`** reverse-engineering MCP server in
`../mcp-re-assistant`, but it will drive **any** MCP server you list in
`servers.json`.

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [Install](#2-install)
3. [Configure your servers](#3-configure-your-servers)
4. [The two modes](#4-the-two-modes)
5. [Choosing a chat backend (Claude or Ollama)](#5-choosing-a-chat-backend-claude-or-ollama)
6. [Command reference](#6-command-reference)
7. [Walkthrough: manual mode](#7-walkthrough-manual-mode)
8. [Walkthrough: chat mode](#8-walkthrough-chat-mode)
9. [Adding another MCP server](#9-adding-another-mcp-server)
10. [Configuration options](#10-configuration-options)
11. [Troubleshooting](#11-troubleshooting)
12. [How it works](#12-how-it-works)
13. [Subagents (spawning and controlling instances)](#13-subagents-spawning-and-controlling-instances)
14. [Tool permissions (approve before running)](#14-tool-permissions-approve-before-running)

---

## 1. Quick start

```bash
cd ~/MCP_CLIENT_SEB
pip install -r requirements.txt      # mcp is enough for manual mode
./run.sh
```

You'll land in a prompt. Try:

```
mcp> /servers          # what's connected?
mcp> /tools            # what can I call?
mcp> /call file_info {"path": "/bin/ls"}
mcp> /quit
```

To enable **chat mode**, also install `anthropic` and set an API key (see §2).

---

## 2. Install

Requires **Python 3.10+**.

```bash
cd ~/MCP_CLIENT_SEB
pip install -r requirements.txt
```

This installs:

| Package | Needed for | Required? |
|---|---|---|
| `mcp` | Talking to MCP servers | **Yes** — always |
| `anthropic` | Chat mode via the **Claude** backend | Optional |

The manual REPL works with just `mcp`. Chat mode needs an LLM backend — either
Claude or Ollama:

**Claude (Anthropic API):** install `anthropic` (already in `requirements.txt`)
and set a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...       # or ANTHROPIC_AUTH_TOKEN
```

**Ollama (local, free, no API key):** no extra Python package needed — the
Ollama backend uses only the standard library. Just run an Ollama server with a
**tool-capable** model pulled:

```bash
ollama serve                 # if not already running
ollama pull llama3.1         # or qwen2.5, qwen3, mistral-nemo, llama3.2 …
python3 -m mcp_client_seb --backend ollama --model llama3.1
```

See [§5](#5-choosing-a-chat-backend-claude-or-ollama) for how backends are
chosen. If no backend is available, the client still starts — manual `/call`
access keeps working.

---

## 3. Configure your servers

Servers are declared in `servers.json` using the same `mcpServers` shape as
Claude Desktop and this repo's `.mcp.json`. The shipped default loads the
reverse-engineering server:

```json
{
  "mcpServers": {
    "re-assistant": {
      "command": "python3",
      "args": ["-m", "src.server"],
      "cwd": "/home/debian/mcp-re-assistant"
    }
  }
}
```

- `command` **(required)** — the executable to launch.
- `args` — argument list passed to it.
- `cwd` — working directory the server runs in.
- `env` — extra environment variables.

Point the client at a different file with `--config`:

```bash
python3 -m mcp_client_seb --config /path/to/other-servers.json
```

---

## 4. The two modes

You are always in **one prompt**; what you type decides the mode:

| You type… | What happens |
|---|---|
| A line starting with `/` | It's a **command** (manual mode) — runs locally, no LLM. |
| Anything else | It's a **chat message** — sent to Claude, which may call tools. |

So `/call file_info {"path":"/bin/ls"}` runs the tool directly, while
`is /bin/ls safe?` asks Claude (which will call tools on its own).

Manual mode needs nothing but `mcp`. Chat mode needs an LLM backend (Claude or
Ollama); if none is available, chat is disabled but manual commands keep working.

---

## 5. Choosing a chat backend (Claude or Ollama)

Chat mode can be powered by either backend. Pick with `--backend`:

```bash
python3 -m mcp_client_seb --backend auto        # default
python3 -m mcp_client_seb --backend anthropic   # Claude (Anthropic API)
python3 -m mcp_client_seb --backend ollama      # local Ollama
```

**`auto`** (the default) prefers Claude if an `ANTHROPIC_API_KEY` is set, and
otherwise falls back to a local Ollama server. The startup banner tells you which
one is active, e.g. `Chat enabled via Ollama (llama3.1).`

### Using Ollama

Ollama runs models locally — free, private, no API key. Requirements:

1. An Ollama server running (`ollama serve`, or the desktop app).
2. A **tool-capable** model pulled — tool calling is what lets the model drive
   the MCP tools. Known-good families: `llama3.1`, `llama3.2`, `qwen2.5`,
   `qwen3`, `mistral-nemo`, `firefunction-v2`. A model without tool support will
   just answer from its own knowledge and never call a tool.

```bash
ollama pull llama3.1
python3 -m mcp_client_seb --backend ollama --model llama3.1
```

Configuration:

| What | Flag | Env var | Default |
|---|---|---|---|
| Model | `--model` | `OLLAMA_MODEL` | `llama3.1` |
| Server URL | `--ollama-host` | `OLLAMA_HOST` | `http://localhost:11434` |

Examples:

```bash
# Different model
python3 -m mcp_client_seb --backend ollama --model qwen2.5

# Remote / non-default Ollama host
python3 -m mcp_client_seb --backend ollama --ollama-host http://192.168.1.50:11434

# Via environment
OLLAMA_MODEL=qwen3 python3 -m mcp_client_seb --backend ollama
```

If the chosen model isn't pulled, the client prints a note at startup with the
`ollama pull` command to fix it. If the server isn't reachable, chat is disabled
with a message (and manual `/call` still works).

> **Note:** local models are generally weaker at multi-step tool use than Claude.
> For a demanding RE workflow, expect to guide them more, or use a larger model.

### Which backend for which model?

- `--model` sets the **Ollama** model only.
- The **Claude** model is `claude-opus-4-8`, set in
  `mcp_client_seb/backends/anthropic_backend.py` (change `MODEL` there).

---

## 6. Command reference

| Command | Description |
|---|---|
| `/help` | Show the command list. |
| `/servers` | List configured servers, connection status, tool counts, and errors. |
| `/tools [filter]` | List tools. Optional `filter` matches name or description. |
| `/schema <tool>` | Print a tool's JSON input schema (so you know what args to pass). |
| `/call <tool> [json]` | Call a tool directly. `json` is its arguments; omit for none. |
| `/spawn <name> [prompt]` | Launch a **subagent** (this project in server mode) and connect it dynamically. Drive it via its `<name>__ask` tool. See [§13](#13-subagents-spawning-and-controlling-instances). |
| `/kill <name>` | Stop a spawned subagent: terminate its process and drop its `<name>__` tools. |
| `/thinking` | Toggle showing Claude's reasoning summary in chat mode. |
| `/reset` | Clear the chat conversation and start fresh. |
| `/quit` | Exit. Also `/exit`, `/q`, or Ctrl-D. |

Anything not starting with `/` is a chat message to Claude.

---

## 7. Walkthrough: manual mode

This mode needs **no API key** — great for exploring a server or scripting.

**See what connected:**

```
mcp> /servers
  re-assistant  [connected]  41 tools  python3 -m src.server
```

**Find a tool** (filter by keyword):

```
mcp> /tools file
  file_info (re-assistant)
      File type, size, sha256/md5, ssdeep — runs before load_binary.
  ...
```

**Check its arguments** before calling:

```
mcp> /schema file_info
  file_info  —  File type, size, sha256/md5, ssdeep — runs before load_binary.
  {
    "type": "object",
    "properties": { "path": { "type": "string" } },
    "required": ["path"]
  }
```

**Call it** — pass arguments as a JSON object:

```
mcp> /call file_info {"path": "/bin/ls"}
  [ok]
  {
    "binary": "/bin/ls",
    "file_type": "ELF 64-bit LSB pie executable, x86-64, ... stripped",
    "sha256": "833d6f9cf3ede2225d80eaa159ef78a141c92842a691179aec37d182cc808a5c",
    "size": 158632
  }
```

Tools that take no arguments can be called bare: `/call backend_status`.

> **Tip — JSON quoting.** The part after the tool name must be valid JSON:
> double-quoted keys and strings. `/call file_info {"path": "/bin/ls"}` ✅,
> `/call file_info {path: /bin/ls}` ❌.

---

## 8. Walkthrough: chat mode

Once a backend is active (Claude or Ollama — see [§5](#5-choosing-a-chat-backend-claude-or-ollama)),
just describe what you want. The model picks the tools, runs them, and explains
the result. The flow is identical regardless of backend:

```
mcp> Triage /bin/ls and tell me if anything looks risky.
  ⚙ triage_binary({"path": "/bin/ls"})
    ↳ [ok] {"binary": {...}, "mitigations": {...}, "dangerous_imports": [...]}
Based on the triage: /bin/ls is a standard PIE ELF with NX, RELRO, and a stack
canary enabled. It imports a few functions worth noting (strcpy, ...), but
nothing here indicates an obvious vulnerability. Next I'd look at ...
```

What the markers mean while Claude works:

- `⚙ tool_name({...})` — the model is calling that MCP tool with those arguments.
- `↳ [ok]` / `↳ [error]` — the (truncated) tool result fed back to the model.
- Plain text — the model's answer to you.

Useful chat-mode commands:

- `/thinking` — toggle a reasoning summary between tool calls (Claude; also
  reasoning-capable Ollama models like `qwen3`).
- `/reset` — forget the current conversation and start over.

Conversation context is kept across turns, so you can follow up:
*"now decompile the most suspicious function"* and it builds on what it found.

---

## 9. Adding another MCP server

Add another entry under `mcpServers` in `servers.json`. Example — the official
filesystem server via `npx`:

```json
{
  "mcpServers": {
    "re-assistant": {
      "command": "python3",
      "args": ["-m", "src.server"],
      "cwd": "/home/debian/mcp-re-assistant"
    },
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
```

Restart the client. `/servers` now shows both, and their tools are merged into
one namespace. If two servers expose a tool with the same name, the second is
automatically renamed `server__tool` (e.g. `files__read`) so both stay callable.
To namespace a server's tools up front (not just on collision), give it a
`prefix`: `"prefix": "scout"` exposes every tool as `scout__<tool>`. This is the
way to give a statically-configured subagent tidy `scout__ask` / `scout__reset`
names instead of bare ones.

---

## 10. Configuration options

**Per-server fields** (all under each `mcpServers` entry):

| Field | Required | Meaning |
|---|---|---|
| `command` | ✅ | Executable to launch (e.g. `python3`, `npx`, `node`). |
| `args` | — | List of arguments. |
| `cwd` | — | Working directory for the server process. |
| `env` | — | Extra env vars (layered on top of your current environment). |
| `requirePermission` | — | Gate this server's tools behind human approval: `true` (all), `false`/omitted (none), or a list of tool names. See [§14](#14-tool-permissions-approve-before-running). |
| `prefix` | — | Namespace this server's tools as `<prefix>__<tool>`. Opt-in; most servers keep bare names. Use it to disambiguate a statically-configured subagent (`"prefix": "scout"` → `scout__ask`) or two servers sharing tool names. |

**Command-line flags:**

| Flag | Meaning |
|---|---|
| `--config PATH`, `-c PATH` | Use a different config file (default: `servers.json`). |
| `--backend`, `-b` | Chat backend: `auto` (default), `anthropic`, or `ollama`. |
| `--model`, `-m` | Ollama model to use (default: `$OLLAMA_MODEL` or `llama3.1`). |
| `--ollama-host` | Ollama server URL (default: `$OLLAMA_HOST` or `http://localhost:11434`). |
| `--verbose`, `-v` | Enable debug logging (connection details, errors). |

**Environment variables:** `ANTHROPIC_API_KEY` (Claude), `OLLAMA_MODEL`,
`OLLAMA_HOST`.

**Changing the model:**
- Ollama — use `--model` / `$OLLAMA_MODEL`.
- Claude — edit `MODEL` / `MAX_TOKENS` at the top of
  `mcp_client_seb/backends/anthropic_backend.py` (default `claude-opus-4-8`
  with adaptive thinking).

Only **stdio** transport is supported (what local MCP servers use).

---

## 11. Troubleshooting

**"Chat disabled — anthropic → … not installed / No ANTHROPIC_API_KEY …"**
For the Claude backend: `pip install anthropic` and
`export ANTHROPIC_API_KEY=sk-ant-...`. Or use Ollama: `--backend ollama`.

**"Chat disabled — ollama → No Ollama server reachable …"**
Start Ollama (`ollama serve` or the desktop app), then restart. Check the URL
with `--ollama-host` / `$OLLAMA_HOST`.

**"Note: Model '…' is not pulled …"**
Run the `ollama pull <model>` command shown, or pass a model you already have
with `--model`.

**Ollama chats but never calls any tools.**
The model probably lacks tool-calling support. Switch to a tool-capable model
(`llama3.1`, `qwen2.5`, `qwen3`, `mistral-nemo`, …). Smaller models also call
tools less reliably — try a larger one.

**A server shows `[FAILED]` in `/servers`.**
The error is printed under it. Common causes:
- Wrong `cwd` — the server can't find its own module. Fix the path.
- Missing dependency — run the server's own `command` by hand to see the error.
- Wrong `command` — e.g. `python` vs `python3`, or `npx` not on PATH.
Re-run with `-v` for full logs.

**`Invalid JSON arguments` on `/call`.**
The arguments must be a JSON object with double quotes:
`/call file_info {"path": "/bin/ls"}`.

**`Unknown tool`.**
Run `/tools` to see exact names. Namespaced tools look like `files__read`.

**A stderr line about `unicornlib.so` / `unicorn support disabled`.**
Harmless — it comes from the `re-assistant` server's optional angr acceleration,
not from this client.

---

## 12. How it works

```
 servers.json ─▶ config.py ─▶ MCPManager ─▶ one stdio subprocess per server
                                  │             (mcp ClientSession)
                                  ├─ aggregates all tools into one namespace
                                  ├─ /call ─────────────────▶ session.call_tool()
                                  │
   cli.py ─▶ build_agent(backend) ─▶ BaseAgent  ◀── shared tool-use loop
                                        ├── AnthropicAgent  ─┐
                                        └── OllamaAgent      ├─▶ MCPManager.call_tool()
                                                             ┘
```

Every backend is a `BaseAgent` subclass. The agentic loop (call model → run the
requested tools → feed results back → repeat) is written **once** in
`backends/base.py`; each backend only implements the provider-specific bits
(`create()`, `_complete()`, and history bookkeeping). So the two providers are
fully symmetric — same construction contract, same interface — and the REPL is
backend-agnostic.

- **`config.py`** — parses the `mcpServers` config into `ServerConfig` objects.
- **`manager.py`** — `MCPManager` owns every server subprocess behind one
  `AsyncExitStack`, aggregates their tools, and dispatches calls. Provider-neutral.
- **`backends/base.py`** — `BaseAgent`: the shared tool-use loop + the
  `ChatSettings` / `Turn` / `ToolCall` data types and `BackendUnavailable`.
- **`backends/anthropic_backend.py`** — `AnthropicAgent` (`claude-opus-4-8`,
  adaptive thinking; `anthropic` SDK imported lazily).
- **`backends/ollama_backend.py`** — `OllamaAgent` (Ollama `/api/chat`, stdlib
  only).
- **`backends/__init__.py`** — the `BACKENDS` registry + `build_agent()` factory
  (`auto`/`anthropic`/`ollama` selection).
- **`cli.py`** — the REPL. Routes `/commands` to the manager and everything else
  to the active agent.

**Adding another provider** = drop a `BaseAgent` subclass in `backends/` and add
it to `BACKENDS`. Nothing else changes.

### Project layout

```
MCP_CLIENT_SEB/
├── servers.json                    # your MCP server list
├── requirements.txt
├── run.sh                          # ./run.sh launcher
├── README.md                       # this file
└── mcp_client_seb/
    ├── __main__.py                 # python -m mcp_client_seb
    ├── cli.py                      # REPL + backend selection + /spawn
    ├── manager.py                  # MCPManager (provider-neutral, dynamic connect)
    ├── config.py                   # config parsing
    ├── server.py                   # --serve: expose this client AS an MCP server
    ├── subagent.py                 # spawn_subagent tool + /spawn helper
    └── backends/
        ├── __init__.py             # BACKENDS registry + build_agent()
        ├── base.py                 # BaseAgent: shared tool-use loop
        ├── anthropic_backend.py    # Claude backend
        └── ollama_backend.py       # Ollama backend
```

---

## 13. Subagents (spawning and controlling instances)

This client is **both an MCP client and an MCP server**. As a client it consumes
tools from MCP servers (everything above). As a server it can *be* a tool for
another instance of itself — a **subagent** that a parent instance spawns and
controls. An agent driving a subagent driving a sub-subagent, and so on.

### The idea

* A subagent is just `python -m mcp_client_seb --serve`: instead of a REPL it
  speaks MCP over stdio.
* It is **never put in `servers.json`.** The parent launches it and connects to
  it **dynamically at runtime** — that is the whole point. (`servers.json` is
  for your fixed, always-on tool servers.)
* A subagent is a *full agent*: it has its own LLM and its own (initially empty)
  set of MCP servers, which you add on the fly.

### The tools a subagent exposes

Once connected as `worker`, these appear in the parent's tool namespace:

| Tool | What it does |
|---|---|
| `worker__ask(prompt)` | Delegate a task. The subagent reasons with its **own** LLM and tools, then returns its answer. |
| `worker__connect_server(name, command, args, env, cwd)` | Give the subagent a new MCP server — **dynamic configuration**. |
| `worker__list_tools()` | List the tools the subagent currently has. |
| `worker__reset()` | Start the subagent on a fresh conversation. |

A subagent's **system prompt is fixed at spawn time**, delivered through its
config file's `systemPrompt` key (created on the fly when you pass a prompt to
`/spawn` or `spawn_subagent`). There is no runtime re-steering tool — to change
it, respawn the subagent or edit its config.

**Telling the parent what a subagent is for.** The parent's LLM only sees the
subagent's `ask` tool — so it can only route work to the right subagent if that
tool's *description* says what the subagent specialises in. Add a one-line
`description` to the subagent's config and it's woven into the `ask` description
the parent receives:

```json
{
  "description": "Scout: a binary reverse-engineering specialist. Give it a path; it triages the binary and reports mitigations, dangerous imports, and exploitability.",
  "systemPrompt": "You are Scout, a focused binary RE subagent …",
  "mcpServers": { ... }
}
```

Without a `description`, the subagent falls back to a trimmed `systemPrompt`
(so `/spawn worker <prompt>` already gives the parent *something* to route on),
and with neither it uses a generic "delegate a task" description. The
`systemPrompt` still steers the subagent's own reasoning; `description` is purely
the outward-facing blurb the parent sees.

### Two ways to spawn

**From the REPL** (human-driven):

```
mcp> /spawn worker You are a focused web-research subagent.
  → spawning subagent 'worker' …
Spawned subagent 'worker'. New tools:
  - worker__ask
  - worker__connect_server
  - worker__list_tools
  - worker__reset
  system prompt: baked into the subagent's config
Delegate work with worker__ask.

mcp> /call worker__connect_server {"name": "web", "command": "python3", "args": ["-m", "some_web_server"]}
mcp> /call worker__ask {"prompt": "Summarize example.com"}
```

**From chat** (LLM-driven): the parent's model is given built-in
`spawn_subagent` and `remove_subagent` tools, so it can create, orchestrate, and
tear down subagents itself — spawn a worker, give it servers via
`worker__connect_server`, delegate with `worker__ask`, then `remove_subagent` it
when done — all within one conversation.

**Removing a subagent** — `/kill <name>` (or the `remove_subagent` tool) stops
the subagent's process and drops its `<name>__` tools; the name is then free to
reuse. It only acts on spawned subagents, never on statically configured
servers.

### Notes

* Subagents inherit the parent's **backend choice** (`--backend`, `--model`, …)
  so they use the same LLM. API keys flow through the **environment**, not the
  command line.
* From Python you can drive this directly:
  `await manager.connect(cfg, prefix="worker")` connects any MCP server at
  runtime; `spawn_subagent(manager, "worker", SpawnSettings())` wraps the launch.
* Subagent processes are children of the parent and are shut down cleanly when
  the parent exits.

---

## 14. Tool permissions (approve before running)

Some tools shouldn't run unattended — running a shell command, deleting a file,
posting to an API. This client can **pause and ask you** before it executes such
a tool, whether the call came from the LLM in chat mode or from a manual
`/call`. The gate is generic: it works for **every tool on every MCP server**,
not just ones written for this client.

### Two places decide — and they OR together

Whether a given tool needs approval is decided by **two independent sources**:

1. **The MCP server** that owns the tool can declare it needs permission, in its
   tool metadata (see below).
2. **This client's config file** can require it, per-server or globally.

The rule is a logical **OR**: if *either* side asks for permission, you are
asked. A tool runs without prompting **only when both sides stay silent**. So a
cautious server is honoured even if your config says nothing, and a cautious
config is honoured even for a server that declares nothing.

### Configuring it in the client (`servers.json`)

**Per server** — add `requirePermission` inside a server's entry:

```json
{
  "mcpServers": {
    "bash":  { "command": "python3", "args": ["bash_server.py"],
               "requirePermission": true },
    "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
               "requirePermission": ["write_file", "move_file"] }
  }
}
```

- `true` — every tool from this server needs approval.
- `false` / omitted — this server adds no client-side requirement (the server's
  own declaration can still trigger it).
- `["a", "b"]` — only those tools (by their un-namespaced name) need approval.

**Globally** — a top-level `requirePermission` applies across *all* servers:

```json
{
  "requirePermission": true,                 // ask before every tool, everywhere
  "mcpServers": { ... }
}
```

or a list of tool names: `"requirePermission": ["run_bash", "delete_user"]`.

### Configuring it in the server

A server marks a tool as needing approval through standard MCP tool metadata —
either the tool's `_meta` or a custom annotation. The bundled **`bash_server.py`**
does both:

```python
mcp.add_tool(
    run_bash, name="run_bash",
    description="Execute a bash command on the host and return its output.",
    annotations=ToolAnnotations(destructiveHint=True, requiresPermission=True),
    meta={"requiresPermission": True},
)
```

This client reads `requiresPermission` from either place. Because the flag
travels **with the tool**, the server author's intent is respected by any client
that honours it — the protection ships with the server, not the config.

### What the prompt looks like

When a gated tool is about to run:

```
mcp> run /bin/ls in the current directory

  🔒 permission required — run_bash (bash)
     args: {"command": "ls -la"}
     allow? [y]es once / [n]o / [a]lways this session:
```

- **`y`** — allow this one call.
- **`n`** (or just Enter — the safe default) — deny it; in chat mode the model is
  told the call was denied and can react.
- **`a`** — allow it **and** stop asking for this tool for the rest of the
  session.

Gated tools are flagged with `🔒` in `/tools`.

### The bundled bash server

`bash_server.py` is a minimal, ready-to-use example: an MCP server exposing a
single `run_bash` tool, pre-declared as permission-required. `servers.json`
already wires it up, so out of the box:

```
mcp> /tools
  run_bash (bash) 🔒
      Execute a bash command on the host and return its output.
mcp> /call run_bash {"command": "uname -a"}
  🔒 permission required — run_bash (bash)
     allow? [y]es once / [n]o / [a]lways this session: y
  [ok]
  [exit 0]
  Linux ...
```

### Headless / subagent mode

A [subagent](#13-subagents-spawning-and-controlling-instances) (`--serve`) has no
human attached, so it **cannot** prompt. A permission-gated tool called inside a
subagent is **denied by default**, and the caller is told why. The subagent
loads the same `requirePermission` rules its config declares, so it gates the
same tools the parent would — it just can't approve them. Give a subagent only
servers/tools it's allowed to run unattended.

---

# INSTALLING OLLAMA
- `curl -fsSL https://ollama.com/install.sh | sh`
- `systemctl start ollama`
- `ollama pull llama3.1`

# TESTING PROMPT
Can you create a subagent. Its config file is at /home/debian/IA/MCP_CLIENT_SEB/web.json
