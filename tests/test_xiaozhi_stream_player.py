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


def test_playback_controller_defers_until_turn_done_and_stops_in_background():
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
        ):
            assert stream_url.endswith("/streams/token")
            assert label == "周杰伦 - 晴天"
            assert transport is None
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
            await asyncio.wait_for(stream_started.wait(), timeout=0.2)
            assert connection.playback.snapshot()["state"] == "playing"

            stopped = await connection.playback.stop(
                playback_id=started["playback_id"],
            )
            assert stopped["stopped"] is True
            assert stopped["active"] is False
            assert stopped["state"] == "stopped"

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

    asyncio.run(run())


@pytest.mark.skipif(
    os.getenv("EASY_MUSIC_INTEGRATION") != "1",
    reason="set EASY_MUSIC_INTEGRATION=1 to exercise the live MCP music source",
)
def test_real_easy_music_mcp_streams_to_fake_xiaozhi():
    async def run():
        binary = os.environ["EASY_MUSIC_BIN"]
        runtime = McpRuntime()
        try:
            await runtime.initialize(
                {
                    "easy-music": McpServerConfig(
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
                    "easy-music",
                    "search_music",
                    {"title": "晴天", "artist": "周杰伦"},
                )
            )
            assert selection["ok"] is True
            assert selection["selected"]["id"]

            prepared = json.loads(
                await runtime.call_tool(
                    "easy-music",
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
                    "easy-music",
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
        with pytest.raises(RuntimeError, match="unsupported content type"):
            await _play_stream(
                _connection(),
                stream_url="http://127.0.0.1:32123/streams/token",
                label="",
                transport=transport,
            )

    asyncio.run(run())


def test_packet_parser_rejects_invalid_and_preserves_incomplete_frames():
    incomplete = bytearray((5).to_bytes(4, "big") + b"no")
    assert _take_packet(incomplete) is None
    assert incomplete == (5).to_bytes(4, "big") + b"no"

    invalid = bytearray((0).to_bytes(4, "big"))
    with pytest.raises(RuntimeError, match="invalid Opus packet length"):
        _take_packet(invalid)
