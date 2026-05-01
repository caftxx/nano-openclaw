"""Tests for workspace bootstrap file loading and prompt injection.

Mirrors openclaw's workspace file loading behavior:
- File discovery and loading
- Security checks (path escaping)
- Budget-based truncation
- Session-scoped caching
- System prompt injection
"""

import tempfile
from pathlib import Path

import pytest

from nano_openclaw.workspace import (
    BOOTSTRAP_FILES,
    CONTEXT_FILE_ORDER,
    DEFAULT_AGENTS_FILENAME,
    DEFAULT_SOUL_FILENAME,
    MINIMAL_BOOTSTRAP_ALLOWLIST,
    WorkspaceBootstrapFile,
    build_bootstrap_context,
    clear_all_cache,
    get_or_load_bootstrap_files,
    load_workspace_bootstrap_files,
    trim_bootstrap_content,
)
from nano_openclaw.prompt import build_system_prompt
from nano_openclaw.tools import ToolRegistry


@pytest.fixture
def workspace_dir():
    """Create a temporary workspace with test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        (ws / "AGENTS.md").write_text("# Agent Rules\n- Be concise\n- Use tools wisely")
        (ws / "SOUL.md").write_text("# Personality\n- Direct and helpful")
        yield ws


def test_load_workspace_bootstrap_files(workspace_dir):
    """Test loading all 8 bootstrap files."""
    files = load_workspace_bootstrap_files(workspace_dir)

    # Should attempt all 8 files
    assert len(files) == len(BOOTSTRAP_FILES)

    # AGENTS.md and SOUL.md should be present
    present = [f for f in files if not f.missing]
    assert len(present) == 2
    assert any(f.name == "AGENTS.md" for f in present)
    assert any(f.name == "SOUL.md" for f in present)

    # Other files should be missing
    missing = [f for f in files if f.missing]
    assert len(missing) == 6


def test_load_empty_workspace():
    """Test loading from empty workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        files = load_workspace_bootstrap_files(ws)

        assert len(files) == 8
        assert all(f.missing for f in files)


def test_trim_content_within_budget():
    """Test that short content is not truncated."""
    content = "Short content"
    result = trim_bootstrap_content(content, "AGENTS.md", max_chars=1000)
    assert result == content


def test_trim_content_exceeds_budget():
    """Test truncation with 75/25 strategy."""
    # Create content that exceeds budget
    content = "A" * 15000
    result = trim_bootstrap_content(content, "AGENTS.md", max_chars=12000)

    assert len(result) <= 12000
    assert "truncated" in result


def test_build_context_filters_missing():
    """Test that missing files are filtered out."""
    files = [
        WorkspaceBootstrapFile("AGENTS.md", "/path/AGENTS.md", "content", False),
        WorkspaceBootstrapFile("SOUL.md", "/path/SOUL.md", missing=True),
    ]

    result = build_bootstrap_context(files)
    assert len(result) == 1
    assert result[0].name == "AGENTS.md"


def test_build_context_respects_total_budget():
    """Test that total budget limit is enforced."""
    large_content = "X" * 50000
    files = [
        WorkspaceBootstrapFile("AGENTS.md", "/path/AGENTS.md", large_content, False),
        WorkspaceBootstrapFile("SOUL.md", "/path/SOUL.md", large_content, False),
    ]

    result = build_bootstrap_context(files, total_max_chars=60000)

    total_chars = sum(len(f.content or "") for f in result)
    assert total_chars <= 60000


def test_build_context_sorts_by_order():
    """Test that files are sorted by CONTEXT_FILE_ORDER."""
    files = [
        WorkspaceBootstrapFile("TOOLS.md", "/path/TOOLS.md", "tools", False),
        WorkspaceBootstrapFile("AGENTS.md", "/path/AGENTS.md", "agents", False),
        WorkspaceBootstrapFile("SOUL.md", "/path/SOUL.md", "soul", False),
    ]

    result = build_bootstrap_context(files)
    names = [f.name for f in result]
    assert names == ["AGENTS.md", "SOUL.md", "TOOLS.md"]


def test_cache_returns_same_object():
    """Test that cached files return the same object."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        (ws / "AGENTS.md").write_text("test")

        session_key = "test-session"
        clear_all_cache()

        files1 = get_or_load_bootstrap_files(ws, session_key)
        files2 = get_or_load_bootstrap_files(ws, session_key)

        assert files1 is files2


def test_prompt_injection_with_bootstrap_files(workspace_dir):
    """Test that bootstrap files are injected into system prompt."""
    files = load_workspace_bootstrap_files(workspace_dir)
    registry = ToolRegistry()

    prompt = build_system_prompt(registry, workspace_dir, files)

    assert "# Project Context" in prompt
    assert "## AGENTS.md" in prompt
    assert "## SOUL.md" in prompt


def test_prompt_soul_special_instruction(workspace_dir):
    """Test that SOUL.md triggers special instruction."""
    files = load_workspace_bootstrap_files(workspace_dir)
    registry = ToolRegistry()

    prompt = build_system_prompt(registry, workspace_dir, files)

    assert "embody its persona and tone" in prompt


def test_prompt_without_soul():
    """Test prompt without SOUL.md (no special instruction)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        (ws / "AGENTS.md").write_text("# Rules")

        files = load_workspace_bootstrap_files(ws)
        registry = ToolRegistry()
        prompt = build_system_prompt(registry, ws, files)

        assert "# Project Context" in prompt
        assert "## AGENTS.md" in prompt
        # SOUL.md special instruction should not be present
        assert "embody its persona and tone" not in prompt


def test_empty_bootstrap_files():
    """Test that empty file list produces no Project Context section."""
    registry = ToolRegistry()
    prompt = build_system_prompt(registry, bootstrap_files=[])

    assert "# Project Context" not in prompt


def test_constants_are_defined():
    """Test that all expected constants are defined."""
    assert DEFAULT_AGENTS_FILENAME == "AGENTS.md"
    assert DEFAULT_SOUL_FILENAME == "SOUL.md"
    assert len(BOOTSTRAP_FILES) == 8
    assert len(CONTEXT_FILE_ORDER) == 7
    assert len(MINIMAL_BOOTSTRAP_ALLOWLIST) == 5
