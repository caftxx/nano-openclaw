from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nano_openclaw.adapters.channels.base import ChannelAccount
from nano_openclaw.adapters.xiaozhi.channel import XiaozhiChannel
from nano_openclaw.adapters.xiaozhi.aliyun_asr import AliyunTranscriber
from nano_openclaw.adapters.xiaozhi.codec import OpusCodec
from nano_openclaw.adapters.xiaozhi.connection import XiaozhiConnection
from nano_openclaw.adapters.xiaozhi.local_speech import LocalSpeechTranscriber
from nano_openclaw.adapters.xiaozhi.mcp import DeviceMcpPeer, XiaozhiHub
from nano_openclaw.adapters.xiaozhi.protocol import (
    ProtocolError,
    sanitize_tool_name,
    server_hello,
    split_sentences,
    validate_hello,
)
from nano_openclaw.adapters.xiaozhi.routes import _websocket_public_url, register_xiaozhi_routes
from nano_openclaw.adapters.xiaozhi.sessions import DeviceSessionStore
from nano_openclaw.config.types import NanoOpenClawConfig, XiaozhiConfig
from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.services.backend import BusyError


def hello(**audio_overrides):
    audio = {
        "format": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration": 60,
        **audio_overrides,
    }
    return {"type": "hello", "version": 1, "transport": "websocket", "audio_params": audio}


def test_xiaozhi_config_defaults_and_validation():
    cfg = NanoOpenClawConfig()
    assert cfg.xiaozhi == XiaozhiConfig()
    assert cfg.xiaozhi.enabled is False
    assert cfg.xiaozhi.noVoiceTimeoutSeconds == 120
    assert cfg.xiaozhi.maxPhotoBytes == 5 * 1024 * 1024
    assert cfg.xiaozhi.ttsVoice == "zhiqi"
    assert cfg.xiaozhi.ttsSampleRate == 24000
    assert cfg.xiaozhi.opusBitrate == 64000
    with pytest.raises(ValueError):
        XiaozhiConfig(mcpTimeoutMs=99)
    with pytest.raises(ValueError):
        XiaozhiConfig(ttsSampleRate=48000)
    with pytest.raises(ValueError):
        XiaozhiConfig(noVoiceTimeoutSeconds=-1)


def test_channel_keeps_configuration_error_visible(tmp_path):
    async def run():
        registry = ChannelManager()
        registry.register(XiaozhiChannel)
        backend = SimpleNamespace(manager=_FakeManager())
        runtime = SimpleNamespace(
            state_dir=tmp_path,
            config=SimpleNamespace(
                xiaozhi=SimpleNamespace(enabled=True, token=""),
                voice=SimpleNamespace(provider="aliyun", available=False, ttsEnabled=False),
            ),
            cfg=SimpleNamespace(image_model=None),
        )
        adapter = await registry.start(
            "xiaozhi",
            ChannelAccount(id="default"),
            runtime,
            SimpleNamespace(backend=backend),
        )
        assert adapter.status().state == "error"
        assert "xiaozhi.token" in adapter.status().error
        assert registry.get_instance("xiaozhi", "default") is adapter

    asyncio.run(run())


def test_channel_accepts_local_speech_gateway_configuration(tmp_path):
    async def run():
        registry = ChannelManager()
        registry.register(XiaozhiChannel)
        backend = SimpleNamespace(manager=_FakeManager())
        runtime = SimpleNamespace(
            state_dir=tmp_path,
            config=SimpleNamespace(
                xiaozhi=XiaozhiConfig(enabled=True, token="device-secret"),
                voice=SimpleNamespace(
                    provider="openai-compatible",
                    ttsEnabled=True,
                    baseUrl="http://127.0.0.1:5100/v1",
                    realtimeUrl="ws://127.0.0.1:5100/v1/realtime",
                    apiKey="local-secret",
                ),
            ),
            cfg=SimpleNamespace(image_model="vision-model"),
        )
        adapter = await registry.start(
            "xiaozhi",
            ChannelAccount(id="default"),
            runtime,
            SimpleNamespace(backend=backend),
        )
        assert adapter.status().state == "running"
        assert adapter.token_provider is None

    asyncio.run(run())


def test_protocol_v1_hello_and_helpers():
    assert validate_hello(hello()) == 1
    assert server_hello("s1")["audio_params"]["frame_duration"] == 60
    assert server_hello("s1")["audio_params"]["sample_rate"] == 24000
    assert sanitize_tool_name("self.camera.take-photo") == "xiaozhi__self_camera_take_photo"
    assert split_sentences("你好。世界！ last") == ["你好。", "世界！", "last"]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "listen"},
        {key: value for key, value in hello().items() if key != "version"},
        {**hello(), "version": 2},
        {**hello(), "transport": "mqtt"},
        hello(sample_rate=24000),
        hello(format="pcm"),
    ],
)
def test_protocol_rejects_non_v1_hello(payload):
    with pytest.raises(ProtocolError):
        validate_hello(payload)


def test_opus_codec_60ms_roundtrip():
    codec = OpusCodec()
    # Uplink stays at 16 kHz (960 samples), while downlink uses 24 kHz
    # (1440 samples). Decoding our own 24 kHz packet verifies the decoder
    # resamples back to the ASR's required 16 kHz frame size.
    packet = codec.encode(bytes(2880))
    assert packet
    decoded = codec.decode(packet)
    assert len(decoded) == 1920
    assert len(codec.encode_stream(bytes(2880 * 3))) == 3
    assert len(codec.encode_stream(b"partial")) == 1
    with pytest.raises(ValueError, match="empty"):
        codec.decode(b"")


class _FakeManager:
    def __init__(self):
        self.items = {}
        self.created = 0

    def create(self):
        self.created += 1
        session = SimpleNamespace(session_id=f"session-{self.created}")
        self.items[session.session_id] = session
        return session

    def get_or_load(self, session_id):
        if session_id not in self.items:
            raise KeyError(session_id)
        return self.items[session_id]


def test_device_session_store_persists_and_recovers(tmp_path):
    manager = _FakeManager()
    backend = SimpleNamespace(manager=manager)
    path = tmp_path / "xiaozhi-sessions.json"
    store = DeviceSessionStore(path, backend)
    assert store.resolve("AA:BB") == "session-1"
    assert store.resolve("aa:bb") == "session-1"
    assert json.loads(path.read_text())["aa:bb"] == "session-1"

    manager.items.clear()
    assert store.resolve("aa:bb") == "session-2"


def test_device_mcp_initialize_materialize_and_call():
    async def run():
        peer = None

        async def send(message):
            payload = message["payload"]
            method = payload["method"]
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05"}
            elif method == "tools/list":
                assert payload["params"]["withUserTools"] is True
                result = {
                    "tools": [{
                        "name": "self.light.set_rgb",
                        "description": "set light",
                        "inputSchema": {"type": "object", "properties": {"r": {"type": "integer"}}},
                    }]
                }
            else:
                result = {"content": [{"type": "text", "text": "true"}], "isError": False}
            asyncio.get_running_loop().call_soon(
                peer.handle,
                {"jsonrpc": "2.0", "id": payload["id"], "result": result},
            )

        peer = DeviceMcpPeer(
            session_id="s1",
            send_json=send,
            vision_url="http://host/xiaozhi/vision/explain",
            token="secret",
            timeout_ms=1000,
        )
        await peer.initialize()
        tools = peer.materialize_tools()
        assert [tool.name for tool in tools] == ["xiaozhi__self_light_set_rgb"]
        assert await tools[0].run({"r": 1}) == "true"
        peer.close()

    asyncio.run(run())


class _RouteBackend:
    def __init__(self):
        self.manager = _FakeManager()
        self.runtime = SimpleNamespace()


def _route_fixture(tmp_path: Path):
    backend = _RouteBackend()
    runtime = SimpleNamespace(
        state_dir=tmp_path,
        config=SimpleNamespace(
            xiaozhi=SimpleNamespace(
                enabled=True,
                token="secret",
                websocketUrl="",
                mcpTimeoutMs=1000,
                noVoiceTimeoutSeconds=120,
                maxPhotoBytes=1024 * 1024,
                ttsVoice="zhiqi",
                ttsSampleRate=24000,
                opusBitrate=64000,
            ),
            voice=SimpleNamespace(
                provider="aliyun",
                ttsVoice="xiaoxian",
                baseUrl="",
                realtimeUrl="",
                apiKey="",
                asrModel="paraformer-zh-streaming",
                ttsModel="fun-cosyvoice3-0.5b",
            ),
        ),
        cfg=SimpleNamespace(image_model="vision-model", model_has_vision=False, api="openai"),
        model_id="chat-model",
        client=object(),
    )
    adapter = XiaozhiChannel(ChannelAccount(id="default"))
    adapter.runtime = runtime
    adapter.backend = backend
    adapter.config = runtime.config.xiaozhi
    adapter.hub = XiaozhiHub()
    adapter.sessions = DeviceSessionStore(tmp_path / "sessions.json", backend)
    adapter.token_provider = object()
    adapter._state = "running"
    registry = ChannelManager()
    registry._instances[("xiaozhi", "default")] = adapter
    ctx = SimpleNamespace(channel_manager=registry, backend=backend)
    app = FastAPI()
    register_xiaozhi_routes(app, ctx)
    return app, adapter


def test_ota_and_websocket_auth_and_hello(tmp_path):
    app, _ = _route_fixture(tmp_path)
    client = TestClient(app)
    before_ms = int(time.time() * 1000)
    ota = client.post("/xiaozhi/ota/", headers={"host": "192.168.1.8:5000"})
    after_ms = int(time.time() * 1000)
    assert ota.status_code == 200
    assert before_ms <= ota.json()["server_time"]["timestamp"] <= after_ms
    utc_offset = datetime.now().astimezone().utcoffset()
    assert ota.json()["server_time"]["timezone_offset"] == (
        int(utc_offset.total_seconds() // 60) if utc_offset else 0
    )
    assert ota.json()["websocket"] == {
        "url": "ws://192.168.1.8:5000/xiaozhi/v1/",
        "token": "secret",
        "version": 1,
    }

    with client.websocket_connect("/xiaozhi/v1/") as ws:
        assert ws.receive()["type"] == "websocket.close"

    headers = {"authorization": "Bearer secret", "device-id": "AA:BB", "client-id": "client-1"}
    with client.websocket_connect("/xiaozhi/v1/", headers=headers) as ws:
        ws.send_json(hello())
        server_greeting = ws.receive_json()
        assert server_greeting["type"] == "hello"
        assert server_greeting["audio_params"]["sample_rate"] == 24000
        initialize = ws.receive_json()
        assert initialize["payload"]["method"] == "initialize"
        ws.send_json({
            "type": "mcp",
            "payload": {"jsonrpc": "2.0", "id": initialize["payload"]["id"], "result": {}},
        })
        list_tools = ws.receive_json()
        assert list_tools["payload"]["method"] == "tools/list"


def test_websocket_public_url_restores_firmware_omitted_port():
    websocket = SimpleNamespace(
        url="ws://192.168.0.83/xiaozhi/v1/",
        scope={"server": ("0.0.0.0", 5000)},
    )
    assert _websocket_public_url(websocket, "") == "ws://192.168.0.83:5000/xiaozhi/v1/"
    assert _websocket_public_url(websocket, "wss://voice.example/xiaozhi/v1/") == (
        "wss://voice.example/xiaozhi/v1/"
    )


def test_websocket_device_sessions_reconnect_and_isolate(tmp_path):
    app, _ = _route_fixture(tmp_path)
    client = TestClient(app)

    def connect(device_id):
        headers = {"authorization": "Bearer secret", "device-id": device_id}
        with client.websocket_connect("/xiaozhi/v1/", headers=headers) as ws:
            ws.send_json(hello())
            return ws.receive_json()["session_id"]

    first = connect("device-a")
    assert connect("DEVICE-A") == first
    assert connect("device-b") != first


def test_websocket_unknown_message_closes_with_protocol_error(tmp_path):
    app, _ = _route_fixture(tmp_path)
    client = TestClient(app)
    headers = {"authorization": "Bearer secret", "device-id": "device-a"}
    with client.websocket_connect("/xiaozhi/v1/", headers=headers) as ws:
        ws.send_json(hello())
        ws.receive_json()
        initialize = ws.receive_json()
        ws.send_json({
            "type": "mcp",
            "payload": {"jsonrpc": "2.0", "id": initialize["payload"]["id"], "result": {}},
        })
        tools_list = ws.receive_json()
        ws.send_json({
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": tools_list["payload"]["id"],
                "result": {"tools": []},
            },
        })
        ws.send_json({"type": "not-supported"})
        closed = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1003


def test_photo_endpoint_uses_question_without_retaining_file(tmp_path):
    app, _ = _route_fixture(tmp_path)
    client = TestClient(app)
    headers = {"authorization": "Bearer secret", "device-id": "AA:BB"}
    with patch(
        "nano_openclaw.adapters.xiaozhi.routes.describe_image",
        new=AsyncMock(return_value="桌上有一个杯子"),
    ) as describe:
        response = client.post(
            "/xiaozhi/vision/explain",
            headers=headers,
            data={"question": "这是什么？"},
            files={"file": ("camera.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")},
        )
    assert response.status_code == 200
    assert response.json() == {"success": True, "result": "桌上有一个杯子"}
    assert describe.await_args.kwargs["prompt"] == "这是什么？"
    assert not list(tmp_path.glob("*.jpg"))


def test_photo_endpoint_rejects_auth_and_mime(tmp_path):
    app, _ = _route_fixture(tmp_path)
    client = TestClient(app)
    assert client.post("/xiaozhi/vision/explain").status_code == 401
    response = client.post(
        "/xiaozhi/vision/explain",
        headers={"authorization": "Bearer secret", "device-id": "dev"},
        data={"question": "x"},
        files={"file": ("camera.png", b"png", "image/png")},
    )
    assert response.status_code == 415


class _FakeAliyunWebSocket:
    def __init__(self):
        self.sent = []
        self.events = asyncio.Queue()
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)
        if not isinstance(payload, str):
            return
        name = json.loads(payload)["header"]["name"]
        if name == "StartTranscription":
            await self.events.put(json.dumps({"header": {"name": "TranscriptionStarted"}}))
        elif name == "StopTranscription":
            await self.events.put(json.dumps({"header": {"name": "TranscriptionCompleted"}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.events.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True
        await self.events.put(None)


def test_aliyun_transcriber_sentence_end_and_manual_stop():
    async def run():
        ws = _FakeAliyunWebSocket()
        final = AsyncMock()
        partial = AsyncMock()

        async def connect(url):
            assert url.endswith("token=token-value")
            return ws

        transcriber = AliyunTranscriber(
            endpoint="wss://nls-gateway.example/ws/v1",
            appkey="app-key",
            token_provider=SimpleNamespace(get_token=lambda: ("token-value", 0)),
            on_final=final,
            on_partial=partial,
            connect_impl=connect,
        )
        await transcriber.start()
        await transcriber.send_audio(bytes(1920))
        await ws.events.put(json.dumps({
            "header": {"name": "TranscriptionResultChanged"},
            "payload": {"result": "临时文本"},
        }))
        await ws.events.put(json.dumps({
            "header": {"name": "SentenceEnd"},
            "payload": {"result": "最终文本"},
        }))
        await asyncio.sleep(0)
        await transcriber.stop()

        names = [json.loads(item)["header"]["name"] for item in ws.sent if isinstance(item, str)]
        assert names == ["StartTranscription", "StopTranscription"]
        assert bytes(1920) in ws.sent
        partial.assert_awaited_once_with("临时文本")
        final.assert_awaited_once_with("最终文本")
        assert ws.closed

    asyncio.run(run())


class _RecordingWebSocket:
    def __init__(self):
        self.json = []
        self.binary = []
        self.close_calls = []
        self.closed = asyncio.Event()

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    async def close(self, **kwargs):
        self.close_calls.append(kwargs)
        self.closed.set()


def _connection_fixture(tmp_path):
    _, adapter = _route_fixture(tmp_path)
    adapter.backend.talk_speak = AsyncMock(return_value={
        "audioBase64": base64.b64encode(bytes(2880)).decode(),
    })
    adapter.backend.chat_abort = AsyncMock()
    ws = _RecordingWebSocket()
    connection = XiaozhiConnection(
        websocket=ws,
        adapter=adapter,
        device_id="dev-1",
        client_id="client-1",
        session_id="session-1",
        vision_url="http://host/xiaozhi/vision/explain",
    )
    return connection, ws, adapter


def test_tts_sentence_order_and_opus_audio(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        await connection._speak("第一句。第二句！")
        states = [item.get("state") for item in ws.json if item.get("type") == "tts"]
        assert states == ["start", "sentence_start", "sentence_start", "stop"]
        assert [call.kwargs["text"] for call in adapter.backend.talk_speak.await_args_list] == [
            "第一句。", "第二句！"
        ]
        assert all(
            call.kwargs["voice_id"] == "zhiqi"
            and call.kwargs["sample_rate"] == 24000
            for call in adapter.backend.talk_speak.await_args_list
        )
        assert ws.binary
        assert all(isinstance(packet, bytes) and packet for packet in ws.binary)

    asyncio.run(run())


def test_local_tts_streams_pcm_directly_to_opus(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        adapter.runtime.config.voice.provider = "openai-compatible"
        adapter.runtime.config.voice.baseUrl = "http://speech.local/v1"
        adapter.runtime.config.voice.apiKey = "local-token"
        adapter.runtime.config.voice.ttsModel = "fun-cosyvoice3-0.5b"
        adapter.config.ttsVoice = "nano"

        async def chunks(**kwargs):
            assert kwargs["text"] == "本地语音。"
            assert kwargs["sample_rate"] == 24000
            yield bytes(1000)
            yield bytes(1880)

        with patch(
            "nano_openclaw.adapters.xiaozhi.connection.stream_local_speech",
            new=chunks,
        ):
            await connection._speak("本地语音。")
        assert len(ws.binary) == 1
        adapter.backend.talk_speak.assert_not_awaited()

    asyncio.run(run())


def test_local_tts_prefetches_sentence_streams(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        adapter.runtime.config.voice.provider = "openai-compatible"
        adapter.runtime.config.voice.baseUrl = "http://speech.local/v1"
        adapter.runtime.config.voice.apiKey = "local-token"
        adapter.runtime.config.voice.ttsModel = "fun-cosyvoice3-0.5b"
        adapter.config.ttsVoice = "nano"
        full_answer = "第一句。第二句！第三句？"
        requests = []

        async def chunks(**kwargs):
            requests.append(kwargs["text"])
            yield bytes(2880)

        with patch(
            "nano_openclaw.adapters.xiaozhi.connection.stream_local_speech",
            new=chunks,
        ):
            await connection._speak(full_answer)

        assert requests == ["第一句。", "第二句！第三句？"]
        sentence_starts = [
            item for item in ws.json
            if item.get("type") == "tts" and item.get("state") == "sentence_start"
        ]
        assert [item["text"] for item in sentence_starts] == requests
        assert len(ws.binary) == 2
        adapter.backend.talk_speak.assert_not_awaited()

    asyncio.run(run())


class _FakeLocalSpeechWebSocket:
    def __init__(self):
        self.sent = []
        self.events = asyncio.Queue()
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))
        kind = self.sent[-1]["type"]
        if kind == "session.update":
            await self.events.put(json.dumps({"type": "session.updated"}))
        elif kind == "input_audio_buffer.commit":
            await self.events.put(json.dumps({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "本地识别结果",
            }))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.events.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True
        await self.events.put(None)


def test_local_speech_transcriber_realtime_protocol():
    async def run():
        ws = _FakeLocalSpeechWebSocket()
        await ws.events.put(json.dumps({"type": "session.created"}))
        final = AsyncMock()
        partial = AsyncMock()

        async def connect(url, **kwargs):
            assert url == "ws://speech.local/v1/realtime?model=paraformer-zh-streaming"
            assert kwargs["additional_headers"]["Authorization"] == "Bearer secret"
            return ws

        transcriber = LocalSpeechTranscriber(
            url="ws://speech.local/v1/realtime",
            api_key="secret",
            model="paraformer-zh-streaming",
            on_final=final,
            on_partial=partial,
            connect_impl=connect,
        )
        await transcriber.start()
        await transcriber.send_audio(bytes(1920))
        await ws.events.put(json.dumps({
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "本地识别",
        }))
        await ws.events.put(json.dumps({
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "结果",
        }))
        await asyncio.sleep(0)
        await transcriber.stop()
        assert [item["type"] for item in ws.sent] == [
            "session.update",
            "input_audio_buffer.append",
            "input_audio_buffer.commit",
        ]
        assert [call.args[0] for call in partial.await_args_list] == [
            "本地识别", "本地识别结果"
        ]
        final.assert_awaited_once_with("本地识别结果")
        assert ws.closed

    asyncio.run(run())


def test_local_speech_close_cancels_reader_before_websocket_close():
    async def run():
        reader_cancelled = asyncio.Event()

        class CloseWaitsForReader:
            def __init__(self):
                self.closed = False

            async def close(self):
                await reader_cancelled.wait()
                self.closed = True

        async def reader():
            try:
                await asyncio.Event().wait()
            finally:
                reader_cancelled.set()

        ws = CloseWaitsForReader()
        transcriber = LocalSpeechTranscriber(
            url="ws://speech.local/v1/realtime",
            api_key="",
            model="paraformer-zh-streaming",
            on_final=AsyncMock(),
        )
        transcriber._ws = ws
        transcriber._reader = asyncio.create_task(reader())
        await asyncio.sleep(0)

        await asyncio.wait_for(transcriber.close(), timeout=0.2)

        assert reader_cancelled.is_set()
        assert ws.closed

    asyncio.run(run())


def test_connection_forwards_throttled_partial_stt(tmp_path):
    async def run():
        connection, ws, _ = _connection_fixture(tmp_path)
        connection._asr = object()
        connection._asr_generation = 3

        await connection._on_partial_transcript("正在", generation=3)
        await connection._on_partial_transcript("正在", generation=3)
        await connection._on_partial_transcript("正在识别", generation=2)
        await connection._on_partial_transcript("正在识别", generation=3)
        connection._last_partial_sent_at = 0.0
        await connection._on_partial_transcript("正在识别", generation=3)

        stt = [item for item in ws.json if item.get("type") == "stt"]
        assert [item["text"] for item in stt] == ["正在", "正在识别"]

    asyncio.run(run())


def test_final_stt_is_sent_before_asr_cleanup_finishes(tmp_path):
    async def run():
        connection, ws, _ = _connection_fixture(tmp_path)
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def slow_cleanup():
            cleanup_started.set()
            await allow_cleanup.wait()

        connection.mcp.ready.set()
        connection._stop_asr = slow_cleanup
        connection._collect_agent_reply = AsyncMock(return_value="")

        turn = asyncio.create_task(connection._run_transcript("今晚还下雨吗？"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)

        stt = [item for item in ws.json if item.get("type") == "stt"]
        assert stt[-1]["text"] == "今晚还下雨吗？"
        assert not turn.done()

        allow_cleanup.set()
        await turn

    asyncio.run(run())


def test_no_voice_timeout_closes_device_websocket(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        adapter.config.noVoiceTimeoutSeconds = 0.03
        connection._want_listening = True

        connection._arm_no_voice_timeout()
        await asyncio.wait_for(ws.closed.wait(), timeout=0.2)

        assert ws.close_calls == [{"code": 1000, "reason": "no voice timeout"}]
        assert connection._want_listening is False
        assert connection._no_voice_task is None

    asyncio.run(run())


def test_recognized_voice_refreshes_no_voice_timeout(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        adapter.config.noVoiceTimeoutSeconds = 0.06
        connection._want_listening = True

        connection._arm_no_voice_timeout()
        await asyncio.sleep(0.04)
        connection._record_voice_activity()
        await asyncio.sleep(0.04)
        assert ws.close_calls == []

        await asyncio.sleep(0.08)
        assert ws.close_calls == [{"code": 1000, "reason": "no voice timeout"}]

    asyncio.run(run())


def test_stopping_asr_pauses_no_voice_timeout(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        adapter.config.noVoiceTimeoutSeconds = 0.03
        connection._want_listening = True

        connection._arm_no_voice_timeout()
        await connection._stop_asr()
        await asyncio.sleep(0.06)

        assert ws.close_calls == []
        assert connection._no_voice_task is None

    asyncio.run(run())


def test_local_asr_final_stops_no_voice_watcher_without_deadlock(tmp_path):
    async def run():
        connection, _, adapter = _connection_fixture(tmp_path)
        adapter.config.noVoiceTimeoutSeconds = 0.2
        connection._want_listening = True
        connection._asr_generation = 1
        connection.mcp.ready.set()
        connection._collect_agent_reply = AsyncMock(return_value="")

        ws = _FakeLocalSpeechWebSocket()
        transcriber = LocalSpeechTranscriber(
            url="ws://speech.local/v1/realtime",
            api_key="",
            model="paraformer-zh-streaming",
            on_final=lambda text: connection._on_final_transcript(text, generation=1),
        )
        transcriber._ws = ws
        transcriber._reader = asyncio.create_task(transcriber._read_loop())
        transcriber._audio_sent = True
        connection._asr = transcriber
        connection._arm_no_voice_timeout()

        await ws.events.put(json.dumps({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "今晚还下雨吗？",
        }))
        while connection._turn_task is None:
            await asyncio.sleep(0)
        await asyncio.wait_for(connection._turn_task, timeout=0.1)

        assert connection._no_voice_task is None
        assert transcriber._closed

    asyncio.run(run())


def test_agent_reply_uses_only_last_completed_message(tmp_path):
    async def run():
        connection, _, adapter = _connection_fixture(tmp_path)
        payloads = [
            {"turn_id": "turn-1", "type": "text.delta", "text": "我要调用工具"},
            {"turn_id": "turn-1", "type": "message.end"},
            {"turn_id": "turn-1", "type": "text.delta", "text": "最终答案"},
            {"turn_id": "turn-1", "type": "message.end"},
            {"turn_id": "turn-1", "type": "turn.done"},
        ]

        async def event_stream():
            for payload in payloads:
                yield SimpleNamespace(payload=payload)

        adapter.backend.subscribe = lambda **_kwargs: event_stream()
        adapter.backend.chat_send = AsyncMock(return_value="turn-1")
        assert await connection._collect_agent_reply("问题") == "最终答案"
        assert adapter.backend.chat_send.await_args.kwargs["turn_source"] == "xiaozhi"
        assert adapter.backend.chat_send.await_args.kwargs["response_style"] == "voice"
        assert adapter.backend.chat_send.await_args.kwargs["channel_sender_key"] == "dev-1"

    asyncio.run(run())


def test_one_asr_generation_starts_only_one_turn(tmp_path):
    async def run():
        connection, _, _ = _connection_fixture(tmp_path)
        connection._asr = object()
        connection._asr_generation = 7
        gate = asyncio.Event()

        async def hold_turn(_text):
            await gate.wait()

        with patch.object(connection, "_run_transcript", side_effect=hold_turn) as run_turn:
            await connection._on_final_transcript("第一段", generation=7)
            await connection._on_final_transcript("第二段", generation=7)
            await asyncio.sleep(0)
            run_turn.assert_called_once_with("第一段")
            gate.set()
            await connection._turn_task

    asyncio.run(run())


def test_busy_turn_waits_for_release_and_retries(tmp_path):
    async def run():
        connection, _, adapter = _connection_fixture(tmp_path)
        adapter.backend.chat_send = AsyncMock(side_effect=[
            BusyError(
                "session has an active turn",
                retry_after_ms=500,
                details={"active_turn_id": "old-turn"},
            ),
            "new-turn",
        ])
        adapter.backend.sessions_get = AsyncMock(return_value=SimpleNamespace(active_turn_id=None))

        assert await connection._send_agent_turn("长语音") == "new-turn"
        assert adapter.backend.chat_send.await_count == 2
        adapter.backend.sessions_get.assert_awaited_once_with("session-1")

    asyncio.run(run())


def test_auto_mode_waits_for_device_to_restart_asr(tmp_path):
    async def run():
        connection, _, _ = _connection_fixture(tmp_path)
        connection._want_listening = True
        connection._listening_mode = "auto"
        connection.mcp.ready.set()
        connection._stop_asr = AsyncMock()
        connection._collect_agent_reply = AsyncMock(return_value="")
        connection._start_asr = AsyncMock()

        await connection._run_transcript("自动模式")

        connection._start_asr.assert_not_awaited()

    asyncio.run(run())


def test_realtime_mode_restarts_asr_server_side(tmp_path):
    async def run():
        connection, _, _ = _connection_fixture(tmp_path)
        connection._want_listening = True
        connection._listening_mode = "realtime"
        connection.mcp.ready.set()
        connection._stop_asr = AsyncMock()
        connection._collect_agent_reply = AsyncMock(return_value="")
        connection._start_asr = AsyncMock()

        await connection._run_transcript("实时模式")

        connection._start_asr.assert_awaited_once_with()

    asyncio.run(run())


def test_abort_cancels_turn_and_backend(tmp_path):
    async def run():
        connection, ws, adapter = _connection_fixture(tmp_path)
        connection._current_turn_id = "turn-1"
        connection._turn_task = asyncio.create_task(asyncio.Event().wait())
        adapter.backend.sessions_get = AsyncMock(
            return_value=SimpleNamespace(active_turn_id=None)
        )
        await connection.abort(send_tts_stop=True)
        adapter.backend.chat_abort.assert_awaited_once_with(turn_id="turn-1")
        adapter.backend.sessions_get.assert_awaited_once_with("session-1")
        assert connection._turn_task is None
        assert ws.json[-1]["type"] == "tts" and ws.json[-1]["state"] == "stop"

    asyncio.run(run())


def test_abort_discards_final_emitted_while_stopping_asr(tmp_path):
    async def run():
        connection, _, _ = _connection_fixture(tmp_path)
        connection._asr_generation = 4

        class FinalOnStop:
            async def stop(self):
                await connection._on_final_transcript("不应提交", generation=4)

        connection._asr = FinalOnStop()
        with patch.object(connection, "_run_transcript") as run_turn:
            await connection.abort(send_tts_stop=False)
            await asyncio.sleep(0)
            run_turn.assert_not_called()
        assert connection._turn_task is None

    asyncio.run(run())


def test_mcp_timeout_and_disconnect():
    async def run():
        peer = DeviceMcpPeer(
            session_id="s1",
            send_json=AsyncMock(),
            vision_url="http://host/vision",
            token="secret",
            timeout_ms=10,
        )
        with pytest.raises(asyncio.TimeoutError):
            await peer.request("tools/list", {})

        pending = asyncio.create_task(peer.request("tools/list", {}))
        await asyncio.sleep(0)
        peer.close()
        with pytest.raises(RuntimeError, match="disconnected"):
            await pending

    asyncio.run(run())
