"""Tests for Stage 1 correctness fixes in compact.py.

Covers the helpers that keep compaction from producing message lists the
Anthropic API rejects (orphan tool_use/tool_result) and from accidentally
compressing the user's most recent request out of the active context.

All tests are pure-Python — the helpers don't call the LLM. The end-to-end
compact_if_needed test mocks the summarizer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nano_openclaw.compact import (
    SUMMARY_PREFIX,
    _align_boundary_backward,
    _ensure_last_user_message_in_tail,
    _find_last_real_user_message_idx,
    _is_real_user_message,
    _is_tool_result_reply,
    _sanitize_tool_pairs,
    compact_if_needed,
)
from nano_openclaw.loop import Message


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[{"type": "text", "text": text}])


def _assistant_tool_use(*calls: tuple[str, str]) -> Message:
    """Build an assistant message containing one tool_use block per (id, name)."""
    return Message(
        role="assistant",
        content=[
            {"type": "tool_use", "id": tid, "name": name, "input": {}}
            for tid, name in calls
        ],
    )


def _user_tool_result(*results: tuple[str, str]) -> Message:
    """Build a user message containing one tool_result block per (id, content)."""
    return Message(
        role="user",
        content=[
            {"type": "tool_result", "tool_use_id": tid, "content": content}
            for tid, content in results
        ],
    )


# ---------------------------------------------------------------------------
# _is_real_user_message / _is_tool_result_reply
# ---------------------------------------------------------------------------


def test_real_user_message_distinguishes_text_from_tool_result():
    assert _is_real_user_message(_text("user", "hi"))
    assert not _is_real_user_message(_user_tool_result(("t1", "ok")))
    assert not _is_real_user_message(_text("assistant", "reply"))


def test_real_user_message_mixed_content_counts_as_real():
    msg = Message(role="user", content=[
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "text", "text": "follow-up question"},
    ])
    assert _is_real_user_message(msg)


def test_tool_result_reply_only_when_all_blocks_are_tool_results():
    assert _is_tool_result_reply(_user_tool_result(("t1", "ok")))
    assert _is_tool_result_reply(_user_tool_result(("t1", "a"), ("t2", "b")))
    assert not _is_tool_result_reply(_text("user", "hi"))
    mixed = Message(role="user", content=[
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "text", "text": "more"},
    ])
    assert not _is_tool_result_reply(mixed)


# ---------------------------------------------------------------------------
# _align_boundary_backward
# ---------------------------------------------------------------------------


def test_align_boundary_no_op_when_cut_lands_on_real_user_msg():
    history = [
        _text("user", "first"),
        _text("assistant", "reply"),
        _text("user", "second"),
    ]
    assert _align_boundary_backward(history, 2) == 2


def test_align_boundary_pulls_cut_back_past_tool_result_to_assistant():
    # cut_idx points at the tool_result — slide back to include the parent
    # assistant tool_use in the tail.
    history = [
        _text("user", "do something"),
        _assistant_tool_use(("t1", "bash")),         # idx 1
        _user_tool_result(("t1", "ok")),             # idx 2 ← initial cut
        _text("assistant", "done"),
    ]
    assert _align_boundary_backward(history, 2) == 1


def test_align_boundary_no_op_when_predecessor_is_not_assistant_tool_use():
    history = [
        _text("user", "hi"),
        _text("assistant", "no tools used"),
        _user_tool_result(("t1", "stranded")),  # orphan; predecessor has no tool_use
    ]
    # cut at the orphan tool_result message — no parent to align to, leave alone
    assert _align_boundary_backward(history, 2) == 2


def test_align_boundary_ignores_out_of_range_indices():
    history = [_text("user", "a")]
    assert _align_boundary_backward(history, 0) == 0
    assert _align_boundary_backward(history, 1) == 1


# ---------------------------------------------------------------------------
# _find_last_real_user_message_idx / _ensure_last_user_message_in_tail
# ---------------------------------------------------------------------------


def test_find_last_real_user_skips_tool_result_only_messages():
    history = [
        _text("user", "ask"),                 # idx 0
        _assistant_tool_use(("t1", "bash")),
        _user_tool_result(("t1", "ok")),       # idx 2 — should NOT count
        _text("assistant", "done"),
    ]
    assert _find_last_real_user_message_idx(history, head_end=0) == 0


def test_ensure_last_user_in_tail_pulls_cut_back_to_user_msg():
    history = [
        _text("user", "first ask"),
        _text("assistant", "early answer"),
        _text("user", "REAL latest ask"),      # idx 2
        _text("assistant", "long reply"),
        _text("assistant", "still going"),
        _text("assistant", "more"),
        _text("assistant", "still more"),
    ]
    # initial cut would put recent_messages = history[5:] (drops the user msg)
    new_cut = _ensure_last_user_message_in_tail(history, cut_idx=5, head_end=0)
    assert new_cut == 2


def test_ensure_last_user_in_tail_no_op_when_already_in_tail():
    """When the last real user message is already at or past cut_idx, leave alone."""
    history = [
        _text("user", "old"),                # idx 0
        _text("assistant", "old reply"),     # idx 1
        _text("user", "RECENT"),             # idx 2 — last real user
        _text("assistant", "recent reply"),  # idx 3
    ]
    # cut_idx=2 → recent = history[2:] starts with "RECENT": already in tail.
    assert _ensure_last_user_message_in_tail(history, cut_idx=2, head_end=0) == 2
    # cut_idx=4 → recent = history[4:] is empty; user msg is NOT in tail.
    # The helper must pull cut_idx back to include it.
    assert _ensure_last_user_message_in_tail(history, cut_idx=4, head_end=0) == 2


def test_ensure_last_user_in_tail_does_not_invade_head_region():
    # If the only real user msg is at head_end (or below), cut_idx is left alone
    # rather than pulled into the head and producing an empty older_messages.
    history = [
        _text("user", "the only user msg, at idx 0"),
        _text("assistant", "self-run 1"),
        _text("assistant", "self-run 2"),
        _text("assistant", "self-run 3"),
        _text("assistant", "self-run 4"),
    ]
    # head_end=0 means "no head protection"; last_real_user_idx == 0 == head_end
    # so we don't pull cut_idx back.
    assert _ensure_last_user_message_in_tail(history, cut_idx=3, head_end=0) == 3


# ---------------------------------------------------------------------------
# _sanitize_tool_pairs
# ---------------------------------------------------------------------------


def test_sanitize_drops_orphan_tool_result_block():
    history = [
        _text("user", "hi"),
        _text("assistant", "no tool calls"),
        _user_tool_result(("t1", "stale")),
    ]
    _sanitize_tool_pairs(history)
    # The orphan tool_result message had only one block — drop the whole message
    assert len(history) == 2
    assert history[0].content[0]["text"] == "hi"
    assert history[1].content[0]["text"] == "no tool calls"


def test_sanitize_preserves_partial_message_when_some_blocks_remain():
    history = [
        _text("user", "hi"),
        _assistant_tool_use(("t1", "bash")),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "ORPHAN", "content": "stale"},
        ]),
    ]
    _sanitize_tool_pairs(history)
    # Message survives, but the orphan block is dropped
    assert len(history) == 3
    assert len(history[2].content) == 1
    assert history[2].content[0]["tool_use_id"] == "t1"


def test_sanitize_injects_stub_for_orphan_tool_use():
    history = [
        _text("user", "do it"),
        _assistant_tool_use(("t1", "bash")),  # no matching tool_result follows
        _text("assistant", "wrap-up"),
    ]
    _sanitize_tool_pairs(history)
    # A stub user message is inserted right after the assistant tool_use
    assert len(history) == 4
    stub = history[2]
    assert stub.role == "user"
    assert len(stub.content) == 1
    assert stub.content[0]["type"] == "tool_result"
    assert stub.content[0]["tool_use_id"] == "t1"
    assert "earlier conversation" in stub.content[0]["content"]


def test_sanitize_groups_multiple_orphans_per_assistant_into_one_stub():
    history = [
        _text("user", "do two things"),
        _assistant_tool_use(("t1", "bash"), ("t2", "read_file")),
    ]
    _sanitize_tool_pairs(history)
    assert len(history) == 3
    stub = history[2]
    ids = [b["tool_use_id"] for b in stub.content]
    assert ids == ["t1", "t2"]


def test_sanitize_handles_both_orphans_in_one_pass():
    history = [
        _text("user", "ask"),
        _assistant_tool_use(("t-good", "bash"), ("t-orphan", "read_file")),
        _user_tool_result(("t-good", "ok"), ("t-stale", "stranded")),
        _text("assistant", "done"),
    ]
    _sanitize_tool_pairs(history)
    # t-stale is dropped from the user message; t-orphan gets a new stub user msg
    # Find the stub: should have tool_use_id == t-orphan
    stub_msgs = [
        m for m in history
        if m.role == "user"
        and any(b.get("tool_use_id") == "t-orphan" for b in m.content)
    ]
    assert len(stub_msgs) == 1
    # The original tool_result message should now contain only t-good
    good_msgs = [
        m for m in history
        if m.role == "user"
        and any(b.get("tool_use_id") == "t-good" for b in m.content)
    ]
    assert len(good_msgs) == 1
    assert all(
        b.get("tool_use_id") != "t-stale"
        for m in history for b in m.content
        if isinstance(b, dict)
    )


def test_sanitize_no_op_on_well_formed_history():
    history = [
        _text("user", "hi"),
        _assistant_tool_use(("t1", "bash")),
        _user_tool_result(("t1", "ok")),
        _text("assistant", "done"),
    ]
    snapshot = [(m.role, list(m.content)) for m in history]
    _sanitize_tool_pairs(history)
    assert [(m.role, m.content) for m in history] == snapshot


# ---------------------------------------------------------------------------
# End-to-end: compact_if_needed produces a sanitized message list
# ---------------------------------------------------------------------------


def test_compact_if_needed_does_not_split_tool_use_result_pair():
    """If recent_turns boundary lands on a tool_result, the parent tool_use
    must come along to the tail rather than dangling in the summarized middle.
    """
    pad = "padding context that makes these messages realistic-sized "
    history = [
        # 6 older messages — bulk to summarize (padded so compaction triggers)
        _text("user", f"long-ago request 1 {pad}"),
        _text("assistant", f"long-ago reply 1 {pad}"),
        _text("user", f"long-ago request 2 {pad}"),
        _text("assistant", f"long-ago reply 2 {pad}"),
        _text("user", f"long-ago request 3 {pad}"),
        _text("assistant", f"long-ago reply 3 {pad}"),
        # tool group straddling the boundary: assistant at idx 6, result at idx 7
        _assistant_tool_use(("t1", "bash")),
        _user_tool_result(("t1", "ok")),
        # recent text
        _text("assistant", "wrap-up"),
        _text("user", "follow-up question"),
    ]

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="summary text")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    # recent_turns=2 means keep 4 messages; initial cut_idx = 10-4 = 6.
    # That index is the assistant tool_use — alignment leaves it alone.
    # But if we simulate cut_idx=7 (tool_result), alignment must pull back to 6.
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=60,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
    ))

    assert summary == "summary text"
    # No orphans should remain in the resulting history
    surviving_uses = {
        b["id"]
        for m in result if m.role == "assistant"
        for b in m.content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    }
    referenced = {
        b["tool_use_id"]
        for m in result if m.role == "user"
        for b in m.content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    # Either both empty (tool group fully summarized) or fully paired
    assert surviving_uses == referenced


def test_compact_if_needed_summary_uses_strong_prefix():
    # Older content padded so estimate_tokens > threshold and compaction
    # actually triggers (with budget=80, threshold=64 → need >64 tokens).
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
    mock_resp.content = [MagicMock(type="text", text="brief summary")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=80,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
    ))

    assert summary is not None
    assert result[0].content[0]["text"].startswith(SUMMARY_PREFIX)
    # Sanity: the strong prefix actually mentions reference-only framing
    assert "REFERENCE ONLY" in result[0].content[0]["text"]


def test_compact_if_needed_does_not_drop_latest_user_request():
    """Last real user request must survive in the tail, even if it sits
    deep in the recent window or got pushed back by a long agent self-run.
    Both the alignment pull-back AND the secondary aggressive trim must
    respect this invariant.
    """
    pad = "extra padding text " * 4
    history = []
    # Older bulk — padded so older tokens dominate and compaction fires
    for i in range(3):
        history.append(_text("user", f"old user {i} {pad}"))
        history.append(_text("assistant", f"old reply {i} {pad}"))
    # The user's latest real ask (idx 6)
    history.append(_text("user", "REAL FINAL ASK — must survive"))
    # Long agent self-run after, pushing the user msg further from the tail
    for i in range(5):
        history.append(_text("assistant", f"agent self-run step {i}"))

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="summary")]
    mock_client.messages.create = AsyncMock(return_value=mock_resp)

    # recent_turns=2 → keep_count=4 → initial cut_idx = 12-4 = 8
    # Without _ensure_last_user_message_in_tail, the user msg at idx 6 would
    # be summarized away.
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=60,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
    ))

    assert summary is not None
    # The real final ask must still appear in the post-compaction history
    found = any(
        "REAL FINAL ASK" in str(b.get("text", ""))
        for m in result for b in m.content
        if isinstance(b, dict)
    )
    assert found, "compaction summarized the user's latest request — bug regression"
