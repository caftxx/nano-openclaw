"""Skills module - public interface.

Provides skill loading, formatting, gating, and slash command support
for nano-openclaw, mirroring openclaw's AgentSkills-compatible system.
"""

from __future__ import annotations

from nano_openclaw.skills.cache import (
    clear_skills_cache,
    get_or_load_skills,
    invalidate_skills_cache,
)
from nano_openclaw.skills.constants import (
    DEFAULT_MAX_SKILL_FILE_BYTES,
    DEFAULT_MAX_SKILLS_IN_PROMPT,
    DEFAULT_MAX_SKILLS_PROMPT_CHARS,
    SKILL_FILE_NAME,
    SKILL_SOURCE_ORDER,
    resolve_bundled_skills_dir,
    resolve_managed_skills_dir,
    resolve_personal_agent_skills_dir,
    resolve_project_agent_skills_dir,
    resolve_workspace_skills_dir,
)
from nano_openclaw.skills.formatter import (
    apply_skills_prompt_limits,
    escape_xml,
    format_skills_compact,
    format_skills_for_prompt,
)
from nano_openclaw.skills.gating import (
    check_skill_eligibility,
    filter_eligible_skills,
    filter_visible_skills,
)
from nano_openclaw.skills.loader import (
    load_skill_entries,
    load_skill_from_file,
    load_skills_from_dir,
    parse_frontmatter,
)
from nano_openclaw.skills.slash_commands import (
    SlashCommand,
    build_skill_registry_from_entries,
    build_slash_command_context,
    parse_slash_command,
)
from nano_openclaw.skills.types import (
    Skill,
    SkillEntry,
    SkillExposure,
    SkillInstallSpec,
    SkillInvocationPolicy,
    SkillMetadata,
    SkillRequires,
    SkillSnapshot,
)

__all__ = [
    # Types
    "Skill",
    "SkillEntry",
    "SkillExposure",
    "SkillInstallSpec",
    "SkillInvocationPolicy",
    "SkillMetadata",
    "SkillRequires",
    "SkillSnapshot",
    "SlashCommand",
    # Constants
    "DEFAULT_MAX_SKILL_FILE_BYTES",
    "DEFAULT_MAX_SKILLS_IN_PROMPT",
    "DEFAULT_MAX_SKILLS_PROMPT_CHARS",
    "SKILL_FILE_NAME",
    "SKILL_SOURCE_ORDER",
    "resolve_bundled_skills_dir",
    "resolve_managed_skills_dir",
    "resolve_personal_agent_skills_dir",
    "resolve_project_agent_skills_dir",
    "resolve_workspace_skills_dir",
    # Loader
    "load_skill_entries",
    "load_skill_from_file",
    "load_skills_from_dir",
    "parse_frontmatter",
    # Formatter
    "format_skills_for_prompt",
    "format_skills_compact",
    "apply_skills_prompt_limits",
    "escape_xml",
    # Gating
    "check_skill_eligibility",
    "filter_eligible_skills",
    "filter_visible_skills",
    # Slash commands
    "parse_slash_command",
    "build_slash_command_context",
    "build_skill_registry_from_entries",
    # Cache
    "get_or_load_skills",
    "invalidate_skills_cache",
    "clear_skills_cache",
]