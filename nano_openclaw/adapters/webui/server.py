"""FastAPI WebUI server."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nano_openclaw.core.attachments import decode_attachment_payloads
from nano_openclaw.services.event_payload import (
    event_to_payload as _event_to_payload,
    is_replayable_activity_payload as _is_replayable_activity_payload,
    jsonable as _jsonable,
)
from nano_openclaw.services.backend import BackendError, BusyError, NotFoundError, PushEvent, VoiceError
from nano_openclaw.services.webui_state import (
    agent_options as _agent_options,
    has_active_turn as _has_active_turn,
    image_model_options as _image_model_options,
    model_options as _model_options,
    read_assistant_name as _read_assistant_name,
    read_user_name as _read_user_name,
    thinking_levels as _thinking_levels,
)
from nano_openclaw.services.agent_session import BackendSessionManager, display_history, message_text


SESSION_PAYLOAD_HISTORY_LIMIT = 80
SESSION_PAYLOAD_ACTIVITY_LIMIT = 120

class ChatRequest(BaseModel):
    session_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] = []
    response_style: str = ""   # "voice" → spoken-style system directive (web voice mode)


class ApprovalDecisionRequest(BaseModel):
    request_id: str
    decision: str


class TurnCancelRequest(BaseModel):
    turn_id: str


class SessionSelectRequest(BaseModel):
    session_id: str


class ThinkingSetRequest(BaseModel):
    level: str


class RuntimeSetRequest(BaseModel):
    agent_id: str | None = None
    model_ref: str | None = None
    image_model_ref: str | None = None
    thinking_level: str | None = None


class VoiceTtsRequest(BaseModel):
    text: str
    voice: str = ""        # legacy alias for voice_id
    voiceId: str = ""
    sample_rate: int = 0   # 0 则用 config 默认采样率
    sampleRate: int = 0
    speed: float | None = None
    rateWpm: int | None = None


def create_app(
    *,
    backend: Any,
    token: str | None = None,
) -> FastAPI:
    """Build the FastAPI WebUI mounted by the gateway daemon.

    WebUI is only a browser transport surface. Runtime ownership, session
    ownership, turn execution, approvals, and push-event fanout all live in
    the injected gateway Backend.
    """
    static_dir = Path(__file__).with_name("static")
    app = FastAPI(title="nano-openclaw WebUI")
    app.state.backend = backend
    app.state.token = token
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    async def require_http_token(authorization: str | None = Header(default=None)) -> None:
        expected = app.state.token
        if not expected:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        if not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        path = static_dir / "index.html"
        return path.read_text(encoding="utf-8")

    @app.get("/voice", response_class=HTMLResponse)
    async def voice() -> str:
        # /voice is a deep-link into the unified WebUI: it serves index.html and
        # voice-shell.js auto-opens the hands-free overlay (recognizer adapter
        # -> chat.send over /ws -> synthesizer chain reads back), sharing the
        # same session/runtime as the chat surface.
        path = static_dir / "index.html"
        return path.read_text(encoding="utf-8")

    @app.get("/api/state", dependencies=[Depends(require_http_token)])
    async def state() -> dict[str, Any]:
        return await app.state.backend.webui_state()

    @app.get("/api/voice/config", dependencies=[Depends(require_http_token)])
    async def voice_config() -> dict[str, Any]:
        return await app.state.backend.voice_config()

    @app.get("/api/talk/config", dependencies=[Depends(require_http_token)])
    async def talk_config() -> dict[str, Any]:
        return {"config": await app.state.backend.voice_config()}

    @app.get("/api/voice/token", dependencies=[Depends(require_http_token)])
    async def voice_token() -> dict[str, Any]:
        try:
            return await app.state.backend.voice_token()
        except VoiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/talk/speak", dependencies=[Depends(require_http_token)])
    async def talk_speak(req: VoiceTtsRequest) -> dict[str, Any]:
        try:
            return await app.state.backend.talk_speak(
                text=req.text,
                voice_id=req.voiceId or req.voice or None,
                sample_rate=req.sampleRate or req.sample_rate or None,
                speed=req.speed,
                rate_wpm=req.rateWpm,
            )
        except VoiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "message": str(exc),
                    "reason": exc.reason,
                    "fallbackEligible": exc.fallback_eligible,
                },
            ) from exc

    @app.get("/api/sessions", dependencies=[Depends(require_http_token)])
    async def sessions() -> dict[str, Any]:
        manager: BackendSessionManager = app.state.backend.manager
        current = manager.get_or_load(None)
        return {
            "sessions": manager.list(),
            "current_session_id": current.session_id,
            "history": manager.history_json(current),
        }

    @app.post("/api/sessions", dependencies=[Depends(require_http_token)])
    async def create_session() -> dict[str, Any]:
        manager: BackendSessionManager = app.state.backend.manager
        session = manager.create()
        return {"session": _session_payload(manager, session), "sessions": manager.list()}

    @app.post("/api/sessions/{session_id}/select", dependencies=[Depends(require_http_token)])
    async def select_session(session_id: str) -> dict[str, Any]:
        manager: BackendSessionManager = app.state.backend.manager
        try:
            session = manager.select(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"session": _session_payload(manager, session), "sessions": manager.list()}

    @app.post("/api/sessions/{session_id}/clear", dependencies=[Depends(require_http_token)])
    async def clear_session(session_id: str) -> dict[str, Any]:
        manager: BackendSessionManager = app.state.backend.manager
        try:
            session = await manager.clear(session_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session": _session_payload(manager, session), "sessions": manager.list()}

    @app.post("/api/sessions/{session_id}/compact", dependencies=[Depends(require_http_token)])
    async def compact_session(session_id: str) -> dict[str, Any]:
        manager: BackendSessionManager = app.state.backend.manager
        result = await app.state.backend.sessions_compact(session_id)
        session = manager.get_or_load(session_id)
        return {
            "compacted": result.success,
            "summary": result.summary,
            "tokens": result.tokens_after,
            "session": _session_payload(manager, session),
        }

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        expected = app.state.token
        supplied = websocket.query_params.get("token", "")
        if expected and not secrets.compare_digest(supplied, expected):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        backend = app.state.backend
        manager: BackendSessionManager = backend.manager
        send_lock = asyncio.Lock()
        turn_sessions: dict[str, str] = {}

        async def send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await _ws_send(websocket, payload)

        async def emit(payload: dict[str, Any]) -> None:
            await send(payload)

        async def push_pump() -> None:
            async for event in backend.subscribe():
                if event.event == "runtime.changed":
                    await send({"type": "state.updated", **await backend.webui_state()})
                    continue
                for payload in _webui_payloads_from_push(event, manager, turn_sessions):
                    await send(payload)

        push_task = asyncio.create_task(push_pump(), name="webui.backend.push")

        try:
            await emit({"type": "state.updated", **await backend.webui_state()})
            current = manager.create()
            await emit({"type": "session.updated", "session": _session_payload(manager, current), "sessions": manager.list()})
            while True:
                message = await websocket.receive_json()
                msg_type = message.get("type")
                if msg_type == "chat.send":
                    req = ChatRequest(**message)
                    session = manager.get_or_load(req.session_id)
                    try:
                        attachments = decode_attachment_payloads(req.attachments)
                    except Exception as exc:  # noqa: BLE001
                        await emit({"type": "turn.error", "session_id": session.session_id, "message": f"attachment error: {exc}"})
                        continue
                    if not req.text.strip() and not attachments:
                        await emit({"type": "turn.error", "session_id": session.session_id, "message": "empty message"})
                        continue
                    try:
                        await backend.chat_send(
                            session_key=session.session_id,
                            text=req.text,
                            attachments=attachments,
                            turn_source="webui",
                            response_style=req.response_style,
                        )
                    except BusyError as exc:
                        await emit({"type": "turn.error", "session_id": session.session_id, "message": str(exc)})
                    except BackendError as exc:
                        await emit({"type": "turn.error", "session_id": session.session_id, "message": str(exc)})
                elif msg_type == "turn.cancel":
                    req = TurnCancelRequest(**message)
                    await backend.chat_abort(turn_id=req.turn_id)
                    await emit({
                        "type": "turn.cancelled",
                        "turn_id": req.turn_id,
                        "session_id": turn_sessions.get(req.turn_id),
                    })
                elif msg_type == "approval.decide":
                    req = ApprovalDecisionRequest(**message)
                    allow = req.decision != "deny"
                    scope = "always" if req.decision == "allow-always" else "once"
                    try:
                        await backend.approvals_respond(req.request_id, allow=allow, scope=scope)
                        ok = True
                    except NotFoundError:
                        ok = False
                    await emit({"type": "approval.decided", "request_id": req.request_id, "accepted": ok})
                elif msg_type == "session.select":
                    req = SessionSelectRequest(**message)
                    try:
                        session = manager.select(req.session_id)
                    except KeyError as exc:
                        await emit({"type": "session.error", "session_id": req.session_id, "message": str(exc), "sessions": manager.list()})
                        continue
                    await emit({"type": "session.updated", "session": _session_payload(manager, session)})
                elif msg_type == "thinking.set":
                    req = ThinkingSetRequest(**message)
                    if req.level not in _thinking_levels():
                        await emit({"type": "turn.error", "message": f"invalid thinking level: {req.level}"})
                        continue
                    try:
                        await backend.runtime_update(thinking_level=req.level)
                    except BackendError as exc:
                        await emit({"type": "turn.error", "message": str(exc)})
                elif msg_type == "runtime.set":
                    req = RuntimeSetRequest(**message)
                    if req.thinking_level is not None and req.thinking_level not in _thinking_levels():
                        await emit({"type": "turn.error", "message": f"invalid thinking level: {req.thinking_level}"})
                        continue
                    state_payload = await backend.webui_state()
                    agent_ids = {item["id"] for item in state_payload.get("agent_options", [])}
                    model_refs = {item["ref"] for item in state_payload.get("model_options", [])}
                    image_model_refs = {item["ref"] for item in state_payload.get("image_model_options", [])}
                    if req.agent_id is not None and req.agent_id not in agent_ids:
                        await emit({"type": "turn.error", "message": f"invalid agent: {req.agent_id}"})
                        continue
                    if req.model_ref is not None and req.model_ref not in model_refs:
                        await emit({"type": "turn.error", "message": f"invalid model: {req.model_ref}"})
                        continue
                    if req.image_model_ref is not None and req.image_model_ref not in image_model_refs:
                        await emit({"type": "turn.error", "message": f"invalid image model: {req.image_model_ref}"})
                        continue
                    if _has_active_turn(manager):
                        await emit({"type": "turn.error", "message": "cannot change runtime while a turn is active"})
                        continue

                    try:
                        await backend.runtime_update(
                            agent_id=req.agent_id,
                            model_ref=req.model_ref,
                            image_model_ref=req.image_model_ref,
                            thinking_level=req.thinking_level,
                        )
                    except Exception as exc:  # noqa: BLE001
                        await emit({"type": "turn.error", "message": f"runtime update failed: {type(exc).__name__}: {exc}"})
                elif msg_type == "command.run":
                    cmd_text = message.get("command", "")
                    session_id_cmd = message.get("session_id")
                    state_before_command = await backend.webui_state()

                    # Try the shared dispatcher first (services/slash.py). It
                    # handles all 19 core commands — including /models /model
                    # — through Backend RPCs, so webui no longer needs its own
                    # branches for /clear /new /sessions /model /context …
                    from nano_openclaw.services.slash import handle_slash, QuitREPL
                    from nano_openclaw.services.slash_renderer import MarkdownRenderer
                    backend = app.state.backend
                    md = MarkdownRenderer()
                    slash_state = {
                        "session_key": session_id_cmd or "",
                        "session_changed": False,
                    }
                    try:
                        handled_by_shared = await handle_slash(cmd_text, backend, md, slash_state)
                    except QuitREPL:
                        # WebUI cannot quit a daemon-mounted server.
                        await emit({
                            "type": "command.result",
                            "command": cmd_text,
                            "text": "_(WebUI cannot quit the gateway — close the tab instead)_",
                        })
                        handled_by_shared = True

                    if handled_by_shared:
                        text = md.collect()
                        await emit({"type": "command.result", "command": cmd_text, "text": text})
                        # The shared dispatcher may have swapped runtime state
                        # (via /model) or mutated session bindings (via /new
                        # /clear /session). Re-sync local references so this
                        # ws handler keeps using the current service state.
                        state_after_command = await backend.webui_state()
                        if state_after_command != state_before_command:
                            await emit({"type": "state.updated", **state_after_command})
                        if slash_state.get("session_changed"):
                            target_id = slash_state.get("session_key") or session_id_cmd
                            try:
                                refreshed = manager.get_or_load(target_id) if target_id else manager.get_or_load(None)
                            except KeyError:
                                refreshed = manager.get_or_load(None)
                            await emit({
                                "type": "session.updated",
                                "session": _session_payload(manager, refreshed),
                                "sessions": manager.list(),
                            })
                        continue

                    # Shared dispatcher returned False — the input either is
                    # not a slash (shouldn't happen for command.run) or is an
                    # unrecognised one. Route it to the agent loop so the
                    # model can decide what to do with it (matches CLI fall-
                    # through semantics).
                    session = manager.get_or_load(session_id_cmd)
                    await emit({"type": "command.result", "command": cmd_text, "text": ""})
                    try:
                        await backend.chat_send(session_key=session.session_id, text=cmd_text, attachments=[], turn_source="webui")
                    except BackendError as exc:
                        await emit({"type": "turn.error", "session_id": session.session_id, "message": str(exc)})
                elif msg_type == "session.refresh":
                    try:
                        session = manager.get_or_load(message.get("session_id"))
                    except KeyError:
                        session = manager.get_or_load(None)
                    await emit({"type": "state.updated", **await backend.webui_state()})
                    await emit({"type": "session.updated", "session": _session_payload(manager, session), "sessions": manager.list()})
                else:
                    await emit({"type": "turn.error", "message": f"unknown message type: {msg_type}"})
        except WebSocketDisconnect:
            pass
        finally:
            push_task.cancel()
            try:
                await push_task
            except (asyncio.CancelledError, BaseException):
                pass

    return app


def _webui_payloads_from_push(
    event: PushEvent,
    manager: BackendSessionManager,
    turn_sessions: dict[str, str],
) -> list[dict[str, Any]]:
    if event.event == "agent.event":
        payload = dict(event.payload)
        if payload.get("type") == "turn.started":
            payload["type"] = "chat.accepted"
        turn_id = payload.get("turn_id")
        session_id = payload.get("session_id") or payload.get("session_key")
        if isinstance(turn_id, str) and isinstance(session_id, str):
            turn_sessions[turn_id] = session_id
        elif isinstance(turn_id, str) and turn_id in turn_sessions:
            payload["session_id"] = turn_sessions[turn_id]
            session_id = payload["session_id"]
        if payload.get("type") in {"turn.done", "turn.cancelled", "turn.error"} and isinstance(session_id, str):
            try:
                session = manager.get_or_load(session_id)
                payload["session"] = _session_payload(manager, session)
                payload["sessions"] = manager.list()
            except KeyError:
                pass
        return [payload]

    if event.event == "approval.request":
        payload = dict(event.payload)
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id in turn_sessions:
            payload["session_id"] = turn_sessions[turn_id]
        return [payload]

    if event.event == "session.changed":
        session_id = event.payload.get("session_id") or event.payload.get("session_key")
        if isinstance(session_id, str):
            try:
                session = manager.get_or_load(session_id)
            except KeyError:
                session = manager.get_or_load(None)
        else:
            session = manager.get_or_load(None)
        return [{"type": "session.updated", "session": _session_payload(manager, session), "sessions": manager.list()}]

    if event.event == "gap":
        return [{"type": "session.error", "message": "event stream lagged; refreshing session", "sessions": manager.list()}]

    return []


async def _ws_send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_text(json.dumps(_jsonable(payload), ensure_ascii=False))
    except RuntimeError as e:
        if "websocket.close" in str(e) or "already completed" in str(e):
            return
        raise


def _session_payload(manager: BackendSessionManager, session: Any) -> dict[str, Any]:
    visible_history = display_history(session.history)
    history_offset = max(0, len(visible_history) - SESSION_PAYLOAD_HISTORY_LIMIT)
    payload_history = visible_history[history_offset:]
    payload_activities = _recent_activity_json(manager, session, history_offset)
    return {
        "session_id": session.session_id,
        "message_count": session.writer.message_count,
        "compaction_count": session.writer.compaction_count,
        "active_turn_id": session.active_turn_id,
        "history": [{"role": message.role, "content": message.content} for message in payload_history],
        "history_offset": history_offset,
        "history_truncated": history_offset > 0,
        "activities": payload_activities,
        "preview": message_text(visible_history[-1])[:160] if visible_history else "",
    }


def _recent_activity_json(manager: BackendSessionManager, session: Any, history_offset: int) -> list[dict[str, Any]]:
    activities = manager.activity_json(session)
    recent = []
    for activity in activities[-SESSION_PAYLOAD_ACTIVITY_LIMIT:]:
        item = dict(activity)
        insert_after_index = item.get("insert_after_index")
        if isinstance(insert_after_index, int) and insert_after_index >= history_offset:
            item["insert_after_index"] = insert_after_index - history_offset
        elif history_offset > 0:
            item["insert_after_index"] = -1
        recent.append(item)
    return recent
