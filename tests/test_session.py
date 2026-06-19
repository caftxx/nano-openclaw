"""Tests for session persistence module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from nano_openclaw.core.loop import Message
from nano_openclaw.session import (
    TranscriptWriter,
    TranscriptReader,
    load_session_store,
    save_session_store,
    get_last_session,
    update_session,
    list_sessions,
    new_session_id,
)
from nano_openclaw.session.truncate import truncate_tool_result, MAX_TOOL_RESULT_CHARS


# ---- Truncate Tests ----

def test_truncate_under_limit_unchanged():
    content = [{"type": "text", "text": "short text"}]
    result = truncate_tool_result(content)
    assert result == content


def test_truncate_over_limit_truncates():
    long_text = "x" * (MAX_TOOL_RESULT_CHARS + 100)
    content = [{"type": "text", "text": long_text}]
    result = truncate_tool_result(content)
    assert len(result[0]["text"]) <= MAX_TOOL_RESULT_CHARS + len("[nano truncated: ") + 20
    assert "nano truncated" in result[0]["text"]


def test_truncate_preserves_non_text_blocks():
    long_text = "x" * (MAX_TOOL_RESULT_CHARS + 100)
    content = [
        {"type": "text", "text": long_text},
        {"type": "image", "source": {"data": "abc"}},
    ]
    result = truncate_tool_result(content)
    text_blocks = [b for b in result if b.get("type") == "text"]
    image_blocks = [b for b in result if b.get("type") == "image"]
    assert len(text_blocks) == 1
    assert len(image_blocks) == 1
    assert "nano truncated" in text_blocks[0]["text"]


# ---- Store Tests ----

def test_load_empty_store_returns_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "sessions.json"
        store = load_session_store(store_path)
        assert store == {"lastSessionId": None, "sessions": {}}


def test_save_and_load_store():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "sessions.json"
        store = {
            "lastSessionId": "abc123",
            "sessions": {"abc123": {"created_at": 1000, "updated_at": 2000}},
        }
        save_session_store(store_path, store)
        loaded = load_session_store(store_path)
        assert loaded["lastSessionId"] == "abc123"
        assert "abc123" in loaded["sessions"]


def test_update_session_creates_new():
    store = {"lastSessionId": None, "sessions": {}}
    update_session(store, "session-1", model="test-model", message_count=5)
    assert store["lastSessionId"] == "session-1"
    assert store["sessions"]["session-1"]["model"] == "test-model"
    assert store["sessions"]["session-1"]["message_count"] == 5


def test_update_session_updates_existing():
    store = {
        "lastSessionId": "session-1",
        "sessions": {
            "session-1": {
                "created_at": 1000,
                "updated_at": 1000,
                "model": "old-model",
                "message_count": 0,
                "compaction_count": 0,
            }
        },
    }
    update_session(store, "session-1", model="new-model", message_count=10, compaction_count=2)
    assert store["sessions"]["session-1"]["model"] == "new-model"
    assert store["sessions"]["session-1"]["message_count"] == 10
    assert store["sessions"]["session-1"]["compaction_count"] == 2


def test_get_last_session_returns_none_when_empty():
    store = {"lastSessionId": None, "sessions": {}}
    assert get_last_session(store) is None


def test_get_last_session_returns_metadata():
    store = {
        "lastSessionId": "abc",
        "sessions": {
            "abc": {
                "created_at": 1000,
                "updated_at": 2000,
                "model": "test",
                "message_count": 3,
                "compaction_count": 1,
            }
        },
    }
    info = get_last_session(store)
    assert info is not None
    assert info.session_id == "abc"
    assert info.model == "test"
    assert info.message_count == 3


def test_list_sessions_sorted_by_updated_at():
    store = {
        "lastSessionId": "b",
        "sessions": {
            "a": {"created_at": 1000, "updated_at": 3000, "model": "m1", "message_count": 0, "compaction_count": 0},
            "b": {"created_at": 1000, "updated_at": 1000, "model": "m2", "message_count": 0, "compaction_count": 0},
        },
    }
    sessions = list_sessions(store)
    assert len(sessions) == 2
    assert sessions[0].session_id == "a"  # most recent first
    assert sessions[1].session_id == "b"


# ---- Transcript Tests ----

def test_writer_and_reader_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.jsonl"
        writer = TranscriptWriter(path)
        sid = writer.start(model="test-model", cwd="/tmp")
        assert sid == writer.session_id

        msg1 = Message(role="user", content=[{"type": "text", "text": "hello"}])
        writer.append_message(msg1)

        msg2 = Message(role="assistant", content=[{"type": "text", "text": "hi"}])
        writer.append_message(msg2)

        writer.append_compaction("summarized")

        reader = TranscriptReader(path)
        history, loaded_sid, msg_count, comp_count, last_msg_id = reader.load_history()

        assert loaded_sid == sid
        assert msg_count == 2
        assert comp_count == 1
        assert len(history) == 2
        assert last_msg_id != ""
        assert history[0].role == "user"
        assert history[0].content[0]["text"] == "hello"
        assert history[1].role == "assistant"
        assert history[1].content[0]["text"] == "hi"


def test_reader_empty_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty.jsonl"
        reader = TranscriptReader(path)
        history, sid, msg_count, comp_count, last_msg_id = reader.load_history()
        assert history == []
        assert sid == ""
        assert msg_count == 0
        assert last_msg_id == ""


def test_reader_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nonexistent.jsonl"
        reader = TranscriptReader(path)
        history, sid, msg_count, comp_count, last_msg_id = reader.load_history()
        assert history == []


def test_writer_truncates_large_tool_results():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.jsonl"
        writer = TranscriptWriter(path)
        writer.start(model="test")

        long_text = "x" * (MAX_TOOL_RESULT_CHARS + 500)
        msg = Message(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": [{"type": "text", "text": long_text}],
                }
            ],
        )
        writer.append_message(msg)

        # Read back and verify truncation
        reader = TranscriptReader(path)
        history, _, _, _, _ = reader.load_history()
        assert len(history) == 1
        content = history[0].content[0]["content"]
        text = content[0].get("text", "")
        assert "nano truncated" in text
        assert len(text) < len(long_text)


def test_writer_resume_continues_parent_chain():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.jsonl"
        writer = TranscriptWriter(path)
        writer.start(model="test-model", cwd="/tmp")
        writer.append_message(Message(role="user", content=[{"type": "text", "text": "hello"}]))

        reader = TranscriptReader(path)
        _, sid, msg_count, comp_count, last_msg_id = reader.load_history()

        resumed = TranscriptWriter.resume(path, sid, msg_count, comp_count, last_msg_id)
        assert resumed.session_id == sid
        assert resumed.message_count == msg_count
        assert resumed._last_message_id == last_msg_id

        resumed.append_message(Message(role="assistant", content=[{"type": "text", "text": "hi"}]))

        reader2 = TranscriptReader(path)
        history, _, final_count, _, _ = reader2.load_history()
        assert final_count == 2
        assert history[1].content[0]["text"] == "hi"


def test_new_session_id_is_valid_uuid():
    sid = new_session_id()
    assert isinstance(sid, str)
    assert len(sid) > 0
    # Basic UUID format check
    assert "-" in sid or len(sid) >= 32


# ---- Rotation Tests ----

def test_rotate_rewrites_file_with_summary_and_kept_messages():
    from nano_openclaw.session.transcript import is_synthetic_summary

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rot.jsonl"
        writer = TranscriptWriter(path)
        sid = writer.start(model="test-model", cwd="/tmp")

        # Build up a fat transcript: 5 messages, then rotate keeping last 2.
        for i in range(5):
            writer.append_message(
                Message(role="user" if i % 2 == 0 else "assistant",
                        content=[{"type": "text", "text": f"msg{i}"}])
            )
        assert writer.message_count == 5
        assert writer.compaction_count == 0
        size_before = path.stat().st_size

        kept = [
            Message(role="user", content=[{"type": "text", "text": "msg3"}]),
            Message(role="assistant", content=[{"type": "text", "text": "msg4"}]),
        ]
        writer.rotate("a summary of msg0..msg2", kept)

        # Writer state is reset to reflect the new file shape.
        assert writer.message_count == 2
        assert writer.compaction_count == 1
        assert writer.session_id == sid

        # File shrunk and contains exactly: header + compaction + kept msgs.
        assert path.stat().st_size < size_before
        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        assert [e["type"] for e in lines] == ["session", "compaction", "message", "message"]
        assert lines[0]["id"] == sid
        assert lines[1]["summary"] == "a summary of msg0..msg2"
        assert lines[2]["content"][0]["text"] == "msg3"
        assert lines[3]["content"][0]["text"] == "msg4"

        # Reader materializes the leading compaction as a synthetic summary
        # message so the in-memory shape matches compact_if_needed output.
        reader = TranscriptReader(path)
        history, loaded_sid, msg_count, comp_count, _ = reader.load_history()
        assert loaded_sid == sid
        assert msg_count == 2
        assert comp_count == 1
        assert len(history) == 3
        assert is_synthetic_summary(history[0])
        assert "a summary of msg0..msg2" in history[0].content[0]["text"]
        assert history[1].content[0]["text"] == "msg3"
        assert history[2].content[0]["text"] == "msg4"


def test_legacy_midstream_compaction_is_not_materialized():
    """Pre-rotation transcripts have compaction markers AFTER messages.

    Materializing those would inflate context redundantly (originals are still
    on disk), so the reader keeps the legacy "compaction is just a marker"
    semantics for that pattern.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.jsonl"
        writer = TranscriptWriter(path)
        writer.start(model="test")
        writer.append_message(Message(role="user", content=[{"type": "text", "text": "a"}]))
        writer.append_message(Message(role="assistant", content=[{"type": "text", "text": "b"}]))
        writer.append_compaction("legacy summary")
        writer.append_message(Message(role="user", content=[{"type": "text", "text": "c"}]))

        history, _, msg_count, comp_count, _ = TranscriptReader(path).load_history()
        assert msg_count == 3
        assert comp_count == 1
        # No synthetic summary inserted — exactly the three real messages.
        assert len(history) == 3
        assert [m.content[0]["text"] for m in history] == ["a", "b", "c"]


def test_commit_turn_rotates_when_compaction_pending(tmp_path):
    """End-to-end: feed _commit_turn pending ops with a compaction and verify
    the on-disk file is rewritten to the post-compaction shape."""
    from nano_openclaw.gateway.agent_backend_session import AgentBackendSession
    from nano_openclaw.core.loop import AgentSession, LoopConfig
    from nano_openclaw.session.transcript import _build_synthetic_summary_message
    from nano_openclaw.core.tools import ToolRegistry

    transcript = tmp_path / "s.jsonl"
    writer = TranscriptWriter(transcript)
    writer.start(model="test", session_id="sess-1")
    # Pre-existing on-disk history that is about to be summarized.
    for i in range(4):
        writer.append_message(
            Message(role="user" if i % 2 == 0 else "assistant",
                    content=[{"type": "text", "text": f"old{i}"}])
        )

    history = [Message(role=m.role, content=m.content) for m in
               TranscriptReader(transcript).load_history()[0]]
    session = AgentBackendSession(
        session_id="sess-1",
        transcript_path=transcript,
        history=history,
        writer=writer,
    )

    cfg = LoopConfig(model="test")  # truncate_after_compaction defaults to True
    agent_session = AgentSession(
        history=session.history,
        registry=ToolRegistry(),
        on_event=lambda _e: None,
        client=None,
        cfg=cfg,
        transcript_writer=session.writer,
    )

    summary_text = "summary of old0..old1"
    new_assistant = Message(role="assistant", content=[{"type": "text", "text": "new-a"}])
    new_user = Message(role="user", content=[{"type": "text", "text": "new-u"}])
    scratch = [
        _build_synthetic_summary_message(summary_text),
        Message(role="user", content=[{"type": "text", "text": "old2"}]),
        Message(role="assistant", content=[{"type": "text", "text": "old3"}]),
        new_assistant,
        new_user,
    ]
    pending_ops = [
        ("compaction", summary_text),
        ("message", new_assistant),
        ("message", new_user),
    ]
    agent_session._commit_turn(scratch, pending_ops)

    lines = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    types = [e["type"] for e in lines]
    # Header + one compaction + four kept messages (old2, old3, new-a, new-u).
    # The synthetic summary at scratch[0] is dropped from the kept set.
    assert types == ["session", "compaction", "message", "message", "message", "message"]
    assert lines[1]["summary"] == summary_text
    assert [lines[i]["content"][0]["text"] for i in range(2, 6)] == ["old2", "old3", "new-a", "new-u"]
    assert writer.message_count == 4
    assert writer.compaction_count == 1


def test_commit_turn_skips_rotation_when_disabled(tmp_path):
    from nano_openclaw.gateway.agent_backend_session import AgentBackendSession
    from nano_openclaw.core.loop import AgentSession, LoopConfig
    from nano_openclaw.session.transcript import _build_synthetic_summary_message
    from nano_openclaw.core.tools import ToolRegistry

    transcript = tmp_path / "s.jsonl"
    writer = TranscriptWriter(transcript)
    writer.start(model="test", session_id="sess-2")
    writer.append_message(Message(role="user", content=[{"type": "text", "text": "old0"}]))

    session = AgentBackendSession(
        session_id="sess-2",
        transcript_path=transcript,
        history=[Message(role="user", content=[{"type": "text", "text": "old0"}])],
        writer=writer,
    )

    cfg = LoopConfig(model="test", truncate_after_compaction=False)
    agent_session = AgentSession(
        history=session.history,
        registry=ToolRegistry(),
        on_event=lambda _e: None,
        client=None,
        cfg=cfg,
        transcript_writer=session.writer,
    )

    summary_text = "s"
    new_msg = Message(role="assistant", content=[{"type": "text", "text": "new"}])
    scratch = [
        _build_synthetic_summary_message(summary_text),
        new_msg,
    ]
    agent_session._commit_turn(scratch, [("compaction", summary_text), ("message", new_msg)])

    # Without rotation: header + old0 + compaction marker + new (no rewrite).
    lines = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    assert [e["type"] for e in lines] == ["session", "message", "compaction", "message"]
    assert writer.message_count == 2
    assert writer.compaction_count == 1
