"""One live xiaozhi-esp32 WebSocket connection."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from typing import Any

from nano_openclaw.adapters.xiaozhi.aliyun_asr import AliyunTranscriber
from nano_openclaw.adapters.xiaozhi.codec import OpusCodec
from nano_openclaw.adapters.xiaozhi.mcp import DeviceMcpPeer
from nano_openclaw.adapters.xiaozhi.protocol import (
    FRAME_DURATION_MS,
    ProtocolError,
    envelope,
    parse_text_message,
    server_hello,
    split_sentences,
    validate_hello,
)
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


class XiaozhiConnection:
    def __init__(
        self,
        *,
        websocket: Any,
        adapter: Any,
        device_id: str,
        client_id: str,
        session_id: str,
        vision_url: str,
    ) -> None:
        self.websocket = websocket
        self.adapter = adapter
        self.device_id = device_id
        self.client_id = client_id
        self.session_id = session_id
        self.vision_url = vision_url
        self.codec = OpusCodec(
            encode_sample_rate=adapter.config.ttsSampleRate,
            encode_bitrate=adapter.config.opusBitrate,
        )
        self._send_lock = asyncio.Lock()
        self._asr: AliyunTranscriber | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._current_turn_id = ""
        self._closed = False
        self._listening_mode = "manual"
        self._want_listening = False
        self.mcp = DeviceMcpPeer(
            session_id=session_id,
            send_json=self.send_json,
            vision_url=vision_url,
            token=adapter.config.token,
            timeout_ms=adapter.config.mcpTimeoutMs,
        )
        self._mcp_init_task: asyncio.Task[None] | None = None

    async def serve(self) -> None:
        previous = self.adapter.hub.add(self.device_id, self)
        if previous is not None and previous is not self:
            await previous.close()
        log.info("xiaozhi.connected", f"device={self.device_id} client={self.client_id or '(unknown)'}")
        try:
            first = await asyncio.wait_for(self.websocket.receive_text(), timeout=10)
            hello = parse_text_message(first)
            validate_hello(hello)
            await self.send_json(server_hello(
                self.session_id,
                output_sample_rate=self.adapter.config.ttsSampleRate,
            ))
            self._mcp_init_task = asyncio.create_task(
                self._initialize_mcp(), name=f"xiaozhi-mcp-init:{self.device_id}"
            )
            while not self._closed:
                event = await self.websocket.receive()
                kind = event.get("type")
                if kind == "websocket.disconnect":
                    break
                if event.get("bytes") is not None:
                    await self._handle_audio(bytes(event["bytes"]))
                elif event.get("text") is not None:
                    await self._handle_json(parse_text_message(str(event["text"])))
        except (asyncio.TimeoutError, ProtocolError) as exc:
            log.warning("xiaozhi.protocol.error", f"device={self.device_id}: {exc}")
            with suppress(Exception):
                await self.websocket.close(code=1003, reason=str(exc)[:120])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                log.warning("xiaozhi.connection.error", f"device={self.device_id}: {type(exc).__name__}: {exc}")
        finally:
            await self.close()

    async def _initialize_mcp(self) -> None:
        try:
            await self.mcp.initialize()
            log.info("xiaozhi.mcp.ready", f"device={self.device_id} tools={len(self.mcp.tools)}")
        except Exception as exc:  # noqa: BLE001
            log.warning("xiaozhi.mcp.error", f"device={self.device_id}: {type(exc).__name__}: {exc}")

    async def _handle_json(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "listen":
            state = str(message.get("state") or "")
            if state == "start":
                await self.abort(send_tts_stop=True)
                self._listening_mode = str(message.get("mode") or "manual")
                self._want_listening = True
                await self._start_asr()
            elif state == "stop":
                self._want_listening = False
                await self._stop_asr()
            elif state == "detect":
                # Some firmware builds send cached wake-word audio followed by
                # this marker before the normal listen/start message.
                return
            else:
                raise ProtocolError(f"unsupported listen state: {state!r}")
        elif kind == "abort":
            await self.abort(send_tts_stop=True)
        elif kind == "mcp":
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise ProtocolError("mcp.payload must be an object")
            self.mcp.handle(payload)
        else:
            raise ProtocolError(f"unsupported message type: {kind!r}")

    async def _handle_audio(self, packet: bytes) -> None:
        if self._asr is None or self._turn_task is not None:
            return
        try:
            pcm = self.codec.decode(packet)
            await self._asr.send_audio(pcm)
        except Exception as exc:  # noqa: BLE001
            log.warning("xiaozhi.audio.decode", f"device={self.device_id}: {type(exc).__name__}: {exc}")

    async def _start_asr(self) -> None:
        if self._closed or self._asr is not None or self._turn_task is not None:
            return
        voice = self.adapter.runtime.config.voice
        transcriber = AliyunTranscriber(
            endpoint=voice.resolved_endpoint(),
            appkey=voice.appkey,
            token_provider=self.adapter.token_provider,
            on_final=self._on_final_transcript,
        )
        self._asr = transcriber
        try:
            await transcriber.start()
            log.info("xiaozhi.asr.started", f"device={self.device_id}")
        except Exception:
            self._asr = None
            await transcriber.close()
            raise

    async def _stop_asr(self) -> None:
        transcriber, self._asr = self._asr, None
        if transcriber is not None:
            await transcriber.stop()
            log.info("xiaozhi.asr.stopped", f"device={self.device_id}")

    async def _on_final_transcript(self, text: str) -> None:
        text = text.strip()
        if not text or self._closed or self._turn_task is not None:
            return
        self._turn_task = asyncio.create_task(
            self._run_transcript(text), name=f"xiaozhi-turn:{self.device_id}"
        )

    async def _run_transcript(self, text: str) -> None:
        current = asyncio.current_task()
        try:
            log.info("xiaozhi.turn.started", f"device={self.device_id} text_chars={len(text)}")
            await self._stop_asr()
            await self.send_json(envelope(self.session_id, "stt", text=text))
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.mcp.ready.wait(), timeout=self.adapter.config.mcpTimeoutMs / 1000)
            reply = await self._collect_agent_reply(text)
            if reply:
                await self._speak(reply)
            log.info("xiaozhi.turn.done", f"device={self.device_id} reply_chars={len(reply)}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("xiaozhi.turn.error", f"device={self.device_id}: {type(exc).__name__}: {exc}")
            with suppress(Exception):
                await self.send_json(envelope(
                    self.session_id,
                    "alert",
                    status="error",
                    message="处理请求失败，请稍后再试",
                    emotion="sad",
                ))
        finally:
            self._current_turn_id = ""
            if self._turn_task is current:
                self._turn_task = None
            if self._want_listening and self._listening_mode in {"auto", "realtime"} and not self._closed:
                with suppress(Exception):
                    await self._start_asr()

    async def _collect_agent_reply(self, text: str) -> str:
        backend = self.adapter.backend
        events = backend.subscribe(session_key=self.session_id, events=["agent.event"])
        candidate = ""
        current_message = ""
        try:
            turn_id = await backend.chat_send(
                session_key=self.session_id,
                text=text,
                turn_source="xiaozhi",
                response_style="voice",
                voice_id=self.adapter.config.ttsVoice,
                voice_output="aliyun",
                channel_id="xiaozhi",
                channel_account_id="default",
                channel_sender_key=self.device_id,
            )
            self._current_turn_id = turn_id
            log.info("xiaozhi.agent.started", f"device={self.device_id} turn={turn_id}")
            async for event in events:
                payload = event.payload
                if payload.get("turn_id") != turn_id:
                    continue
                kind = payload.get("type")
                if kind == "text.delta":
                    current_message += str(payload.get("text") or "")
                elif kind == "message.end":
                    if current_message.strip():
                        candidate = current_message.strip()
                    current_message = ""
                elif kind == "turn.done":
                    if current_message.strip():
                        candidate = current_message.strip()
                    return candidate
                elif kind == "turn.cancelled":
                    return ""
                elif kind == "turn.error":
                    raise RuntimeError(str(payload.get("message") or "agent turn failed"))
            return candidate
        finally:
            await events.aclose()

    async def _speak(self, text: str) -> None:
        sentences = split_sentences(text)
        if not sentences:
            return
        # One continuous encoder state per answer. A new answer starts clean
        # because the device resets its decoder when entering speaking state.
        self.codec = OpusCodec(
            encode_sample_rate=self.adapter.config.ttsSampleRate,
            encode_bitrate=self.adapter.config.opusBitrate,
        )
        log.info("xiaozhi.tts.started", f"device={self.device_id} sentences={len(sentences)}")
        started = False
        try:
            for sentence in sentences:
                if not started:
                    await self.send_json(envelope(self.session_id, "tts", state="start"))
                    started = True
                await self.send_json(envelope(
                    self.session_id, "tts", state="sentence_start", text=sentence
                ))
                result = await self.adapter.backend.talk_speak(
                    text=sentence,
                    voice_id=self.adapter.config.ttsVoice,
                    sample_rate=self.adapter.config.ttsSampleRate,
                )
                pcm = base64.b64decode(result["audioBase64"])
                for packet in self.codec.encode_stream(pcm):
                    await self.send_bytes(packet)
                    await asyncio.sleep(FRAME_DURATION_MS / 1000)
        finally:
            if started and not self._closed:
                with suppress(Exception):
                    await self.send_json(envelope(self.session_id, "tts", state="stop"))
            log.info("xiaozhi.tts.done", f"device={self.device_id} started={started}")

    async def abort(self, *, send_tts_stop: bool) -> None:
        self._want_listening = False
        await self._stop_asr()
        turn_id, self._current_turn_id = self._current_turn_id, ""
        if turn_id:
            await self.adapter.backend.chat_abort(turn_id=turn_id)
        task, self._turn_task = self._turn_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if send_tts_stop and not self._closed:
            with suppress(Exception):
                await self.send_json(envelope(self.session_id, "tts", state="stop"))

    async def send_json(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        async with self._send_lock:
            await self.websocket.send_bytes(payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.adapter.hub.remove(self.device_id, self)
        self.mcp.close()
        if self._mcp_init_task is not None:
            self._mcp_init_task.cancel()
            await asyncio.gather(self._mcp_init_task, return_exceptions=True)
            self._mcp_init_task = None
        await self.abort(send_tts_stop=False)
        with suppress(Exception):
            await self.websocket.close()
        log.info("xiaozhi.disconnected", f"device={self.device_id}")
