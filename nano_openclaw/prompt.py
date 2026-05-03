"""System prompt assembly.

Mirrors `src/agents/system-prompt.ts:189+` and
`src/agents/pi-embedded-runner/system-prompt.ts:12-95`. Production
OpenClaw stitches together ~10 fragment sources (identity, bootstrap,
workspace context, skills, memory, heartbeat, tool catalog, channel
hints, runtime info, provider quirks). nano keeps just five: identity,
runtime info, project context (AGENTS.md/SOUL.md/etc.), skills catalog,
tool catalog — enough to teach the *shape* of dynamic prompt assembly
without drowning the reader.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING

from nano_openclaw.tools import ToolRegistry
from nano_openclaw.workspace import (
    WorkspaceBootstrapFile,
    CONTEXT_FILE_ORDER,
    DEFAULT_SOUL_FILENAME,
)

if TYPE_CHECKING:
    from nano_openclaw.skills.types import Skill


_IDENTITY = "You are a personal assistant running inside nano-openclaw."

_EXECUTION_BIAS = """\
## Execution Bias
- Be concise and precise. Use tools when they help; otherwise answer directly.
- Actionable request: act in this turn.
- Non-final turn: use tools to advance, or ask for the one missing decision that blocks safe progress.
- Continue until done or genuinely blocked; do not finish with a plan/promise when tools can move it forward.
- Weak/empty tool result: vary query, path, command, or source before concluding.
- Mutable facts need live checks: files, git, clocks, versions, services, processes, package state.
- Final answer needs evidence: test/build/lint, tool output, or a named blocker.\
"""

_SAFETY = """\
## Safety
You have no independent goals: do not pursue self-preservation, replication, resource acquisition, or power-seeking; avoid long-term plans beyond the user's request.
Prioritize safety and human oversight over completion; if instructions conflict, pause and ask; comply with stop/pause/audit requests and never bypass safeguards.
Do not manipulate or persuade anyone to expand access or disable safeguards.\
"""

_SKILLS_SECTION_PREFIX = """\
## Skills (mandatory)
Before replying: scan <available_skills> <description> entries.
- If exactly one skill clearly applies: invoke the Skill tool with its <location>, then follow it.
- If multiple could apply: choose the most specific one, then invoke/follow it.
- If none clearly apply: do not load any skill.
Constraints: never load more than one skill up front; only load after selecting.
- When a skill drives external API writes, assume rate limits: prefer fewer larger writes, avoid tight one-item loops, serialize bursts when possible, and respect 429/Retry-After.\
"""


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
    skills: list["Skill"] | None = None,
    max_skills_in_prompt: int = 150,
    max_skills_prompt_chars: int = 18_000,
) -> str:
    """Build the complete system prompt for the agent.

    Args:
        registry: Tool registry for dynamic tool catalog
        workspace_dir: Path to workspace directory (for loading bootstrap files)
        bootstrap_files: Pre-loaded bootstrap files (overrides workspace_dir)
        skills: Pre-loaded and filtered skills for prompt injection
        max_skills_in_prompt: Max number of skills to include
        max_skills_prompt_chars: Max characters for the skills section

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

    # Skills section (mirrors openclaw applySkillsPromptLimits + formatSkillsForPrompt)
    skills_block = ""
    if skills:
        from nano_openclaw.skills import (
            apply_skills_prompt_limits,
            format_skills_compact,
            format_skills_for_prompt,
        )
        limited, _, use_compact = apply_skills_prompt_limits(
            skills,
            max_skills=max_skills_in_prompt,
            max_chars=max_skills_prompt_chars,
        )
        if limited:
            skills_block = format_skills_compact(limited) if use_compact else format_skills_for_prompt(limited)

    # Daily memory prelude (mirrors openclaw startup-context.ts)
    daily_memory = ""
    if workspace_dir:
        from nano_openclaw.memory.daily import build_daily_memory_prelude
        daily_memory = build_daily_memory_prelude(workspace_dir) or ""

    prompt = (
        f"{_IDENTITY}\n\n"
        "Runtime:\n" + "\n".join(runtime_lines) + "\n\n"
    )

    if project_context:
        prompt += project_context + "\n"

    prompt += tools_block + "\n"

    # Memory tool guidance (after tools section)
    prompt += _MEMORY_TOOL_GUIDANCE + "\n"

    prompt += _EXECUTION_BIAS + "\n\n"
    prompt += _SAFETY + "\n\n"

    if skills_block:
        prompt += _SKILLS_SECTION_PREFIX + "\n" + skills_block + "\n"

    prompt += "\nWhen the task is done, stop. Never invent file paths."

    # Prepend daily memory prelude if available
    if daily_memory:
        prompt = f"{daily_memory}\n\n{prompt}"

    return prompt


_MEMORY_TOOL_GUIDANCE = """
## Memory Recall
Before answering anything about prior work, decisions, dates, people, preferences, or todos:
run memory_search on MEMORY.md + memory/*.md; then use memory_get to pull needed lines.
If low confidence after search, say you checked.
"""
