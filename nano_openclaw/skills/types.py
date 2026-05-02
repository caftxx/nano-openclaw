"""Skill data types for nano-openclaw.

Mirrors openclaw's skills types from:
- src/agents/skills/types.ts (SkillSnapshot, SkillEntry, SkillInstallSpec)
- src/agents/skills/skill-contract.ts (Skill)
- src/agents/skills/frontmatter.ts (OpenClawSkillMetadata)

Key concepts:
- Skill: One loaded SKILL.md file with name, description, content
- SkillMetadata: openclaw-specific gating and invocation metadata
- SkillEntry: Skill + frontmatter + eligibility state
- SkillInstallSpec: Installer specs (brew/node/go/uv/download)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass
class SkillRequires:
    """Required conditions for a skill to be eligible.

    Mirrors openclaw OpenClawSkillMetadata.requires.
    """
    bins: list[str] | None = None
    anyBins: list[str] | None = None
    env: list[str] | None = None
    config: list[str] | None = None


@dataclass
class SkillInstallSpec:
    """Installer spec for a skill dependency.

    Mirrors openclaw SkillInstallSpec.
    """
    id: str | None = None
    kind: Literal["brew", "node", "go", "uv", "download"] = "brew"
    label: str | None = None
    bins: list[str] | None = None
    os: list[str] | None = None
    formula: str | None = None
    package: str | None = None
    module: str | None = None
    url: str | None = None
    archive: str | None = None
    extract: bool | None = None
    stripComponents: int | None = None
    targetDir: str | None = None


@dataclass
class SkillMetadata:
    """OpenClaw-specific skill metadata.

    Mirrors openclaw OpenClawSkillMetadata from frontmatter.ts.
    """
    always: bool = False
    skillKey: str | None = None
    primaryEnv: str | None = None
    emoji: str | None = None
    homepage: str | None = None
    os: list[str] | None = None
    requires: SkillRequires | None = None
    install: list[SkillInstallSpec] | None = None


@dataclass
class SkillInvocationPolicy:
    """Invocation policy derived from frontmatter.

    Mirrors openclaw SkillInvocationPolicy.
    """
    userInvocable: bool = True
    disableModelInvocation: bool = False


@dataclass
class SkillExposure:
    """Exposure policy for runtime registry and prompt.

    Mirrors openclaw SkillExposure.
    """
    includeInRuntimeRegistry: bool = True
    includeInAvailableSkillsPrompt: bool = True
    userInvocable: bool = True


@dataclass
class Skill:
    """One loaded skill with its SKILL.md content.

    Mirrors openclaw Skill from skill-contract.ts.
    """
    name: str
    description: str
    filePath: str
    baseDir: str
    source: str = "unknown"  # "workspace", "agents-project", "agents-personal", "managed", "bundled", "extra"
    content: str | None = None


@dataclass
class SkillEntry:
    """A skill entry with frontmatter and eligibility state.

    Mirrors openclaw SkillEntry from types.ts.
    """
    skill: Skill
    frontmatter: dict[str, str] = field(default_factory=dict)
    metadata: SkillMetadata | None = None
    invocation: SkillInvocationPolicy | None = None
    exposure: SkillExposure | None = None
    eligible: bool = True
    eligibilityReason: str | None = None


@dataclass
class SkillSnapshot:
    """Snapshot of eligible skills for a session.

    Mirrors openclaw SkillSnapshot from types.ts.
    """
    prompt: str
    skills: list[dict[str, Any]] = field(default_factory=list)
    skillFilter: list[str] | None = None
    resolvedSkills: list[Skill] | None = None
    version: int | None = None