"""FastAPI WebUI server."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nano_openclaw.attachments import decode_attachment_payloads
from nano_openclaw.gateway._event_payload import (
    event_to_payload as _event_to_payload,
    is_replayable_activity_payload as _is_replayable_activity_payload,
    jsonable as _jsonable,
)
from nano_openclaw.gateway.backend import BackendError, BusyError, NotFoundError, PushEvent
from nano_openclaw.runtime import AgentRuntime
from nano_openclaw.gateway.agent_backend_session import BackendSessionManager, display_history, message_text


SESSION_PAYLOAD_HISTORY_LIMIT = 80
SESSION_PAYLOAD_ACTIVITY_LIMIT = 120

class ChatRequest(BaseModel):
    session_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] = []


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

    @app.get("/api/state", dependencies=[Depends(require_http_token)])
    async def state() -> dict[str, Any]:
        return _state_payload(app.state.backend.runtime)

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
                for payload in _webui_payloads_from_push(event, manager, backend.runtime, turn_sessions):
                    await send(payload)

        push_task = asyncio.create_task(push_pump(), name="webui.backend.push")

        try:
            await emit({"type": "state.updated", **_state_payload(backend.runtime)})
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
                    runtime: AgentRuntime = backend.runtime
                    if req.thinking_level is not None and req.thinking_level not in _thinking_levels():
                        await emit({"type": "turn.error", "message": f"invalid thinking level: {req.thinking_level}"})
                        continue
                    agent_ids = {item["id"] for item in _agent_options(runtime.config)}
                    model_refs = {item["ref"] for item in _model_options(runtime.config)}
                    image_model_refs = {item["ref"] for item in _image_model_options(runtime.config)}
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
                    runtime_before_command = backend.runtime

                    # Try the shared dispatcher first (gateway/slash.py). It
                    # handles all 19 core commands — including /models /model
                    # — through Backend RPCs, so webui no longer needs its own
                    # branches for /clear /new /sessions /model /context …
                    from nano_openclaw.gateway.slash import handle_slash, QuitREPL
                    from nano_openclaw.gateway.slash_renderer import MarkdownRenderer
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
                        if text:
                            await emit({"type": "command.result", "command": cmd_text, "text": text})
                        # The shared dispatcher may have swapped backend.runtime
                        # (via /model) or mutated session bindings (via /new
                        # /clear /session). Re-sync local references so this
                        # ws handler keeps using the current runtime + session
                        # set, and broadcast state.updated for the front-end.
                        new_runtime = backend.runtime
                        if new_runtime is not runtime_before_command:
                            await emit({"type": "state.updated", **_state_payload(new_runtime)})
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
                    try:
                        await backend.chat_send(session_key=session.session_id, text=cmd_text, attachments=[], turn_source="webui")
                    except BackendError as exc:
                        await emit({"type": "turn.error", "session_id": session.session_id, "message": str(exc)})
                elif msg_type == "session.refresh":
                    try:
                        session = manager.get_or_load(message.get("session_id"))
                    except KeyError:
                        session = manager.get_or_load(None)
                    await emit({"type": "state.updated", **_state_payload(backend.runtime)})
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
    runtime: AgentRuntime,
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

    if event.event == "runtime.changed":
        return [{"type": "state.updated", **_state_payload(runtime)}]

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


def _state_payload(runtime: AgentRuntime) -> dict[str, Any]:
    hook_registry = runtime.registry.hook_registry()
    return {
        "agent_id": runtime.agent_id,
        "agent_options": _agent_options(runtime.config),
        "model": runtime.model_id,
        "model_ref": runtime.model_ref,
        "model_options": _model_options(runtime.config),
        "image_model": runtime.cfg.image_model,
        "image_model_ref": runtime.image_model_ref or "",
        "image_model_options": _image_model_options(runtime.config),
        "thinking_level": runtime.cfg.thinking_level,
        "thinking_options": list(_thinking_levels()),
        "assistant_name": _read_assistant_name(runtime.workspace_dir),
        "user_name": _read_user_name(runtime.workspace_dir),
        "workspace_dir": str(runtime.workspace_dir),
        "tools": runtime.registry.names(),
        "plugins": [_jsonable(plugin) for plugin in getattr(hook_registry, "plugins", lambda: [])()],
        "hooks": getattr(hook_registry, "handler_counts", lambda: {})(),
        "skills": {
            "filter": runtime.cfg.skill_filter,
            "extra_dirs": runtime.cfg.extra_skill_dirs,
        },
        "warnings": runtime.warnings,
    }


def _thinking_levels() -> tuple[str, ...]:
    return ("off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max")


def _agent_options(config: Any) -> list[dict[str, Any]]:
    agents = list(config.agents.list or [])
    default_id = None
    for agent in agents:
        if agent.default:
            default_id = agent.id
            break
    if default_id is None:
        default_id = agents[0].id if agents else "default"

    if not agents:
        return [{"id": "default", "name": "Default Agent", "default": True}]

    return [
        {
            "id": agent.id,
            "name": agent.name or agent.id,
            "default": agent.id == default_id,
        }
        for agent in agents
    ]


def _model_options(config: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []

    def add(ref: str | None, name: str | None = None, input: list[str] | None = None) -> None:
        if not ref or "/" not in ref:
            return
        if ref in seen:
            if name or input:
                for item in result:
                    if item["ref"] != ref:
                        continue
                    if name and item["name"] == ref:
                        item["name"] = name
                    if input and not item.get("input"):
                        item["input"] = input
                        break
            return
        seen.add(ref)
        result.append({"ref": ref, "name": name or ref, "input": input or []})

    add(config.resolve_primary_model())
    for agent in config.agents.list:
        add(config.resolve_primary_model(agent.id))
    for provider_id, provider in config.models.providers.items():
        for model in provider.models:
            add(f"{provider_id}/{model.id}", model.name or model.id, list(model.input or []))

    return result


def _image_model_options(config: Any) -> list[dict[str, Any]]:
    seen: set[str] = {""}
    result: list[dict[str, Any]] = [{"ref": "", "name": "Native Vision", "input": ["image"]}]

    def add(ref: str | None, name: str | None = None, input: list[str] | None = None) -> None:
        if not ref or "/" not in ref:
            return
        if "image" not in (input or []):
            return
        if ref in seen:
            if name or input:
                for item in result:
                    if item["ref"] != ref:
                        continue
                    if name and item["name"] == ref:
                        item["name"] = name
                    if input and not item.get("input"):
                        item["input"] = input
                        break
            return
        seen.add(ref)
        result.append({"ref": ref, "name": name or ref, "input": input or []})

    for provider_id, provider in config.models.providers.items():
        for model in provider.models:
            add(f"{provider_id}/{model.id}", model.name or model.id, list(model.input or []))

    return result


def _has_active_turn(manager: BackendSessionManager) -> bool:
    return any(session.active_turn_id for session in manager._loaded.values())


def _read_assistant_name(workspace_dir: Path) -> str:
    return _read_profile_field(workspace_dir / "IDENTITY.md", "Name", "Assistant")


def _read_user_name(workspace_dir: Path) -> str:
    return _read_profile_field(workspace_dir / "USER.md", "What to call them", "User")


def _read_profile_field(path: Path, field_name: str, fallback: str) -> str:
    if not path.exists() or not path.is_file():
        return fallback
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return fallback

    for index, line in enumerate(lines):
        parsed = _parse_profile_field_line(line, field_name)
        if parsed:
            return parsed
        if _profile_line_label(line).lower() == field_name.lower():
            for follow in lines[index + 1:index + 4]:
                candidate = _clean_profile_value(follow)
                if candidate:
                    return candidate
    return fallback


def _parse_profile_field_line(line: str, field_name: str) -> str:
    normalized = line.strip().lstrip("-").strip()
    normalized = normalized.replace("**", "")
    if not normalized.lower().startswith(field_name.lower()):
        return ""
    _label, sep, value = normalized.partition(":")
    if not sep:
        return ""
    if _label.strip().lower() != field_name.lower():
        return ""
    return _clean_profile_value(value)


def _profile_line_label(line: str) -> str:
    normalized = line.strip().lstrip("-").strip().replace("**", "")
    label, sep, _value = normalized.partition(":")
    return label.strip() if sep else ""


def _clean_profile_value(value: str) -> str:
    cleaned = value.strip().lstrip("-").strip()
    cleaned = cleaned.replace("**", "").strip()
    if not cleaned or cleaned.startswith("_(") or cleaned.startswith("("):
        return ""
    return cleaned[:80]
