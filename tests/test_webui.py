from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalDecision, ApprovalPolicy, ApprovalRequest
from nano_openclaw.attachments import AttachmentAttached, AttachmentError
from nano_openclaw.loop import CancellationToken
from nano_openclaw.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.session import TranscriptWriter, load_session_store
from nano_openclaw.session.store import save_session_store, update_session
from nano_openclaw.tools import Tool, ToolRegistry
from nano_openclaw.webui.approvals import WebApprovalBroker
from nano_openclaw.webui.server import _event_to_payload, _read_assistant_name, _read_user_name, run_webui
from nano_openclaw.webui.sessions import WebSessionManager


def test_webui_event_serializer_core_stream_events():
    turn_id = "turn-1"
    session_id = "session-1"

    assert _event_to_payload(TextDelta("hi"), turn_id, session_id) == {
        "type": "text.delta",
        "turn_id": turn_id,
        "session_id": session_id,
        "text": "hi",
    }
    assert _event_to_payload(ToolUseStart("tool-1", "bash"), turn_id, session_id)["type"] == "tool.start"
    assert _event_to_payload(ToolUseDelta("tool-1", "{}"), turn_id, session_id)["type"] == "tool.delta"
    assert _event_to_payload(ToolUseEnd("tool-1"), turn_id, session_id)["type"] == "tool.end"
    assert _event_to_payload(MessageEnd("end_turn", {"input_tokens": 1}), turn_id, session_id) == {
        "type": "message.end",
        "turn_id": turn_id,
        "session_id": session_id,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1},
    }
    assert _event_to_payload(AttachmentAttached([".nano-openclaw/web-attachments/s/t/demo.pdf"]), turn_id, session_id) == {
        "type": "attachment.status",
        "turn_id": turn_id,
        "session_id": session_id,
        "refs": [".nano-openclaw/web-attachments/s/t/demo.pdf"],
        "status": "attached",
    }
    assert _event_to_payload(AttachmentError("demo.pdf", "bad"), turn_id, session_id) == {
        "type": "attachment.status",
        "turn_id": turn_id,
        "session_id": session_id,
        "ref": "demo.pdf",
        "status": "error",
        "error": "bad",
    }


def test_web_approval_broker_waits_for_decision():
    emitted = []

    async def run():
        broker = WebApprovalBroker(lambda payload: _emit(emitted, payload))
        request = ApprovalRequest(
            request_id="req-1",
            tool_name="bash",
            tool_args={"command": "pwd"},
            risk_level="medium",
            reason="test",
        )
        task = asyncio.create_task(broker.request_decision(request))
        await asyncio.sleep(0)
        assert emitted[0]["type"] == "approval.requested"
        assert broker.decide("req-1", "allow-once") is True
        return await task

    assert asyncio.run(run()) == ApprovalDecision.ALLOW_ONCE


def test_web_approval_broker_denies_on_cancellation():
    emitted = []

    async def run():
        broker = WebApprovalBroker(lambda payload: _emit(emitted, payload))
        request = ApprovalRequest(
            request_id="req-1",
            tool_name="bash",
            tool_args={"command": "pwd"},
            risk_level="medium",
            reason="test",
        )
        token = CancellationToken()
        task = asyncio.create_task(broker.request_decision(request, cancellation_token=token))
        await asyncio.sleep(0)
        token.cancel()
        return await asyncio.wait_for(task, timeout=1)

    assert asyncio.run(run()) == ApprovalDecision.DENY
    assert emitted[0]["type"] == "approval.requested"


async def _emit(target, payload):
    target.append(payload)


def test_tool_registry_uses_web_approval_handler():
    registry = ToolRegistry()
    registry.approval_manager = ApprovalManager(ApprovalPolicy(ask_mode="always", dangerous_tools=["demo"]))
    registry.approval_handler = lambda _request, _token: ApprovalDecision.ALLOW_ONCE
    registry.register(Tool("demo", "demo", {"type": "object"}, lambda _args: "ok"))

    raw = registry.dispatch("tool-1", "demo", {})
    result = asyncio.run(raw) if asyncio.iscoroutine(raw) else raw

    assert result["content"][0]["text"] == "ok"


def test_web_session_manager_create_select_clear():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        assert session.session_id
        assert session.writer.session_id == session.session_id
        # Session is pending (not yet written to disk), so store has no entry yet.
        assert load_session_store(store_path)["lastSessionId"] is None
        # get_or_load(None) must return the same pending session within this process.
        assert manager.get_or_load(None).session_id == session.session_id

        # select() explicitly saves metadata, so the store is updated at that point.
        loaded = manager.select(session.session_id)
        assert loaded.session_id == session.session_id
        assert load_session_store(store_path)["lastSessionId"] == session.session_id

        cleared = asyncio.run(manager.clear(session.session_id))
        assert cleared.history == []
        assert cleared.writer.message_count == 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_select_uses_store_id_not_transcript_header_id():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session_id = str(uuid.uuid4())
        header_id = str(uuid.uuid4())
        path = session_dir / f"{session_id}.jsonl"
        writer = TranscriptWriter(path)
        writer.start(model="model", session_id=header_id)
        # Trigger lazy write so the transcript file exists for _load_existing.
        writer.append_compaction("bootstrap")
        store = load_session_store(store_path)
        update_session(store, session_id, model="model", message_count=0, compaction_count=0)
        save_session_store(store_path, store)

        selected = manager.select(session_id)

        assert selected.session_id == session_id
        assert selected.writer.session_id == session_id
        assert load_session_store(store_path)["lastSessionId"] == session_id
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_clear_rejects_active_turn():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        session.active_turn_id = "turn-1"

        with pytest.raises(RuntimeError):
            asyncio.run(manager.clear(session.session_id))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_list_uses_conversation_text_for_title_and_search():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        session.history.extend([
            _message("user", "小番茄是什么星座"),
            _message("assistant", "小番茄是射手座"),
            _message("user", "我是双鱼座吗"),
        ])
        session.writer.append_message(session.history[0])
        session.writer.append_message(session.history[1])
        session.writer.append_message(session.history[2])
        manager.save_metadata(session)

        listed = manager.list()[0]
        assert listed["title"] == "小番茄是什么星座"
        assert listed["preview"] == "我是双鱼座吗"
        assert "射手座" in listed["search_text"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_list_hides_zero_message_sessions():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        session.history.append(_message("user", "旧对话标题"))

        assert manager.list() == []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_payload_reloads_activity_history():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        session.history.extend([
            _message("user", "hello"),
            _message("assistant", "world"),
        ])
        session.writer.append_message(session.history[0])
        session.writer.append_message(session.history[1])
        activity = {
            "turn_id": "turn-1",
            "session_id": session.session_id,
            "insert_after_index": 0,
            "duration_ms": 1200,
            "payloads": [{"type": "tool.start", "name": "demo"}],
        }
        session.activities.append(activity)
        session.writer.append_activity(activity)
        manager.save_metadata(session)
        manager._loaded.clear()

        loaded = manager.select(session.session_id)

        assert manager.activity_json(loaded) == [activity]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_list_hides_store_entries_without_valid_transcript():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")
        store = load_session_store(store_path)
        update_session(store, "missing-session", model="model", message_count=88, compaction_count=0)
        save_session_store(store_path, store)

        assert manager.list() == []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_select_invalid_explicit_session_does_not_create_new_session():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")
        store = load_session_store(store_path)
        update_session(store, "missing-session", model="model", message_count=88, compaction_count=0)
        save_session_store(store_path, store)

        with pytest.raises(KeyError):
            manager.select("missing-session")

        assert manager.list() == []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_select_legacy_header_alias_opens_canonical_file():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = WebSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        canonical = manager.create()
        canonical.history.append(_message("user", "旧别名 session 内容"))
        canonical.writer.append_message(canonical.history[0])  # triggers lazy header write
        alias_id = canonical.transcript_path.read_text(encoding="utf-8").split('"id": "')[1].split('"', 1)[0]
        manager.save_metadata(canonical)

        store = load_session_store(store_path)
        update_session(store, alias_id, model="model", message_count=1, compaction_count=0)
        save_session_store(store_path, store)
        manager._loaded.clear()

        selected = manager.select(alias_id)

        assert selected.session_id == canonical.session_id
        assert selected.transcript_path == canonical.transcript_path
        assert selected.history[0].content[0]["text"] == "旧别名 session 内容"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _message(role: str, text: str):
    from nano_openclaw.loop import Message

    return Message(role, [{"type": "text", "text": text}])


def test_webui_requires_token_for_non_local_host():
    with pytest.raises(SystemExit):
        run_webui(config_path=None, agent_id="default", host="0.0.0.0", port=8765, token=None)


def test_webui_reads_assistant_name_from_identity():
    tmp_dir = Path("tests") / f".tmp-webui-identity-{uuid.uuid4().hex}"
    try:
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "IDENTITY.md").write_text("- **Name:** Yui\n", encoding="utf-8")

        assert _read_assistant_name(tmp_dir) == "Yui"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_reads_assistant_name_from_following_line():
    tmp_dir = Path("tests") / f".tmp-webui-identity-{uuid.uuid4().hex}"
    try:
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "IDENTITY.md").write_text("- **Name:**\n  Yui\n", encoding="utf-8")

        assert _read_assistant_name(tmp_dir) == "Yui"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_reads_user_name_from_user_profile():
    tmp_dir = Path("tests") / f".tmp-webui-user-{uuid.uuid4().hex}"
    try:
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "USER.md").write_text("- **What to call them:** 主人\n", encoding="utf-8")

        assert _read_user_name(tmp_dir) == "主人"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_reads_user_name_from_following_line():
    tmp_dir = Path("tests") / f".tmp-webui-user-{uuid.uuid4().hex}"
    try:
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "USER.md").write_text("- **What to call them:**\n  主人\n", encoding="utf-8")

        assert _read_user_name(tmp_dir) == "主人"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
