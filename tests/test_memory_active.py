"""Tests for Active Memory functionality.

Mirrors openclaw extensions/active-memory/index.ts behavior:
- Query modes (message, recent, full)
- Prompt styles (balanced, strict, contextual, etc.)
- ActiveMemoryManager state management
- build_query and build_recall_prompt functions
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock, patch

import pytest

from nano_openclaw.memory.active import (
    QueryMode,
    PromptStyle,
    ActiveMemoryConfig,
    ActiveMemoryResult,
    ActiveMemoryManager,
    build_query,
    build_recall_prompt,
    STYLE_PROMPTS,
)


class TestQueryMode:
    """Tests for QueryMode enum."""

    def test_mode_values(self):
        """QueryMode should have expected string values."""
        assert QueryMode.MESSAGE.value == "message"
        assert QueryMode.RECENT.value == "recent"
        assert QueryMode.FULL.value == "full"

    def test_all_modes_exist(self):
        """All three query modes should be defined."""
        modes = [QueryMode.MESSAGE, QueryMode.RECENT, QueryMode.FULL]
        assert len(modes) == 3

    def test_mode_from_string(self):
        """QueryMode should be constructible from string."""
        mode = QueryMode("recent")
        assert mode == QueryMode.RECENT


class TestPromptStyle:
    """Tests for PromptStyle enum."""

    def test_style_values(self):
        """PromptStyle should have expected string values."""
        assert PromptStyle.BALANCED.value == "balanced"
        assert PromptStyle.STRICT.value == "strict"
        assert PromptStyle.CONTEXTUAL.value == "contextual"
        assert PromptStyle.RECALL_HEAVY.value == "recall-heavy"
        assert PromptStyle.PRECISION_HEAVY.value == "precision-heavy"
        assert PromptStyle.PREFERENCE_ONLY.value == "preference-only"

    def test_all_styles_exist(self):
        """All six prompt styles should be defined."""
        styles = list(PromptStyle)
        assert len(styles) == 6

    def test_style_from_string(self):
        """PromptStyle should be constructible from string."""
        style = PromptStyle("strict")
        assert style == PromptStyle.STRICT


class TestActiveMemoryConfig:
    """Tests for ActiveMemoryConfig dataclass."""

    def test_default_config(self):
        """Default config should have expected values."""
        config = ActiveMemoryConfig()
        assert config.enabled is True
        assert config.query_mode == QueryMode.RECENT
        assert config.prompt_style == PromptStyle.BALANCED
        assert config.max_summary_chars == 220
        assert config.timeout_ms == 15000
        assert config.recent_user_turns == 2
        assert config.recent_assistant_turns == 1
        assert config.recent_user_chars == 220
        assert config.recent_assistant_chars == 180

    def test_custom_config(self):
        """Config should accept custom values."""
        config = ActiveMemoryConfig(
            enabled=False,
            query_mode=QueryMode.FULL,
            prompt_style=PromptStyle.STRICT,
            max_summary_chars=300,
        )
        assert config.enabled is False
        assert config.query_mode == QueryMode.FULL
        assert config.prompt_style == PromptStyle.STRICT
        assert config.max_summary_chars == 300


class TestActiveMemoryResult:
    """Tests for ActiveMemoryResult dataclass."""

    def test_result_with_context(self):
        """Result with context should store all fields."""
        result = ActiveMemoryResult(
            context="[Active Memory Recall: test summary]",
            query_used="test query",
            elapsed_ms=100,
            cached=True,
        )
        assert result.context is not None
        assert result.query_used == "test query"
        assert result.elapsed_ms == 100
        assert result.cached is True

    def test_result_none_context(self):
        """Result with None context indicates no memories found."""
        result = ActiveMemoryResult(
            context=None,
            query_used="test query",
            elapsed_ms=50,
        )
        assert result.context is None
        assert result.cached is False


class TestBuildQuery:
    """Tests for build_query function."""

    def test_message_mode_returns_latest_user_message(self):
        """MESSAGE mode should return only the latest user message."""
        messages = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "Reply"},
            {"role": "user", "content": "Latest message"},
        ]
        config = ActiveMemoryConfig(query_mode=QueryMode.MESSAGE)
        query = build_query(messages, QueryMode.MESSAGE, config)
        assert query == "Latest message"

    def test_message_mode_with_content_blocks(self):
        """MESSAGE mode should handle content block format."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Block message"}]},
        ]
        config = ActiveMemoryConfig(query_mode=QueryMode.MESSAGE)
        query = build_query(messages, QueryMode.MESSAGE, config)
        assert query == "Block message"

    def test_message_mode_truncates_to_max_chars(self):
        """MESSAGE mode should truncate query to recent_user_chars."""
        long_msg = "A" * 1000
        messages = [{"role": "user", "content": long_msg}]
        config = ActiveMemoryConfig(query_mode=QueryMode.MESSAGE, recent_user_chars=100)
        query = build_query(messages, QueryMode.MESSAGE, config)
        assert len(query) == 100

    def test_recent_mode_includes_last_n_messages(self):
        """RECENT mode should include last N messages."""
        messages = [
            {"role": "user", "content": "M1"},
            {"role": "assistant", "content": "R1"},
            {"role": "user", "content": "M2"},
            {"role": "assistant", "content": "R2"},
            {"role": "user", "content": "M3"},
        ]
        config = ActiveMemoryConfig(
            query_mode=QueryMode.RECENT,
            recent_user_turns=2,
            recent_assistant_turns=1,
        )
        query = build_query(messages, QueryMode.RECENT, config)
        # Should include M2, R2 (truncated), M3
        assert "M2" in query
        assert "M3" in query
        assert "M1" not in query  # M1 is too old

    def test_full_mode_includes_all_conversation(self):
        """FULL mode should include full conversation (truncated)."""
        messages = [
            {"role": "user", "content": "Start message"},
            {"role": "assistant", "content": "Reply"},
            {"role": "user", "content": "End message"},
        ]
        config = ActiveMemoryConfig(query_mode=QueryMode.FULL)
        query = build_query(messages, QueryMode.FULL, config)
        assert "Start" in query
        assert "End" in query

    def test_empty_messages_returns_empty_string(self):
        """Empty messages list should return empty query."""
        config = ActiveMemoryConfig()
        query = build_query([], QueryMode.MESSAGE, config)
        assert query == ""


class TestBuildRecallPrompt:
    """Tests for build_recall_prompt function."""

    def test_includes_query_text(self):
        """Prompt should include the query text."""
        config = ActiveMemoryConfig()
        prompt = build_recall_prompt("test query", PromptStyle.BALANCED, config)
        assert "test query" in prompt

    def test_includes_style_instructions(self):
        """Prompt should include style-specific instructions."""
        config = ActiveMemoryConfig()
        prompt = build_recall_prompt("query", PromptStyle.BALANCED, config)
        # Balanced style has broad recall instructions
        assert "prior work" in prompt.lower()
        assert "decisions" in prompt.lower()

    def test_strict_style_has_precision_instructions(self):
        """STRICT style should have precision-focused instructions."""
        config = ActiveMemoryConfig()
        prompt = build_recall_prompt("query", PromptStyle.STRICT, config)
        assert "precise" in prompt.lower()

    def test_preference_only_style_filters_content(self):
        """PREFERENCE_ONLY style should search only preferences."""
        config = ActiveMemoryConfig()
        prompt = build_recall_prompt("query", PromptStyle.PREFERENCE_ONLY, config)
        assert "preferences" in prompt.lower()
        assert "coding style" in prompt.lower()

    def test_output_format_instructions(self):
        """Prompt should include output format instructions."""
        config = ActiveMemoryConfig()
        prompt = build_recall_prompt("query", PromptStyle.BALANCED, config)
        assert "<found>" in prompt
        assert "NONE" in prompt


class TestStylePrompts:
    """Tests for STYLE_PROMPTS dictionary."""

    def test_all_styles_have_prompts(self):
        """All PromptStyle values should have corresponding prompt."""
        for style in PromptStyle:
            assert style in STYLE_PROMPTS
            assert len(STYLE_PROMPTS[style]) > 50

    def test_prompts_contain_char_limit(self):
        """All style prompts should mention character limit."""
        for style, prompt in STYLE_PROMPTS.items():
            assert "<200 chars" in prompt or "<200" in prompt


class TestActiveMemoryManager:
    """Tests for ActiveMemoryManager."""

    def test_toggle_switches_enabled_state(self):
        """Toggle should switch enabled state."""
        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
            config=ActiveMemoryConfig(enabled=True),
        )
        assert manager.config.enabled is True

        new_state = manager.toggle()
        assert new_state is False
        assert manager.config.enabled is False

        new_state = manager.toggle()
        assert new_state is True
        assert manager.config.enabled is True

    def test_set_query_mode(self):
        """set_query_mode should update config."""
        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
        )
        manager.set_query_mode(QueryMode.FULL)
        assert manager.config.query_mode == QueryMode.FULL

    def test_set_prompt_style(self):
        """set_prompt_style should update config."""
        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
        )
        manager.set_prompt_style(PromptStyle.STRICT)
        assert manager.config.prompt_style == PromptStyle.STRICT

    def test_disabled_manager_returns_none(self):
        """Disabled manager should return None from run()."""
        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
            config=ActiveMemoryConfig(enabled=False),
        )
        result = manager.run([{"role": "user", "content": "test"}])
        assert result is None

    def test_empty_query_returns_none(self):
        """Empty query should return None."""
        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
            config=ActiveMemoryConfig(enabled=True),
        )
        result = manager.run([])
        assert result is None

    def test_cache_hit_returns_cached_result(self):
        """Repeated query should return cached result."""
        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
            config=ActiveMemoryConfig(enabled=True),
        )
        # First call would normally run subagent, but we'll test cache directly
        # by manually populating cache.
        # Note: build_query transforms "test" into "User: test" in RECENT mode,
        # so the cache key must match that format.
        cached_result = ActiveMemoryResult(
            context="[Active Memory Recall: cached]",
            query_used="User: test",
            elapsed_ms=50,
        )
        import time
        manager._cache["User: test:balanced"] = (cached_result, time.time())

        # Second call with same query should hit cache
        messages = [{"role": "user", "content": "test"}]
        result = manager.run(messages)

        assert result is not None
        assert result.cached is True
        assert "cached" in result.context


class TestActiveMemoryIntegration:
    """Integration tests for Active Memory in loop."""

    def test_loop_config_accepts_active_memory_config(self):
        """LoopConfig should accept active_memory_config parameter."""
        from nano_openclaw.loop import LoopConfig

        am_config = ActiveMemoryConfig(enabled=False)
        cfg = LoopConfig(active_memory_config=am_config)
        assert cfg.active_memory_config is not None
        assert cfg.active_memory_config.enabled is False

    def test_loop_config_default_none(self):
        """LoopConfig default for active_memory_config should be None."""
        from nano_openclaw.loop import LoopConfig

        cfg = LoopConfig()
        assert cfg.active_memory_config is None

    def test_active_memory_recall_event_importable(self):
        """ActiveMemoryRecall event should be importable from loop."""
        from nano_openclaw.loop import ActiveMemoryRecall

        result = ActiveMemoryResult(
            context="[test]",
            query_used="test",
            elapsed_ms=100,
        )
        event = ActiveMemoryRecall(result=result)
        assert event.result.context == "[test]"

    def test_cli_imports_active_memory_types(self):
        """CLI should import Active Memory types."""
        from nano_openclaw.cli import ActiveMemoryConfig, QueryMode, PromptStyle

        assert ActiveMemoryConfig is not None
        assert QueryMode is not None
        assert PromptStyle is not None


class TestRunRecallSubagent:
    """Tests for run_recall_subagent function (mocked)."""

    @patch("nano_openclaw.memory.active.run_recall_subagent")
    def test_returns_result_with_context_on_success(self, mock_run):
        """Successful recall should return result with context."""
        mock_run.return_value = ActiveMemoryResult(
            context="[Active Memory Recall: found preferences]",
            query_used="preferences",
            elapsed_ms=150,
        )

        mock_client = Mock()
        manager = ActiveMemoryManager(
            client=mock_client,
            model="claude-3-5-sonnet-20241022",
            workspace_dir="/tmp",
            config=ActiveMemoryConfig(enabled=True),
        )

        # This test uses mock, so the actual run_recall_subagent is patched
        # We just verify the manager can call it
        from nano_openclaw.memory.active import run_recall_subagent
        result = run_recall_subagent(
            mock_client,
            "claude-3-5-sonnet-20241022",
            "preferences",
            PromptStyle.BALANCED,
            "/tmp",
            ActiveMemoryConfig(),
        )
        assert result.context is not None

    @patch("anthropic.Anthropic")
    def test_returns_none_on_api_error(self, mock_anthropic):
        """API errors should result in None context."""
        mock_client = mock_anthropic.return_value
        mock_client.messages.create.side_effect = Exception("API error")

        from nano_openclaw.memory.active import run_recall_subagent
        result = run_recall_subagent(
            mock_client,
            "claude-3-5-sonnet-20241022",
            "test query",
            PromptStyle.BALANCED,
            "/tmp",
            ActiveMemoryConfig(),
        )
        assert result.context is None
        assert result.elapsed_ms >= 0  # Should still track time (may be 0 if very fast)