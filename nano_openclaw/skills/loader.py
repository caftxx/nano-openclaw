"""Skill loader - load SKILL.md files from multiple sources.

Mirrors openclaw's skills workspace.ts:
- loadSkillsFromDirSafe (scan directories for SKILL.md)
- loadSkillEntries (multi-source loading with precedence)
- readSkillFrontmatterSafe (parse YAML frontmatter)

Key behaviors:
- Skill roots must be real directories
- SKILL.md files must stay inside configured root (path escape check)
- Per-file size limit: DEFAULT_MAX_SKILL_FILE_BYTES
- Name conflicts resolved by precedence (higher wins)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from nano_openclaw.skills.constants import (
    DEFAULT_MAX_CANDIDATES_PER_ROOT,
    DEFAULT_MAX_SKILL_FILE_BYTES,
    DEFAULT_MAX_SKILLS_LOADED_PER_SOURCE,
    SKILL_FILE_NAME,
    SKILL_SOURCE_ORDER,
    resolve_bundled_skills_dir,
    resolve_managed_skills_dir,
    resolve_personal_agent_skills_dir,
    resolve_project_agent_skills_dir,
    resolve_workspace_skills_dir,
)
from nano_openclaw.skills.types import (
    Skill,
    SkillEntry,
    SkillExposure,
    SkillInvocationPolicy,
    SkillMetadata,
)

# Frontmatter pattern: YAML between --- lines
FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL
)


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Parse YAML frontmatter from SKILL.md content.

    Mirrors openclaw parseFrontmatterBlock.
    Returns empty dict if no frontmatter found or parse fails.
    """
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}

    frontmatter_text = match.group(1)
    try:
        result = yaml.safe_load(frontmatter_text)
        if isinstance(result, dict):
            return result
    except yaml.YAMLError:
        pass
    return {}


def extract_content_after_frontmatter(content: str) -> str:
    """Extract markdown body after frontmatter."""
    match = FRONTMATTER_PATTERN.match(content)
    if match:
        return content[match.end():]
    return content


def parse_metadata_json(frontmatter: dict[str, Any]) -> dict[str, Any] | None:
    """Parse metadata.openclaw JSON from frontmatter.

    Mirrors openclaw resolveOpenClawManifestBlock.
    Handles both dict (PyYAML inline mapping) and str (JSON string) values.
    """
    metadata_raw = frontmatter.get("metadata")
    if not metadata_raw:
        return None

    if isinstance(metadata_raw, dict):
        return metadata_raw.get("openclaw", metadata_raw)

    try:
        metadata_obj = json.loads(str(metadata_raw))
        if isinstance(metadata_obj, dict):
            return metadata_obj.get("openclaw", metadata_obj)
    except json.JSONDecodeError:
        pass

    return None


def resolve_skill_metadata(frontmatter: dict[str, Any]) -> SkillMetadata | None:
    """Resolve SkillMetadata from frontmatter.

    Mirrors openclaw resolveOpenClawMetadata.
    """
    metadata_obj = parse_metadata_json(frontmatter)
    if not metadata_obj:
        return None

    requires_obj = metadata_obj.get("requires", {})
    requires = None
    if requires_obj:
        from nano_openclaw.skills.types import SkillRequires
        requires = SkillRequires(
            bins=requires_obj.get("bins"),
            anyBins=requires_obj.get("anyBins"),
            env=requires_obj.get("env"),
            config=requires_obj.get("config"),
        )

    install_list = metadata_obj.get("install", [])
    install = None
    if install_list and isinstance(install_list, list):
        from nano_openclaw.skills.types import SkillInstallSpec
        install = [
            SkillInstallSpec(
                id=item.get("id"),
                kind=item.get("kind", "brew"),
                label=item.get("label"),
                bins=item.get("bins"),
                os=item.get("os"),
                formula=item.get("formula"),
                package=item.get("package"),
                module=item.get("module"),
                url=item.get("url"),
                archive=item.get("archive"),
                extract=item.get("extract"),
                stripComponents=item.get("stripComponents"),
                targetDir=item.get("targetDir"),
            )
            for item in install_list
            if isinstance(item, dict)
        ]

    return SkillMetadata(
        always=bool(metadata_obj.get("always", False)),
        skillKey=metadata_obj.get("skillKey"),
        primaryEnv=metadata_obj.get("primaryEnv"),
        emoji=metadata_obj.get("emoji"),
        homepage=metadata_obj.get("homepage"),
        os=metadata_obj.get("os"),
        requires=requires,
        install=install,
    )


def resolve_invocation_policy(frontmatter: dict[str, Any]) -> SkillInvocationPolicy:
    """Resolve invocation policy from frontmatter.

    Mirrors openclaw resolveSkillInvocationPolicy.
    Handles native bool from PyYAML and legacy string values.
    """
    user_invocable_raw = frontmatter.get("user-invocable", True)
    disable_model_raw = frontmatter.get("disable-model-invocation", False)

    if isinstance(user_invocable_raw, bool):
        user_invocable = user_invocable_raw
    else:
        user_invocable = str(user_invocable_raw).lower() == "true"

    if isinstance(disable_model_raw, bool):
        disable_model = disable_model_raw
    else:
        disable_model = str(disable_model_raw).lower() == "true"

    return SkillInvocationPolicy(
        userInvocable=user_invocable,
        disableModelInvocation=disable_model,
    )


def is_path_inside(parent: Path, child: Path) -> bool:
    """Check if child path is inside parent (path escape check).

    Mirrors openclaw isPathInside.
    """
    try:
        parent_resolved = parent.resolve()
        child_resolved = child.resolve()
        return child_resolved.is_relative_to(parent_resolved)
    except (ValueError, OSError):
        return False


def load_skill_from_file(
    skill_file: Path,
    base_dir: Path,
    source: str,
    max_bytes: int = DEFAULT_MAX_SKILL_FILE_BYTES,
) -> Skill | None:
    """Load one SKILL.md file.

    Returns None if:
    - File doesn't exist
    - File escapes base_dir
    - File exceeds max_bytes
    - Missing required name/description in frontmatter
    """
    if not skill_file.exists():
        return None

    # Path escape check
    if not is_path_inside(base_dir, skill_file):
        return None

    # Size check
    try:
        stat = skill_file.stat()
        if stat.st_size > max_bytes:
            return None
    except OSError:
        return None

    # Read content
    try:
        content = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    # Parse frontmatter
    frontmatter = parse_frontmatter(content)
    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not name or not description:
        return None

    # Extract markdown body
    body = extract_content_after_frontmatter(content)

    return Skill(
        name=name,
        description=description,
        filePath=str(skill_file),
        baseDir=str(base_dir),
        source=source,
        content=body,
    )


def load_skills_from_dir(
    skill_dir: Path,
    source: str,
    max_bytes: int = DEFAULT_MAX_SKILL_FILE_BYTES,
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_ROOT,
    max_loaded: int = DEFAULT_MAX_SKILLS_LOADED_PER_SOURCE,
) -> list[Skill]:
    """Load all skills from a directory.

    Mirrors openclaw loadSkillsFromDirSafe.

    Skills are expected in:
    - {dir}/SKILL.md (single skill in root)
    - {dir}/{name}/SKILL.md (multiple skills in subdirs)

    Only immediate subdirs are scanned.
    """
    if not skill_dir.is_dir():
        return []

    skills: list[Skill] = []

    # Check if root itself is a skill
    root_skill_file = skill_dir / SKILL_FILE_NAME
    if root_skill_file.exists():
        skill = load_skill_from_file(root_skill_file, skill_dir, source, max_bytes)
        if skill:
            skills.append(skill)
            return skills  # Single skill in root

    # Scan immediate subdirs
    try:
        entries = sorted(
            [e for e in skill_dir.iterdir() if e.is_dir() and not e.name.startswith(".")],
            key=lambda e: e.name,
        )
    except OSError:
        return []

    if len(entries) > max_candidates:
        entries = entries[:max_candidates]

    for subdir in entries:
        skill_file = subdir / SKILL_FILE_NAME
        skill = load_skill_from_file(skill_file, skill_dir, source, max_bytes)
        if skill:
            skills.append(skill)

        if len(skills) >= max_loaded:
            break

    return skills


def load_skill_entries(
    workspace_dir: Path,
    extra_dirs: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_SKILL_FILE_BYTES,
) -> list[SkillEntry]:
    """Load all skill entries from all sources with precedence.

    Mirrors openclaw loadSkillEntries.

    Lower precedence sources are loaded first, then overwritten by higher.
    """
    entries_by_name: dict[str, SkillEntry] = {}

    # Resolve all skill directories in load order (lowest precedence first)
    skill_dirs: list[tuple[str, Path]] = []

    # Extra dirs (lowest)
    if extra_dirs:
        for extra_path in extra_dirs:
            p = Path(extra_path).expanduser()
            if p.is_dir():
                skill_dirs.append(("extra", p))

    # Bundled
    bundled_dir = resolve_bundled_skills_dir()
    if bundled_dir:
        skill_dirs.append(("bundled", bundled_dir))

    # Managed
    managed_dir = resolve_managed_skills_dir()
    if managed_dir.is_dir():
        skill_dirs.append(("managed", managed_dir))

    # Personal agent skills
    personal_dir = resolve_personal_agent_skills_dir()
    if personal_dir.is_dir():
        skill_dirs.append(("agents-personal", personal_dir))

    # Project agent skills
    project_dir = resolve_project_agent_skills_dir(workspace_dir)
    if project_dir.is_dir():
        skill_dirs.append(("agents-project", project_dir))

    # Workspace skills (highest)
    workspace_dir_skills = resolve_workspace_skills_dir(workspace_dir)
    if workspace_dir_skills.is_dir():
        skill_dirs.append(("workspace", workspace_dir_skills))

    # Load from each source (lower precedence first, later overwrites)
    for source, skill_dir in skill_dirs:
        skills = load_skills_from_dir(skill_dir, source, max_bytes)
        for skill in skills:
            # Re-read raw file to parse frontmatter (skill.content is the stripped body)
            try:
                raw = Path(skill.filePath).read_text(encoding="utf-8")
                frontmatter = parse_frontmatter(raw)
            except (OSError, UnicodeDecodeError):
                frontmatter = {}
            metadata = resolve_skill_metadata(frontmatter)
            invocation = resolve_invocation_policy(frontmatter)

            entries_by_name[skill.name] = SkillEntry(
                skill=skill,
                frontmatter=frontmatter,
                metadata=metadata,
                invocation=invocation,
                exposure=SkillExposure(
                    includeInRuntimeRegistry=True,
                    includeInAvailableSkillsPrompt=invocation.disableModelInvocation is not True,
                    userInvocable=invocation.userInvocable is not False,
                ),
                eligible=True,  # Will be checked by gating
            )

    # Sort by name
    return sorted(entries_by_name.values(), key=lambda e: e.skill.name)