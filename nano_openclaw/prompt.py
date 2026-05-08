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

_MEMORY_TOOL_GUIDANCE = """
## Memory Recall
Before answering anything about prior work, decisions, dates, people, preferences, or todos:
run memory_search on MEMORY.md + memory/*.md; then use memory_get to pull needed lines.
If low confidence after search, say you checked.
"""

def _build_subagent_section(registry: ToolRegistry) -> str:
    """Dynamically build the sub-agent orchestration section.

    Mirrors openclaw system-prompt.ts buildMessagingSection() and the Tooling
    paragraph that says 'If a task is more complex or takes longer, spawn a
    sub-agent. Completion is push-based: it will auto-announce when done.'

    Only injected when sessions_spawn and/or subagents tools are present.
    """
    has_spawn = registry.get("sessions_spawn") is not None
    has_manage = registry.get("subagents") is not None

    if not has_spawn and not has_manage:
        return ""

    lines = ["## Sub-Agent Orchestration"]

    if has_spawn and has_manage:
        lines.append(
            "- Sub-agent orchestration → use `sessions_spawn(...)` to delegate complex, "
            "slow, or parallelizable work to a fresh isolated child session; "
            "use `subagents` only for on-demand status checks, debugging, or to kill a run."
        )
    elif has_spawn:
        lines.append(
            "- Use `sessions_spawn(...)` to delegate complex, slow, or parallelizable work "
            "to a fresh isolated child session."
        )
    else:
        lines.append("- Use `subagents` for on-demand status checks, debugging, or to kill a run.")

    lines += [
        "- **When to spawn**: task is complex, slow, or can run in parallel with the current session.",
        "- **Completion is push-based**: results arrive as a user message automatically — "
        "do NOT poll `subagents` in a loop; only check on-demand.",
        "- **Default context is isolated** (fresh transcript). "
        "Sub-agents inherit the workspace directory.",
        "- After spawning, continue other work or wait; the completion message will arrive in "
        "the next user turn.",
    ]

    return "\n".join(lines)


def _build_schedule_section(registry: ToolRegistry) -> str:
    """Inject cron/schedule guidance when schedule tools are registered.

    Mirrors openclaw system-prompt.ts coreToolSummaries["cron"] guidance.
    Only injected when at least one of cron_create / schedule_wakeup is present.
    """
    has_create = registry.get("cron_create") is not None
    has_wakeup = registry.get("schedule_wakeup") is not None
    has_list = registry.get("cron_list") is not None
    has_delete = registry.get("cron_delete") is not None

    if not has_create and not has_wakeup:
        return ""

    lines = ["## Cron Schedule"]

    if has_create:
        lines.append(
            "- `cron_create` schedules a recurring background task on a cron expression "
            "(e.g. `0 9 * * *` for daily 9 AM)."
        )
    if has_wakeup:
        lines.append(
            "- `schedule_wakeup` schedules a one-shot task to run after a delay (minimum 60 s)."
        )

    mgmt = []
    if has_list:
        mgmt.append("`cron_list` to view jobs and their next run time")
    if has_delete:
        mgmt.append("`cron_delete` to remove a job")
    if mgmt:
        lines.append(f"- Use {' and '.join(mgmt)}.")

    lines += [
        "- **Writing reminder prompts**: phrase the prompt so it reads naturally when it fires "
        "(e.g. 'Remind me to review the PR' → prompt: 'This is your reminder to review the PR. "
        "Check its current status and summarise what still needs attention.'). "
        "For longer gaps (hours/days), mention it is a reminder. "
        "Include relevant context from the current conversation in the prompt text.",
        "- Scheduled tasks run as isolated background agents with access to workspace tools.",
    ]

    return "\n".join(lines)


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

    prompt = (
        f"{_IDENTITY}\n\n"
        "Runtime:\n" + "\n".join(runtime_lines) + "\n\n"
    )

    if project_context:
        prompt += project_context + "\n"

    prompt += tools_block + "\n"

    subagent_section = _build_subagent_section(registry)
    if subagent_section:
        prompt += "\n" + subagent_section + "\n"

    schedule_section = _build_schedule_section(registry)
    if schedule_section:
        prompt += "\n" + schedule_section + "\n"

    # Memory tool guidance (after tools section)
    if registry.get("memory_search") is not None and registry.get("memory_get") is not None:
        prompt += _MEMORY_TOOL_GUIDANCE + "\n"

    prompt += _EXECUTION_BIAS + "\n\n"
    prompt += _SAFETY + "\n\n"

    if skills_block:
        prompt += _SKILLS_SECTION_PREFIX + "\n" + skills_block + "\n"

    prompt += "\nWhen the task is done, stop. Never invent file paths."

    return prompt
