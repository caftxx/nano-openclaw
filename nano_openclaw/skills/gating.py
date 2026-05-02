"""Skill gating - check eligibility conditions.

Mirrors openclaw's skills config.ts shouldIncludeSkill.

Gating conditions:
- always: skip all gating
- config enabled/disabled
- bundled skills allowlist
- os: platform filter
- requires.bins: required binaries on PATH
- requires.anyBins: at least one binary on PATH
- requires.env: required environment variables
- requires.config: required config paths truthy
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import TYPE_CHECKING, Any

from nano_openclaw.skills.constants import OS_MAP

if TYPE_CHECKING:
    from nano_openclaw.skills.types import Skill, SkillEntry


def get_current_os() -> str:
    """Get normalized OS name (darwin/linux/win32)."""
    system = platform.system()
    return OS_MAP.get(system, system.lower())


def check_bin_exists(bin_name: str) -> bool:
    """Check if binary exists on PATH."""
    return shutil.which(bin_name) is not None


def check_env_exists(env_name: str) -> bool:
    """Check if environment variable is set."""
    return os.getenv(env_name) is not None


def check_config_path_truthy(config: Any, path: str) -> bool:
    """Check if config path is truthy.

    Path is dot-separated, e.g., "agents.defaults.model"
    """
    if config is None:
        return False

    parts = path.split(".")
    current = config

    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return False

        if current is None:
            return False

    # Truthy check
    if isinstance(current, bool):
        return current
    if isinstance(current, (int, float)):
        return current > 0
    if isinstance(current, str):
        return bool(current.strip())
    if isinstance(current, (list, dict)):
        return bool(current)

    return bool(current)


def check_skill_eligibility(
    entry: "SkillEntry",
    config: Any = None,
    skill_filter: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Check if a skill entry is eligible for use.

    Mirrors openclaw shouldIncludeSkill.

    Returns:
        (eligible, reason) - whether eligible, and why not if ineligible
    """
    skill = entry.skill
    metadata = entry.metadata

    # 1. Skill filter check
    if skill_filter is not None:
        if skill.name not in skill_filter:
            return False, "not in skill filter"

    # 2. always flag skips all gating
    if metadata and metadata.always:
        return True, None

    # 3. Config enabled check
    if config is not None:
        # Check skills.entries.{name}.enabled
        entries = getattr(config, "skills", None)
        if entries is not None:
            skill_entries = getattr(entries, "entries", None)
            if skill_entries is not None:
                skill_config = skill_entries.get(skill.name) or skill_entries.get(metadata.skillKey if metadata else None)
                if skill_config is not None:
                    enabled = getattr(skill_config, "enabled", True)
                    if enabled is False:
                        return False, "disabled in config"

    # 4. Bundled skills allowlist
    if skill.source == "bundled" and config is not None:
        skills_config = getattr(config, "skills", None)
        if skills_config is not None:
            allow_bundled = getattr(skills_config, "allowBundled", None)
            if allow_bundled is not None and len(allow_bundled) > 0:
                if skill.name not in allow_bundled:
                    return False, "not in allowBundled list"

    # 5. OS check
    if metadata and metadata.os:
        current_os = get_current_os()
        if current_os not in metadata.os:
            return False, f"OS mismatch: requires {metadata.os}, current {current_os}"

    # 6. requires.bins check (all must exist)
    if metadata and metadata.requires:
        if metadata.requires.bins:
            for bin_name in metadata.requires.bins:
                if not check_bin_exists(bin_name):
                    return False, f"missing required binary: {bin_name}"

        # 7. requires.anyBins check (at least one must exist)
        if metadata.requires.anyBins:
            found_any = any(check_bin_exists(b) for b in metadata.requires.anyBins)
            if not found_any:
                return False, f"missing any of required binaries: {metadata.requires.anyBins}"

        # 8. requires.env check
        if metadata.requires.env:
            for env_name in metadata.requires.env:
                # Check env or config apiKey/env override
                has_env = check_env_exists(env_name)
                has_config_env = False

                if config is not None:
                    skills_config = getattr(config, "skills", None)
                    if skills_config is not None:
                        skill_entries = getattr(skills_config, "entries", None)
                        if skill_entries is not None:
                            skill_config = skill_entries.get(skill.name)
                            if skill_config is not None:
                                env_overrides = getattr(skill_config, "env", None)
                                if env_overrides is not None and env_name in env_overrides:
                                    has_config_env = True
                                api_key = getattr(skill_config, "apiKey", None)
                                if api_key and metadata.primaryEnv == env_name:
                                    has_config_env = True

                if not has_env and not has_config_env:
                    return False, f"missing required env var: {env_name}"

        # 9. requires.config check
        if metadata.requires.config:
            for config_path in metadata.requires.config:
                # Convert config object to dict for path traversal
                config_dict = config if isinstance(config, dict) else vars(config) if hasattr(config, "__dict__") else None
                if not check_config_path_truthy(config_dict, config_path):
                    return False, f"config path not truthy: {config_path}"

    return True, None


def filter_eligible_skills(
    entries: list["SkillEntry"],
    config: Any = None,
    skill_filter: list[str] | None = None,
) -> list["SkillEntry"]:
    """Filter skills by eligibility.

    Updates each entry's eligible and eligibilityReason fields.
    """
    result: list["SkillEntry"] = []

    for entry in entries:
        eligible, reason = check_skill_eligibility(entry, config, skill_filter)
        entry.eligible = eligible
        entry.eligibilityReason = reason

        if eligible:
            result.append(entry)

    return result


def filter_visible_skills(entries: list["SkillEntry"]) -> list["Skill"]:
    """Filter skills visible in prompt (exclude disableModelInvocation).

    Mirrors openclaw filterWorkspaceSkillEntriesWithOptions.
    """
    result: list["Skill"] = []

    for entry in entries:
        if not entry.eligible:
            continue

        # Check exposure
        if entry.exposure:
            if entry.exposure.includeInAvailableSkillsPrompt is False:
                continue
        elif entry.invocation:
            if entry.invocation.disableModelInvocation is True:
                continue

        result.append(entry.skill)

    return result