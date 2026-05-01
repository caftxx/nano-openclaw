"""Tests for session persistence module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from nano_openclaw.loop import Message
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
