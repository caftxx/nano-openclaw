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
    return SimpleNamespace(
        device_id="device-1",
        session_id="session-1",
        _closed=False,
        send_json=AsyncMock(),
        send_bytes=AsyncMock(side_effect=send_bytes),
    )


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

    connection = SimpleNamespace(
        mcp=SimpleNamespace(materialize_tools=list),
    )
    adapter.hub.add("device-1", connection)
    assert adapter.decorate_tools(base, "device-1").names() == [
        "base_tool",
        "xiaozhi_play_stream",
    ]
    assert base.names() == ["base_tool"]


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
                        "duration_seconds": 0.3,
                    },
                )
            )
            assert prepared["ok"] is True
            assert prepared["content_type"] == "application/x-opus-packets"
            assert prepared["framing"] == "len32be"

            connection = _connection()
            result = await _play_stream(
                connection,
                stream_url=prepared["stream_url"],
                label="integration track",
            )
            assert result["packets"] > 0
            assert connection.send_bytes.await_count == result["packets"]
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
