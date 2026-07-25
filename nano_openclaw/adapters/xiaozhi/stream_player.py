"""Play a trusted loopback Opus stream on one live Xiaozhi connection."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import httpx

from nano_openclaw.adapters.xiaozhi.protocol import FRAME_DURATION_MS, envelope
from nano_openclaw.core.tools import Tool
from nano_openclaw.logger import get_logger

log = get_logger(__name__)

_CONTENT_TYPE = "application/x-opus-packets"
_MAX_OPUS_PACKET_BYTES = 1024 * 1024
_MAX_BUFFER_BYTES = 2 * _MAX_OPUS_PACKET_BYTES
_STREAM_IDLE_TIMEOUT_SECONDS = 20.0
_FRAME_SECONDS = FRAME_DURATION_MS / 1000


class XiaozhiPlaybackController:
    """Own at most one background music stream for a device connection."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._playback_id = ""
        self._label = ""
        self._state = "idle"
        self._error = ""
        self._result: dict[str, Any] | None = None
        self._stop_reason = ""

    async def start(self, *, stream_url: str, label: str) -> dict[str, Any]:
        _validate_stream_url(stream_url)
        async with self._lock:
            replaced_playback_id = await self._cancel_locked(reason="replaced")
            playback_id = uuid.uuid4().hex
            active_turn = getattr(self.connection, "_turn_task", None)
            if active_turn is not None and active_turn.done():
                active_turn = None
            self._playback_id = playback_id
            self._label = label
            self._state = "queued" if active_turn is not None else "starting"
            self._error = ""
            self._result = None
            self._stop_reason = ""
            self._task = asyncio.create_task(
                self._run(
                    playback_id,
                    stream_url=stream_url,
                    label=label,
                    after_turn=active_turn,
                ),
                name=f"xiaozhi-playback:{self.connection.device_id}:{playback_id[:8]}",
            )
            result = self.snapshot()
            if replaced_playback_id:
                result["replaced_playback_id"] = replaced_playback_id
            return result

    async def stop(
        self,
        *,
        playback_id: str = "",
        reason: str = "requested",
    ) -> dict[str, Any]:
        async with self._lock:
            if playback_id and playback_id != self._playback_id:
                result = self.snapshot()
                result.update(
                    {
                        "ok": False,
                        "stopped": False,
                        "reason": "playback_id_mismatch",
                    }
                )
                return result
            stopped_playback_id = await self._cancel_locked(reason=reason)
            result = self.snapshot()
            result["stopped"] = bool(stopped_playback_id)
            return result

    async def close(self) -> None:
        await self.stop(reason="connection_closed")

    def snapshot(self) -> dict[str, Any]:
        active = self._task is not None and not self._task.done()
        result: dict[str, Any] = {
            "ok": True,
            "active": active,
            "state": self._state,
            "playback_id": self._playback_id,
            "title": self._label,
        }
        if self._stop_reason:
            result["stop_reason"] = self._stop_reason
        if self._error:
            result["error"] = self._error
        if self._result is not None:
            result["stats"] = self._result
        return result

    async def _cancel_locked(self, *, reason: str) -> str:
        task = self._task
        if task is None or task.done():
            return ""
        playback_id = self._playback_id
        self._state = "stopping"
        self._stop_reason = reason
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._playback_id == playback_id:
            self._task = None
            self._state = "stopped"
        return playback_id

    async def _run(
        self,
        playback_id: str,
        *,
        stream_url: str,
        label: str,
        after_turn: asyncio.Task[Any] | None,
    ) -> None:
        try:
            if after_turn is not None:
                try:
                    await asyncio.shield(after_turn)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    self._mark_stopped(playback_id, "turn_cancelled")
                    return
            if getattr(self.connection, "_closed", False):
                self._mark_stopped(playback_id, "connection_closed")
                return
            if self._playback_id == playback_id:
                self._state = "starting"

            def mark_started() -> None:
                if self._playback_id == playback_id:
                    self._state = "playing"

            result = await _play_stream(
                self.connection,
                stream_url=stream_url,
                label=label,
                on_started=mark_started,
            )
        except asyncio.CancelledError:
            self._mark_stopped(playback_id, self._stop_reason or "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            if self._playback_id == playback_id:
                self._state = "failed"
                self._error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning(  # noqa: PLE1205
                    "xiaozhi.playback.failed",
                    f"device={self.connection.device_id}: {self._error}",
                )
        else:
            if self._playback_id == playback_id:
                self._state = "completed"
                self._result = result
        finally:
            if (
                self._playback_id == playback_id
                and self._task is asyncio.current_task()
            ):
                self._task = None

    def _mark_stopped(self, playback_id: str, reason: str) -> None:
        if self._playback_id == playback_id:
            self._state = "stopped"
            self._stop_reason = reason


def materialize_stream_tools(connection: Any) -> list[Tool]:
    controller: XiaozhiPlaybackController = connection.playback

    async def start(args: dict[str, Any]) -> str:
        stream_url = str(args.get("stream_url") or "").strip()
        if not stream_url:
            raise ValueError("stream_url is required")
        label = _playback_label(
            str(args.get("title") or ""),
            str(args.get("artist") or ""),
        )
        result = await controller.start(
            stream_url=stream_url,
            label=label,
        )
        return json.dumps(result, ensure_ascii=False)

    async def stop(args: dict[str, Any]) -> str:
        result = await controller.stop(
            playback_id=str(args.get("playback_id") or "").strip(),
        )
        return json.dumps(result, ensure_ascii=False)

    async def status(_args: dict[str, Any]) -> str:
        return json.dumps(controller.snapshot(), ensure_ascii=False)

    return [
        Tool(
            name="xiaozhi_start_playback",
            description=(
                "Queue a one-time xiaozhi-v1 profile stream (24 kHz mono, "
                "60 ms Opus packets with len32be framing) for background "
                "playback on the current Xiaozhi device. Playback starts "
                "after the current spoken reply ends and returns a playback_id "
                "immediately. Do not retry automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "stream_url": {
                        "type": "string",
                        "description": (
                            "Short-lived, one-time loopback URL from a trusted "
                            "audio preparation tool using profile xiaozhi-v1."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Selected title for the device display.",
                    },
                    "artist": {
                        "type": "string",
                        "description": "Selected artist for the device display.",
                    },
                },
                "required": ["stream_url"],
                "additionalProperties": False,
            },
            run=start,
        ),
        Tool(
            name="xiaozhi_stop_playback",
            description=(
                "Stop background music on the current Xiaozhi device. Omit "
                "playback_id to stop whichever playback is active."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "playback_id": {
                        "type": "string",
                        "description": "Optional ID returned by xiaozhi_start_playback.",
                    }
                },
                "additionalProperties": False,
            },
            run=stop,
        ),
        Tool(
            name="xiaozhi_playback_status",
            description="Return background music status for the current Xiaozhi device.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            run=status,
        ),
    ]


async def _play_stream(
    connection: Any,
    *,
    stream_url: str,
    label: str,
    transport: httpx.AsyncBaseTransport | None = None,
    on_started: Callable[[], None] | None = None,
    read_timeout_seconds: float = _STREAM_IDLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    _validate_stream_url(stream_url)
    if read_timeout_seconds <= 0:
        raise ValueError("read_timeout_seconds must be positive")
    timeout = httpx.Timeout(connect=5, read=None, write=5, pool=5)
    started = False
    packets = 0
    audio_bytes = 0
    pacing_resyncs = 0
    first_packet_latency_seconds: float | None = None
    started_at = asyncio.get_running_loop().time()
    next_send_at = started_at
    buffer = bytearray()

    try:
        async with (
            httpx.AsyncClient(
                follow_redirects=False,
                timeout=timeout,
                transport=transport,
                trust_env=False,
            ) as client,
            client.stream("GET", stream_url) as response,
        ):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if content_type.partition(";")[0].strip().lower() != _CONTENT_TYPE:
                raise RuntimeError("music stream returned an unsupported content type")

            async for chunk in _iter_response_bytes(
                response,
                idle_timeout_seconds=read_timeout_seconds,
            ):
                buffer.extend(chunk)
                if len(buffer) > _MAX_BUFFER_BYTES:
                    raise RuntimeError("music stream buffer exceeded its limit")
                while True:
                    packet = _take_packet(buffer)
                    if packet is None:
                        break
                    if not started:
                        await connection.send_json(
                            envelope(connection.session_id, "tts", state="start")
                        )
                        if label:
                            await connection.send_json(
                                envelope(
                                    connection.session_id,
                                    "tts",
                                    state="sentence_start",
                                    text=label,
                                )
                            )
                        started = True
                        first_packet_latency_seconds = (
                            asyncio.get_running_loop().time() - started_at
                        )
                        if on_started is not None:
                            on_started()

                    if packets:
                        next_send_at, resynced = _next_packet_deadline(
                            next_send_at,
                            asyncio.get_running_loop().time(),
                        )
                        pacing_resyncs += int(resynced)
                        await asyncio.sleep(
                            max(
                                0.0,
                                next_send_at - asyncio.get_running_loop().time(),
                            )
                        )
                    await connection.send_bytes(packet)
                    packets += 1
                    audio_bytes += len(packet)
    except asyncio.CancelledError:
        log.info(
            "xiaozhi.stream.cancelled",
            device=connection.device_id,
            packets=packets,
        )
        raise
    except httpx.HTTPError as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"music stream request failed: {detail}") from exc
    finally:
        if started and not getattr(connection, "_closed", False):
            with suppress(Exception):
                await connection.send_json(
                    envelope(connection.session_id, "tts", state="stop")
                )

    if buffer:
        raise RuntimeError("music stream ended inside an Opus packet")
    if packets == 0:
        raise RuntimeError("music stream produced no Opus packets")

    elapsed = asyncio.get_running_loop().time() - started_at
    log.info(
        "xiaozhi.stream.done",
        device=connection.device_id,
        packets=packets,
        audio_bytes=audio_bytes,
        pacing_resyncs=pacing_resyncs,
    )
    return {
        "ok": True,
        "title": label,
        "packets": packets,
        "audio_bytes": audio_bytes,
        "audio_duration_seconds": round(packets * _FRAME_SECONDS, 3),
        "first_packet_latency_seconds": round(first_packet_latency_seconds or 0.0, 3),
        "pacing_resyncs": pacing_resyncs,
        "elapsed_seconds": round(elapsed, 3),
    }


async def _iter_response_bytes(
    response: httpx.Response,
    *,
    idle_timeout_seconds: float,
) -> AsyncIterator[bytes]:
    iterator = response.aiter_bytes().__aiter__()
    while True:
        try:
            async with asyncio.timeout(idle_timeout_seconds):
                chunk = await anext(iterator)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise RuntimeError(
                f"music stream stalled for {idle_timeout_seconds:g} seconds"
            ) from exc
        yield chunk


def _next_packet_deadline(previous: float, now: float) -> tuple[float, bool]:
    deadline = previous + _FRAME_SECONDS
    if now - deadline >= _FRAME_SECONDS:
        return now, True
    return deadline, False


def _take_packet(buffer: bytearray) -> bytes | None:
    if len(buffer) < 4:
        return None
    packet_length = int.from_bytes(buffer[:4], "big")
    if packet_length <= 0 or packet_length > _MAX_OPUS_PACKET_BYTES:
        raise RuntimeError(
            f"music stream returned invalid Opus packet length: {packet_length}"
        )
    framed_length = 4 + packet_length
    if len(buffer) < framed_length:
        return None
    packet = bytes(buffer[4:framed_length])
    del buffer[:framed_length]
    return packet


def _validate_stream_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("stream_url must be an unauthenticated loopback HTTP URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("stream_url must use a literal loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError("stream_url must use a loopback IP address")
    if parsed.port is None:
        raise ValueError("stream_url must include the provider port")
    if not parsed.path.startswith("/streams/"):
        raise ValueError("stream_url must use the provider stream path")
    if parsed.query or parsed.fragment:
        raise ValueError("stream_url must not include a query or fragment")


def _playback_label(title: str, artist: str) -> str:
    title = title.strip()
    artist = artist.strip()
    if title and artist:
        return f"{artist} - {title}"[:200]
    return (title or artist)[:200]
