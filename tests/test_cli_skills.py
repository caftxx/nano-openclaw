"""Tests for /skills command in CLI."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from nano_openclaw.cli import _list_skills
from nano_openclaw.loop import LoopConfig
from nano_openclaw.skills import Skill, SkillEntry


def test_list_skills_no_workspace():
    """/skills with no workspace shows unavailable message."""
    console = Console()
    cfg = LoopConfig(workspace_dir=None)
    
    # Capture output
    with console.capture() as capture:
        _list_skills(console, cfg)
    
    output = capture.get()
    assert "no workspace configured" in output


def test_list_skills_with_skills(tmp_path: Path):
    """/skills displays skills table with status."""
    # Create a skill in workspace
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("""---
name: test-skill
description: A test skill
---
# Test Skill
""")
    
    console = Console()
    cfg = LoopConfig(
        workspace_dir=tmp_path,
        session_key="test",
        extra_skill_dirs=None,
    )
    
    # Capture output
    with console.capture() as capture:
        _list_skills(console, cfg)
    
    output = capture.get()
    
    # Should show table header
    assert "Skills" in output
    assert "test-skill" in output


def test_list_skills_with_filter(tmp_path: Path):
    """/skills shows skill filter in summary."""
    console = Console()
    cfg = LoopConfig(
        workspace_dir=tmp_path,
        session_key="test",
        skill_filter=["github", "weather"],
    )
    
    with console.capture() as capture:
        _list_skills(console, cfg)
    
    output = capture.get()
    assert "skill filter:" in output
    assert "github" in output


def test_list_skills_shows_blocked(tmp_path: Path):
    """/skills shows blocked skills with reason."""
    console = Console()
    
    # Create blocked skill entry manually
    skill = Skill(
        name="blocked-skill",
        description="Blocked skill",
        filePath="/path/blocked/SKILL.md",
        baseDir="/path/blocked",
        source="bundled",
    )
    
    from nano_openclaw.skills import SkillMetadata, SkillRequires
    metadata = SkillMetadata(
        requires=SkillRequires(bins=["nonexistent-cli"]),
    )
    
    entry = SkillEntry(
        skill=skill,
        metadata=metadata,
        eligible=False,
        eligibilityReason="missing required binary: nonexistent-cli",
    )
    
    cfg = LoopConfig(workspace_dir=tmp_path, session_key="test")
    
    # Mock to return our blocked skill
    with patch("nano_openclaw.cli.get_or_load_skills") as mock_load:
        mock_load.return_value = [entry]
        with console.capture() as capture:
            _list_skills(console, cfg)
    
    output = capture.get()
    
    # Should show blocked status
    assert "blocked" in output
    assert "blocked-skill" in output


def test_list_skills_shows_eligible(tmp_path: Path):
    """/skills shows eligible skills."""
    # Create a simple skill without gating
    skill_dir = tmp_path / "skills" / "simple"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("""---
name: simple
description: Simple skill
---
# Simple
""")
    
    console = Console()
    cfg = LoopConfig(workspace_dir=tmp_path, session_key="test")
    
    with console.capture() as capture:
        _list_skills(console, cfg)
    
    output = capture.get()
    
    # Should show eligible
    assert "eligible" in output


def test_list_skills_empty(tmp_path: Path):
    """/skills shows no skills found when empty."""
    # Empty skills directory
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    
    console = Console()
    cfg = LoopConfig(workspace_dir=tmp_path, session_key="test")
    
    # Mock to avoid loading ~/.agents/skills
    with patch("nano_openclaw.cli.get_or_load_skills") as mock_load:
        mock_load.return_value = []
        with console.capture() as capture:
            _list_skills(console, cfg)
    
    output = capture.get()
    assert "no skills found" in output


def test_list_skills_shows_in_prompt_column(tmp_path: Path):
    """/skills shows 'In Prompt' column."""
    skill_dir = tmp_path / "skills" / "visible-skill"
    skill_dir.mkdir(parents=True)
    
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("""---
name: visible-skill
description: Visible skill
---
# Visible
""")
    
    console = Console()
    cfg = LoopConfig(workspace_dir=tmp_path, session_key="test")
    
    with console.capture() as capture:
        _list_skills(console, cfg)
    
    output = capture.get()
    
    # Table should have "In Prompt" column
    assert "In Prompt" in output