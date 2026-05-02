"""Tests for skills gating module."""

import platform

import pytest

from nano_openclaw.skills import (
    Skill,
    SkillEntry,
    SkillExposure,
    SkillInvocationPolicy,
    SkillMetadata,
    SkillRequires,
)
from nano_openclaw.skills.gating import (
    check_bin_exists,
    check_config_path_truthy,
    check_env_exists,
    check_skill_eligibility,
    filter_eligible_skills,
    filter_visible_skills,
    get_current_os,
)


def test_get_current_os():
    """Get normalized OS name."""
    os_name = get_current_os()
    assert os_name in ("darwin", "linux", "win32")


def test_check_bin_exists():
    """Check if binary exists on PATH."""
    # Common binaries that should exist
    assert check_bin_exists("python") or check_bin_exists("python3")
    
    # Non-existent binary
    assert not check_bin_exists("nonexistent-binary-xyz")


def test_check_env_exists(monkeypatch):
    """Check if env var exists."""
    monkeypatch.setenv("TEST_VAR", "value")
    assert check_env_exists("TEST_VAR")
    
    monkeypatch.delenv("TEST_VAR", raising=False)
    assert not check_env_exists("TEST_VAR")


def test_check_config_path_truthy():
    """Check config path is truthy."""
    config = {"skills": {"enabled": True, "count": 5, "name": "test"}}
    
    assert check_config_path_truthy(config, "skills.enabled")
    assert check_config_path_truthy(config, "skills.count")
    assert check_config_path_truthy(config, "skills.name")
    
    # Non-existent path
    assert not check_config_path_truthy(config, "skills.missing")
    
    # Falsey value
    config["skills"]["enabled"] = False
    assert not check_config_path_truthy(config, "skills.enabled")


def test_skill_eligibility_always():
    """Skill with always=True skips all gating."""
    skill = Skill(
        name="always-skill",
        description="Always eligible",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    metadata = SkillMetadata(always=True)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is True
    assert reason is None


def test_skill_eligibility_os_mismatch():
    """Skill with OS requirement mismatched."""
    skill = Skill(
        name="mac-only",
        description="Mac only skill",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    current_os = get_current_os()
    other_os = "win32" if current_os == "darwin" else "darwin"
    
    metadata = SkillMetadata(os=[other_os])
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is False
    assert "OS mismatch" in reason


def test_skill_eligibility_os_match():
    """Skill with OS requirement matched."""
    current_os = get_current_os()
    
    skill = Skill(
        name="current-os-skill",
        description="Current OS skill",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    metadata = SkillMetadata(os=[current_os])
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is True


def test_skill_eligibility_missing_binary():
    """Skill requires missing binary."""
    skill = Skill(
        name="needs-gh",
        description="Needs GitHub CLI",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    requires = SkillRequires(bins=["nonexistent-cli-xyz"])
    metadata = SkillMetadata(requires=requires)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is False
    assert "missing required binary" in reason


def test_skill_eligibility_existing_binary():
    """Skill requires existing binary."""
    # Find an existing binary
    existing_bin = "python" if check_bin_exists("python") else "python3"
    
    skill = Skill(
        name="needs-python",
        description="Needs Python",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    requires = SkillRequires(bins=[existing_bin])
    metadata = SkillMetadata(requires=requires)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is True


def test_skill_eligibility_any_bins():
    """Skill requires at least one of multiple binaries."""
    skill = Skill(
        name="needs-any",
        description="Needs any of several CLIs",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    # Include one existing and one non-existent
    existing_bin = "python" if check_bin_exists("python") else "python3"
    requires = SkillRequires(anyBins=[existing_bin, "nonexistent-xyz"])
    metadata = SkillMetadata(requires=requires)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is True


def test_skill_eligibility_any_bins_none_exist():
    """Skill requires any bins but none exist."""
    skill = Skill(
        name="needs-any-none",
        description="Needs any CLI",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    requires = SkillRequires(anyBins=["nonexistent-a", "nonexistent-b"])
    metadata = SkillMetadata(requires=requires)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is False
    assert "missing any of required binaries" in reason


def test_skill_eligibility_missing_env(monkeypatch):
    """Skill requires missing env var."""
    skill = Skill(
        name="needs-token",
        description="Needs API token",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    requires = SkillRequires(env=["MISSING_TOKEN"])
    metadata = SkillMetadata(requires=requires)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    # Ensure env var is not set
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is False
    assert "missing required env var" in reason


def test_skill_eligibility_existing_env(monkeypatch):
    """Skill requires existing env var."""
    skill = Skill(
        name="needs-existing",
        description="Needs existing var",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    monkeypatch.setenv("EXISTING_VAR", "value")
    
    requires = SkillRequires(env=["EXISTING_VAR"])
    metadata = SkillMetadata(requires=requires)
    entry = SkillEntry(skill=skill, metadata=metadata)
    
    eligible, reason = check_skill_eligibility(entry)
    assert eligible is True


def test_skill_eligibility_skill_filter():
    """Skill not in filter list."""
    skill = Skill(
        name="filtered-out",
        description="Filtered out",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    entry = SkillEntry(skill=skill)
    
    eligible, reason = check_skill_eligibility(entry, skill_filter=["other-skill"])
    assert eligible is False
    assert "not in skill filter" in reason


def test_skill_eligibility_in_filter():
    """Skill in filter list."""
    skill = Skill(
        name="allowed",
        description="Allowed skill",
        filePath="/path",
        baseDir="/path",
        source="bundled",
    )
    
    entry = SkillEntry(skill=skill)
    
    eligible, reason = check_skill_eligibility(entry, skill_filter=["allowed"])
    assert eligible is True


def test_filter_eligible_skills():
    """Filter list of entries."""
    skills = [
        SkillEntry(
            skill=Skill(name="skill1", description="1", filePath="/p1", baseDir="/p1", source="bundled"),
            metadata=SkillMetadata(always=True),
        ),
        SkillEntry(
            skill=Skill(name="skill2", description="2", filePath="/p2", baseDir="/p2", source="bundled"),
            metadata=SkillMetadata(os=["nonexistent-os"]),
        ),
        SkillEntry(
            skill=Skill(name="skill3", description="3", filePath="/p3", baseDir="/p3", source="bundled"),
        ),
    ]
    
    filtered = filter_eligible_skills(skills)
    
    # skill1 (always) and skill3 (no gating) should be eligible
    # skill2 (OS mismatch) should not
    eligible_names = [e.skill.name for e in filtered]
    assert "skill1" in eligible_names
    assert "skill3" in eligible_names
    assert "skill2" not in eligible_names


def test_filter_visible_skills():
    """Filter visible skills (exclude disableModelInvocation)."""
    skills = [
        SkillEntry(
            skill=Skill(name="visible1", description="1", filePath="/p1", baseDir="/p1", source="bundled"),
            eligible=True,
            exposure=SkillExposure(includeInAvailableSkillsPrompt=True),
        ),
        SkillEntry(
            skill=Skill(name="hidden", description="2", filePath="/p2", baseDir="/p2", source="bundled"),
            eligible=True,
            exposure=SkillExposure(includeInAvailableSkillsPrompt=False),
        ),
        SkillEntry(
            skill=Skill(name="visible2", description="3", filePath="/p3", baseDir="/p3", source="bundled"),
            eligible=True,
            invocation=SkillInvocationPolicy(disableModelInvocation=False),
        ),
        SkillEntry(
            skill=Skill(name="hidden2", description="4", filePath="/p4", baseDir="/p4", source="bundled"),
            eligible=True,
            invocation=SkillInvocationPolicy(disableModelInvocation=True),
        ),
        SkillEntry(
            skill=Skill(name="ineligible", description="5", filePath="/p5", baseDir="/p5", source="bundled"),
            eligible=False,
        ),
    ]
    
    visible = filter_visible_skills(skills)
    
    # Only visible1 and visible2 should be visible
    visible_names = [s.name for s in visible]
    assert "visible1" in visible_names
    assert "visible2" in visible_names
    assert "hidden" not in visible_names
    assert "hidden2" not in visible_names
    assert "ineligible" not in visible_names