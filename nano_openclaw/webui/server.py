"""FastAPI WebUI server."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nano_openclaw.compact import compact_if_needed, estimate_tokens
from nano_openclaw.loop import (
    ActiveMemoryRecall,
    CancellationToken,
    Compaction,
    ImageAttached,
    ImageDescribe,
    ImageError,
    ImageSkip,
    SkillInvoked,
    SubagentAnnounced,
    SubagentKilled,
    SubagentProgress,
    SubagentSpawned,
    ToolResult,
    TurnCancelled,
    agent_loop,
    run_pre_compaction_memory_flush,
)
from nano_openclaw.provider import (
    MessageEnd,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
)
from nano_openclaw.tools import ToolRegistry
from nano_openclaw.webui.approvals import WebApprovalBroker
from nano_openclaw.webui.runtime import AgentRuntime, build_agent_runtime, build_approval_manager
from nano_openclaw.webui.sessions import WebSessionManager, message_text


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ChatRequest(BaseModel):
    session_id: str | None = None
    text: str


class ApprovalDecisionRequest(BaseModel):
    request_id: str
    decision: str


class TurnCancelRequest(BaseModel):
    turn_id: str


class SessionSelectRequest(BaseModel):
    session_id: str


def create_app(*, config_path: str | None, agent_id: str, token: str | None) -> FastAPI:
    static_dir = Path(__file__).with_name("static")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = await build_agent_runtime(config_path=config_path, agent_id=agent_id)
        sessions = WebSessionManager(
            session_dir=runtime.session_dir,
            store_path=runtime.store_path,
            model=runtime.model_id,
            cwd=str(runtime.workspace_dir),
        )
        sessions.get_or_load(None)
        app.state.runtime = runtime
        app.state.sessions = sessions
        app.state.token = token
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="nano-openclaw WebUI", lifespan=lifespan)
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
        runtime: AgentRuntime = app.state.runtime
        return _state_payload(runtime)

    @app.get("/api/sessions", dependencies=[Depends(require_http_token)])
    async def sessions() -> dict[str, Any]:
        manager: WebSessionManager = app.state.sessions
        current = manager.get_or_load(None)
        return {
            "sessions": manager.list(),
            "current_session_id": current.session_id,
            "history": manager.history_json(current),
        }

    @app.post("/api/sessions", dependencies=[Depends(require_http_token)])
    async def create_session() -> dict[str, Any]:
        manager: WebSessionManager = app.state.sessions
        session = manager.create()
        return {"session": _session_payload(manager, session), "sessions": manager.list()}

    @app.post("/api/sessions/{session_id}/select", dependencies=[Depends(require_http_token)])
    async def select_session(session_id: str) -> dict[str, Any]:
        manager: WebSessionManager = app.state.sessions
        try:
            session = manager.select(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"session": _session_payload(manager, session), "sessions": manager.list()}

    @app.post("/api/sessions/{session_id}/clear", dependencies=[Depends(require_http_token)])
    async def clear_session(session_id: str) -> dict[str, Any]:
        manager: WebSessionManager = app.state.sessions
        try:
            session = await manager.clear(session_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session": _session_payload(manager, session), "sessions": manager.list()}

    @app.post("/api/sessions/{session_id}/compact", dependencies=[Depends(require_http_token)])
    async def compact_session(session_id: str) -> dict[str, Any]:
        runtime: AgentRuntime = app.state.runtime
        manager: WebSessionManager = app.state.sessions
        session = manager.get_or_load(session_id)
        async with session.lock:
            if len(session.history) < runtime.cfg.context_recent_turns * 2:
                return {"compacted": False, "reason": "not enough history", "session": _session_payload(manager, session)}
            await run_pre_compaction_memory_flush(
                client=runtime.client,
                cfg=replace(runtime.cfg, session_key=session.session_id),
                history=session.history,
                registry=_clone_registry(runtime.registry),
                force=True,
            )
            _, summary = await compact_if_needed(
                session.history,
                budget=1,
                client=runtime.client,
                model=runtime.cfg.model,
                api=runtime.cfg.api,
                threshold_ratio=1.0,
                recent_turns=runtime.cfg.context_recent_turns,
            )
            if summary:
                session.writer.append_compaction(summary)
                manager.save_metadata(session)
            return {
                "compacted": bool(summary),
                "summary": summary,
                "tokens": estimate_tokens(session.history),
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
        runtime: AgentRuntime = app.state.runtime
        manager: WebSessionManager = app.state.sessions
        send_lock = asyncio.Lock()

        async def send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await _ws_send(websocket, payload)

        approvals = WebApprovalBroker(send)
        active_tokens: dict[str, CancellationToken] = {}

        async def emit(payload: dict[str, Any]) -> None:
            await send(payload)

        try:
            await emit({"type": "state.updated", **_state_payload(runtime)})
            current = manager.get_or_load(None)
            await emit({"type": "session.updated", "session": _session_payload(manager, current), "sessions": manager.list()})
            while True:
                message = await websocket.receive_json()
                msg_type = message.get("type")
                if msg_type == "chat.send":
                    req = ChatRequest(**message)
                    asyncio.create_task(_run_turn(
                        runtime=runtime,
                        manager=manager,
                        send=send,
                        approvals=approvals,
                        active_tokens=active_tokens,
                        session_id=req.session_id,
                        text=req.text,
                    ))
                elif msg_type == "turn.cancel":
                    req = TurnCancelRequest(**message)
                    token_obj = active_tokens.get(req.turn_id)
                    if token_obj:
                        token_obj.cancel()
                        await emit({"type": "turn.cancelled", "turn_id": req.turn_id})
                elif msg_type == "approval.decide":
                    req = ApprovalDecisionRequest(**message)
                    ok = approvals.decide(req.request_id, req.decision)
                    await emit({"type": "approval.decided", "request_id": req.request_id, "accepted": ok})
                elif msg_type == "session.select":
                    req = SessionSelectRequest(**message)
                    try:
                        session = manager.select(req.session_id)
                    except KeyError as exc:
                        await emit({"type": "session.error", "session_id": req.session_id, "message": str(exc), "sessions": manager.list()})
                        continue
                    await emit({"type": "session.updated", "session": _session_payload(manager, session), "sessions": manager.list()})
                elif msg_type == "session.refresh":
                    try:
                        session = manager.get_or_load(message.get("session_id"))
                    except KeyError:
                        session = manager.get_or_load(None)
                    await emit({"type": "state.updated", **_state_payload(runtime)})
                    await emit({"type": "session.updated", "session": _session_payload(manager, session), "sessions": manager.list()})
                else:
                    await emit({"type": "turn.error", "message": f"unknown message type: {msg_type}"})
        except WebSocketDisconnect:
            for token_obj in active_tokens.values():
                token_obj.cancel()
            approvals.deny_all()

    return app


async def _run_turn(
    *,
    runtime: AgentRuntime,
    manager: WebSessionManager,
    send: Any,
    approvals: WebApprovalBroker,
    active_tokens: dict[str, CancellationToken],
    session_id: str | None,
    text: str,
) -> None:
    session = manager.get_or_load(session_id)
    if not text.strip():
        await send({"type": "turn.error", "session_id": session.session_id, "message": "empty message"})
        return
    if session.active_turn_id:
        await send({
            "type": "turn.error",
            "session_id": session.session_id,
            "message": "session already has an active turn",
        })
        return

    turn_id = uuid.uuid4().hex
    token = CancellationToken()
    active_tokens[turn_id] = token
    session.active_turn_id = turn_id
    turn_registry = _clone_registry(runtime.registry)
    turn_registry.approval_handler = approvals.request_decision
    _wire_spawn_context(turn_registry, runtime, session.session_id)
    cfg = replace(runtime.cfg, session_key=session.session_id)
    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def drain_events() -> None:
        send_failed = False
        while True:
            payload = await event_queue.get()
            try:
                if payload is None:
                    return
                if not send_failed:
                    try:
                        await send(payload)
                    except Exception:  # noqa: BLE001 - connection is already closing
                        send_failed = True
            finally:
                event_queue.task_done()

    def on_event(event: Any) -> None:
        event_queue.put_nowait(_event_to_payload(event, turn_id, session.session_id))

    event_drain_task = asyncio.create_task(drain_events())

    await send({
        "type": "chat.accepted",
        "turn_id": turn_id,
        "session_id": session.session_id,
        "user_text": text,
    })

    try:
        async with session.lock:
            await agent_loop(
                user_input=text,
                history=session.history,
                registry=turn_registry,
                on_event=on_event,
                client=runtime.client,
                cfg=cfg,
                transcript_writer=session.writer,
                cancellation_token=token,
            )
            manager.save_metadata(session)
        await send({
            "type": "turn.done",
            "turn_id": turn_id,
            "session_id": session.session_id,
            "session": _session_payload(manager, session),
            "sessions": manager.list(),
        })
    except TurnCancelled:
        await send({"type": "turn.cancelled", "turn_id": turn_id, "session_id": session.session_id})
    except Exception as exc:  # noqa: BLE001
        await send({
            "type": "turn.error",
            "turn_id": turn_id,
            "session_id": session.session_id,
            "message": f"{type(exc).__name__}: {exc}",
        })
    finally:
        event_queue.put_nowait(None)
        await event_queue.join()
        await event_drain_task
        active_tokens.pop(turn_id, None)
        if session.active_turn_id == turn_id:
            session.active_turn_id = None


def _event_to_payload(event: Any, turn_id: str, session_id: str) -> dict[str, Any]:
    base = {"turn_id": turn_id, "session_id": session_id}
    if isinstance(event, TextDelta):
        return {"type": "text.delta", **base, "text": event.text}
    if isinstance(event, ThinkingDelta):
        return {"type": "thinking.delta", **base, "text": event.text}
    if isinstance(event, ThinkingBlockComplete):
        return {"type": "thinking.done", **base, "redacted": event.redacted}
    if isinstance(event, ToolUseStart):
        return {"type": "tool.start", **base, "tool_use_id": event.id, "name": event.name}
    if isinstance(event, ToolUseDelta):
        return {"type": "tool.delta", **base, "tool_use_id": event.id, "partial_json": event.partial_json}
    if isinstance(event, ToolUseEnd):
        return {"type": "tool.end", **base, "tool_use_id": event.id}
    if isinstance(event, ToolResult):
        return {
            "type": "tool.result",
            **base,
            "tool_use_id": event.tool_use_id,
            "name": event.name,
            "args": event.args,
            "result": event.result,
        }
    if isinstance(event, MessageEnd):
        return {"type": "message.end", **base, "stop_reason": event.stop_reason, "usage": event.usage}
    if isinstance(event, Compaction):
        return {"type": "compaction", **base, "summary": event.summary}
    if isinstance(event, ImageDescribe):
        return {"type": "image.status", **base, "ref": event.ref, "status": "describing"}
    if isinstance(event, ImageAttached):
        return {"type": "image.status", **base, "refs": event.refs, "status": "described" if event.via_model else "attached"}
    if isinstance(event, ImageError):
        return {"type": "image.status", **base, "ref": event.ref, "status": "error", "error": event.error}
    if isinstance(event, ImageSkip):
        return {"type": "image.status", **base, "ref": event.ref, "status": "skipped", "reason": event.reason}
    if isinstance(event, SkillInvoked):
        return {"type": "skill.invoked", **base, "skill_name": event.skill_name, "skill_path": event.skill_path}
    if isinstance(event, ActiveMemoryRecall):
        return {"type": "active_memory", **base, "result": _jsonable(event.result)}
    if isinstance(event, SubagentSpawned):
        return {"type": "subagent.status", **base, "status": "spawned", **_jsonable(event)}
    if isinstance(event, SubagentAnnounced):
        return {"type": "subagent.status", **base, "status": event.status, **_jsonable(event)}
    if isinstance(event, SubagentKilled):
        return {"type": "subagent.status", **base, "status": "killed", **_jsonable(event)}
    if isinstance(event, SubagentProgress):
        return {"type": "subagent.status", **base, "status": "progress", **_jsonable(event)}
    return {"type": "event", **base, "event_type": type(event).__name__, "payload": _jsonable(event)}


async def _ws_send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_text(json.dumps(_jsonable(payload), ensure_ascii=False))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _session_payload(manager: WebSessionManager, session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "message_count": session.writer.message_count,
        "compaction_count": session.writer.compaction_count,
        "active_turn_id": session.active_turn_id,
        "history": manager.history_json(session),
        "preview": message_text(session.history[-1])[:160] if session.history else "",
    }


def _state_payload(runtime: AgentRuntime) -> dict[str, Any]:
    hook_registry = runtime.registry.hook_registry()
    return {
        "agent_id": runtime.agent_id,
        "model": runtime.model_id,
        "model_ref": runtime.model_ref,
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


def _clone_registry(registry: ToolRegistry) -> ToolRegistry:
    clone = ToolRegistry(
        _tools=dict(registry._tools),
        approval_manager=registry.approval_manager,
        console=registry.console,
        _workspace_dir=registry._workspace_dir,
    )
    clone.set_session_status_context(**registry._session_status_context)
    clone.set_eligible_skills(dict(registry._eligible_skills))
    if registry.hook_registry() is not None:
        clone.set_hook_registry(registry.hook_registry())
    return clone


def _wire_spawn_context(registry: ToolRegistry, runtime: AgentRuntime, session_id: str) -> None:
    if registry.get("sessions_spawn") is None:
        return
    from nano_openclaw.subagent.tools import SpawnToolContext
    registry.set_spawn_tool_context(SpawnToolContext(
        requester_session_key=session_id,
        session_dir=runtime.session_dir,
        workspace_dir=runtime.workspace_dir,
        client=runtime.client,
        base_cfg=replace(runtime.cfg, session_key=session_id),
        on_event=lambda _event: None,
        parent_registry=registry,
    ))


def run_webui(
    *,
    config_path: str | None,
    agent_id: str,
    host: str,
    port: int,
    token: str | None,
) -> None:
    if host not in LOCAL_HOSTS and not token:
        raise SystemExit("error: --token is required when --host is not localhost")
    import uvicorn
    app = create_app(config_path=config_path, agent_id=agent_id, token=token)
    uvicorn.run(app, host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nano-openclaw web")
    parser.add_argument("--config", default=None)
    parser.add_argument("--agent", default="default")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None)
    args = parser.parse_args(argv)
    run_webui(
        config_path=args.config,
        agent_id=args.agent,
        host=args.host,
        port=args.port,
        token=args.token,
    )
