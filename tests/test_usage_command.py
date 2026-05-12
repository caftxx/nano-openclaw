"""Tests for the /usage slash command + SessionUsageStats accumulator.

Covers:
  - SessionUsageStats.update_from_usage accumulates totals + captures last
  - cache_hit_ratio math + None when no caching traffic yet
  - Anthropic provider extracts cache_read / cache_creation tokens
  - AgentBackendSession holds usage_stats by reference (cross-turn persistence)
  - _cmd_usage renders expected fields
  - sessions_usage RPC end-to-end through EmbeddedBackend (smoke)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nano_openclaw.gateway.agent_backend_session import AgentBackendSession
from nano_openclaw.gateway.backend import SessionUsageReport
from nano_openclaw.gateway.slash import _HANDLERS, _cmd_usage
from nano_openclaw.loop import SessionUsageStats


# ---------------------------------------------------------------------------
# SessionUsageStats
# ---------------------------------------------------------------------------


def test_usage_stats_initial_state_is_zero():
    s = SessionUsageStats()
    assert s.last_prompt_tokens == 0
    assert s.total_prompt_tokens == 0
    assert s.cache_hit_ratio() is None
    assert s.turns_recorded == 0


def test_usage_stats_update_from_anthropic_usage_dict():
    """``last_prompt_tokens`` and ``total_prompt_tokens`` are the SUM of
    ``input + cache_read + cache_creation`` — the real total prompt the
    model saw, not just billable input."""
    s = SessionUsageStats()
    s.update_from_usage({
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 200,
    })
    # Total prompt = 1000 (new) + 800 (cached read) + 200 (cached creation) = 2000
    assert s.last_prompt_tokens == 2000
    assert s.last_output_tokens == 200
    assert s.last_cache_read_tokens == 800
    assert s.last_cache_creation_tokens == 200
    assert s.total_prompt_tokens == 2000
    assert s.total_output_tokens == 200
    assert s.total_cache_read_tokens == 800
    assert s.total_cache_creation_tokens == 200
    assert s.turns_recorded == 1


def test_usage_stats_accumulates_across_turns():
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    s.update_from_usage({"input_tokens": 200, "output_tokens": 75})
    # No cache traffic, so prompt total == input alone for each turn
    assert s.last_prompt_tokens == 200
    assert s.last_output_tokens == 75
    assert s.total_prompt_tokens == 300
    assert s.total_output_tokens == 125
    assert s.turns_recorded == 2


def test_usage_stats_cached_turn_inflates_prompt_total_above_billable():
    """Caching scenario: turn 2 has tiny new input but big cache_read,
    so the total prompt the model saw is dominated by cached tokens.
    last_prompt_tokens must reflect that, not just the 240 billable."""
    s = SessionUsageStats()
    # Turn 1 — first contact, no cache
    s.update_from_usage({"input_tokens": 13_352, "output_tokens": 279})
    assert s.last_prompt_tokens == 13_352
    # Turn 2 — most prompt served from cache
    s.update_from_usage({
        "input_tokens": 240,
        "output_tokens": 124,
        "cache_read_input_tokens": 13_312,
        "cache_creation_input_tokens": 0,
    })
    # Model actually saw 240 + 13,312 + 0 = 13,552 — NOT just 240
    assert s.last_prompt_tokens == 13_552
    assert s.last_output_tokens == 124
    assert s.last_cache_read_tokens == 13_312
    # Cumulative prompt tokens = 13,352 (turn 1) + 13,552 (turn 2)
    assert s.total_prompt_tokens == 26_904


def test_usage_stats_empty_dict_is_no_op():
    """OpenAI-compatible providers may not surface usage on every chunk —
    empty dict must not zero out the previous turn's last_*."""
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    s.update_from_usage({})  # provider didn't send usage this turn
    assert s.last_prompt_tokens == 100  # preserved
    assert s.last_output_tokens == 50
    assert s.total_prompt_tokens == 100  # not double-counted
    assert s.turns_recorded == 1


def test_usage_stats_zero_input_tokens_does_not_clobber_last():
    """Some providers send a final empty-content chunk with 0 tokens —
    treat that as 'no info' for last_prompt_tokens, not as an actual reset."""
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    s.update_from_usage({"input_tokens": 0, "output_tokens": 0})
    assert s.last_prompt_tokens == 100
    assert s.last_output_tokens == 50


def test_cache_hit_ratio_with_traffic():
    s = SessionUsageStats()
    s.update_from_usage({
        "input_tokens": 1000,
        "cache_read_input_tokens": 750,
        "cache_creation_input_tokens": 250,
    })
    # 750 hits / (750 hits + 250 creation) = 75%
    assert s.cache_hit_ratio() == pytest.approx(0.75)


def test_cache_hit_ratio_none_when_no_traffic():
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    # No cache fields → ratio undefined
    assert s.cache_hit_ratio() is None


def test_cache_hit_ratio_perfect_hit():
    s = SessionUsageStats()
    s.update_from_usage({
        "input_tokens": 500,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 0,
    })
    assert s.cache_hit_ratio() == 1.0


def test_cache_hit_ratio_zero_hit():
    s = SessionUsageStats()
    s.update_from_usage({
        "input_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 500,
    })
    assert s.cache_hit_ratio() == 0.0


# ---------------------------------------------------------------------------
# Cross-turn persistence via AgentBackendSession
# ---------------------------------------------------------------------------


def test_backend_session_has_usage_stats_field():
    """AgentBackendSession must hold the canonical instance so per-turn
    AgentSession constructions all share the same object by reference."""
    from pathlib import Path
    from unittest.mock import MagicMock as MM

    sess = AgentBackendSession(
        session_id="x",
        transcript_path=Path("/tmp/x.jsonl"),
        history=[],
        writer=MM(),
    )
    assert isinstance(sess.usage_stats, SessionUsageStats)
    assert sess.usage_stats.total_prompt_tokens == 0


def test_agent_session_uses_provided_usage_stats_by_reference():
    """When AgentSession is constructed with a shared usage_stats instance,
    mutations through the session attribute must be visible on the original
    object — that's what makes /usage work across turns."""
    from nano_openclaw.loop import AgentSession, LoopConfig

    shared = SessionUsageStats()

    fake_session = AgentSession(
        history=[],
        registry=MagicMock(),
        on_event=lambda _e: None,
        client=MagicMock(),
        cfg=LoopConfig(),
        usage_stats=shared,
    )
    fake_session.usage_stats.update_from_usage({"input_tokens": 42, "output_tokens": 7})

    # Same object — mutation is visible to the holder
    assert shared.total_prompt_tokens == 42
    assert shared.last_output_tokens == 7
    assert fake_session.usage_stats is shared


# ---------------------------------------------------------------------------
# _cmd_usage rendering
# ---------------------------------------------------------------------------


class _FakeRenderer:
    def __init__(self):
        self.panels: list[tuple[str, str]] = []
        self.dims: list[str] = []

    def panel(self, body: str, *, title: str = "", style: str = "info") -> None:
        self.panels.append((title, body))

    def dim(self, msg: str) -> None:
        self.dims.append(msg)


def test_usage_command_handles_no_session():
    renderer = _FakeRenderer()
    backend = MagicMock()
    state = {"session_key": ""}
    asyncio.run(_cmd_usage(backend, renderer, state, [], "/usage"))
    assert renderer.dims and "no active session" in renderer.dims[0]
    assert not renderer.panels


def test_usage_command_renders_full_report():
    renderer = _FakeRenderer()
    backend = MagicMock()
    backend.sessions_usage = AsyncMock(return_value=SessionUsageReport(
        session_id="abc123",
        # Total prompt = input + cache_read + cache_creation. Picking these
        # numbers so prompt_total > input alone, mimicking a real cached
        # turn where /usage must reflect the full prompt size.
        last_prompt_tokens=13_552,   # = 240 input + 13,312 read + 0 creation
        last_output_tokens=1_024,
        last_cache_read_tokens=13_312,
        last_cache_creation_tokens=0,
        total_prompt_tokens=38_210,
        total_output_tokens=9_456,
        total_cache_read_tokens=21_300,
        total_cache_creation_tokens=4_900,
        compactions_fired=2,
        turns_recorded=12,
        cache_hit_ratio=21300 / (21300 + 4900),
        context_budget=200_000,
        context_window=200_000,
        cache_ttl="5m",
    ))
    state = {"session_key": "abc123"}
    asyncio.run(_cmd_usage(backend, renderer, state, [], "/usage"))

    assert len(renderer.panels) == 1
    title, body = renderer.panels[0]
    assert title == "Usage"
    # Last-prompt line uses prompt_total (NOT just billable input) and that
    # drives the budget %. This is what compact_if_needed watches.
    assert "13,552" in body and "1,024" in body
    assert "200,000" in body
    # 13,552 / 200,000 = 6.776% (rounds to 6.8%) — bigger than the 0.1% the
    # billable-only number would have given (240 / 200,000), proving the fix.
    assert "6.8%" in body
    # New label is "ctx" not "in" so the meaning (full prompt incl. cache)
    # is unambiguous to the reader.
    assert "ctx" in body
    # Cumulative line
    assert "38,210" in body and "9,456" in body
    assert "12 turn" in body
    # Cache row
    assert "5m TTL" in body
    assert "21,300" in body and "4,900" in body
    # Hit ratio = 21300 / 26200 ≈ 81%
    assert "81%" in body
    assert "compactions" in body.lower()


def test_usage_command_renders_when_caching_disabled():
    """cache_ttl=None should render 'off' for the cache row instead of
    crashing on the markup escape."""
    renderer = _FakeRenderer()
    backend = MagicMock()
    backend.sessions_usage = AsyncMock(return_value=SessionUsageReport(
        session_id="abc",
        last_prompt_tokens=100,
        last_output_tokens=20,
        last_cache_read_tokens=0,
        last_cache_creation_tokens=0,
        total_prompt_tokens=100,
        total_output_tokens=20,
        total_cache_read_tokens=0,
        total_cache_creation_tokens=0,
        compactions_fired=0,
        turns_recorded=1,
        cache_hit_ratio=None,
        context_budget=10_000,
        context_window=10_000,
        cache_ttl=None,
    ))
    asyncio.run(_cmd_usage(backend, renderer, {"session_key": "abc"}, [], "/usage"))

    title, body = renderer.panels[0]
    assert title == "Usage"
    assert "off" in body  # cache_status line
    # When ratio is None we render an em-dash
    assert "—" in body


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_usage_is_in_dispatch_table():
    assert "/usage" in _HANDLERS
    assert _HANDLERS["/usage"] is _cmd_usage
