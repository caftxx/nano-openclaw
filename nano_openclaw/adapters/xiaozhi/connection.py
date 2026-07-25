"""One live xiaozhi-esp32 WebSocket connection."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterable
from contextlib import suppress
from typing import Any

from nano_openclaw.adapters.xiaozhi.aliyun_asr import AliyunTranscriber
from nano_openclaw.adapters.xiaozhi.codec import OpusCodec
from nano_openclaw.adapters.xiaozhi.local_speech import (
    LocalSpeechTranscriber,
    stream_local_speech,
)
from nano_openclaw.adapters.xiaozhi.mcp import DeviceMcpPeer
from nano_openclaw.adapters.xiaozhi.protocol import (
    FRAME_DURATION_MS,
    ProtocolError,
    SpeechTextChunker,
    envelope,
    parse_text_message,
    server_hello,
    split_sentences,
    validate_hello,
)
from nano_openclaw.adapters.xiaozhi.stream_player import (
    XiaozhiPlaybackController,
    _next_packet_deadline,
)
from nano_openclaw.logger import get_logger
from nano_openclaw.services.backend import BusyError

log = get_logger(__name__)


PARTIAL_STT_INTERVAL_SECONDS = 0.12
ABORT_TURN_RELEASE_TIMEOUT_SECONDS = 3.0
BUSY_RETRY_TIMEOUT_SECONDS = 5.0
TURN_RELEASE_POLL_SECONDS = 0.05
_TEXT_SEGMENT_END = object()


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
        self._asr: Any | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._current_turn_id = ""
        self._closed = False
        self._listening_mode = "manual"
        self._want_listening = False
        self._asr_generation = 0
        self._accepted_final_generation = -1
        self._last_partial_text = ""
        self._last_partial_sent_at = 0.0
        self._last_voice_activity_at = 0.0
        self._no_voice_activity = asyncio.Event()
        self._no_voice_task: asyncio.Task[None] | None = None
        self._idle_after_turn_reason: str | None = None
        self.mcp = DeviceMcpPeer(
            session_id=session_id,
            send_json=self.send_json,
            vision_url=vision_url,
            token=adapter.config.token,
            timeout_ms=adapter.config.mcpTimeoutMs,
        )
        self._mcp_init_task: asyncio.Task[None] | None = None
        self.playback = XiaozhiPlaybackController(self)

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
                self._arm_no_voice_timeout()
                # speech-gateway accepts idle realtime sessions, so overlap
                # its handshake with the device entering listening. Aliyun
                # closes idle ASR sockets after about ten seconds and remains
                # deferred until the first audio packet.
                if self.adapter.runtime.config.voice.provider == "openai-compatible":
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
        if not self._want_listening or (
            self._turn_task is not None and not self._barge_in_enabled()
        ):
            return
        try:
            pcm = self.codec.decode(packet)
        except Exception as exc:  # noqa: BLE001
            log.warning("xiaozhi.audio.decode", f"device={self.device_id}: {type(exc).__name__}: {exc}")
            return

        if self._asr is None:
            await self._start_asr()
        transcriber = self._asr
        if transcriber is None:
            return
        try:
            await transcriber.send_audio(pcm)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "xiaozhi.asr.send_error",
                f"device={self.device_id}: {type(exc).__name__}: {exc}",
            )
            # Do not send every subsequent 60 ms frame to a dead socket. The
            # next frame may establish one fresh ASR connection.
            if self._asr is transcriber:
                self._asr = None
            with suppress(Exception):
                await transcriber.close()

    async def _start_asr(self) -> None:
        if (
            self._closed
            or self._asr is not None
            or (self._turn_task is not None and not self._barge_in_enabled())
        ):
            return
        self._asr_generation += 1
        generation = self._asr_generation
        self._last_partial_text = ""
        self._last_partial_sent_at = 0.0

        async def on_partial(text: str) -> None:
            await self._on_partial_transcript(text, generation=generation)

        async def on_final(text: str) -> None:
            await self._on_final_transcript(text, generation=generation)

        voice = self.adapter.runtime.config.voice
        if voice.provider == "openai-compatible":
            transcriber = LocalSpeechTranscriber(
                url=voice.realtimeUrl,
                api_key=voice.apiKey,
                model=voice.asrModel,
                on_final=on_final,
                on_partial=on_partial,
            )
        else:
            transcriber = AliyunTranscriber(
                endpoint=voice.resolved_endpoint(),
                appkey=voice.appkey,
                token_provider=self.adapter.token_provider,
                on_final=on_final,
                on_partial=on_partial,
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
        await self._pause_no_voice_timeout()
        transcriber, self._asr = self._asr, None
        if transcriber is not None:
            await transcriber.stop()
            log.info("xiaozhi.asr.stopped", f"device={self.device_id}")

    def _arm_no_voice_timeout(self) -> None:
        timeout = self.adapter.config.noVoiceTimeoutSeconds
        if timeout <= 0 or self._closed or not self._want_listening:
            return
        self._last_voice_activity_at = asyncio.get_running_loop().time()
        self._no_voice_activity.set()
        if self._no_voice_task is None or self._no_voice_task.done():
            self._no_voice_task = asyncio.create_task(
                self._watch_no_voice_timeout(),
                name=f"xiaozhi-no-voice:{self.device_id}",
            )

    def _record_voice_activity(self) -> None:
        if self._no_voice_task is None:
            return
        self._last_voice_activity_at = asyncio.get_running_loop().time()
        self._no_voice_activity.set()

    async def _pause_no_voice_timeout(self) -> None:
        task, self._no_voice_task = self._no_voice_task, None
        # Retire the watcher before waking it. Combining Event.set() with
        # Task.cancel() here can leave Python 3.11's wait_for(Event.wait())
        # pending until its original timeout.
        self._no_voice_activity.set()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def _watch_no_voice_timeout(self) -> None:
        current = asyncio.current_task()
        timeout = self.adapter.config.noVoiceTimeoutSeconds
        try:
            while (
                self._no_voice_task is current
                and self._want_listening
                and not self._closed
            ):
                remaining = self._last_voice_activity_at + timeout - asyncio.get_running_loop().time()
                if remaining <= 0:
                    log.info(
                        "xiaozhi.no_voice_timeout",
                        f"device={self.device_id} timeout_seconds={timeout}",
                    )
                    await self._return_to_idle(reason="no voice timeout")
                    return
                self._no_voice_activity.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._no_voice_activity.wait(), timeout=remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection cleanup handles close failures
            if not self._closed:
                log.warning(
                    "xiaozhi.no_voice_timeout.error",
                    f"device={self.device_id}: {type(exc).__name__}: {exc}",
                )
        finally:
            if self._no_voice_task is current:
                self._no_voice_task = None

    async def _on_partial_transcript(self, text: str, *, generation: int) -> None:
        text = text.strip()
        if (
            not text
            or self._closed
            or self._asr is None
            or (self._turn_task is not None and not self._barge_in_enabled())
            or generation != self._asr_generation
        ):
            return
        self._record_voice_activity()
        if text == self._last_partial_text:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_partial_sent_at < PARTIAL_STT_INTERVAL_SECONDS:
            return
        self._last_partial_text = text
        self._last_partial_sent_at = now
        await self.send_json(envelope(self.session_id, "stt", text=text))

    async def _on_final_transcript(self, text: str, *, generation: int | None = None) -> None:
        text = text.strip()
        interrupted_task = self._turn_task
        is_barge_in = interrupted_task is not None and self._barge_in_enabled()
        drop_reason = ""
        if not text:
            drop_reason = "empty_text"
        elif self._closed:
            drop_reason = "connection_closed"
        elif interrupted_task is not None and not is_barge_in:
            drop_reason = "turn_active"
        elif generation is not None and generation != self._asr_generation:
            drop_reason = "stale_generation"
        elif generation is not None and generation == self._accepted_final_generation:
            drop_reason = "generation_already_accepted"
        if drop_reason:
            log.warning(
                "xiaozhi.asr.final_dropped",
                (
                    f"device={self.device_id} reason={drop_reason} text_chars={len(text)} "
                    f"generation={generation} current_generation={self._asr_generation} "
                    f"accepted_generation={self._accepted_final_generation}"
                ),
            )
            return
        self._record_voice_activity()
        await self.playback.stop(reason="voice_input")
        if generation is not None:
            # Server VAD may produce more than one completed event while a
            # long utterance is being stopped. Exactly one final result from
            # each ASR instance is allowed to start an agent turn.
            self._accepted_final_generation = generation
        log.info(
            "xiaozhi.asr.final_accepted",
            (
                f"device={self.device_id} text_chars={len(text)} "
                f"generation={generation} barge_in={is_barge_in}"
            ),
        )
        if is_barge_in:
            interrupted_turn_id = self._current_turn_id
            task = asyncio.create_task(
                self._interrupt_and_run(
                    text,
                    interrupted_task=interrupted_task,
                    interrupted_turn_id=interrupted_turn_id,
                ),
                name=f"xiaozhi-barge-in:{self.device_id}",
            )
        else:
            task = asyncio.create_task(
                self._run_transcript(text), name=f"xiaozhi-turn:{self.device_id}"
            )
        self._turn_task = task

    def _barge_in_enabled(self) -> bool:
        """Whether the device keeps sending AEC-processed audio during replies."""
        return self._want_listening and self._listening_mode == "realtime"

    async def _interrupt_and_run(
        self,
        text: str,
        *,
        interrupted_task: asyncio.Task[None],
        interrupted_turn_id: str,
    ) -> None:
        """Stop the audible reply and replace it with the recognized interruption."""
        log.info(
            "xiaozhi.barge_in",
            (
                f"device={self.device_id} text_chars={len(text)} "
                f"turn={interrupted_turn_id or '(starting)'}"
            ),
        )

        # Tell the device to clear queued playback before waiting for model/TTS
        # cleanup. Keep the current ASR attached until the retiring task has
        # run its finally block; that prevents the old task from opening a
        # redundant ASR.
        if not self._closed:
            with suppress(Exception):
                await self.send_json(envelope(self.session_id, "tts", state="stop"))
        interrupted_task.cancel()
        await asyncio.gather(interrupted_task, return_exceptions=True)
        self._current_turn_id = ""

        try:
            await self._stop_asr()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "xiaozhi.barge_in.asr_cleanup_error",
                f"device={self.device_id}: {type(exc).__name__}: {exc}",
            )

        if interrupted_turn_id:
            await self.adapter.backend.chat_abort(turn_id=interrupted_turn_id)
            released = await self._wait_for_turn_release(
                interrupted_turn_id, timeout=ABORT_TURN_RELEASE_TIMEOUT_SECONDS
            )
            if not released:
                log.warning(
                    "xiaozhi.turn.release_timeout",
                    f"device={self.device_id} turn={interrupted_turn_id}",
                )

        await self._run_transcript(text)

    async def _run_transcript(self, text: str) -> None:
        current = asyncio.current_task()
        try:
            log.info("xiaozhi.turn.started", f"device={self.device_id} text_chars={len(text)}")
            # The final transcript is authoritative and must reach the device
            # before transport cleanup. A slow WebSocket close must not leave
            # the firmware displaying the last partial transcript forever.
            await self.send_json(envelope(self.session_id, "stt", text=text))
            log.info(
                "xiaozhi.asr.final_stt_sent",
                f"device={self.device_id} text_chars={len(text)}",
            )
            log.info(
                "xiaozhi.asr.final_cleanup_started",
                f"device={self.device_id}",
            )
            try:
                await self._stop_asr()
            except Exception as exc:  # noqa: BLE001
                # ASR has already been detached by _stop_asr. Cleanup failure
                # is non-fatal and must not prevent the accepted turn.
                log.warning(
                    "xiaozhi.asr.final_cleanup_error",
                    f"device={self.device_id}: {type(exc).__name__}: {exc}",
                )
            log.info(
                "xiaozhi.asr.final_cleanup_done",
                f"device={self.device_id}",
            )
            if self._barge_in_enabled():
                try:
                    await self._start_asr()
                except Exception as exc:  # noqa: BLE001
                    # Losing barge-in recognition must not discard the answer
                    # that was already accepted for this turn.
                    log.warning(
                        "xiaozhi.barge_in.asr_start_error",
                        f"device={self.device_id}: {type(exc).__name__}: {exc}",
                    )
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.mcp.ready.wait(), timeout=self.adapter.config.mcpTimeoutMs / 1000)
            voice = self.adapter.runtime.config.voice
            if voice.provider == "openai-compatible":
                reply = await self._stream_local_agent_reply(text, voice)
            else:
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
            if self._idle_after_turn_reason is not None and not self._closed:
                reason, self._idle_after_turn_reason = self._idle_after_turn_reason, None
                try:
                    await self._return_to_idle(reason="exit requested")
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "xiaozhi.exit.error",
                        f"device={self.device_id}: {type(exc).__name__}: {exc}",
                    )
                else:
                    log.info(
                        "xiaozhi.exit",
                        f"device={self.device_id} reason_chars={len(reason)}",
                    )
            # In auto mode the firmware returns to Listening after tts/stop
            # and sends the next listen/start itself. Starting ASR here would
            # race that message and create a redundant open/close/open cycle.
            # Realtime mode keeps device voice processing active while TTS is
            # playing, so it does not emit another listen/start and still
            # needs the server-side restart.
            if (
                self._barge_in_enabled()
                and self._asr is None
                and not self._closed
            ):
                with suppress(Exception):
                    await self._start_asr()

    async def request_idle_after_turn(self, *, reason: str = "") -> None:
        """Return the device to idle now, or after its active reply finishes."""
        if self._closed:
            return
        if self._turn_task is not None:
            self._idle_after_turn_reason = reason
            return
        await self._return_to_idle(reason="exit requested")
        log.info(
            "xiaozhi.exit",
            f"device={self.device_id} reason_chars={len(reason)}",
        )

    async def _return_to_idle(self, *, reason: str) -> None:
        """Shared transport action for idle timeout and the channel exit tool."""
        self._want_listening = False
        await self._pause_no_voice_timeout()
        await self.websocket.close(code=1000, reason=reason)

    async def _collect_agent_reply(self, text: str) -> str:
        backend = self.adapter.backend
        events = backend.subscribe(session_key=self.session_id, events=["agent.event"])
        candidate = ""
        current_message = ""
        try:
            turn_id = await self._send_agent_turn(text)
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

    async def _stream_local_agent_reply(self, text: str, voice: Any) -> str:
        """Overlap model text, realtime synthesis, and device playback."""
        backend = self.adapter.backend
        events = backend.subscribe(session_key=self.session_id, events=["agent.event"])
        segments: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        started_at = time.perf_counter()

        async def read_agent() -> None:
            current_message = ""
            candidate = ""
            chunker = SpeechTextChunker()
            current_segment: asyncio.Queue[object] | None = None
            first_delta = True
            first_chunk = True

            async def emit_chunks(chunks: list[str]) -> None:
                nonlocal current_segment, first_chunk
                if not chunks:
                    return
                if current_segment is None:
                    current_segment = asyncio.Queue(maxsize=32)
                    await segments.put(("segment", current_segment))
                for chunk in chunks:
                    if first_chunk:
                        first_chunk = False
                        log.info(
                            "xiaozhi.latency.first_tts_text",
                            (
                                f"device={self.device_id} "
                                f"elapsed_ms={(time.perf_counter() - started_at) * 1000:.1f}"
                            ),
                        )
                    await current_segment.put(chunk)

            async def finish_message(*, flush: bool = True) -> None:
                nonlocal chunker, current_segment
                if flush:
                    await emit_chunks(chunker.finish())
                if current_segment is not None:
                    await current_segment.put(_TEXT_SEGMENT_END)
                    current_segment = None
                chunker = SpeechTextChunker()

            try:
                turn_id = await self._send_agent_turn(text)
                self._current_turn_id = turn_id
                log.info("xiaozhi.agent.started", f"device={self.device_id} turn={turn_id}")
                async for event in events:
                    payload = event.payload
                    if payload.get("turn_id") != turn_id:
                        continue
                    kind = payload.get("type")
                    if kind == "text.delta":
                        delta = str(payload.get("text") or "")
                        if first_delta and delta:
                            first_delta = False
                            log.info(
                                "xiaozhi.latency.first_agent_delta",
                                (
                                    f"device={self.device_id} "
                                    f"elapsed_ms={(time.perf_counter() - started_at) * 1000:.1f}"
                                ),
                            )
                        current_message += delta
                        await emit_chunks(chunker.feed(delta))
                    elif kind == "message.end":
                        await finish_message()
                        if current_message.strip():
                            candidate = current_message.strip()
                        current_message = ""
                    elif kind == "turn.done":
                        await finish_message()
                        if current_message.strip():
                            candidate = current_message.strip()
                        await segments.put(("done", candidate))
                        return
                    elif kind == "turn.cancelled":
                        await finish_message(flush=False)
                        await segments.put(("done", ""))
                        return
                    elif kind == "turn.error":
                        raise RuntimeError(str(payload.get("message") or "agent turn failed"))
                await finish_message()
                await segments.put(("done", candidate))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - forwarded to playback
                # Do not turn a partial, unstable tail into audible speech after
                # cancellation/error. Already queued stable chunks may drain.
                await finish_message(flush=False)
                await segments.put(("error", exc))
            finally:
                await events.aclose()

        reader = asyncio.create_task(
            read_agent(), name=f"xiaozhi-agent-stream:{self.device_id}"
        )
        started_speech = False
        reply = ""
        try:
            while True:
                kind, payload = await segments.get()
                if kind == "segment":
                    if not started_speech:
                        self.codec = OpusCodec(
                            encode_sample_rate=self.adapter.config.ttsSampleRate,
                            encode_bitrate=self.adapter.config.opusBitrate,
                        )
                        await self.send_json(envelope(self.session_id, "tts", state="start"))
                        started_speech = True
                    await self._play_local_text_segment(payload, voice, started_at=started_at)
                elif kind == "error":
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError(str(payload))
                else:
                    reply = str(payload or "")
                    return reply
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            if started_speech and not self._closed:
                with suppress(Exception):
                    await self.send_json(envelope(self.session_id, "tts", state="stop"))

    async def _play_local_text_segment(
        self,
        text_queue: asyncio.Queue[object],
        voice: Any,
        *,
        started_at: float,
    ) -> None:
        async def text_chunks():
            while True:
                item = await text_queue.get()
                if item is _TEXT_SEGMENT_END:
                    return
                text = str(item)
                await self.send_json(envelope(
                    self.session_id, "tts", state="sentence_start", text=text
                ))
                yield text

        first_audio = True

        async def observed_pcm() -> AsyncIterable[bytes]:
            nonlocal first_audio
            async for chunk in stream_local_speech(
                realtime_url=voice.realtimeUrl,
                api_key=voice.apiKey,
                model=voice.ttsModel,
                voice=self.adapter.config.ttsVoice,
                text_chunks=text_chunks(),
                sample_rate=self.adapter.config.ttsSampleRate,
                prebuffer_ms=getattr(self.adapter.config, "ttsPrebufferMs", 2400),
                prebuffer_max_wait_ms=getattr(
                    self.adapter.config, "ttsPrebufferMaxWaitMs", 1800
                ),
            ):
                if first_audio:
                    first_audio = False
                    log.info(
                        "xiaozhi.latency.first_tts_audio",
                        (
                            f"device={self.device_id} "
                            f"elapsed_ms={(time.perf_counter() - started_at) * 1000:.1f}"
                        ),
                    )
                yield chunk

        await self._send_local_pcm(observed_pcm())

    async def _send_local_pcm(self, chunks: AsyncIterable[bytes]) -> None:
        """Encode and pace local PCM without bursting after synthesis stalls."""
        pcm_buffer = bytearray()
        frame_bytes = self.adapter.config.ttsSampleRate * FRAME_DURATION_MS // 1000 * 2
        next_send_at: float | None = None

        async def send_frame(pcm: bytes) -> None:
            nonlocal next_send_at
            loop = asyncio.get_running_loop()
            if next_send_at is not None:
                next_send_at, _ = _next_packet_deadline(
                    next_send_at,
                    loop.time(),
                )
                await asyncio.sleep(max(0.0, next_send_at - loop.time()))
            await self.send_bytes(self.codec.encode(pcm))
            if next_send_at is None:
                next_send_at = loop.time()

        async for chunk in chunks:
            pcm_buffer.extend(chunk)
            while len(pcm_buffer) >= frame_bytes:
                await send_frame(bytes(pcm_buffer[:frame_bytes]))
                del pcm_buffer[:frame_bytes]
        if pcm_buffer:
            await send_frame(bytes(pcm_buffer))

        # Keep the turn active for the final frame's playout duration. The
        # music controller adds a small device-side handoff guard after this.
        if next_send_at is not None:
            loop = asyncio.get_running_loop()
            final_deadline, _ = _next_packet_deadline(next_send_at, loop.time())
            await asyncio.sleep(max(0.0, final_deadline - loop.time()))

    async def _send_agent_turn(self, text: str) -> str:
        """Submit a device turn, tolerating a retiring turn from this session."""
        backend = self.adapter.backend
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BUSY_RETRY_TIMEOUT_SECONDS
        reported_busy = False
        while True:
            try:
                return await backend.chat_send(
                    session_key=self.session_id,
                    text=text,
                    turn_source="xiaozhi",
                    response_style="voice",
                    voice_id=self.adapter.config.ttsVoice,
                    voice_output=(
                        "local"
                        if self.adapter.runtime.config.voice.provider == "openai-compatible"
                        else "aliyun"
                    ),
                    channel_id="xiaozhi",
                    channel_account_id="default",
                    channel_sender_key=self.device_id,
                )
            except BusyError as exc:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                active_turn_id = str(exc.details.get("active_turn_id") or "")
                if not reported_busy:
                    log.info(
                        "xiaozhi.turn.waiting",
                        f"device={self.device_id} active_turn={active_turn_id or '(locked)'}",
                    )
                    reported_busy = True
                if active_turn_id and await self._wait_for_turn_release(
                    active_turn_id, timeout=remaining
                ):
                    continue
                delay = max(0.1, min(1.0, exc.retry_after_ms / 1000))
                await asyncio.sleep(min(delay, remaining))

    async def _wait_for_turn_release(self, turn_id: str, *, timeout: float) -> bool:
        """Wait until the backend no longer marks ``turn_id`` active."""
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            try:
                details = await self.adapter.backend.sessions_get(self.session_id)
            except Exception:  # noqa: BLE001 - polling is an optional optimization
                return False
            if details.active_turn_id != turn_id:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(TURN_RELEASE_POLL_SECONDS, remaining))

    async def _speak(self, text: str) -> None:
        voice = self.adapter.runtime.config.voice
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
            await self.send_json(envelope(self.session_id, "tts", state="start"))
            started = True
            if voice.provider == "openai-compatible":
                await self._speak_local_sentences(sentences, voice)
            else:
                for sentence in sentences:
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

    async def _speak_local_sentences(self, sentences: list[str], voice: Any) -> None:
        """Feed stable sentence deltas into one realtime TTS response."""

        async def text_chunks():
            for sentence in sentences:
                await self.send_json(envelope(
                    self.session_id, "tts", state="sentence_start", text=sentence
                ))
                yield sentence

        chunks = stream_local_speech(
            realtime_url=voice.realtimeUrl,
            api_key=voice.apiKey,
            model=voice.ttsModel,
            voice=self.adapter.config.ttsVoice,
            text_chunks=text_chunks(),
            sample_rate=self.adapter.config.ttsSampleRate,
            prebuffer_ms=getattr(self.adapter.config, "ttsPrebufferMs", 2400),
            prebuffer_max_wait_ms=getattr(
                self.adapter.config, "ttsPrebufferMaxWaitMs", 1800
            ),
        )
        await self._send_local_pcm(chunks)

    async def abort(self, *, send_tts_stop: bool) -> None:
        self._want_listening = False
        await self.playback.stop(reason="device_abort")
        # An explicit abort discards any final callback produced while the ASR
        # socket is being stopped. ``listen/stop`` intentionally does not take
        # this path because manual mode uses its final transcript.
        self._accepted_final_generation = self._asr_generation
        await self._stop_asr()
        turn_id, self._current_turn_id = self._current_turn_id, ""
        if turn_id:
            await self.adapter.backend.chat_abort(turn_id=turn_id)
            released = await self._wait_for_turn_release(
                turn_id, timeout=ABORT_TURN_RELEASE_TIMEOUT_SECONDS
            )
            if not released:
                log.warning(
                    "xiaozhi.turn.release_timeout",
                    f"device={self.device_id} turn={turn_id}",
                )
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
        await self._pause_no_voice_timeout()
        self.adapter.hub.remove(self.device_id, self)
        await self.playback.close()
        self.mcp.close()
        if self._mcp_init_task is not None:
            self._mcp_init_task.cancel()
            await asyncio.gather(self._mcp_init_task, return_exceptions=True)
            self._mcp_init_task = None
        await self.abort(send_tts_stop=False)
        with suppress(Exception):
            await self.websocket.close()
        log.info("xiaozhi.disconnected", f"device={self.device_id}")
