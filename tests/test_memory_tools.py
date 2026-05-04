"""Tests for memory tools: memory_get and memory_search.

Mirrors openclaw's memory-core/src/tools.ts behavior:
- memory_get: read specific memory files or excerpts
- memory_search: lexical search across MEMORY.md + memory/*.md
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
import tempfile
from pathlib import Path

import pytest

from nano_openclaw.memory.tools import (
    memory_get,
    memory_search,
    MemorySearchResult,
    _apply_temporal_decay,
)
from nano_openclaw.config.types import MemorySearchConfig, NanoOpenClawConfig, PluginsConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.tools import build_core_registry


def _dispatch_result(registry, *args):
    result = registry.dispatch(*args)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


@pytest.fixture
def workspace_with_memory_files():
    """Create a workspace with MEMORY.md and daily memory files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)

        # Create MEMORY.md
        (ws / "MEMORY.md").write_text(
            "# Long-term Memory\n\n"
            "## Key Decisions\n"
            "- Use lexical search for simplicity\n"
            "- Keep nano lightweight\n\n"
            "## Preferences\n"
            "- Python code style: concise\n"
            "- Test frequently\n"
        )

        # Create memory/ directory with daily files
        memory_dir = ws / "memory"
        memory_dir.mkdir()
        (memory_dir / "2026-05-02.md").write_text(
            "# Daily log\n\n"
            "Implemented memory feature today.\n"
            "Added memory_get and memory_search tools.\n"
        )
        (memory_dir / "2026-05-01.md").write_text(
            "# Yesterday\n\n"
            "Started planning memory implementation.\n"
        )

        yield str(ws)


class TestMemoryGet:
    """Tests for memory_get tool."""

    def test_read_memory_md(self, workspace_with_memory_files):
        """Should read MEMORY.md file."""
        result = memory_get({"path": "MEMORY.md"}, workspace_with_memory_files)

        assert "[MEMORY.md]" in result
        assert "Long-term Memory" in result
        assert "Key Decisions" in result

    def test_read_daily_memory(self, workspace_with_memory_files):
        """Should read daily memory file."""
        result = memory_get({"path": "memory/2026-05-02.md"}, workspace_with_memory_files)

        assert "[memory/2026-05-02.md]" in result
        assert "Daily log" in result

    def test_read_with_line_range(self, workspace_with_memory_files):
        """Should read specific line range."""
        result = memory_get(
            {"path": "MEMORY.md", "from": 1, "lines": 3},
            workspace_with_memory_files
        )

        assert "[MEMORY.md lines 1-3]" in result
        assert "1:" in result  # Line numbers should be present

    def test_file_not_found(self, workspace_with_memory_files):
        """Should return error for missing file."""
        result = memory_get({"path": "nonexistent.md"}, workspace_with_memory_files)

        assert "[file not found:" in result

    def test_no_workspace_directory(self):
        """Should return error when no workspace directory."""
        result = memory_get({"path": "MEMORY.md"}, None)

        assert "[error: no workspace directory]" in result

    def test_path_escaping_workspace(self, workspace_with_memory_files):
        """Should reject paths that escape workspace."""
        result = memory_get({"path": "../outside.md"}, workspace_with_memory_files)

        # Should either error or not read the file
        assert "error" in result.lower() or "not found" in result.lower()


class TestMemorySearch:
    """Tests for memory_search tool."""

    def test_search_single_keyword(self, workspace_with_memory_files):
        """Should find matches for single keyword."""
        result = memory_search({"query": "preferences"}, workspace_with_memory_files)

        assert "Memory search results:" in result
        assert "MEMORY.md" in result
        assert "preferences" in result.lower()

    def test_search_multiple_keywords(self, workspace_with_memory_files):
        """Should find matches across files for multiple keywords."""
        result = memory_search({"query": "memory feature"}, workspace_with_memory_files)

        assert "Memory search results:" in result
        # Should find in daily files
        assert "2026-05" in result

    def test_search_max_results(self, workspace_with_memory_files):
        """Should respect maxResults parameter."""
        result = memory_search(
            {"query": "memory", "maxResults": 2},
            workspace_with_memory_files
        )

        # Count result lines (each result has 2 lines)
        lines = result.split("\n")
        result_lines = [l for l in lines if l.startswith("- ")]
        assert len(result_lines) <= 2

    def test_search_min_score(self, workspace_with_memory_files):
        """Should respect minScore parameter."""
        # High minScore should filter out weak matches
        result = memory_search(
            {"query": "xyz nonexistent keyword", "minScore": 0.5},
            workspace_with_memory_files
        )

        assert "no matches found" in result.lower()

    def test_search_empty_query(self, workspace_with_memory_files):
        """Should handle empty query."""
        result = memory_search({"query": ""}, workspace_with_memory_files)

        assert "results" in result.lower()

    def test_search_no_workspace(self):
        """Should return error when no workspace."""
        result = memory_search({"query": "test"}, None)

        assert "error" in result.lower() or "no workspace" in result.lower()

    def test_search_includes_line_numbers(self, workspace_with_memory_files):
        """Should include line numbers in results."""
        result = memory_search({"query": "decisions"}, workspace_with_memory_files)

        assert "MEMORY.md" in result
        # Line numbers format: path:start-end
        assert ":" in result  # Should have line separator

    def test_temporal_decay_disabled_by_default(self):
        """Temporal decay should not apply unless explicitly configured."""
        results = [
            MemorySearchResult("memory/2026-01-01.md", "old", 1.0, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            MemorySearchConfig(),
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == 1.0

    def test_temporal_decay_halves_score_at_half_life(self):
        """A dated daily note 30 days old should score at roughly 50%."""
        results = [
            MemorySearchResult("memory/2026-01-01.md", "old", 0.8, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            {"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == pytest.approx(0.4, abs=0.01)
        assert decayed[0].raw_score == 0.8

    def test_temporal_decay_can_change_search_ranking(self, tmp_path):
        """An older stronger raw lexical hit can rank below a newer daily note."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "2026-01-01.md").write_text("# alpha beta\nalpha beta\n", encoding="utf-8")
        (memory_dir / "2026-01-31.md").write_text("alpha beta\n", encoding="utf-8")

        result = memory_search(
            {"query": "alpha beta", "maxResults": 2},
            str(tmp_path),
            config={"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        result_lines = [line for line in result.splitlines() if line.startswith("- ")]
        assert result_lines[0].startswith("- memory/2026-01-31.md")
        assert result_lines[1].startswith("- memory/2026-01-01.md")

    def test_temporal_decay_does_not_decay_evergreen_memory_paths(self):
        """MEMORY.md and non-dated memory/*.md files are evergreen."""
        results = [
            MemorySearchResult("MEMORY.md", "root", 1.0, 1, 1),
            MemorySearchResult("memory/projects.md", "topic", 0.75, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            {"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == 1.0
        assert decayed[1].score == 0.75

    def test_temporal_decay_future_dates_do_not_boost_or_decay(self):
        """Future dated daily notes are treated as age zero."""
        results = [
            MemorySearchResult("memory/2099-01-01.md", "future", 0.9, 1, 1),
        ]

        decayed = _apply_temporal_decay(
            results,
            {"temporalDecay": {"enabled": True, "halfLifeDays": 30}},
            now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )

        assert decayed[0].score == 0.9


class TestMemorySearchResult:
    """Tests for MemorySearchResult dataclass."""

    def test_dataclass_fields(self):
        """Should have all expected fields."""
        result = MemorySearchResult(
            path="MEMORY.md",
            snippet="test snippet",
            score=0.8,
            start_line=5,
            end_line=5,
        )

        assert result.path == "MEMORY.md"
        assert result.snippet == "test snippet"
        assert result.score == 0.8
        assert result.raw_score == 0.8
        assert result.start_line == 5
        assert result.end_line == 5


class TestToolIntegration:
    """Tests for memory tools in registry."""

    def _registry(self):
        registry = build_core_registry()
        load_plugins(PluginsConfig(load=["memory"]), registry, NanoOpenClawConfig())
        return registry

    def test_tools_registered(self):
        """memory_get and memory_search should be registered."""
        registry = self._registry()

        names = registry.names()
        assert "memory_get" in names
        assert "memory_search" in names

    def test_memory_get_via_dispatch(self, workspace_with_memory_files):
        """Should be able to call memory_get via registry.dispatch."""
        registry = self._registry()
        registry.set_workspace_dir(workspace_with_memory_files)

        result = _dispatch_result(
            registry,
            "test-id",
            "memory_get",
            {"path": "MEMORY.md"}
        )

        assert result.get("is_error") is None
        text = result["content"][0]["text"]
        assert "Long-term Memory" in text

    def test_memory_search_via_dispatch(self, workspace_with_memory_files):
        """Should be able to call memory_search via registry.dispatch."""
        registry = self._registry()
        registry.set_workspace_dir(workspace_with_memory_files)

        result = _dispatch_result(
            registry,
            "test-id",
            "memory_search",
            {"query": "decisions"}
        )

        assert result.get("is_error") is None
        text = result["content"][0]["text"]
        assert "Memory search" in text
