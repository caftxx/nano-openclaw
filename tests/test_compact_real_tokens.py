"""Tests for the real-token (``last_prompt_tokens``) compaction trigger.

Covers:
  - Short history whose estimate is well under threshold STILL passes the
    entry-point trigger when the upstream-reported prompt size exceeds the
    threshold. Catches the cached-session blind spot where char-based
    estimation massively under-counts the actual prompt the model saw.
  - Conversely, a verbose-looking history whose actual prompt size is small
    (e.g. heavy prompt caching) must short-circuit at the entry point —
    the real number wins and we return immediately without calling the
    upstream summarizer or running prune.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import nano_openclaw.compact as compact_mod
from nano_openclaw.compact import (
    CompactionState,
    compact_if_needed,
    estimate_tokens,
)
from nano_openclaw.loop import Message


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[{"type": "text", "text": text}])


def _summary_client() -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text="REAL TOKEN SUMMARY")]
    client.messages.create = AsyncMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# Test 1: short history but high real prompt -> compaction triggers
# ---------------------------------------------------------------------------


def test_real_tokens_high_triggers_compaction_when_estimate_is_low(monkeypatch):
    """High ``last_prompt_tokens`` must drag the trigger past the entry gate
    even when the local char-based estimate would short-circuit early.

    To isolate the entry-gate decision we pin ``estimate_tokens`` to a stable
    above-threshold value so the post-prune check (which deliberately uses
    estimate rather than the real number — see compact.py:932 comment) lets
    the LLM call fire. The asymmetry we're proving is: WITHOUT
    ``last_prompt_tokens`` the entry gate would already have returned at the
    top of the function on the raw history; WITH it the function proceeds.
    """
    history = []
    for i in range(4):
        history.append(_text("user", f"u{i}"))
        history.append(_text("assistant", f"a{i}"))
    history.append(_text("user", "latest ask"))
    history.append(_text("assistant", "latest reply"))

    threshold = int(1000 * 0.8)
    raw_estimate = estimate_tokens(history)
    assert raw_estimate < threshold, (
        f"setup precondition failed: raw estimate {raw_estimate} should be "
        f"below threshold {threshold} so the test is meaningful"
    )

    # Sanity check: WITHOUT last_prompt_tokens, compact_if_needed should
    # short-circuit at the top (current_tokens = raw_estimate < threshold).
    # Use a tracking client to prove the LLM never got called.
    client_no_real = _summary_client()
    _, summary_no_real = asyncio.run(compact_if_needed(
        list(history),
        budget=1000,
        client=client_no_real,
        model="test",
        api="anthropic",
        recent_turns=2,
        state=CompactionState(),
    ))
    assert summary_no_real is None
    client_no_real.messages.create.assert_not_called()

    # With last_prompt_tokens high the entry gate passes. Pin estimate_tokens
    # to >= threshold so the post-prune branch stays hot and we end up at
    # the LLM call — that's what proves the entry gate let us through.
    monkeypatch.setattr(compact_mod, "estimate_tokens", lambda _msgs: 900)

    client = _summary_client()
    state = CompactionState()
    _, summary = asyncio.run(compact_if_needed(
        list(history),
        budget=1000,
        client=client,
        model="test",
        api="anthropic",
        recent_turns=2,
        last_prompt_tokens=900,
        state=state,
    ))
    assert summary == "REAL TOKEN SUMMARY"
    client.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: verbose-looking history but low real prompt -> NO compaction
# ---------------------------------------------------------------------------


def test_real_tokens_low_suppresses_compaction_despite_large_estimate():
    # Verbose history: characters galore so estimate_tokens(history) climbs
    # above the threshold. We rely on the real prompt count overriding.
    pad = "lorem ipsum dolor sit amet " * 50
    history = []
    for i in range(4):
        history.append(_text("user", f"u{i} {pad}"))
        history.append(_text("assistant", f"a{i} {pad}"))

    estimated = estimate_tokens(history)
    threshold = int(800 * 0.8)
    assert estimated >= threshold, (
        f"setup precondition failed: estimate {estimated} should be at or "
        f"above threshold {threshold} so the test is meaningful"
    )

    client = _summary_client()
    state = CompactionState()
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=800,
        client=client,
        model="test",
        api="anthropic",
        recent_turns=2,
        # Real upstream usage says the actual prompt is only 100 tokens
        # (e.g. heavy cache reads aren't billable). Trigger must defer to
        # this real number and skip compaction.
        last_prompt_tokens=100,
        state=state,
    ))

    assert summary is None
    client.messages.create.assert_not_called()
    # History returned untouched.
    assert len(result) == len(history)
