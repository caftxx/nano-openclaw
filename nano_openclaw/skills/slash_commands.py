"""Slash commands - parse and handle /skill-name invocations.

Mirrors openclaw's tools/slash-commands.md behavior.

When user types "/skill-name args", the agent:
1. Detects the slash command
2. Loads the skill's SKILL.md content
3. Injects skill content + args into the message context
4. The model follows skill instructions for the task

Skills must have user-invocable: true to be slash command eligible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nano_openclaw.skills.types import Skill, SkillEntry

logger = logging.getLogger(__name__)


@dataclass
class SlashCommand:
    """Parsed slash command from user input."""
    name: str                    # Skill name
    skill: "Skill"               # Corresponding skill
    args: str                    # User-provided arguments


def parse_slash_command(
    user_input: str,
    skill_registry: dict[str, "Skill"],
) -> tuple[SlashCommand | None, str]:
    """Parse user input to check for slash command.

    Args:
        user_input: Raw user input string
        skill_registry: Dict mapping skill name -> Skill

    Returns:
        (command, remaining_text)
        - If slash command found: command object + args text
        - If not a slash command: None + original input
    """
    if not user_input.startswith("/"):
        return None, user_input

    # Extract command name and args
    # Format: /skill-name [args...]
    parts = user_input[1:].split(maxsplit=1)

    if not parts:
        return None, user_input

    cmd_name = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    # Handle special built-in commands that aren't skills
    # (e.g., /quit, /clear, /help, /new - these are handled by CLI)
    builtin_commands = {"quit", "clear", "help", "new", "context", "compact", "sessions", "save"}
    if cmd_name in builtin_commands:
        return None, user_input

    # Look up skill by name
    skill = skill_registry.get(cmd_name)
    if skill is None:
        # Not a known skill - return original input
        logger.debug("Unknown slash command: /%s", cmd_name)
        return None, user_input

    return SlashCommand(name=cmd_name, skill=skill, args=args), args


def build_slash_command_context(command: SlashCommand) -> str:
    """Build context message for slash command invocation.

    This message is injected into the conversation, informing the model
    that a skill was invoked and providing the skill's instructions.
    """
    lines = [
        f"[Skill invoked: {command.name}]",
        "",
        f"Skill file location: {command.skill.filePath}",
        "",
        "Skill instructions:",
    ]

    # Include skill content if available
    if command.skill.content:
        lines.append(command.skill.content)
    else:
        lines.append("(Skill content not loaded)")

    lines.append("")

    # Include user arguments
    if command.args:
        lines.append(f"User arguments: {command.args}")
    else:
        lines.append("No user arguments provided.")

    return "\n".join(lines)


def build_skill_registry_from_entries(
    entries: list["SkillEntry"],
    user_invocable_only: bool = True,
) -> dict[str, "Skill"]:
    """Build skill registry dict from entries.

    Args:
        entries: List of skill entries
        user_invocable_only: Only include skills with userInvocable=True

    Returns:
        Dict mapping skill name -> Skill
    """
    registry: dict[str, "Skill"] = {}

    for entry in entries:
        if not entry.eligible:
            continue

        # Check user_invocable
        if user_invocable_only:
            if entry.exposure and entry.exposure.userInvocable is False:
                continue
            elif entry.invocation and entry.invocation.userInvocable is False:
                continue

        registry[entry.skill.name] = entry.skill

    return registry


def is_skill_user_invocable(skill: "Skill", entry: "SkillEntry | None" = None) -> bool:
    """Check if a skill can be invoked via slash command."""
    if entry:
        if entry.exposure:
            return entry.exposure.userInvocable is not False
        if entry.invocation:
            return entry.invocation.userInvocable is not False
    return True  # Default to invocable