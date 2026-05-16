"""Tests for the compaction circuit breaker + exponential backoff.

Covers:
  - consecutive_failures counter increments per summarize error
  - After MAX_CONSECUTIVE_COMPACT_FAILURES the LLM is NOT called even
    though pre-prune still runs (the local trim path stays alive).
  - A successful summarize resets the counter back to 0.
  - Cooldown grows exponentially: ~60s, ~120s, ~240s on consecutive errors.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from nano_openclaw.compact import (
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    CompactionState,
    _SUMMARY_FAILURE_COOLDOWN_S,
    compact_if_needed,
)
from nano_openclaw.loop import Message


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[{"type": "text", "text": text}])


def _big_history() -> list[Message]:
    """History large enough to trigger compaction at budget=80."""
    pad = "extra context " * 6
    history: list[Message] = []
    for i in range(4):
        history.append(_text("user", f"old user {i} {pad}"))
        history.append(_text("assistant", f"old reply {i} {pad}"))
    for i in range(2):
        history.append(_text("user", f"recent {i}"))
        history.append(_text("assistant", f"recent reply {i}"))
    return history


def _failing_client() -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("upstream 503"))
    return client


# ---------------------------------------------------------------------------
# Test 1: 3 consecutive failures trip the breaker; 4th call skips the LLM
# ---------------------------------------------------------------------------


def test_circuit_opens_after_three_failures():
    state = CompactionState()
    client = _failing_client()

    # First three calls all attempt the LLM and fail.
    for i in range(MAX_CONSECUTIVE_COMPACT_FAILURES):
        # Clear cooldown so we always reach the failing call path.
        state.summary_cooldown_until = 0.0
        asyncio.run(compact_if_needed(
            _big_history(),
            budget=80,
            client=client,
            model="test",
            api="anthropic",
            recent_turns=2,
            state=state,
        ))
        assert state.consecutive_failures == i + 1

    assert client.messages.create.call_count == MAX_CONSECUTIVE_COMPACT_FAILURES
    assert state.consecutive_failures == MAX_CONSECUTIVE_COMPACT_FAILURES
    assert state.circuit_open() is True

    # Fourth call: even with cooldown cleared, the breaker keeps the LLM
    # call suppressed. Call count must NOT advance.
    state.summary_cooldown_until = 0.0
    result, summary = asyncio.run(compact_if_needed(
        _big_history(),
        budget=80,
        client=client,
        model="test",
        api="anthropic",
        recent_turns=2,
        state=state,
    ))
    assert client.messages.create.call_count == MAX_CONSECUTIVE_COMPACT_FAILURES
    assert summary is None
    # Compaction still trimmed the history — the breaker only gates the LLM.
    assert len(result) <= len(_big_history())


# ---------------------------------------------------------------------------
# Test 2: a successful call resets the counter
# ---------------------------------------------------------------------------


def test_success_resets_consecutive_failures():
    state = CompactionState()
    failing = _failing_client()

    # Two failures
    for i in range(2):
        state.summary_cooldown_until = 0.0
        asyncio.run(compact_if_needed(
            _big_history(),
            budget=80,
            client=failing,
            model="test",
            api="anthropic",
            recent_turns=2,
            state=state,
        ))
    assert state.consecutive_failures == 2

    # One success: same state, new client that returns a fake summary
    state.summary_cooldown_until = 0.0
    ok_client = MagicMock()
    ok_resp = MagicMock()
    ok_resp.content = [MagicMock(type="text", text="RECOVERED SUMMARY")]
    ok_client.messages.create = AsyncMock(return_value=ok_resp)

    _, summary = asyncio.run(compact_if_needed(
        _big_history(),
        budget=80,
        client=ok_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        state=state,
    ))

    assert summary == "RECOVERED SUMMARY"
    assert state.consecutive_failures == 0
    assert state.last_summary_error is None
    assert state.summary_cooldown_until == 0.0


# ---------------------------------------------------------------------------
# Test 3: cooldown grows exponentially (60s, 120s, 240s)
# ---------------------------------------------------------------------------


def test_cooldown_grows_exponentially():
    state = CompactionState()
    client = _failing_client()

    expected_cooldowns = [
        _SUMMARY_FAILURE_COOLDOWN_S,        # 60 after 1st failure
        _SUMMARY_FAILURE_COOLDOWN_S * 2,    # 120 after 2nd failure
        _SUMMARY_FAILURE_COOLDOWN_S * 4,    # 240 after 3rd failure
    ]

    for i, expected in enumerate(expected_cooldowns):
        state.summary_cooldown_until = 0.0
        t_before = time.monotonic()
        asyncio.run(compact_if_needed(
            _big_history(),
            budget=80,
            client=client,
            model="test",
            api="anthropic",
            recent_turns=2,
            state=state,
        ))
        t_after = time.monotonic()
        # The deadline must sit within [t_before+expected, t_after+expected]
        # — allow a small slack on the upper bound for clock drift / scheduler
        # variance during the test execution.
        slack = 2.0
        assert state.consecutive_failures == i + 1
        assert state.summary_cooldown_until >= t_before + expected - slack, (
            f"failure {i+1}: cooldown deadline {state.summary_cooldown_until} "
            f"too small for expected {expected}s after t_before={t_before}"
        )
        assert state.summary_cooldown_until <= t_after + expected + slack, (
            f"failure {i+1}: cooldown deadline {state.summary_cooldown_until} "
            f"too large for expected {expected}s after t_after={t_after}"
        )
