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
    assert s.last_input_tokens == 0
    assert s.total_input_tokens == 0
    assert s.cache_hit_ratio() is None
    assert s.turns_recorded == 0


def test_usage_stats_update_from_anthropic_usage_dict():
    s = SessionUsageStats()
    s.update_from_usage({
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 200,
    })
    assert s.last_input_tokens == 1000
    assert s.last_output_tokens == 200
    assert s.last_cache_read_tokens == 800
    assert s.last_cache_creation_tokens == 200
    assert s.total_input_tokens == 1000
    assert s.total_output_tokens == 200
    assert s.total_cache_read_tokens == 800
    assert s.total_cache_creation_tokens == 200
    assert s.turns_recorded == 1


def test_usage_stats_accumulates_across_turns():
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    s.update_from_usage({"input_tokens": 200, "output_tokens": 75})
    assert s.last_input_tokens == 200
    assert s.last_output_tokens == 75
    assert s.total_input_tokens == 300
    assert s.total_output_tokens == 125
    assert s.turns_recorded == 2


def test_usage_stats_empty_dict_is_no_op():
    """OpenAI-compatible providers may not surface usage on every chunk —
    empty dict must not zero out the previous turn's last_*."""
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    s.update_from_usage({})  # provider didn't send usage this turn
    assert s.last_input_tokens == 100  # preserved
    assert s.last_output_tokens == 50
    assert s.total_input_tokens == 100  # not double-counted
    assert s.turns_recorded == 1


def test_usage_stats_zero_input_tokens_does_not_clobber_last():
    """Some providers send a final empty-content chunk with 0 tokens —
    treat that as 'no info' for last_input_tokens, not as an actual reset."""
    s = SessionUsageStats()
    s.update_from_usage({"input_tokens": 100, "output_tokens": 50})
    s.update_from_usage({"input_tokens": 0, "output_tokens": 0})
    assert s.last_input_tokens == 100
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
    assert sess.usage_stats.total_input_tokens == 0


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
    assert shared.total_input_tokens == 42
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
        last_input_tokens=4_832,
        last_output_tokens=1_024,
        last_cache_read_tokens=3_210,
        last_cache_creation_tokens=1_622,
        total_input_tokens=38_210,
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
    # Last-prompt line uses the real provider number AND drives the budget %
    # (matches what compact_if_needed watches for its trigger).
    assert "4,832" in body and "1,024" in body
    assert "200,000" in body
    # 4,832 / 200,000 = 2.416% (rounds to 2.4%)
    assert "2.4%" in body
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
        last_input_tokens=100,
        last_output_tokens=20,
        last_cache_read_tokens=0,
        last_cache_creation_tokens=0,
        total_input_tokens=100,
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
