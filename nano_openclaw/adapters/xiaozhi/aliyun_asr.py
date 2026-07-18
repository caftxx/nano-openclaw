"""Async Alibaba Cloud SpeechTranscriber client for server-side PCM input."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import quote


FinalCallback = Callable[[str], Awaitable[None]]


def _id() -> str:
    return uuid.uuid4().hex


def _command(appkey: str, task_id: str, name: str, payload: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {
        "header": {
            "message_id": _id(),
            "task_id": task_id,
            "namespace": "SpeechTranscriber",
            "name": name,
            "appkey": appkey,
        }
    }
    if payload is not None:
        body["payload"] = payload
    return json.dumps(body, ensure_ascii=False)


class AliyunTranscriber:
    def __init__(
        self,
        *,
        endpoint: str,
        appkey: str,
        token_provider: Any,
        on_final: FinalCallback,
        connect_impl: Any | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.appkey = appkey
        self.token_provider = token_provider
        self.on_final = on_final
        self._connect_impl = connect_impl
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._started: asyncio.Future[None] | None = None
        self._completed: asyncio.Event | None = None
        self._task_id = ""
        self._closed = False
        self.last_interim = ""

    async def start(self) -> None:
        if self._ws is not None:
            return
        token, _ = await asyncio.to_thread(self.token_provider.get_token)
        sep = "&" if "?" in self.endpoint else "?"
        url = f"{self.endpoint}{sep}token={quote(token, safe='')}"
        connect = self._connect_impl
        if connect is None:
            from websockets.asyncio.client import connect
        self._ws = await connect(url)
        self._task_id = _id()
        loop = asyncio.get_running_loop()
        self._started = loop.create_future()
        self._completed = asyncio.Event()
        self._reader = asyncio.create_task(self._read_loop(), name=f"xiaozhi-asr:{self._task_id}")
        await self._ws.send(_command(
            self.appkey,
            self._task_id,
            "StartTranscription",
            {
                "format": "pcm",
                "sample_rate": 16000,
                "enable_intermediate_result": True,
                "enable_punctuation_prediction": True,
                "enable_inverse_text_normalization": True,
            },
        ))
        await asyncio.wait_for(asyncio.shield(self._started), timeout=10)

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is not None and not self._closed:
            await self._ws.send(pcm)

    async def stop(self) -> None:
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(_command(self.appkey, self._task_id, "StopTranscription"))
            if self._completed is not None:
                try:
                    await asyncio.wait_for(self._completed.wait(), timeout=2)
                except asyncio.TimeoutError:
                    if self.last_interim:
                        await self.on_final(self.last_interim)
                        self.last_interim = ""
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
                header = event.get("header") or {}
                payload = event.get("payload") or {}
                name = header.get("name")
                if name == "TranscriptionStarted":
                    if self._started is not None and not self._started.done():
                        self._started.set_result(None)
                elif name == "TranscriptionResultChanged":
                    self.last_interim = str(payload.get("result") or "")
                elif name == "SentenceEnd":
                    text = str(payload.get("result") or "").strip()
                    self.last_interim = ""
                    if text:
                        await self.on_final(text)
                elif name == "TranscriptionCompleted":
                    if self._completed is not None:
                        self._completed.set()
                elif name == "TaskFailed":
                    message = str(
                        header.get("status_message")
                        or header.get("status_text")
                        or "Aliyun transcription failed"
                    )
                    if self._started is not None and not self._started.done():
                        self._started.set_exception(RuntimeError(message))
                    if self._completed is not None:
                        self._completed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._started is not None and not self._started.done():
                self._started.set_exception(exc)
            if self._completed is not None:
                self._completed.set()
