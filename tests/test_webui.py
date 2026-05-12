from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalDecision, ApprovalPolicy, ApprovalRequest
from nano_openclaw.attachments import AttachmentAttached, AttachmentError
from nano_openclaw.gateway.backend import PushEvent
from nano_openclaw.loop import CancellationToken, SubagentAnnounced, SubagentEvent, ToolResult
from nano_openclaw.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.session import TranscriptWriter, load_session_store
from nano_openclaw.session.store import save_session_store, update_session
from nano_openclaw.tools import Tool, ToolRegistry
from nano_openclaw.gateway.approval_broker import ApprovalBroker
from nano_openclaw.config.types import AgentDefaultsConfig, AgentsConfig, ModelDefinition, ModelProvider, ModelsConfig, NanoOpenClawConfig
from nano_openclaw.gateway.webui.server import (
    _event_to_payload,
    _image_model_options,
    _model_options,
    _read_assistant_name,
    _read_user_name,
    _session_payload,
    _is_replayable_activity_payload,
    _webui_payloads_from_push,
)
from nano_openclaw.gateway.agent_backend_session import BackendSessionManager


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


def test_webui_serializes_subagent_completion_for_activity():
    payload = _event_to_payload(
        SubagentAnnounced(
            run_id="run-1",
            status="completed",
            task="research task",
            result_text="child result",
            elapsed_ms=1234,
        ),
        "turn-1",
        "session-1",
    )

    assert payload == {
        "type": "subagent.status",
        "turn_id": "turn-1",
        "session_id": "session-1",
        "status": "completed",
        "run_id": "run-1",
        "task": "research task",
        "result_text": "child result",
        "elapsed_ms": 1234,
        "error_message": None,
    }
    assert _is_replayable_activity_payload(payload)


def test_webui_serializes_subagent_internal_event_for_activity():
    payload = _event_to_payload(
        SubagentEvent(
            run_id="run-12345678",
            label="research",
            task="research task",
            event=ToolResult(
                tool_use_id="tool-1",
                name="web_search",
                args={"query": "nano-openclaw"},
                result={"content": [{"type": "text", "text": "done"}]},
            ),
        ),
        "turn-1",
        "session-1",
    )

    assert payload == {
        "type": "subagent.event",
        "turn_id": "turn-1",
        "session_id": "session-1",
        "run_id": "run-12345678",
        "label": "research",
        "task": "research task",
        "event": {
            "type": "tool.result",
            "tool_use_id": "tool-1",
            "name": "web_search",
            "args": {"query": "nano-openclaw"},
            "result": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    assert _is_replayable_activity_payload(payload)


def test_web_approval_broker_waits_for_decision():
    emitted = []

    async def run():
        broker = ApprovalBroker(lambda payload: _emit(emitted, payload))
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
        broker = ApprovalBroker(lambda payload: _emit(emitted, payload))
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


def test_web_approval_broker_decision_wins_over_cancel_watcher():
    emitted = []

    async def run():
        broker = ApprovalBroker(lambda payload: _emit(emitted, payload))
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
        assert broker.decide("req-1", "deny") is True
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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        assert session.session_id
        assert session.writer.session_id == session.session_id
        # Session is pending (not yet written to disk), so store has no entry yet.
        assert load_session_store(store_path)["lastSessionId"] is None
        # get_or_load(None) must return the same pending session within this process.
        assert manager.get_or_load(None).session_id == session.session_id

        # select() should not persist a blank pending session.
        loaded = manager.select(session.session_id)
        assert loaded.session_id == session.session_id
        assert load_session_store(store_path)["lastSessionId"] is None

        cleared = asyncio.run(manager.clear(session.session_id))
        assert cleared.history == []
        assert cleared.writer.message_count == 0
        assert load_session_store(store_path)["lastSessionId"] is None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_manager_keeps_multiple_pending_sessions_independent():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        first = manager.create()
        second = manager.create()

        assert manager.get_or_load(None).session_id == second.session_id

        first.history.append(_message("user", "first prompt"))
        first.writer.append_message(first.history[0])

        store = load_session_store(store_path)
        assert first.session_id in store["sessions"]
        assert second.session_id not in store["sessions"]
        assert manager.get_or_load(None).session_id == second.session_id
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_push_adapter_maps_backend_turn_started_to_chat_accepted():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")
        session = manager.create()
        turn_sessions: dict[str, str] = {}

        payloads = _webui_payloads_from_push(
            PushEvent(
                event="agent.event",
                payload={
                    "type": "turn.started",
                    "turn_id": "turn-1",
                    "session_id": session.session_id,
                    "user_text": "hello",
                    "attachments": [],
                },
                seq=1,
            ),
            manager,
            runtime=object(),
            turn_sessions=turn_sessions,
        )

        assert payloads[0]["type"] == "chat.accepted"
        assert payloads[0]["turn_id"] == "turn-1"
        assert turn_sessions == {"turn-1": session.session_id}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_push_adapter_enriches_turn_done_with_session_payload():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")
        session = manager.create()
        session.history.extend([
            _message("user", "hello"),
            _message("assistant", "world"),
        ])
        for message in session.history:
            session.writer.append_message(message)
        manager.save_metadata(session)

        payloads = _webui_payloads_from_push(
            PushEvent(
                event="agent.event",
                payload={"type": "turn.done", "turn_id": "turn-1", "session_id": session.session_id},
                seq=1,
            ),
            manager,
            runtime=object(),
            turn_sessions={"turn-1": session.session_id},
        )

        assert payloads[0]["type"] == "turn.done"
        assert payloads[0]["session"]["session_id"] == session.session_id
        assert payloads[0]["session"]["history"][-1]["content"][0]["text"] == "world"
        assert payloads[0]["sessions"][0]["session_id"] == session.session_id
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_push_adapter_routes_approval_to_turn_session():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")
        session = manager.create()

        payloads = _webui_payloads_from_push(
            PushEvent(
                event="approval.request",
                payload={"type": "approval.requested", "turn_id": "turn-1", "request_id": "req-1"},
                seq=1,
            ),
            manager,
            runtime=object(),
            turn_sessions={"turn-1": session.session_id},
        )

        assert payloads == [
            {
                "type": "approval.requested",
                "turn_id": "turn-1",
                "request_id": "req-1",
                "session_id": session.session_id,
            }
        ]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_webui_push_adapter_maps_session_changed_to_session_updated():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")
        session = manager.create()

        payloads = _webui_payloads_from_push(
            PushEvent(
                event="session.changed",
                payload={"session_id": session.session_id},
                seq=1,
            ),
            manager,
            runtime=object(),
            turn_sessions={},
        )

        assert payloads[0]["type"] == "session.updated"
        assert payloads[0]["session"]["session_id"] == session.session_id
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_select_uses_store_id_not_transcript_header_id():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

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


def test_web_session_list_uses_stored_summary_without_reloading_transcript(monkeypatch: pytest.MonkeyPatch):
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        session.history.extend([
            _message("user", "cached title"),
            _message("assistant", "cached reply"),
        ])
        for message in session.history:
            session.writer.append_message(message)
        manager.save_metadata(session)
        manager._loaded.clear()

        def fail_read(_path):
            raise AssertionError("list() should use sessions.json webui_summary")

        monkeypatch.setattr(manager, "_read_transcript", fail_read)

        listed = manager.list()

        assert listed[0]["title"] == "cached title"
        assert listed[0]["preview"] == "cached reply"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_display_hides_subagent_announcements():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        subagent_announcement = (
            '<subagent_completion runId="run-1" status="completed">\n'
            "  <task>research task</task>\n"
            "  <result>raw child result</result>\n"
            "</subagent_completion>"
        )
        session = manager.create()
        session.history.extend([
            _message("user", "please research this"),
            _message("assistant", "I will check."),
            _message("user", subagent_announcement),
            _message("assistant", "Here is the final answer."),
        ])
        for message in session.history:
            session.writer.append_message(message)
        manager.save_metadata(session)

        history = manager.history_json(session)
        listed = manager.list()[0]

        assert [message["role"] for message in history] == ["user", "assistant", "assistant"]
        assert all("subagent_completion" not in _json_text(message) for message in history)
        assert listed["title"] == "please research this"
        assert listed["preview"] == "Here is the final answer."
        assert "raw child result" not in listed["search_text"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_payload_truncates_long_history():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        session = manager.create()
        for index in range(90):
            session.history.append(_message("user", f"message {index}"))
        payload = _session_payload(manager, session)

        assert payload["history_truncated"] is True
        assert payload["history_offset"] == 10
        assert len(payload["history"]) == 80
        assert payload["history"][0]["content"][0]["text"] == "message 10"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_list_sorts_by_creation_time_not_completion_time():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

        older = manager.create()
        newer = manager.create()
        older.created_at = 100
        newer.created_at = 200

        newer.history.append(_message("user", "newer created"))
        newer.writer.append_message(newer.history[0])
        manager.save_metadata(newer)

        older.history.append(_message("user", "older created"))
        older.writer.append_message(older.history[0])
        manager.save_metadata(older)

        listed = manager.list()

        assert [item["session_id"] for item in listed] == [newer.session_id, older.session_id]
        assert [item["created_at"] for item in listed] == [200, 100]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_session_list_hides_zero_message_sessions():
    tmp_dir = Path("tests") / f".tmp-webui-{uuid.uuid4().hex}"
    try:
        session_dir = tmp_dir / "sessions"
        store_path = session_dir / "sessions.json"
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")
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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")
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
        manager = BackendSessionManager(session_dir=session_dir, store_path=store_path, model="model")

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


def test_runtime_model_options_fill_name_and_input_for_default_model():
    cfg = NanoOpenClawConfig(
        agents=AgentsConfig(defaults=AgentDefaultsConfig(model="ali-coding/glm-5")),
        models=ModelsConfig(
            providers={
                "ali-coding": ModelProvider(
                    models=[
                        ModelDefinition(id="glm-5", name="GLM 5", input=["text"]),
                    ]
                )
            }
        ),
    )

    options = _model_options(cfg)

    assert options == [{"ref": "ali-coding/glm-5", "name": "GLM 5", "input": ["text"]}]


def test_runtime_image_model_options_only_include_image_capable_models():
    cfg = NanoOpenClawConfig(
        agents=AgentsConfig(defaults=AgentDefaultsConfig(imageModel="ali-coding/text-only")),
        models=ModelsConfig(
            providers={
                "ali-coding": ModelProvider(
                    models=[
                        ModelDefinition(id="text-only", name="Text Only", input=["text"]),
                        ModelDefinition(id="vision", name="Vision", input=["text", "image"]),
                    ]
                )
            }
        ),
    )

    options = _image_model_options(cfg)

    assert options == [
        {"ref": "", "name": "Native Vision", "input": ["image"]},
        {"ref": "ali-coding/vision", "name": "Vision", "input": ["text", "image"]},
    ]


def _message(role: str, text: str):
    from nano_openclaw.loop import Message

    return Message(role, [{"type": "text", "text": text}])


def _json_text(message: dict) -> str:
    return "\n".join(
        str(block.get("text", ""))
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


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
