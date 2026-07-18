"""OpenAI-compatible local speech gateway clients for xiaozhi."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx


FinalCallback = Callable[[str], Awaitable[None]]
PartialCallback = Callable[[str], Awaitable[None]]


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


class LocalSpeechTranscriber:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        on_final: FinalCallback,
        on_partial: PartialCallback | None = None,
        connect_impl: Any | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.on_final = on_final
        self.on_partial = on_partial
        self._connect_impl = connect_impl
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._created: asyncio.Future[None] | None = None
        self._updated: asyncio.Future[None] | None = None
        self._completed = asyncio.Event()
        self._closed = False
        self._has_final = False
        self._audio_sent = False
        self.last_interim = ""

    async def start(self) -> None:
        if self._ws is not None:
            return
        connect = self._connect_impl
        if connect is None:
            from websockets.asyncio.client import connect
        separator = "&" if "?" in self.url else "?"
        url = f"{self.url}{separator}model={self.model}"
        self._ws = await connect(url, additional_headers=_headers(self.api_key))
        loop = asyncio.get_running_loop()
        self._created = loop.create_future()
        self._updated = loop.create_future()
        self._reader = asyncio.create_task(self._read_loop(), name="xiaozhi-local-asr")
        await asyncio.wait_for(asyncio.shield(self._created), timeout=10)
        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": 16000},
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 600,
                        "prefix_padding_ms": 300,
                    },
                }},
            },
        }))
        await asyncio.wait_for(asyncio.shield(self._updated), timeout=10)

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None or self._closed:
            return
        self._audio_sent = True
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }))

    async def stop(self) -> None:
        if self._ws is None or self._closed:
            return
        try:
            if self._audio_sent and not self._has_final:
                await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                try:
                    await asyncio.wait_for(self._completed.wait(), timeout=5)
                except asyncio.TimeoutError:
                    if self.last_interim:
                        await self.on_final(self.last_interim)
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "session.created":
                    if self._created is not None and not self._created.done():
                        self._created.set_result(None)
                elif kind == "session.updated":
                    if self._updated is not None and not self._updated.done():
                        self._updated.set_result(None)
                elif kind == "conversation.item.input_audio_transcription.delta":
                    self.last_interim += str(event.get("delta") or "")
                    if self.last_interim and self.on_partial is not None:
                        await self.on_partial(self.last_interim)
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self._has_final = True
                    self._completed.set()
                    text = str(event.get("transcript") or "").strip()
                    self.last_interim = ""
                    if text:
                        await self.on_final(text)
                elif kind == "error":
                    error = event.get("error") or {}
                    raise RuntimeError(str(error.get("message") or "local ASR failed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in (self._created, self._updated):
                if future is not None and not future.done():
                    future.set_exception(exc)
            self._completed.set()


async def stream_local_speech(
    *,
    base_url: str,
    api_key: str,
    model: str,
    voice: str,
    text: str,
    sample_rate: int,
) -> AsyncIterator[bytes]:
    url = f"{base_url.rstrip('/')}/audio/speech"
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "pcm",
        "stream_format": "audio",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", url, headers=_headers(api_key), json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"local TTS failed ({response.status_code}): {body[:300]}")
            actual_rate = int(response.headers.get("x-audio-sample-rate") or sample_rate)
            if actual_rate != sample_rate:
                raise RuntimeError(
                    f"local TTS sample rate mismatch: expected {sample_rate}, got {actual_rate}"
                )
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
