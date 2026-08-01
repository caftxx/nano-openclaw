"""FastAPI routes for xiaozhi OTA, WebSocket, and camera uploads."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import HTTPException, Request, WebSocket

from nano_openclaw.adapters.xiaozhi.channel import XiaozhiChannel
from nano_openclaw.adapters.xiaozhi.connection import XiaozhiConnection
from nano_openclaw.core.attachments import safe_path_part
from nano_openclaw.core.images import describe_image, load_image_bytes
from nano_openclaw.logger import get_logger

log = get_logger(__name__)


def _adapter(ctx: Any) -> XiaozhiChannel | None:
    instance = ctx.channel_manager.get_instance("xiaozhi", "default")
    if isinstance(instance, XiaozhiChannel) and instance.status().state == "running":
        return instance
    return None


def _bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        return ""
    return value.removeprefix("Bearer ").strip()


def _authorized(expected: str, supplied: str | None) -> bool:
    token = _bearer(supplied)
    return bool(token) and secrets.compare_digest(token, expected)


def _request_ws_url(request: Request, configured: str) -> str:
    if configured.strip():
        return configured.strip()
    scheme = "wss" if request.url.scheme == "https" else "ws"
    return str(request.url.replace(scheme=scheme, path="/xiaozhi/v1/", query=""))


def _vision_url(ws_url: str) -> str:
    parts = urlsplit(ws_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, "/xiaozhi/vision/explain", "", ""))


def _server_time() -> dict[str, int]:
    """Build the wall-clock payload expected by xiaozhi-esp32's OTA client."""
    now = datetime.now().astimezone()
    utc_offset = now.utcoffset()
    return {
        "timestamp": int(now.timestamp() * 1000),
        "timezone_offset": int(utc_offset.total_seconds() // 60) if utc_offset else 0,
    }


def _save_photo(state_dir: Path, device_id: str, photo: bytes) -> Path:
    """Persist one uploaded camera JPEG under the gateway state directory."""
    device_dir = state_dir / "xiaozhi-photos" / safe_path_part(device_id)
    device_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    path = device_dir / f"{timestamp}-{uuid4().hex[:8]}.jpg"
    path.write_bytes(photo)
    return path


def _websocket_public_url(websocket: WebSocket, configured: str) -> str:
    """Recover the public WS URL when firmware omits a non-default Host port.

    xiaozhi-esp32's WebSocket client currently sends ``Host: <hostname>`` even
    when it connects to port 5000. Starlette therefore builds ``websocket.url``
    without that port. The ASGI server tuple still contains the actual listener
    port, so restore it for the vision capability URL.
    """
    if configured.strip():
        return configured.strip()
    parts = urlsplit(str(websocket.url))
    if parts.port is not None:
        return urlunsplit((parts.scheme, parts.netloc, "/xiaozhi/v1/", "", ""))

    server = websocket.scope.get("server")
    server_port = int(server[1]) if isinstance(server, (tuple, list)) and len(server) > 1 else 0
    default_port = 443 if parts.scheme == "wss" else 80
    hostname = parts.hostname or ""
    if server_port and server_port != default_port:
        hostname = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        netloc = f"{hostname}:{server_port}"
    else:
        netloc = parts.netloc
    return urlunsplit((parts.scheme, netloc, "/xiaozhi/v1/", "", ""))


def register_xiaozhi_routes(app: Any, ctx: Any) -> None:
    async def ota_payload(request: Request) -> dict[str, Any]:
        adapter = _adapter(ctx)
        if adapter is None:
            raise HTTPException(status_code=503, detail="xiaozhi adapter is not running")
        ws_url = _request_ws_url(request, adapter.config.websocketUrl)
        return {
            "server_time": _server_time(),
            "websocket": {
                "url": ws_url,
                "token": adapter.config.token,
                "version": 1,
            }
        }

    app.add_api_route("/xiaozhi/ota/", ota_payload, methods=["GET", "POST"])

    @app.websocket("/xiaozhi/v1/")
    async def xiaozhi_ws(websocket: WebSocket) -> None:
        adapter = _adapter(ctx)
        await websocket.accept()
        if adapter is None:
            await websocket.close(code=1013, reason="xiaozhi adapter unavailable")
            return
        if not _authorized(adapter.config.token, websocket.headers.get("authorization")):
            await websocket.close(code=1008, reason="invalid bearer token")
            return
        device_id = str(websocket.headers.get("device-id") or "").strip().lower()
        if not device_id:
            await websocket.close(code=1008, reason="Device-Id is required")
            return
        client_id = str(websocket.headers.get("client-id") or "").strip()
        session_id = adapter.sessions.resolve(device_id)
        public_ws = _websocket_public_url(websocket, adapter.config.websocketUrl)
        connection = XiaozhiConnection(
            websocket=websocket,
            adapter=adapter,
            device_id=device_id,
            client_id=client_id,
            session_id=session_id,
            vision_url=_vision_url(public_ws),
        )
        await connection.serve()

    @app.post("/xiaozhi/vision/explain")
    async def explain(request: Request) -> dict[str, Any]:
        adapter = _adapter(ctx)
        if adapter is None:
            raise HTTPException(status_code=503, detail="xiaozhi adapter is not running")
        if not _authorized(adapter.config.token, request.headers.get("authorization")):
            raise HTTPException(status_code=401, detail="invalid bearer token")
        device_id = str(request.headers.get("device-id") or "").strip().lower()
        if not device_id:
            raise HTTPException(status_code=400, detail="Device-Id is required")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid Content-Length") from None
            if declared_size > adapter.config.maxPhotoBytes + 1024 * 1024:
                raise HTTPException(status_code=413, detail="photo upload is too large")

        upload = None
        try:
            form = await request.form(max_files=1, max_fields=2, max_part_size=adapter.config.maxPhotoBytes)
            question = str(form.get("question") or "请描述这张照片。")[:2000]
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="multipart file field is required")
            if str(getattr(upload, "content_type", "")) != "image/jpeg":
                raise HTTPException(status_code=415, detail="only image/jpeg is supported")
            photo = await upload.read(adapter.config.maxPhotoBytes + 1)
            if len(photo) > adapter.config.maxPhotoBytes:
                raise HTTPException(status_code=413, detail="photo upload is too large")
            if not photo:
                raise HTTPException(status_code=400, detail="photo is empty")
            if not photo.startswith(b"\xff\xd8"):
                raise HTTPException(status_code=415, detail="file is not a JPEG image")

            runtime = adapter.runtime
            photo_path = await asyncio.to_thread(
                _save_photo,
                Path(runtime.state_dir),
                device_id,
                photo,
            )
            model = str(runtime.cfg.image_model or runtime.model_id)
            b64, mime = load_image_bytes(photo, "image/jpeg")
            result = await describe_image(
                b64,
                mime,
                client=runtime.client,
                model=model,
                api=runtime.cfg.api,
                prompt=question,
            )
            saved_path = photo_path.relative_to(Path(runtime.state_dir)).as_posix()
            log.info(
                "xiaozhi.vision.done",
                device=device_id,
                bytes=len(photo),
                saved=saved_path,
            )
            return {"success": True, "result": result.strip()}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "xiaozhi.vision.error",
                device=device_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {"success": False, "message": f"{type(exc).__name__}: {exc}"}
        finally:
            if upload is not None and hasattr(upload, "close"):
                await upload.close()
