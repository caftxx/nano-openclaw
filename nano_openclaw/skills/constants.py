"""Skill path constants and load order.

Mirrors openclaw's skills config from:
- src/agents/skills/config.ts (resolveBundledSkillsDir, skill roots)
- src/agents/skills/workspace.ts:544-598 (load order precedence)

Load order (highest precedence first):
1. workspace skills        - {workspace}/skills/
2. project agent skills    - {workspace}/.agents/skills/
3. personal agent skills   - ~/.agents/skills/
4. managed skills          - ~/.openclaw/skills/
5. bundled skills          - package bundled_skills/
6. extra dirs              - config skills.load.extraDirs

When skill names conflict, higher precedence wins.
"""

from __future__ import annotations

from pathlib import Path

# Skill file name
SKILL_FILE_NAME = "SKILL.md"

# Load order priority (lower number = higher precedence)
SKILL_LOAD_ORDER: dict[str, int] = {
    "workspace": 10,        # {workspace}/skills/
    "agents-project": 20,   # {workspace}/.agents/skills/
    "agents-personal": 30,  # ~/.agents/skills/
    "managed": 40,          # ~/.openclaw/skills/
    "bundled": 50,          # package bundled_skills/
    "extra": 60,            # config skills.load.extraDirs
}

# Source names in load order (lowest precedence first for iteration)
SKILL_SOURCE_ORDER: list[str] = [
    "extra",
    "bundled",
    "managed",
    "agents-personal",
    "agents-project",
    "workspace",
]

# Default limits (mirrors openclaw workspace.ts:124-129)
DEFAULT_MAX_CANDIDATES_PER_ROOT = 300
DEFAULT_MAX_SKILLS_LOADED_PER_SOURCE = 200
DEFAULT_MAX_SKILLS_IN_PROMPT = 150
DEFAULT_MAX_SKILLS_PROMPT_CHARS = 18_000
DEFAULT_MAX_SKILL_FILE_BYTES = 256_000

# OS mapping (openclaw uses "darwin", "linux", "win32")
OS_MAP: dict[str, str] = {
    "Darwin": "darwin",
    "Linux": "linux",
    "Windows": "win32",
}


def resolve_bundled_skills_dir() -> Path | None:
    """Resolve bundled skills directory within the package.

    Mirrors openclaw resolveBundledSkillsDir.
    """
    try:
        # Package directory containing this module
        package_dir = Path(__file__).parent.parent
        bundled_dir = package_dir / "bundled_skills"
        if bundled_dir.is_dir():
            return bundled_dir
    except Exception:
        pass
    return None


def resolve_managed_skills_dir() -> Path:
    """Resolve managed skills directory (~/.openclaw/skills).

    Mirrors openclaw managedSkillsDir.
    """
    return Path.home() / ".openclaw" / "skills"


def resolve_personal_agent_skills_dir() -> Path:
    """Resolve personal agent skills directory (~/.agents/skills).

    Mirrors openclaw personalAgentsSkillsDir.
    """
    return Path.home() / ".agents" / "skills"


def resolve_project_agent_skills_dir(workspace_dir: Path) -> Path:
    """Resolve project agent skills directory ({workspace}/.agents/skills).

    Mirrors openclaw projectAgentsSkillsDir.
    """
    return workspace_dir / ".agents" / "skills"


def resolve_workspace_skills_dir(workspace_dir: Path) -> Path:
    """Resolve workspace skills directory ({workspace}/skills).

    Mirrors openclaw workspaceSkillsDir.
    """
    return workspace_dir / "skills"