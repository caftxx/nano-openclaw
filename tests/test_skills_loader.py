"""Tests for skills loader module."""

from pathlib import Path

import pytest

from nano_openclaw.skills import (
    DEFAULT_MAX_SKILL_FILE_BYTES,
    SKILL_FILE_NAME,
    Skill,
    SkillEntry,
    load_skill_entries,
    load_skill_from_file,
    load_skills_from_dir,
    parse_frontmatter,
    resolve_bundled_skills_dir,
)
from nano_openclaw.skills.loader import (
    extract_content_after_frontmatter,
    resolve_skill_metadata,
)


def test_parse_frontmatter_basic():
    """Parse basic YAML frontmatter."""
    content = """---
name: test-skill
description: A test skill
---
# Test Skill Content"""
    
    result = parse_frontmatter(content)
    assert result["name"] == "test-skill"
    assert result["description"] == "A test skill"


def test_parse_frontmatter_with_metadata():
    """Parse frontmatter with metadata JSON."""
    content = """---
name: github
description: GitHub CLI skill
metadata: {"openclaw": {"requires": {"bins": ["gh"]}}}
---
# GitHub Skill"""
    
    result = parse_frontmatter(content)
    assert result["name"] == "github"
    assert "metadata" in result


def test_parse_frontmatter_missing():
    """Return empty dict when no frontmatter."""
    content = "# Just markdown content"
    result = parse_frontmatter(content)
    assert result == {}


def test_extract_content_after_frontmatter():
    """Extract body after frontmatter."""
    content = """---
name: test
---
# Body content
Some text here."""
    
    body = extract_content_after_frontmatter(content)
    assert "# Body content" in body
    assert "Some text here." in body
    assert "---" not in body


def test_resolve_skill_metadata():
    """Resolve SkillMetadata from frontmatter."""
    frontmatter = {
        "name": "test",
        "metadata": '{"openclaw": {"requires": {"bins": ["gh"], "env": ["TOKEN"]}}}'
    }
    
    metadata = resolve_skill_metadata(frontmatter)
    assert metadata is not None
    assert metadata.requires is not None
    assert metadata.requires.bins == ["gh"]
    assert metadata.requires.env == ["TOKEN"]


def test_resolve_bundled_skills_dir():
    """Bundled skills dir exists."""
    bundled_dir = resolve_bundled_skills_dir()
    assert bundled_dir is not None
    assert bundled_dir.is_dir()
    assert bundled_dir.name == "bundled_skills"


def test_load_bundled_skills():
    """Load bundled skills from package."""
    bundled_dir = resolve_bundled_skills_dir()
    assert bundled_dir is not None
    
    skills = load_skills_from_dir(bundled_dir, "bundled")
    assert len(skills) >= 1  # At least github, weather, or summarize
    
    # Check skill structure
    for skill in skills:
        assert skill.name
        assert skill.description
        assert skill.filePath.endswith(SKILL_FILE_NAME)
        assert skill.source == "bundled"


def test_load_skill_entries_from_workspace(tmp_path: Path):
    """Load skills from workspace directory."""
    # Create a test skill in workspace
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / SKILL_FILE_NAME
    skill_file.write_text("""---
name: test-skill
description: Test skill in workspace
---
# Test Skill
This is a test skill in workspace.
""")
    
    entries = load_skill_entries(tmp_path)
    
    # Should find our test skill
    found = [e for e in entries if e.skill.name == "test-skill"]
    assert len(found) == 1
    assert found[0].skill.source == "workspace"
    assert found[0].skill.content is not None


def test_skill_precedence(tmp_path: Path):
    """Workspace skill overrides bundled."""
    # Create workspace skill with same name as bundled
    skill_dir = tmp_path / "skills" / "github"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / SKILL_FILE_NAME
    skill_file.write_text("""---
name: github
description: Custom GitHub skill override
---
# Custom GitHub Skill
This overrides the bundled github skill.
""")
    
    entries = load_skill_entries(tmp_path)
    
    # Find github skill
    github_entries = [e for e in entries if e.skill.name == "github"]
    assert len(github_entries) == 1
    
    # Should be workspace version (higher precedence)
    entry = github_entries[0]
    assert entry.skill.source == "workspace"
    assert "Custom GitHub skill override" in entry.skill.description


def test_skill_file_size_limit(tmp_path: Path):
    """Skip files exceeding size limit."""
    skill_dir = tmp_path / "skills" / "large-skill"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / SKILL_FILE_NAME
    # Create file larger than limit
    large_content = "---\nname: large\ndescription: Large skill\n---\n" + "x" * (DEFAULT_MAX_SKILL_FILE_BYTES + 1000)
    skill_file.write_text(large_content)
    
    skills = load_skills_from_dir(tmp_path / "skills", "workspace", max_bytes=DEFAULT_MAX_SKILL_FILE_BYTES)
    
    # Large skill should be skipped
    found = [s for s in skills if s.name == "large"]
    assert len(found) == 0


def test_missing_required_frontmatter(tmp_path: Path):
    """Skip files missing name or description."""
    skill_dir = tmp_path / "skills" / "invalid-skill"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / SKILL_FILE_NAME
    skill_file.write_text("""---
name: only-name
---
# Missing description""")
    
    skills = load_skills_from_dir(tmp_path / "skills", "workspace")
    
    # Should be skipped (missing description)
    found = [s for s in skills if s.name == "only-name"]
    assert len(found) == 0


def test_skill_entry_structure():
    """SkillEntry has all expected fields."""
    skill = Skill(
        name="test",
        description="Test",
        filePath="/path/to/SKILL.md",
        baseDir="/path/to",
        source="bundled",
        content="# Content",
    )

    entry = SkillEntry(
        skill=skill,
        frontmatter={"name": "test"},
        eligible=True,
    )

    assert entry.skill.name == "test"
    assert entry.eligible is True


def test_load_skill_entries_parses_frontmatter_metadata(tmp_path: Path):
    """load_skill_entries reads raw file to populate metadata, not stripped body."""
    skill_dir = tmp_path / "skills" / "gated-skill"
    skill_dir.mkdir(parents=True)

    skill_file = skill_dir / SKILL_FILE_NAME
    skill_file.write_text(
        '---\nname: gated-skill\ndescription: Needs a binary\n'
        'metadata: {"openclaw": {"requires": {"bins": ["nonexistent-cli"]}}}\n'
        '---\n# Gated Skill\nContent here.\n'
    )

    entries = load_skill_entries(tmp_path)
    found = [e for e in entries if e.skill.name == "gated-skill"]
    assert len(found) == 1
    entry = found[0]
    assert entry.metadata is not None, "metadata should be populated from raw frontmatter"
    assert entry.metadata.requires is not None
    assert entry.metadata.requires.bins == ["nonexistent-cli"]


def test_load_skill_entries_parses_invocation_policy(tmp_path: Path):
    """load_skill_entries correctly reads user-invocable flag from frontmatter."""
    skill_dir = tmp_path / "skills" / "hidden-skill"
    skill_dir.mkdir(parents=True)

    skill_file = skill_dir / SKILL_FILE_NAME
    skill_file.write_text(
        '---\nname: hidden-skill\ndescription: Not user invocable\n'
        'user-invocable: false\n---\n# Hidden Skill\nContent.\n'
    )

    entries = load_skill_entries(tmp_path)
    found = [e for e in entries if e.skill.name == "hidden-skill"]
    assert len(found) == 1
    entry = found[0]
    assert entry.invocation is not None
    assert entry.invocation.userInvocable is False