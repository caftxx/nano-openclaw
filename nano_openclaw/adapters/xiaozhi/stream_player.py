"""Play a trusted loopback Opus stream on one live Xiaozhi connection."""

from __future__ import annotations

import asyncio
import ipaddress
import json
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


def materialize_stream_tools(connection: Any) -> list[Tool]:
    async def play(args: dict[str, Any]) -> str:
        stream_url = str(args.get("stream_url") or "").strip()
        if not stream_url:
            raise ValueError("stream_url is required")
        label = _playback_label(
            str(args.get("title") or ""),
            str(args.get("artist") or ""),
        )
        result = await _play_stream(
            connection,
            stream_url=stream_url,
            label=label,
        )
        return json.dumps(result, ensure_ascii=False)

    return [
        Tool(
            name="xiaozhi_play_stream",
            description=(
                "Immediately play a one-time 24 kHz mono Opus stream returned "
                "by easy-music prepare_stream on the currently connected "
                "Xiaozhi device. The URL is short-lived and single-use; do not "
                "retry or replay automatically after interruption."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "stream_url": {
                        "type": "string",
                        "description": (
                            "Short-lived, one-time loopback stream_url returned "
                            "by easy-music prepare_stream; use it immediately."
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
            run=play,
        )
    ]


async def _play_stream(
    connection: Any,
    *,
    stream_url: str,
    label: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    _validate_stream_url(stream_url)
    timeout = httpx.Timeout(connect=5, read=None, write=5, pool=5)
    started = False
    packets = 0
    audio_bytes = 0
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

            async for chunk in response.aiter_bytes():
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

                    if packets:
                        next_send_at += FRAME_DURATION_MS / 1000
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
        raise RuntimeError(f"music stream request failed: {exc}") from exc
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
    )
    return {
        "ok": True,
        "title": label,
        "packets": packets,
        "audio_bytes": audio_bytes,
        "elapsed_seconds": round(elapsed, 3),
    }


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
