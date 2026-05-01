"""System prompt assembly.

Mirrors `src/agents/system-prompt.ts:189+` and
`src/agents/pi-embedded-runner/system-prompt.ts:12-95`. Production
OpenClaw stitches together ~10 fragment sources (identity, bootstrap,
workspace context, skills, memory, heartbeat, tool catalog, channel
hints, runtime info, provider quirks). nano keeps just four: identity,
runtime info, project context (AGENTS.md/SOUL.md/etc.), tool catalog —
enough to teach the *shape* of dynamic prompt assembly without drowning
the reader.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from nano_openclaw.tools import ToolRegistry
from nano_openclaw.workspace import (
    WorkspaceBootstrapFile,
    CONTEXT_FILE_ORDER,
    DEFAULT_SOUL_FILENAME,
)


_IDENTITY = (
    "You are nano-openclaw, a small coding assistant running in a terminal. "
    "Be concise and precise. Use tools when they help; otherwise answer directly."
)


def _build_project_context_section(
    files: list[WorkspaceBootstrapFile],
) -> str:
    """Build the '# Project Context' section from loaded bootstrap files.

    Mirrors openclaw system-prompt.ts:95-125 (buildProjectContextSection).

    If SOUL.md is present, includes a special instruction to embody its
    persona and tone.

    Files are sorted by CONTEXT_FILE_ORDER (agents.md → soul.md → ...).
    """
    if not files:
        return ""

    lines = ["# Project Context", ""]

    # Detect SOUL.md for special handling (only present files)
    has_soul = any(
        f.name == DEFAULT_SOUL_FILENAME and not f.missing
        for f in files
    )

    if has_soul:
        lines.append(
            "The following project context files have been loaded:\n"
            "If SOUL.md is present, embody its persona and tone. "
            "Avoid stiff, generic replies; follow its guidance unless "
            "higher-priority instructions override it."
        )
    else:
        lines.append("The following project context files have been loaded:")

    lines.append("")

    # Sort only present files by injection order
    ordered = sorted(
        [f for f in files if not f.missing],
        key=lambda f: CONTEXT_FILE_ORDER.get(f.name.lower(), 999),
    )

    for file in ordered:
        lines.append(f"## {file.name}")
        lines.append("")
        if file.content:
            lines.append(file.content)
        lines.append("")

    return "\n".join(lines)


def build_system_prompt(
    registry: ToolRegistry,
    workspace_dir: Path | None = None,
    bootstrap_files: list[WorkspaceBootstrapFile] | None = None,
) -> str:
    """Build the complete system prompt for the agent.

    Args:
        registry: Tool registry for dynamic tool catalog
        workspace_dir: Path to workspace directory (for loading bootstrap files)
        bootstrap_files: Pre-loaded bootstrap files (overrides workspace_dir)

    Returns:
        Complete system prompt string
    """
    runtime_lines = [
        f"- cwd: {os.getcwd()}",
        f"- workspace: {workspace_dir}" if workspace_dir else None,
        f"- platform: {platform.system()} ({platform.release()})",
    ]
    runtime_lines = [l for l in runtime_lines if l is not None]

    tools = registry.schemas()
    if tools:
        tool_lines = [f"- {t['name']}: {t['description']}" for t in tools]
        tools_block = "Tools available:\n" + "\n".join(tool_lines)
    else:
        tools_block = "No tools available; answer directly from text only."

    project_context = ""
    if bootstrap_files:
        project_context = _build_project_context_section(bootstrap_files)

    prompt = (
        f"{_IDENTITY}\n\n"
        "Runtime:\n" + "\n".join(runtime_lines) + "\n\n"
    )

    if project_context:
        prompt += project_context + "\n"

    prompt += (
        tools_block + "\n\n"
        "If you need the current date, time, or day of week, use session_status.\n"
        "When the task is done, stop. Never invent file paths."
    )

    return prompt
