"""System prompt assembly.

Mirrors `src/agents/system-prompt.ts:189+` and
`src/agents/pi-embedded-runner/system-prompt.ts:12-95`. Production
OpenClaw stitches together ~10 fragment sources (identity, bootstrap,
workspace context, skills, memory, heartbeat, tool catalog, channel
hints, runtime info, provider quirks). nano keeps just three: identity,
runtime info, tool catalog — enough to teach the *shape* of dynamic
prompt assembly without drowning the reader.
"""

from __future__ import annotations

import os
import platform
from datetime import date

from nano_openclaw.tools import ToolRegistry


_IDENTITY = (
    "You are nanoOpenclaw, a small coding assistant running in a terminal. "
    "Be concise and precise. Use tools when they help; otherwise answer directly."
)


def build_system_prompt(registry: ToolRegistry) -> str:
    runtime_lines = [
        f"- cwd: {os.getcwd()}",
        f"- platform: {platform.system()} ({platform.release()})",
        f"- date: {date.today().isoformat()}",
    ]

    tools = registry.schemas()
    if tools:
        tool_lines = [f"- {t['name']}: {t['description']}" for t in tools]
        tools_block = "Tools available:\n" + "\n".join(tool_lines)
    else:
        tools_block = "No tools available; answer directly from text only."

    return (
        f"{_IDENTITY}\n\n"
        "Runtime:\n" + "\n".join(runtime_lines) + "\n\n"
        + tools_block + "\n\n"
        "When the task is done, stop. Never invent file paths."
    )
