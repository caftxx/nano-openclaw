"""Skill prompt formatter - XML format for system prompt.

Mirrors openclaw's skill-contract.ts formatSkillsForPrompt.

Output format:
<available_skills>
  <skill>
    <name>skill-name</name>
    <description>One-line description</description>
    <location>/path/to/SKILL.md</location>
  </skill>
</available_skills>

Also supports compact format (name + location only) when budget exceeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nano_openclaw.skills.types import Skill


def escape_xml(text: str) -> str:
    """Escape XML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_skills_for_prompt(skills: list["Skill"]) -> str:
    """Format skills as XML for system prompt injection.

    Mirrors openclaw formatSkillsForPrompt from skill-contract.ts.

    Returns empty string if no skills.
    """
    if not skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]

    for skill in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape_xml(skill.name)}</name>")
        lines.append(f"    <description>{escape_xml(skill.description)}</description>")
        lines.append(f"    <location>{escape_xml(skill.filePath)}</location>")
        lines.append("  </skill>")

    lines.append("</available_skills>")

    return "\n".join(lines)


def format_skills_compact(skills: list["Skill"]) -> str:
    """Format skills compactly (name + location only, no description).

    Mirrors openclaw formatSkillsCompact from workspace.ts.

    Used when full format exceeds character budget.
    """
    if not skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its name.",
        "When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]

    for skill in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape_xml(skill.name)}</name>")
        lines.append(f"    <location>{escape_xml(skill.filePath)}</location>")
        lines.append("  </skill>")

    lines.append("</available_skills>")

    return "\n".join(lines)


def apply_skills_prompt_limits(
    skills: list["Skill"],
    max_skills: int = 150,
    max_chars: int = 18_000,
) -> tuple[list["Skill"], bool, bool]:
    """Apply limits to skills for prompt injection.

    Mirrors openclaw applySkillsPromptLimits from workspace.ts.

    Returns:
        (skills_for_prompt, truncated, compact)
        - skills_for_prompt: filtered skills list
        - truncated: whether skills were dropped
        - compact: whether using compact format
    """
    if not skills:
        return [], False, False

    # Apply count limit first
    by_count = skills[:max_skills]
    truncated = len(skills) > len(by_count)

    # Check if full format fits
    full_format = format_skills_for_prompt(by_count)
    if len(full_format) <= max_chars:
        return by_count, truncated, False

    # Try compact format
    compact_format = format_skills_compact(by_count)
    if len(compact_format) <= max_chars:
        return by_count, truncated, True

    # Compact still too large — binary search largest prefix
    compact = True
    lo = 0
    hi = len(by_count)

    while lo < hi:
        mid = (lo + hi + 1) // 2
        test_format = format_skills_compact(by_count[:mid])
        if len(test_format) <= max_chars:
            lo = mid
        else:
            hi = mid - 1

    result = by_count[:lo]
    return result, truncated or len(result) < len(by_count), compact