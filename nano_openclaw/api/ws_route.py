"""FastAPI WebSocket ``/rpc`` endpoint — the daemon's remote control surface.

Per connection:

1. **Subscribe task** — pulls events from ``backend.subscribe()`` and forwards
   them as ``PushFrame``. Backend already applies bounded queue + ``gap``
   event on overflow, so slow consumers don't stall the producer.
2. **Read loop** — reads JSON Request frames, dispatches to the handler in
   ``CORE_HANDLERS``, sends back a Response.

Errors:

- Unknown method → ``UNKNOWN_METHOD`` Response.
- Backend ``BusyError`` → ``BUSY`` Response with retry hints.
- Backend ``NotFoundError`` → ``NOT_FOUND``.
- ``NotImplementedError`` from stub Backend methods → ``UNAVAILABLE``.
- Anything else → ``INTERNAL`` (logged at warning level).

v1 has no auth (per user decision). The route binds to ``config.gateway.host``;
non-loopback bind warns at daemon startup.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from nano_openclaw.services.backend import BusyError, NotFoundError
from nano_openclaw.api.context import GatewayContext
from nano_openclaw.api.methods import CORE_HANDLERS
from nano_openclaw.api.protocol import (
    ErrorCode,
    PushFrame,
    Request,
    Response,
    encode_push,
    encode_response,
    make_error_response,
    make_ok_response,
)
from nano_openclaw.logger import get_logger

log = get_logger(__name__)


def register_ws_route(app: FastAPI, ctx: GatewayContext, *, path: str = "/rpc") -> None:
    """Mount the ``/rpc`` WebSocket route on ``app``.

    Idempotent on the FastAPI app — re-registration would double-mount, so
    the daemon should call this exactly once during ``run_daemon`` startup.
    """

    @app.websocket(path)
    async def rpc_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        log.info("ws.rpc.connected", f"client={websocket.client}")

        # ── Push fanout: subscribe to all backend events for this connection ──
        push_iter = ctx.backend.subscribe()

        async def push_pump() -> None:
            try:
                async for evt in push_iter:
                    frame = PushFrame(event=evt.event, payload=evt.payload, seq=evt.seq)
                    try:
                        await websocket.send_text(encode_push(frame))
                    except (WebSocketDisconnect, RuntimeError):
                        return
                    except Exception as exc:  # noqa: BLE001
                        log.warning("ws.rpc.push.error", f"{type(exc).__name__}: {exc}")
                        return
            except asyncio.CancelledError:
                raise

        push_task = asyncio.create_task(push_pump(), name="ws-rpc-push")

        # ── Read loop: dispatch incoming Request frames ──
        try:
            while True:
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    break

                response = await _dispatch_one(ctx, raw)
                try:
                    await websocket.send_text(encode_response(response))
                except (WebSocketDisconnect, RuntimeError):
                    break

        finally:
            push_task.cancel()
            try:
                await push_task
            except (asyncio.CancelledError, BaseException):
                pass
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 — connection may already be closed
                pass
            log.info("ws.rpc.disconnected", f"client={websocket.client}")


async def _dispatch_one(ctx: GatewayContext, raw: str) -> Response:
    """Parse one Request frame and run its handler. Always returns a Response.

    Wrapping every error case in a Response (rather than letting exceptions
    escape) matches openclaw's RespondFn contract and lets clients keep the
    correlation between ``id`` and outcome.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return make_error_response(
            req_id="",
            code=ErrorCode.INVALID_REQUEST,
            message=f"invalid JSON: {exc}",
        )

    try:
        req = Request.model_validate(obj)
    except ValidationError as exc:
        return make_error_response(
            req_id=str(obj.get("id") or ""),
            code=ErrorCode.INVALID_REQUEST,
            message=f"frame validation failed: {exc.errors()[0].get('msg', 'invalid request')}",
        )

    handler = CORE_HANDLERS.get(req.method)
    if handler is None:
        return make_error_response(
            req_id=req.id,
            code=ErrorCode.UNKNOWN_METHOD,
            message=f"unknown method: {req.method!r}",
        )

    try:
        payload = await handler(ctx, req.params)
    except BusyError as exc:
        return make_error_response(
            req_id=req.id,
            code=ErrorCode.BUSY,
            message=str(exc),
            retryable=True,
            retry_after_ms=exc.retry_after_ms,
            details=exc.details,
        )
    except NotFoundError as exc:
        return make_error_response(
            req_id=req.id,
            code=ErrorCode.NOT_FOUND,
            message=str(exc),
        )
    except NotImplementedError as exc:
        # Stub methods (sessions.delete / sessions.compact / runtime.update full
        # impl) raise this until later phases land.
        return make_error_response(
            req_id=req.id,
            code=ErrorCode.UNAVAILABLE,
            message=str(exc) or req.method,
            retryable=False,
        )
    except Exception as exc:  # noqa: BLE001 — anything else is INTERNAL
        log.warning("ws.rpc.handler.error", f"{req.method}: {type(exc).__name__}: {exc}")
        return make_error_response(
            req_id=req.id,
            code=ErrorCode.INTERNAL,
            message=f"{type(exc).__name__}: {exc}",
        )

    return make_ok_response(req.id, payload)
