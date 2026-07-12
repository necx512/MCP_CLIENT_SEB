"""Per-process conversation log.

Every agent process writes its full exchange with the LLM — the system prompt,
the tools offered, each user turn, each assistant turn (reasoning, text, tool
calls) and each tool result — to ``/tmp/<pid>.log``. Subagents are separate
processes, so each one gets its own file named by its PID.

Entries are separated by a horizontal rule and timestamped, so the file reads
top-to-bottom as a chronological transcript. Logging must never break the agent,
so every write is best-effort.
"""

from __future__ import annotations

import os
from datetime import datetime

_RULE = "─" * 100


class ConversationLog:
    """Append-only transcript of one agent's conversation with its LLM."""

    def __init__(self, path: str | None = None):
        self.path = path or f"/tmp/{os.getpid()}.log"
        self.entry("LOG START", f"pid {os.getpid()}")

    def entry(self, label: str, body: str = "") -> None:
        """Append one rule-separated, timestamped entry. Never raises."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        block = f"\n{_RULE}\n{stamp}  {label}\n"
        if body:
            block += body.rstrip("\n") + "\n"
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(block)
        except OSError:
            pass  # a logging failure must not take down the agent
