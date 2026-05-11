"""Tests for Stage 3 summary upgrades.

Covers:
  - _build_summary_prompt selects scratch vs. iterative-update mode
  - Structured template is present in the prompt sent to the LLM
  - _previous_summary is threaded through compact_if_needed via CompactionState
  - On summarizer failure: cooldown set + last_summary_error recorded,
    history is still trimmed (with placeholder summary)
  - In cooldown: LLM not called, but pre-prune still runs
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from nano_openclaw.compact import (
    SUMMARY_PREFIX,
    CompactionState,
    _build_summary_prompt,
    _SUMMARIZER_PREAMBLE,
    _SUMMARY_FAILURE_COOLDOWN_S,
    _SUMMARY_TEMPLATE,
    compact_if_needed,
    summarize_history,
)
from nano_openclaw.loop import Message


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[{"type": "text", "text": text}])


# ---------------------------------------------------------------------------
# _build_summary_prompt
# ---------------------------------------------------------------------------


def test_scratch_prompt_includes_template_and_preamble():
    prompt = _build_summary_prompt("[USER]: hi\n[ASSISTANT]: hello")
    assert _SUMMARIZER_PREAMBLE in prompt
    assert "## Active Task" in prompt
    assert "## Completed Actions" in prompt
    assert "TURNS TO SUMMARIZE:" in prompt
    # Scratch mode should NOT mention previous summary
    assert "PREVIOUS SUMMARY:" not in prompt


def test_iterative_prompt_carries_previous_summary():
    prev = "## Active Task\nUser asked to refactor auth.\n\n## Goal\nMove to JWT."
    prompt = _build_summary_prompt("[USER]: next ask", previous_summary=prev)
    assert "PREVIOUS SUMMARY:" in prompt
    assert prev in prompt
    assert "Update the summary" in prompt
    assert "## Active Task" in prompt  # template still appended


def test_template_keeps_active_task_first():
    """The most important field has to come before other sections so models
    that truncate output don't drop it."""
    idx_active = _SUMMARY_TEMPLATE.index("## Active Task")
    idx_goal = _SUMMARY_TEMPLATE.index("## Goal")
    idx_remaining = _SUMMARY_TEMPLATE.index("## Remaining Work")
    assert idx_active < idx_goal < idx_remaining


# ---------------------------------------------------------------------------
# summarize_history wiring
# ---------------------------------------------------------------------------


def test_summarize_history_passes_previous_summary_to_llm():
    history = [_text("user", "hi"), _text("assistant", "hello")]

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="updated summary")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    result = asyncio.run(summarize_history(
        history,
        client=mock_client,
        model="claude-test",
        api="anthropic",
        previous_summary="OLD SUMMARY GOES HERE",
    ))

    assert result == "updated summary"
    call_args = mock_client.messages.create.call_args
    sent_prompt = call_args.kwargs["messages"][0]["content"]
    assert "PREVIOUS SUMMARY:" in sent_prompt
    assert "OLD SUMMARY GOES HERE" in sent_prompt


def test_summarize_history_uses_scratch_template_without_previous():
    history = [_text("user", "hi")]

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="fresh summary")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    asyncio.run(summarize_history(
        history,
        client=mock_client,
        model="claude-test",
        api="anthropic",
    ))

    sent_prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "TURNS TO SUMMARIZE:" in sent_prompt
    assert "PREVIOUS SUMMARY:" not in sent_prompt


# ---------------------------------------------------------------------------
# CompactionState iterative behaviour
# ---------------------------------------------------------------------------


def test_first_compaction_stores_summary_into_state():
    history = []
    pad = "extra context " * 6
    for i in range(4):
        history.append(_text("user", f"old user {i} {pad}"))
        history.append(_text("assistant", f"old reply {i} {pad}"))
    for i in range(2):
        history.append(_text("user", f"recent {i}"))
        history.append(_text("assistant", f"recent reply {i}"))

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="FIRST SUMMARY OUTPUT")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    state = CompactionState()
    assert state.previous_summary is None

    _, summary = asyncio.run(compact_if_needed(
        history,
        budget=80,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        state=state,
    ))

    assert summary == "FIRST SUMMARY OUTPUT"
    # State now carries the summary for the next iterative update
    assert state.previous_summary == "FIRST SUMMARY OUTPUT"
    assert state.last_summary_error is None
    assert state.summary_cooldown_until == 0.0


def test_second_compaction_passes_previous_summary_to_llm():
    """The second compaction in a session asks the LLM to UPDATE the prior
    summary rather than rewrite from scratch."""
    pad = "extra context " * 6
    history = []
    for i in range(4):
        history.append(_text("user", f"old user {i} {pad}"))
        history.append(_text("assistant", f"old reply {i} {pad}"))
    for i in range(2):
        history.append(_text("user", f"recent {i}"))
        history.append(_text("assistant", f"recent reply {i}"))

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="UPDATED SUMMARY")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    state = CompactionState(previous_summary="EXISTING SUMMARY FROM EARLIER COMPACTION")

    asyncio.run(compact_if_needed(
        history,
        budget=80,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        state=state,
    ))

    # LLM received the iterative-update prompt
    sent_prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "PREVIOUS SUMMARY:" in sent_prompt
    assert "EXISTING SUMMARY FROM EARLIER COMPACTION" in sent_prompt
    # State was updated to the new summary
    assert state.previous_summary == "UPDATED SUMMARY"


# ---------------------------------------------------------------------------
# Failure cooldown
# ---------------------------------------------------------------------------


def test_summarizer_failure_sets_cooldown_and_uses_placeholder():
    pad = "extra context " * 6
    history = []
    for i in range(4):
        history.append(_text("user", f"old user {i} {pad}"))
        history.append(_text("assistant", f"old reply {i} {pad}"))
    for i in range(2):
        history.append(_text("user", f"recent {i}"))
        history.append(_text("assistant", f"recent reply {i}"))

    # Mock raises on every call
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("upstream 503"))

    state = CompactionState()
    t_before = time.monotonic()

    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=80,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        state=state,
    ))

    # LLM was attempted exactly once (the failure recorded)
    assert mock_client.messages.create.call_count == 1
    # Summary returned as None to caller
    assert summary is None
    # State now in cooldown ~now+60s
    assert state.summary_cooldown_until >= t_before + _SUMMARY_FAILURE_COOLDOWN_S - 1
    assert state.last_summary_error is not None
    assert "upstream 503" in state.last_summary_error
    # History still trimmed; placeholder summary is at result[0]
    first_text = result[0].content[0]["text"]
    assert first_text.startswith(SUMMARY_PREFIX)
    assert "dropped without summary" in first_text


def test_cooldown_skips_llm_but_still_prunes():
    """In cooldown, compact_if_needed must NOT call the LLM, but the cheap
    prune pass should still run (it's local and never fails)."""
    big_blob = "DUP " * 60
    history = [
        _text("user", "first"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "echo"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": big_blob}]},
        ]),
        _text("assistant", "ok"),
        _text("user", "next"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "echo"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "t2",
             "content": [{"type": "text", "text": big_blob}]},  # identical
        ]),
        _text("assistant", "still ok"),
    ]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()  # must NOT be called

    state = CompactionState(summary_cooldown_until=time.monotonic() + 120)
    assert state.in_cooldown()

    # last_input_tokens forces trigger past pre-prune check; prune dedupes
    # the two identical tool_results.
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=300,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        last_input_tokens=300,
        state=state,
    ))

    assert summary is None
    # LLM not called — we're in cooldown
    mock_client.messages.create.assert_not_called()
    # And dedupe DID run: at least one tool_result was replaced with the
    # back-reference text.
    saw_duplicate_marker = False
    for msg in result:
        for block in msg.content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content", [])
                if isinstance(content, list):
                    for sub in content:
                        if isinstance(sub, dict) and "Duplicate tool output" in sub.get("text", ""):
                            saw_duplicate_marker = True
    assert saw_duplicate_marker


def test_in_cooldown_helper():
    state = CompactionState()
    assert state.in_cooldown() is False
    state.summary_cooldown_until = time.monotonic() + 30
    assert state.in_cooldown() is True
    state.summary_cooldown_until = time.monotonic() - 1
    assert state.in_cooldown() is False
