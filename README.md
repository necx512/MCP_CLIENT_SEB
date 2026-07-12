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

---

## 10. Configuration options

**Per-server fields** (all under each `mcpServers` entry):

| Field | Required | Meaning |
|---|---|---|
| `command` | ✅ | Executable to launch (e.g. `python3`, `npx`, `node`). |
| `args` | — | List of arguments. |
| `cwd` | — | Working directory for the server process. |
| `env` | — | Extra env vars (layered on top of your current environment). |

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
    ├── cli.py                      # REPL + backend selection
    ├── manager.py                  # MCPManager (provider-neutral)
    ├── config.py                   # config parsing
    └── backends/
        ├── __init__.py             # BACKENDS registry + build_agent()
        ├── base.py                 # BaseAgent: shared tool-use loop
        ├── anthropic_backend.py    # Claude backend
        └── ollama_backend.py       # Ollama backend
```

# INSTALLING OLLAMA
- `curl -fsSL https://ollama.com/install.sh | sh`
- `systemctl start ollama`
- `ollama pull llama3.1`
