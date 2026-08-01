from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nano_openclaw.adapters.channels.base import ChannelAccount
from nano_openclaw.adapters.xiaozhi.channel import XiaozhiChannel
from nano_openclaw.adapters.xiaozhi.mcp import XiaozhiHub
from nano_openclaw.adapters.xiaozhi.stream_player import (
    XiaozhiPlaybackController,
    _next_packet_deadline,
    _play_stream,
    _take_packet,
    _validate_stream_url,
)
from nano_openclaw.config.types import McpServerConfig
from nano_openclaw.core.tools import Tool, ToolRegistry
from nano_openclaw.features.mcp.runtime import McpRuntime


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield _framed(b"opus")
        await self.release.wait()


def _framed(*packets: bytes) -> bytes:
    return b"".join(len(packet).to_bytes(4, "big") + packet for packet in packets)


def _connection(send_bytes=None):
    connection = SimpleNamespace(
        device_id="device-1",
        session_id="session-1",
        _closed=False,
        _turn_task=None,
        send_json=AsyncMock(),
        send_bytes=AsyncMock(side_effect=send_bytes),
    )
    connection.playback = XiaozhiPlaybackController(connection)
    return connection


def test_stream_tool_is_scoped_to_connected_xiaozhi_device():
    adapter = XiaozhiChannel(ChannelAccount(id="default"))
    adapter._state = "running"
    adapter.hub = XiaozhiHub()
    base = ToolRegistry()
    base.register(
        Tool(
            name="base_tool",
            description="base",
            input_schema={"type": "object"},
            run=lambda _args: "ok",
        )
    )

    assert adapter.decorate_tools(base, "missing-device").names() == ["base_tool"]

    connection = _connection()
    connection.mcp = SimpleNamespace(materialize_tools=list)
    adapter.hub.add("device-1", connection)
    assert adapter.decorate_tools(base, "device-1").names() == [
        "base_tool",
        "xiaozhi_start_playback",
        "xiaozhi_stop_playback",
        "xiaozhi_playback_status",
    ]
    assert base.names() == ["base_tool"]


def test_playback_controller_starts_immediately_after_turn_handoff():
    async def run():
        connection = _connection()
        turn_release = asyncio.Event()
        stream_started = asyncio.Event()
        stream_release = asyncio.Event()

        async def active_turn():
            await turn_release.wait()

        async def fake_stream(
            _connection,
            *,
            stream_url,
            label,
            transport=None,
            on_started=None,
            on_releasing=None,
        ):
            assert stream_url.endswith("/streams/token")
            assert label == "周杰伦 - 晴天"
            assert transport is None
            assert on_releasing is not None
            if on_started is not None:
                on_started()
            stream_started.set()
            await stream_release.wait()
            return {"packets": 2, "audio_bytes": 16}

        connection._turn_task = asyncio.create_task(active_turn())
        with patch(
            "nano_openclaw.adapters.xiaozhi.stream_player._play_stream",
            new=fake_stream,
        ):
            started = await connection.playback.start(
                stream_url="http://127.0.0.1:32123/streams/token",
                label="周杰伦 - 晴天",
            )
            assert started["state"] == "queued"
            assert started["active"] is True
            assert len(started["playback_id"]) == 32

            await asyncio.sleep(0)
            assert not stream_started.is_set()
            turn_release.set()
            await connection._turn_task
            # Auto-mode firmware may send listen/start shortly after tts/stop.
            # The queued stream must claim the device on the next event-loop
            # turn instead of waiting in a cancellable handoff window.
            await asyncio.sleep(0)
            assert stream_started.is_set()
            assert connection.playback.snapshot()["state"] == "playing"

            stopped = await connection.playback.stop(
                playback_id=started["playback_id"],
                reason="device_abort",
            )
            assert stopped["stopped"] is True
            assert stopped["active"] is False
            assert stopped["state"] == "stopped"
            assert stopped["stop_reason"] == "device_abort"

    asyncio.run(run())


def test_playback_controller_replaces_active_stream_and_rejects_stale_stop():
    async def run():
        connection = _connection()
        release = asyncio.Event()

        async def fake_stream(_connection, **_kwargs):
            await release.wait()
            return {"packets": 1}

        with patch(
            "nano_openclaw.adapters.xiaozhi.stream_player._play_stream",
            new=fake_stream,
        ):
            first = await connection.playback.start(
                stream_url="http://127.0.0.1:32123/streams/first",
                label="first",
            )
            second = await connection.playback.start(
                stream_url="http://127.0.0.1:32123/streams/second",
                label="second",
            )
            assert second["replaced_playback_id"] == first["playback_id"]
            assert second["playback_id"] != first["playback_id"]

            stale = await connection.playback.stop(
                playback_id=first["playback_id"],
            )
            assert stale["ok"] is False
            assert stale["reason"] == "playback_id_mismatch"
            assert stale["active"] is True
            await connection.playback.stop()

    asyncio.run(run())


def test_stream_player_claims_device_before_opening_loopback_stream():
    async def run():
        events = []
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        class GatedTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, _request):
                events.append("http:get")
                request_started.set()
                await release_request.wait()
                return httpx.Response(
                    200,
                    headers={"content-type": "application/x-opus-packets"},
                    stream=_ChunkStream([_framed(b"opus")]),
                )

        async def send_json(payload):
            events.append(f"tts:{payload['state']}")

        async def send_bytes(_payload):
            events.append("audio")

        connection = _connection(send_bytes)
        connection.send_json = AsyncMock(side_effect=send_json)
        task = asyncio.create_task(
            _play_stream(
                connection,
                stream_url="http://127.0.0.1:32123/streams/token",
                label="周杰伦 - 晴天",
                transport=GatedTransport(),
            )
        )
        try:
            await asyncio.wait_for(request_started.wait(), timeout=0.2)
            assert events == ["tts:start", "http:get"]
        finally:
            release_request.set()
        await task

        assert events == [
            "tts:start",
            "http:get",
            "tts:sentence_start",
            "audio",
            "tts:stop",
        ]

    asyncio.run(run())


def test_stream_player_strips_lengths_and_sends_paced_opus():
    async def run():
        framed = _framed(b"opus-one", b"opus-two")
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/x-opus-packets"},
                stream=_ChunkStream([framed[:3], framed[3:11], framed[11:]]),
            )
        )
        connection = _connection()
        with patch(
            "nano_openclaw.adapters.xiaozhi.stream_player.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await _play_stream(
                connection,
                stream_url="http://127.0.0.1:32123/streams/token",
                label="周杰伦 - 晴天",
                transport=transport,
            )

        assert [call.args[0] for call in connection.send_bytes.await_args_list] == [
            b"opus-one",
            b"opus-two",
        ]
        assert [
            call.args[0]["state"] for call in connection.send_json.await_args_list
        ] == [
            "start",
            "sentence_start",
            "stop",
        ]
        assert result["ok"] is True
        assert result["packets"] == 2
        assert result["audio_bytes"] == 16
        assert result["audio_duration_seconds"] == 0.12
        assert result["first_packet_latency_seconds"] >= 0
        assert result["pacing_resyncs"] == 0

    asyncio.run(run())


def test_stream_player_times_out_when_audio_stalls():
    async def run():
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/x-opus-packets"},
                stream=_BlockingStream(),
            )
        )
        connection = _connection()

        with pytest.raises(RuntimeError, match="stream stalled for 0.01 seconds"):
            await _play_stream(
                connection,
                stream_url="http://127.0.0.1:32123/streams/token",
                label="stalled track",
                transport=transport,
                read_timeout_seconds=0.01,
            )

        assert [
            call.args[0]["state"] for call in connection.send_json.await_args_list
        ] == ["start", "sentence_start", "stop"]
        assert connection.send_bytes.await_count == 1

    asyncio.run(run())


def test_packet_pacing_resyncs_instead_of_bursting_when_far_behind():
    deadline, resynced = _next_packet_deadline(1.0, 1.02)
    assert deadline == pytest.approx(1.06)
    assert resynced is False

    deadline, resynced = _next_packet_deadline(1.0, 1.20)
    assert deadline == pytest.approx(1.20)
    assert resynced is True


@pytest.mark.skipif(
    os.getenv("EASYMUSIC_INTEGRATION") != "1",
    reason="set EASYMUSIC_INTEGRATION=1 to exercise the live MCP music source",
)
def test_real_easymusic_mcp_streams_to_fake_xiaozhi():
    async def run():
        binary = os.environ["EASYMUSIC_BIN"]
        runtime = McpRuntime()
        try:
            await runtime.initialize(
                {
                    "easymusic": McpServerConfig(
                        command=binary,
                        transport="stdio",
                        args=["mcp", "--stream-ttl-seconds=10"],
                    )
                }
            )
            assert runtime.status_snapshot()["connected"] == 1
            assert {tool.tool_name for tool in runtime.get_mcp_tools()} == {
                "search_music",
                "prepare_stream",
            }

            selection = json.loads(
                await runtime.call_tool(
                    "easymusic",
                    "search_music",
                    {"title": "晴天", "artist": "周杰伦"},
                )
            )
            assert selection["ok"] is True
            assert selection["selected"]["id"]

            prepared = json.loads(
                await runtime.call_tool(
                    "easymusic",
                    "prepare_stream",
                    {
                        "id": "MTEyNjE3ODA=",
                        "profile": "xiaozhi-v1",
                        "duration_seconds": 0.3,
                    },
                )
            )
            assert prepared["ok"] is True
            assert prepared["profile"] == "xiaozhi-v1"
            assert prepared["content_type"] == "application/x-opus-packets"
            assert prepared["framing"] == "len32be"
            assert prepared["ready"] is True
            assert prepared["prebuffered_bytes"] > 0
            assert prepared["prepare_latency_ms"] >= 0

            connection = _connection()
            playback = await connection.playback.start(
                stream_url=prepared["stream_url"],
                label="integration track",
            )
            assert playback["active"] is True
            async with asyncio.timeout(3):
                while connection.playback.snapshot()["active"]:
                    await asyncio.sleep(0.02)
            status = connection.playback.snapshot()
            assert status["state"] == "completed"
            assert status["stats"]["packets"] > 0
            assert connection.send_bytes.await_count == status["stats"]["packets"]

            web = json.loads(
                await runtime.call_tool(
                    "easymusic",
                    "prepare_stream",
                    {
                        "id": selection["selected"]["id"],
                        "profile": "web-opus",
                        "duration_seconds": 0.3,
                    },
                )
            )
            assert web["profile"] == "web-opus"
            assert web["content_type"] == "audio/ogg"
            assert web["ready"] is True
            assert web["prebuffered_bytes"] > 0
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(web["stream_url"])
            response.raise_for_status()
            assert response.headers["content-type"] == "audio/ogg"
            assert response.content.startswith(b"OggS")
        finally:
            await runtime.close()

    asyncio.run(run())


def test_stream_player_cancellation_closes_playback():
    async def run():
        sent = asyncio.Event()

        async def send_bytes(_packet):
            sent.set()

        stream = _BlockingStream()
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/x-opus-packets"},
                stream=stream,
            )
        )
        connection = _connection(send_bytes)
        task = asyncio.create_task(
            _play_stream(
                connection,
                stream_url="http://127.0.0.1:32123/streams/token",
                label="track",
                transport=transport,
            )
        )
        await asyncio.wait_for(sent.wait(), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert connection.send_json.await_args_list[-1].args[0]["state"] == "stop"

    asyncio.run(run())


def test_stream_player_rejects_invalid_source_and_content_type():
    for value in (
        "https://127.0.0.1:32123/streams/token",
        "http://localhost:32123/streams/token",
        "http://192.168.1.2:32123/streams/token",
        "http://127.0.0.1:32123/not-streams/token",
    ):
        with pytest.raises(ValueError):
            _validate_stream_url(value)

    async def run():
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "audio/mpeg"},
                content=b"not opus",
            )
        )
        connection = _connection()
        with pytest.raises(RuntimeError, match="unsupported content type"):
            await _play_stream(
                connection,
                stream_url="http://127.0.0.1:32123/streams/token",
                label="",
                transport=transport,
            )
        assert [
            call.args[0]["state"] for call in connection.send_json.await_args_list
        ] == ["start", "stop"]

    asyncio.run(run())


def test_packet_parser_rejects_invalid_and_preserves_incomplete_frames():
    incomplete = bytearray((5).to_bytes(4, "big") + b"no")
    assert _take_packet(incomplete) is None
    assert incomplete == (5).to_bytes(4, "big") + b"no"

    invalid = bytearray((0).to_bytes(4, "big"))
    with pytest.raises(RuntimeError, match="invalid Opus packet length"):
        _take_packet(invalid)
