"""OpenAI-compatible local speech gateway clients for xiaozhi."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterable, AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nano_openclaw.logger import get_logger


log = get_logger(__name__)


ASR_CLOSE_TIMEOUT_SECONDS = 2.0


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
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=ASR_CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.warning(
                    "xiaozhi.local_asr.close_timeout",
                    f"close exceeded {ASR_CLOSE_TIMEOUT_SECONDS:.1f}s; continuing turn",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "xiaozhi.local_asr.close_error",
                    f"{type(exc).__name__}: {exc}",
                )

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
                    log.info(
                        "xiaozhi.local_asr.completed_received",
                        f"text_chars={len(text)} interim_chars={len(self.last_interim)}",
                    )
                    self.last_interim = ""
                    if text:
                        await self.on_final(text)
                    else:
                        log.warning(
                            "xiaozhi.local_asr.completed_empty",
                            "speech gateway returned an empty final transcript",
                        )
                elif kind == "error":
                    error = event.get("error") or {}
                    raise RuntimeError(str(error.get("message") or "local ASR failed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                log.warning(
                    "xiaozhi.local_asr.reader_error",
                    f"{type(exc).__name__}: {exc}",
                )
            for future in (self._created, self._updated):
                if future is not None and not future.done():
                    future.set_exception(exc)
            self._completed.set()


async def stream_local_speech(
    *,
    realtime_url: str,
    api_key: str,
    model: str,
    voice: str,
    text_chunks: AsyncIterable[str],
    sample_rate: int,
    connect_impl: Any | None = None,
) -> AsyncIterator[bytes]:
    """Stream text and PCM through speech-gateway's unified realtime API."""

    connect = connect_impl
    if connect is None:
        from websockets.asyncio.client import connect

    parts = urlsplit(realtime_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"model": model, "voice": voice})
    url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    ws = await connect(url, additional_headers=_headers(api_key))

    async def receive_event(*, timeout: float = 60) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("local TTS realtime response timed out") from exc
        if not isinstance(raw, str):
            raise RuntimeError("local TTS returned an unexpected binary WebSocket frame")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("local TTS returned invalid JSON") from exc
        if event.get("type") == "error":
            error = event.get("error") or {}
            raise RuntimeError(str(error.get("message") or "local TTS failed"))
        return event

    async def wait_for(kind: str, *, timeout: float = 10) -> dict[str, Any]:
        while True:
            event = await receive_event(timeout=timeout)
            if event.get("type") == kind:
                return event

    try:
        await wait_for("session.created")
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {"output": {"model": model, "voice": voice}},
            },
        }))
        updated = await wait_for("session.updated")
        output = ((updated.get("session") or {}).get("audio") or {}).get("output") or {}
        actual_rate = int((output.get("format") or {}).get("rate") or sample_rate)
        if actual_rate != sample_rate:
            raise RuntimeError(
                f"local TTS sample rate mismatch: expected {sample_rate}, got {actual_rate}"
            )

        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"output_modalities": ["audio"], "input_text_stream": True},
        }))
        await wait_for("response.created")
        output: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=4)

        async def send_text() -> None:
            try:
                async for text in text_chunks:
                    if text:
                        await ws.send(json.dumps({
                            "type": "speech.input_text.delta",
                            "delta": text,
                        }))
                await ws.send(json.dumps({"type": "speech.input_text.done"}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - forwarded to the consumer
                await output.put(("error", exc))

        async def receive_audio() -> None:
            try:
                while True:
                    event = await receive_event()
                    kind = event.get("type")
                    if kind == "response.output_audio.delta":
                        try:
                            pcm = base64.b64decode(
                                str(event.get("delta") or ""), validate=True
                            )
                        except (ValueError, binascii.Error) as exc:
                            raise RuntimeError(
                                "local TTS returned invalid base64 audio"
                            ) from exc
                        if pcm:
                            await output.put(("audio", pcm))
                    elif kind == "response.done":
                        response = event.get("response") or {}
                        if response.get("status") != "completed":
                            details = response.get("status_details") or {}
                            raise RuntimeError(
                                str(details.get("error") or "local TTS was cancelled")
                            )
                        await output.put(("done", None))
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - forwarded to the consumer
                await output.put(("error", exc))

        sender = asyncio.create_task(send_text(), name="xiaozhi-local-tts-text")
        receiver = asyncio.create_task(receive_audio(), name="xiaozhi-local-tts-audio")
        try:
            while True:
                kind, payload = await output.get()
                if kind == "audio":
                    yield bytes(payload)
                elif kind == "error":
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError(str(payload))
                else:
                    await sender
                    return
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
    finally:
        try:
            await asyncio.wait_for(ws.close(), timeout=ASR_CLOSE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - socket teardown must not mask synthesis result
            pass
