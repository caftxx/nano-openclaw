"""Tests for daily memory file loading.

Mirrors openclaw's startup-context.ts behavior:
- Date stamp generation
- File discovery in memory/ directory
- Prelude construction with character limits
- Slugged file handling (YYYY-MM-DD-slug.md)
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from nano_openclaw.memory.daily import (
    build_date_stamps,
    list_daily_memory_files,
    format_daily_memory_block,
    build_daily_memory_prelude,
)
from nano_openclaw.workspace.constants import (
    DEFAULT_DAILY_MEMORY_DAYS,
    MAX_DAILY_MEMORY_DAYS,
    DAILY_MEMORY_FILE_MAX_CHARS,
    DAILY_MEMORY_TOTAL_MAX_CHARS,
)


@pytest.fixture
def workspace_with_memory():
    """Create a temporary workspace with memory directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        memory_dir = ws / "memory"
        memory_dir.mkdir()
        # Create AGENTS.md so workspace is valid
        (ws / "AGENTS.md").write_text("# Rules")
        yield ws


class TestBuildDateStamps:
    """Tests for build_date_stamps function."""

    def test_default_days_returns_today_and_yesterday(self):
        """Default should return 2 days: today and yesterday."""
        now = datetime(2026, 5, 2, 10, 30)
        stamps = build_date_stamps(now, DEFAULT_DAILY_MEMORY_DAYS)

        assert len(stamps) == 2
        assert stamps[0] == "2026-05-02"
        assert stamps[1] == "2026-05-01"

    def test_custom_days(self):
        """Test with custom number of days."""
        now = datetime(2026, 5, 2, 10, 30)
        stamps = build_date_stamps(now, 3)

        assert len(stamps) == 3
        assert stamps == ["2026-05-02", "2026-05-01", "2026-04-30"]

    def test_max_days_clamped(self):
        """Days should be clamped to MAX_DAILY_MEMORY_DAYS."""
        now = datetime(2026, 5, 2, 10, 30)
        stamps = build_date_stamps(now, 100)

        assert len(stamps) == MAX_DAILY_MEMORY_DAYS

    def test_min_days_clamped(self):
        """Days should be clamped to minimum 1."""
        now = datetime(2026, 5, 2, 10, 30)
        stamps = build_date_stamps(now, 0)

        assert len(stamps) == 1
        assert stamps[0] == "2026-05-02"


class TestListDailyMemoryFiles:
    """Tests for list_daily_memory_files function."""

    def test_no_memory_directory(self):
        """Should return empty lists when memory/ doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            # No memory/ directory created

            stamps = ["2026-05-02", "2026-05-01"]
            result = list_daily_memory_files(ws, stamps)

            assert result == {"2026-05-02": [], "2026-05-01": []}

    def test_exact_match_file(self, workspace_with_memory):
        """Should find exact YYYY-MM-DD.md file."""
        ws = workspace_with_memory
        # Create only the file we want to find
        (ws / "memory" / "2026-05-02.md").write_text("Daily notes")

        # Only query for the stamp that exists
        stamps = ["2026-05-02"]
        result = list_daily_memory_files(ws, stamps)

        assert result["2026-05-02"] == ["2026-05-02.md"]

    def test_slugged_file(self, workspace_with_memory):
        """Should find slugged YYYY-MM-DD-slug.md files."""
        ws = workspace_with_memory
        (ws / "memory" / "2026-05-02.md").write_text("Main notes")
        (ws / "memory" / "2026-05-02-summary.md").write_text("Summary")
        (ws / "memory" / "2026-05-02-details.md").write_text("Details")

        stamps = ["2026-05-02"]
        result = list_daily_memory_files(ws, stamps)

        # Exact file first, then slugged files
        assert result["2026-05-02"][0] == "2026-05-02.md"
        assert len(result["2026-05-02"]) >= 2  # At least exact + one slugged

    def test_ignores_non_matching_files(self, workspace_with_memory):
        """Should ignore files that don't match date stamp."""
        ws = workspace_with_memory
        (ws / "memory" / "2026-05-02.md").write_text("Today")
        (ws / "memory" / "2026-04-30.md").write_text("Old")
        (ws / "memory" / "notes.md").write_text("Random notes")

        stamps = ["2026-05-02"]
        result = list_daily_memory_files(ws, stamps)

        assert result["2026-05-02"] == ["2026-05-02.md"]

    def test_ignores_non_md_files(self, workspace_with_memory):
        """Should ignore non-.md files."""
        ws = workspace_with_memory
        (ws / "memory" / "2026-05-02.md").write_text("Notes")
        (ws / "memory" / "2026-05-02.txt").write_text("Text file")
        (ws / "memory" / "2026-05-02.json").write_text("{}")

        stamps = ["2026-05-02"]
        result = list_daily_memory_files(ws, stamps)

        assert result["2026-05-02"] == ["2026-05-02.md"]


class TestFormatDailyMemoryBlock:
    """Tests for format_daily_memory_block function."""

    def test_basic_format(self):
        """Test basic block formatting."""
        block = format_daily_memory_block("2026-05-02.md", "Daily notes content")

        assert "[Daily memory: memory/2026-05-02.md]" in block
        assert "```" in block
        assert "Daily notes content" in block


class TestBuildDailyMemoryPrelude:
    """Tests for build_daily_memory_prelude function."""

    def test_returns_none_when_no_files(self):
        """Should return None when no daily memory files exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            result = build_daily_memory_prelude(ws)

            assert result is None

    def test_returns_prelude_with_files(self, workspace_with_memory):
        """Should return prelude string when files exist."""
        ws = workspace_with_memory
        (ws / "memory" / "2026-05-02.md").write_text("Today's work:\n- Implemented memory feature")

        result = build_daily_memory_prelude(ws)

        assert result is not None
        assert "[Startup context: daily memory loaded]" in result
        assert "[Daily memory: memory/2026-05-02.md]" in result
        assert "Implemented memory feature" in result

    def test_truncates_long_content(self, workspace_with_memory):
        """Should truncate content exceeding DAILY_MEMORY_FILE_MAX_CHARS."""
        ws = workspace_with_memory
        long_content = "A" * 2000
        (ws / "memory" / "2026-05-02.md").write_text(long_content)

        result = build_daily_memory_prelude(ws)

        assert result is not None
        # Content should be truncated
        assert len(result) < len(long_content) + 100

    def test_respects_total_budget(self, workspace_with_memory):
        """Should respect total character budget."""
        ws = workspace_with_memory
        # Create multiple files that would exceed total budget
        (ws / "memory" / "2026-05-02.md").write_text("A" * 1000)
        (ws / "memory" / "2026-05-01.md").write_text("B" * 1000)
        (ws / "memory" / "2026-04-30.md").write_text("C" * 1000)

        result = build_daily_memory_prelude(ws, days=3)

        assert result is not None
        # Total should not exceed budget significantly
        assert len(result) <= DAILY_MEMORY_TOTAL_MAX_CHARS + 200  # Allow header overhead

    def test_marks_as_untrusted(self, workspace_with_memory):
        """Should mark daily memory as untrusted."""
        ws = workspace_with_memory
        (ws / "memory" / "2026-05-02.md").write_text("Notes")

        result = build_daily_memory_prelude(ws)

        assert "untrusted" in result.lower()


class TestIntegration:
    """Integration tests for daily memory in system prompt."""

    def test_prelude_in_system_prompt(self, workspace_with_memory):
        """Daily memory prelude should appear in system prompt."""
        from nano_openclaw.prompt import build_system_prompt
        from nano_openclaw.tools import build_default_registry

        ws = workspace_with_memory
        (ws / "memory" / "2026-05-02.md").write_text("Memory test content")
        (ws / "AGENTS.md").write_text("# Rules")

        registry = build_default_registry()
        registry.set_workspace_dir(str(ws))

        prompt = build_system_prompt(registry, ws)

        assert "[Startup context: daily memory loaded]" in prompt
        assert "[Daily memory: memory/2026-05-02.md]" in prompt