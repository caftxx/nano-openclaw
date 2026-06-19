"""Skill prompt formatting helpers."""

from __future__ import annotations

from typing import Any


def escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_skills_for_prompt(skills: list[Any]) -> str:
    if not skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the Skill tool to load a skill when the task matches its description.",
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


def format_skills_compact(skills: list[Any]) -> str:
    if not skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the Skill tool to load a skill when the task matches its name.",
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
    skills: list[Any],
    max_skills: int = 150,
    max_chars: int = 18_000,
) -> tuple[list[Any], bool, bool]:
    if not skills:
        return [], False, False

    by_count = skills[:max_skills]
    truncated = len(skills) > len(by_count)

    full_format = format_skills_for_prompt(by_count)
    if len(full_format) <= max_chars:
        return by_count, truncated, False

    compact_format = format_skills_compact(by_count)
    if len(compact_format) <= max_chars:
        return by_count, truncated, True

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
