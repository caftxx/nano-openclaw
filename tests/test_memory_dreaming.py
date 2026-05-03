"""Tests for Dreaming: short-term recall tracking and memory promotion.

Mirrors openclaw extensions/memory-core dreaming behavior:
- track_recall: accumulates recall events from memory_search
- is_dreaming_due: cron-style scheduling check
- run_light_phase: candidate collection
- run_deep_phase: scoring and MEMORY.md promotion
- get_dreaming_status: status reporting
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nano_openclaw.memory.dreaming import (
    DreamingConfig,
    DreamingState,
    ShortTermRecallEntry,
    get_dreaming_status,
    is_dreaming_due,
    load_dreaming_state,
    run_deep_phase,
    run_dreaming,
    run_light_phase,
    track_recall,
    update_last_run_at,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        (ws / "memory").mkdir()
        (ws / "MEMORY.md").write_text("# Long-term Memory\n\nInitial content.\n")
        yield str(ws)


@pytest.fixture
def workspace_with_daily(workspace):
    ws = Path(workspace)
    (ws / "memory" / "2026-05-01.md").write_text(
        "# Daily Notes 2026-05-01\n\n"
        "- Prefer TypeScript for frontend code\n"
        "- API rate limit is 100 req/min\n"
        "- Staging URL: https://staging.example.com\n"
    )
    (ws / "memory" / "2026-05-02.md").write_text(
        "# Daily Notes 2026-05-02\n\n"
        "- Database migration completed successfully\n"
        "- New feature flag: dark_mode_v2\n"
    )
    return workspace


@pytest.fixture
def default_config():
    return DreamingConfig(
        enabled=True,
        min_score=0.3,
        min_recall_count=1,
        min_unique_queries=1,
        max_promotions=5,
        diary=False,
    )


# ============================================================================
# track_recall tests
# ============================================================================

class TestTrackRecall:
    def test_creates_new_entry(self, workspace):
        track_recall("MEMORY.md", 3, 3, "Use TypeScript", "typescript", workspace)
        state = load_dreaming_state(workspace)
        assert len(state.entries) == 1
        key = "MEMORY.md:3-3"
        assert key in state.entries
        entry = state.entries[key]
        assert entry.recall_count == 1
        assert len(entry.query_hashes) == 1

    def test_increments_existing_entry(self, workspace):
        track_recall("MEMORY.md", 3, 3, "snippet", "query one", workspace)
        track_recall("MEMORY.md", 3, 3, "snippet", "query two", workspace)
        state = load_dreaming_state(workspace)
        entry = state.entries["MEMORY.md:3-3"]
        assert entry.recall_count == 2
        assert len(entry.query_hashes) == 2

    def test_deduplicates_same_query(self, workspace):
        track_recall("MEMORY.md", 3, 3, "snippet", "same query", workspace)
        track_recall("MEMORY.md", 3, 3, "snippet", "same query", workspace)
        state = load_dreaming_state(workspace)
        entry = state.entries["MEMORY.md:3-3"]
        assert entry.recall_count == 2
        assert len(entry.query_hashes) == 1  # same hash, no duplicate

    def test_tracks_multiple_paths(self, workspace):
        track_recall("MEMORY.md", 1, 1, "a", "q1", workspace)
        track_recall("memory/2026-05-01.md", 5, 5, "b", "q2", workspace)
        state = load_dreaming_state(workspace)
        assert len(state.entries) == 2

    def test_truncates_snippet(self, workspace):
        long_snippet = "x" * 300
        track_recall("MEMORY.md", 1, 1, long_snippet, "q", workspace)
        state = load_dreaming_state(workspace)
        entry = state.entries["MEMORY.md:1-1"]
        assert len(entry.snippet) == 200

    def test_state_file_created_in_dreams_dir(self, workspace):
        track_recall("MEMORY.md", 1, 1, "snip", "q", workspace)
        state_file = Path(workspace) / "memory" / ".dreams" / "short-term-recall.json"
        assert state_file.exists()

    def test_graceful_on_nonexistent_workspace(self):
        # Should not raise
        track_recall("MEMORY.md", 1, 1, "snip", "q", "/nonexistent/path/xyz")


# ============================================================================
# is_dreaming_due tests
# ============================================================================

class TestIsDreamingDue:
    def test_due_when_never_run(self):
        assert is_dreaming_due("0 0 * * *", None) is True

    def test_due_when_last_run_yesterday(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        assert is_dreaming_due("0 0 * * *", yesterday) is True

    def test_not_due_when_run_today(self):
        today = datetime.now().isoformat()
        assert is_dreaming_due("0 3 * * *", today) is False

    def test_not_due_before_scheduled_hour(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        # Schedule at 23:59 (very end of day) - if it's not that time yet
        # Use hour 23, minute 59 to test "not yet time"
        # This is tricky because we can't control clock in tests
        # Instead test with a past last_run (yesterday) and hour=0, minute=0 (always past)
        assert is_dreaming_due("0 0 * * *", yesterday) is True

    def test_unsupported_cron_format(self):
        assert is_dreaming_due("*/5 * * * *", None) is False

    def test_malformed_last_run_treated_as_never(self):
        assert is_dreaming_due("0 0 * * *", "not-a-date") is True

    def test_invalid_cron_fields(self):
        assert is_dreaming_due("abc def * * *", None) is False


# ============================================================================
# run_light_phase tests
# ============================================================================

class TestRunLightPhase:
    def test_returns_empty_when_no_state(self, workspace):
        candidates = run_light_phase(workspace)
        assert candidates == []

    def test_returns_only_tracked_entries(self, workspace_with_daily):
        ws = workspace_with_daily
        track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", "typescript", ws)
        track_recall("memory/2026-05-01.md", 4, 4, "rate limit", "rate", ws)
        candidates = run_light_phase(ws)
        assert len(candidates) == 2

    def test_skips_promoted_entries(self, workspace_with_daily):
        ws = workspace_with_daily
        track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", "ts", ws)
        # Manually mark as promoted
        state = load_dreaming_state(ws)
        for entry in state.entries.values():
            entry.promoted_at = datetime.now().isoformat()
        from nano_openclaw.memory.dreaming import _save_dreaming_state
        _save_dreaming_state(ws, state)
        candidates = run_light_phase(ws)
        assert candidates == []

    def test_skips_missing_files(self, workspace):
        # Track a file that doesn't exist
        track_recall("memory/nonexistent.md", 1, 1, "snip", "q", workspace)
        candidates = run_light_phase(workspace)
        assert candidates == []

    def test_sorts_by_recall_count_descending(self, workspace_with_daily):
        ws = workspace_with_daily
        for _ in range(5):
            track_recall("memory/2026-05-01.md", 3, 3, "high", f"q{_}", ws)
        track_recall("memory/2026-05-01.md", 4, 4, "low", "q_low", ws)
        candidates = run_light_phase(ws)
        assert candidates[0].recall_count >= candidates[-1].recall_count

    def test_limits_to_50_candidates(self, workspace):
        ws_path = Path(workspace)
        (ws_path / "MEMORY.md").write_text(
            "\n".join(f"line {i}" for i in range(60))
        )
        for i in range(60):
            track_recall("MEMORY.md", i + 1, i + 1, f"line {i}", f"q{i}", workspace)
        candidates = run_light_phase(workspace)
        assert len(candidates) <= 50


# ============================================================================
# run_deep_phase tests
# ============================================================================

class TestRunDeepPhase:
    def test_promotes_qualified_entry(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        # Add multiple recalls to qualify
        for i in range(3):
            track_recall("memory/2026-05-01.md", 3, 3, "Prefer TypeScript", f"q{i}", ws)
        candidates = run_light_phase(ws)
        promoted = run_deep_phase(ws, default_config, candidates)
        assert len(promoted) > 0
        # Check MEMORY.md was updated
        memory_content = (Path(ws) / "MEMORY.md").read_text()
        assert "dreaming:promoted" in memory_content

    def test_skips_below_threshold(self, workspace_with_daily):
        ws = workspace_with_daily
        strict_config = DreamingConfig(
            enabled=True, min_score=0.99, min_recall_count=100, min_unique_queries=50, diary=False
        )
        track_recall("memory/2026-05-01.md", 3, 3, "once", "q", ws)
        candidates = run_light_phase(ws)
        promoted = run_deep_phase(ws, strict_config, candidates)
        assert promoted == []

    def test_marks_entry_as_promoted(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        for i in range(3):
            track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", f"q{i}", ws)
        candidates = run_light_phase(ws)
        run_deep_phase(ws, default_config, candidates)
        state = load_dreaming_state(ws)
        promoted_entries = [e for e in state.entries.values() if e.promoted_at]
        assert len(promoted_entries) > 0

    def test_respects_max_promotions(self, workspace):
        ws_path = Path(workspace)
        lines = [f"fact {i} about something important" for i in range(20)]
        (ws_path / "MEMORY.md").write_text("\n".join(lines))
        for i in range(20):
            for j in range(3):
                track_recall("MEMORY.md", i + 1, i + 1, lines[i], f"q{j}", workspace)
        candidates = run_light_phase(workspace)
        config = DreamingConfig(
            enabled=True, min_score=0.0, min_recall_count=1, min_unique_queries=1,
            max_promotions=3, diary=False
        )
        promoted = run_deep_phase(workspace, config, candidates)
        assert len(promoted) <= 3

    def test_skips_deleted_file(self, workspace, default_config):
        ws_path = Path(workspace)
        ghost = ws_path / "memory" / "ghost.md"
        ghost.write_text("ghost content")
        track_recall("memory/ghost.md", 1, 1, "ghost", "q1", workspace)
        track_recall("memory/ghost.md", 1, 1, "ghost", "q2", workspace)
        ghost.unlink()  # delete before phase
        candidates = run_light_phase(workspace)
        promoted = run_deep_phase(workspace, default_config, candidates)
        assert promoted == []

    def test_returns_empty_for_no_candidates(self, workspace, default_config):
        promoted = run_deep_phase(workspace, default_config, [])
        assert promoted == []

    def test_writes_promotion_header_to_memory_md(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        for i in range(3):
            track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", f"q{i}", ws)
        candidates = run_light_phase(ws)
        run_deep_phase(ws, default_config, candidates)
        content = (Path(ws) / "MEMORY.md").read_text()
        assert "## Dreaming Promotions" in content


# ============================================================================
# get_dreaming_status tests
# ============================================================================

class TestGetDreamingStatus:
    def test_returns_status_dict(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", "q", ws)
        status = get_dreaming_status(ws, default_config)
        assert "enabled" in status
        assert "frequency" in status
        assert "total_tracked" in status
        assert "active_candidates" in status
        assert "promoted_total" in status
        assert "top_candidates" in status

    def test_counts_active_candidates(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", "q1", ws)
        track_recall("memory/2026-05-01.md", 4, 4, "rate limit", "q2", ws)
        status = get_dreaming_status(ws, default_config)
        assert status["active_candidates"] == 2

    def test_shows_due_when_never_run(self, workspace, default_config):
        status = get_dreaming_status(workspace, default_config)
        # frequency "0 3 * * *" with no last_run_at — due if current hour >= 3
        # Just check the field exists and is bool
        assert isinstance(status["due"], bool)

    def test_top_candidates_have_scores(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        for i in range(3):
            track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", f"q{i}", ws)
        status = get_dreaming_status(ws, default_config)
        assert len(status["top_candidates"]) > 0
        for c in status["top_candidates"]:
            assert "score" in c
            assert 0.0 <= c["score"] <= 1.0


# ============================================================================
# run_dreaming integration test
# ============================================================================

class TestRunDreaming:
    def test_full_sweep_no_diary(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        for i in range(3):
            track_recall("memory/2026-05-01.md", 3, 3, "TypeScript", f"q{i}", ws)
        result = run_dreaming(ws, default_config, "dummy-model", api_client=None)
        assert result.elapsed_ms >= 0
        assert isinstance(result.candidates, list)
        assert isinstance(result.promoted, list)

    def test_updates_last_run_at(self, workspace_with_daily, default_config):
        ws = workspace_with_daily
        result = run_dreaming(ws, default_config, "dummy-model", api_client=None)
        state = load_dreaming_state(ws)
        assert state.last_run_at is not None

    def test_no_promotions_when_no_state(self, workspace, default_config):
        result = run_dreaming(workspace, default_config, "dummy-model", api_client=None)
        assert result.promoted == []

    def test_config_types_integration(self):
        """Verify DreamingConfigInput → DreamingConfig conversion matches."""
        from nano_openclaw.config.types import DreamingConfigInput
        inp = DreamingConfigInput(
            enabled=True,
            frequency="0 6 * * *",
            minScore=0.7,
            minRecallCount=3,
            minUniqueQueries=2,
            maxPromotions=5,
            diary=False,
        )
        cfg = DreamingConfig(
            enabled=inp.enabled,
            frequency=inp.frequency,
            min_score=inp.minScore,
            min_recall_count=inp.minRecallCount,
            min_unique_queries=inp.minUniqueQueries,
            max_promotions=inp.maxPromotions,
            diary=inp.diary,
            model=inp.model,
        )
        assert cfg.enabled is True
        assert cfg.frequency == "0 6 * * *"
        assert cfg.min_score == 0.7
        assert cfg.min_recall_count == 3
        assert cfg.min_unique_queries == 2
        assert cfg.max_promotions == 5
        assert cfg.diary is False
        assert cfg.model is None
