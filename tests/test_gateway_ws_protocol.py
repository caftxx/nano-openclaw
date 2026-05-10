"""Gateway WebSocket protocol tests — frame encoding, dispatch, e2e via TestClient.

Covers:
- Pydantic frame validation (Request / Response / PushFrame).
- ``_dispatch_one`` error mapping for unknown methods, BusyError, NotFoundError,
  NotImplementedError, generic exceptions.
- End-to-end via ``fastapi.testclient.TestClient`` — open ws, send a real
  ``health`` request, observe the Response.

Doesn't exercise the actual daemon process (that's covered in
test_gateway_lifecycle.py); the FastAPI route is mounted on a TestClient
app so we can avoid spawning subprocesses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nano_openclaw.channels.registry import ChannelRegistry
from nano_openclaw.gateway.backend import BusyError, NotFoundError
from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.context import GatewayContext
from nano_openclaw.gateway.protocol import (
    ErrorCode,
    METHODS_V1,
    PushFrame,
    Request,
    Response,
    encode_push,
    encode_response,
    make_error_response,
    make_ok_response,
)
from nano_openclaw.gateway.ws_route import _dispatch_one, register_ws_route
from nano_openclaw.loop import LoopConfig
from nano_openclaw.tools import ToolRegistry


# ────────────────────────────────────────────────────────────────────────────
# Fake runtime + GatewayContext fixture
# ────────────────────────────────────────────────────────────────────────────


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    from nano_openclaw.gateway.run_registry import RunRegistry
    from nano_openclaw.gateway.runtime_lock import RuntimeUpdateGuard
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="test-model", workspace_dir=workspace, session_key="default")
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=SimpleNamespace(),
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=cfg,
        hook_registry=None,
        state_dir=state,
        session_dir=sd,
        store_path=tmp_path / "sessions.json",
        workspace_dir=workspace,
        model_ref="test/test-model",
        model_id="test-model",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
    )


def _make_ctx(tmp_path: Path) -> tuple[GatewayContext, EmbeddedBackend]:
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    ctx = GatewayContext(
        runtime=runtime,
        backend=backend,
        channel_registry=ChannelRegistry(),
    )
    return ctx, backend


# ────────────────────────────────────────────────────────────────────────────
# Frame validation + encoders
# ────────────────────────────────────────────────────────────────────────────


def test_request_frame_roundtrip():
    raw = '{"id": "abc", "method": "health", "params": {}}'
    req = Request.model_validate_json(raw)
    assert req.id == "abc"
    assert req.method == "health"


def test_request_frame_rejects_missing_id():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Request.model_validate({"method": "health"})


def test_response_encode_with_payload():
    resp = make_ok_response("rid", {"runtime_ready": True})
    decoded = json.loads(encode_response(resp))
    assert decoded == {"id": "rid", "ok": True, "payload": {"runtime_ready": True}}


def test_response_encode_with_error():
    resp = make_error_response(
        "rid",
        ErrorCode.BUSY,
        "session has an active turn",
        retryable=True,
        retry_after_ms=500,
        details={"session_id": "s1"},
    )
    decoded = json.loads(encode_response(resp))
    assert decoded["id"] == "rid"
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "BUSY"
    assert decoded["error"]["retry_after_ms"] == 500
    assert decoded["error"]["details"]["session_id"] == "s1"


def test_push_frame_encode():
    frame = PushFrame(event="agent.event", payload={"type": "text.delta", "text": "hi"}, seq=42)
    decoded = json.loads(encode_push(frame))
    assert decoded == {
        "event": "agent.event",
        "payload": {"type": "text.delta", "text": "hi"},
        "seq": 42,
    }


def test_handlers_match_methods_v1():
    """Single source of truth: METHODS_V1 must enumerate exactly what the
    handler registry exposes — drift here means a method is undocumented or
    a handler is missing.
    """
    from nano_openclaw.gateway.methods import CORE_HANDLERS
    assert set(CORE_HANDLERS.keys()) == set(METHODS_V1)


# ────────────────────────────────────────────────────────────────────────────
# _dispatch_one: error mapping
# ────────────────────────────────────────────────────────────────────────────


def test_dispatch_returns_invalid_request_on_bad_json(tmp_path: Path):
    ctx, _ = _make_ctx(tmp_path)
    resp = asyncio.run(_dispatch_one(ctx, "{not json"))
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.INVALID_REQUEST


def test_dispatch_returns_invalid_request_on_missing_method(tmp_path: Path):
    ctx, _ = _make_ctx(tmp_path)
    resp = asyncio.run(_dispatch_one(ctx, json.dumps({"id": "x", "params": {}})))
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.INVALID_REQUEST


def test_dispatch_returns_unknown_method(tmp_path: Path):
    ctx, _ = _make_ctx(tmp_path)
    raw = json.dumps({"id": "x", "method": "does.not.exist", "params": {}})
    resp = asyncio.run(_dispatch_one(ctx, raw))
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.UNKNOWN_METHOD


def test_dispatch_busy_error_returns_busy_code(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)

    async def run():
        # Create the session inside the asyncio loop so its asyncio.Lock
        # binds to the right loop.
        session = backend.manager.create()
        await session.lock.acquire()
        session.active_turn_id = "fake"
        try:
            raw = json.dumps({
                "id": "rid",
                "method": "chat.send",
                "params": {"session_key": session.session_id, "text": "hello"},
            })
            return await _dispatch_one(ctx, raw)
        finally:
            session.lock.release()
            session.active_turn_id = None
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.BUSY
    assert resp.error.retryable is True
    assert resp.error.retry_after_ms == 500


def test_dispatch_not_found_returns_not_found_code(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)
    raw = json.dumps({
        "id": "rid",
        "method": "approvals.respond",
        "params": {"request_id": "unknown-id", "allow": True},
    })

    async def run():
        try:
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.NOT_FOUND


def test_dispatch_unimplemented_method_returns_unavailable(tmp_path: Path):
    """The dispatcher must translate ``NotImplementedError`` into the wire
    ``UNAVAILABLE`` code so clients can present a clear "feature not ready"
    message. We patch a known method to raise NotImplementedError to exercise
    that branch independent of which specific method happens to be a stub
    (``runtime.update`` used to be one but is now implemented).
    """
    from unittest.mock import patch

    ctx, backend = _make_ctx(tmp_path)
    raw = json.dumps({
        "id": "rid",
        "method": "health",
        "params": {},
    })

    async def run():
        try:
            with patch.object(
                backend,
                "health",
                side_effect=NotImplementedError("forced for test"),
            ):
                return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.UNAVAILABLE


def test_dispatch_health_succeeds(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)
    raw = json.dumps({"id": "rid", "method": "health", "params": {}})

    async def run():
        try:
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload is not None
    assert resp.payload["runtime_ready"] is True
    assert resp.payload["channels_running"] == 0
    assert resp.payload["in_flight_turns"] == 0


def test_dispatch_runtime_get_succeeds(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)
    raw = json.dumps({"id": "rid", "method": "runtime.get", "params": {}})

    async def run():
        try:
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["agent_id"] == "default"
    assert resp.payload["model_id"] == "test-model"


def test_dispatch_models_list_succeeds(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)
    raw = json.dumps({"id": "rid", "method": "models.list", "params": {}})

    async def run():
        try:
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert "models" in resp.payload
    assert any(m["id"] == "test-model" for m in resp.payload["models"])


# ────────────────────────────────────────────────────────────────────────────
# End-to-end: FastAPI TestClient over a real WebSocket
# ────────────────────────────────────────────────────────────────────────────


def test_e2e_websocket_health_request(tmp_path: Path):
    """Spin up the FastAPI app with the /rpc route mounted, open a WebSocket
    via TestClient, send a real Request frame, receive a real Response.
    """
    ctx, backend = _make_ctx(tmp_path)
    app = FastAPI()
    register_ws_route(app, ctx)

    try:
        with TestClient(app) as client:
            with client.websocket_connect("/rpc") as ws:
                ws.send_text(json.dumps({"id": "h1", "method": "health", "params": {}}))
                raw = ws.receive_text()
                msg = json.loads(raw)
                assert msg["id"] == "h1"
                assert msg["ok"] is True
                assert msg["payload"]["runtime_ready"] is True
    finally:
        asyncio.run(backend.aclose())


def test_e2e_websocket_unknown_method_returns_error(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)
    app = FastAPI()
    register_ws_route(app, ctx)

    try:
        with TestClient(app) as client:
            with client.websocket_connect("/rpc") as ws:
                ws.send_text(json.dumps({"id": "u1", "method": "does.not.exist"}))
                msg = json.loads(ws.receive_text())
                assert msg["ok"] is False
                assert msg["error"]["code"] == "UNKNOWN_METHOD"
    finally:
        asyncio.run(backend.aclose())


def test_e2e_websocket_runtime_get_returns_snapshot(tmp_path: Path):
    ctx, backend = _make_ctx(tmp_path)
    app = FastAPI()
    register_ws_route(app, ctx)

    try:
        with TestClient(app) as client:
            with client.websocket_connect("/rpc") as ws:
                ws.send_text(json.dumps({"id": "rt", "method": "runtime.get"}))
                msg = json.loads(ws.receive_text())
                assert msg["ok"] is True
                assert msg["payload"]["model_id"] == "test-model"
    finally:
        asyncio.run(backend.aclose())


def test_e2e_websocket_session_changed_push_after_reset(tmp_path: Path):
    """Trigger a sessions.reset request and observe the resulting
    ``session.changed`` push frame on the same connection.
    """
    ctx, backend = _make_ctx(tmp_path)
    app = FastAPI()
    register_ws_route(app, ctx)

    try:
        with TestClient(app) as client:
            with client.websocket_connect("/rpc") as ws:
                ws.send_text(json.dumps({
                    "id": "rs",
                    "method": "sessions.reset",
                    "params": {"session_key": "anything", "reason": "new"},
                }))

                # We expect both a Response (with id="rs") and a PushFrame
                # (event="session.changed"). Collect frames until we see both
                # or hit a small budget.
                seen_response = False
                seen_push = False
                for _ in range(5):
                    raw = ws.receive_text()
                    msg = json.loads(raw)
                    if msg.get("id") == "rs" and msg.get("ok") is True:
                        seen_response = True
                    elif msg.get("event") == "session.changed":
                        seen_push = True
                    if seen_response and seen_push:
                        break

                assert seen_response, "did not receive sessions.reset Response"
                assert seen_push, "did not receive session.changed push"
    finally:
        asyncio.run(backend.aclose())
