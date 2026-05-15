"""DingtalkStreamClient: open_connection + WebSocket loop + ACK + reconnect.

These tests inject a fake WebSocket factory and a mocked HTTP transport so we
don't talk to the real DingTalk servers. They verify the four invariants
the channel relies on:

1. Each connection round begins with a fresh ``POST /gateway/connections/open``.
2. The WebSocket URL embeds the freshly minted ticket (url-quoted).
3. Every inbound frame is ACKed with ``code=200`` and the original ``messageId``.
4. After a clean disconnect, the loop reconnects with a brand new ticket
   (the old one is intentionally not reused).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from nano_openclaw.dingtalk.stream_client import (
    OPEN_CONNECTION_URL,
    DingtalkStreamClient,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeWs:
    """Minimal async-iterable WebSocket double.

    The recv loop in ``stream_client`` does ``async for raw in ws`` so we
    implement ``__aiter__``/``__anext__`` to emit pre-scripted frames and
    then signal a clean close via ``StopAsyncIteration``.
    """

    def __init__(self, frames: list[str], sent: list[str]) -> None:
        self._frames = list(frames)
        self.sent = sent

    async def __aenter__(self) -> "_FakeWs":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False

    def __aiter__(self) -> "_FakeWs":
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        # Cooperative yield so coroutines waiting on cancellation can run.
        await asyncio.sleep(0)
        return self._frames.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeWsConnect:
    """Records connection attempts and returns scripted ``_FakeWs`` instances."""

    def __init__(self, ws_scripts: list[list[str]]) -> None:
        self.ws_scripts = list(ws_scripts)
        self.urls: list[str] = []
        self.sent_per_connection: list[list[str]] = []

    def __call__(self, url: str, **kwargs: Any) -> _FakeWs:
        self.urls.append(url)
        sent: list[str] = []
        self.sent_per_connection.append(sent)
        frames = self.ws_scripts.pop(0) if self.ws_scripts else []
        return _FakeWs(frames, sent)


def _open_connection_handler(tickets: list[str]) -> Any:
    """Build an ``httpx.MockTransport`` handler that hands out tickets in order."""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(OPEN_CONNECTION_URL)
        body = json.loads(request.read())
        calls.append(body)
        ticket = tickets.pop(0) if tickets else f"ticket-{len(calls)}"
        return httpx.Response(200, json={
            "endpoint": "wss://ding.example/stream",
            "ticket": ticket,
        })

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# ── Tests ──────────────────────────────────────────────────────────────────


def test_open_connection_endpoint_and_payload_shape():
    handler = _open_connection_handler(["t-1"])
    ws_connect = _FakeWsConnect(ws_scripts=[[]])  # one connection, zero frames
    received: list[Any] = []

    async def on_callback(frame):
        received.append(frame)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = DingtalkStreamClient(
                client_id="ding-test",
                client_secret="sec",
                on_callback=on_callback,
                http_client=http_client,
                ws_connect=ws_connect,
            )
            task = asyncio.create_task(client.run())
            # Let the loop reach a steady-state reconnect sleep.
            await asyncio.sleep(0.05)
            await client.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(run())

    body = handler.calls[0]
    assert body["clientId"] == "ding-test"
    assert body["clientSecret"] == "sec"
    assert isinstance(body["subscriptions"], list) and body["subscriptions"]
    assert any(s["topic"] == "/v1.0/im/bot/messages/get" for s in body["subscriptions"])


def test_ws_url_carries_url_quoted_ticket():
    handler = _open_connection_handler(["my ticket/with+chars"])
    ws_connect = _FakeWsConnect(ws_scripts=[[]])

    async def on_callback(frame):
        pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = DingtalkStreamClient(
                client_id="c",
                client_secret="s",
                on_callback=on_callback,
                http_client=http_client,
                ws_connect=ws_connect,
            )
            task = asyncio.create_task(client.run())
            await asyncio.sleep(0.05)
            await client.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(run())

    url = ws_connect.urls[0]
    parsed = urlsplit(url)
    ticket = parse_qs(parsed.query)["ticket"][0]
    assert ticket == "my ticket/with+chars", "ticket should round-trip after url-quote"


def test_inbound_callback_frame_is_acked_with_message_id():
    callback_frame = {
        "type": "CALLBACK",
        "specVersion": "1",
        "headers": {
            "messageId": "msg-abc-123",
            "topic": "/v1.0/im/bot/messages/get",
            "contentType": "application/json",
        },
        "data": json.dumps({"msgtype": "text", "text": {"content": "hi"}}),
    }
    handler = _open_connection_handler(["t-1"])
    ws_connect = _FakeWsConnect(ws_scripts=[[json.dumps(callback_frame)]])

    received: list[Any] = []

    async def on_callback(frame):
        received.append(frame)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = DingtalkStreamClient(
                client_id="c",
                client_secret="s",
                on_callback=on_callback,
                http_client=http_client,
                ws_connect=ws_connect,
            )
            task = asyncio.create_task(client.run())
            await asyncio.sleep(0.1)
            await client.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(run())

    assert len(received) == 1
    assert received[0].headers.messageId == "msg-abc-123"

    sent = ws_connect.sent_per_connection[0]
    assert len(sent) == 1
    ack = json.loads(sent[0])
    assert ack["code"] == 200
    assert ack["headers"]["messageId"] == "msg-abc-123"


def test_reconnect_opens_new_ticket_after_clean_disconnect():
    """After the first WebSocket session ends (frames exhausted), the client
    must call ``open_connection`` again before reconnecting — old tickets
    are server-side single-use."""
    handler = _open_connection_handler(["ticket-1", "ticket-2"])
    # Two scripted sessions: first empty (closes immediately), second also
    # empty. The loop will keep cycling so we tear it down after both opens.
    ws_connect = _FakeWsConnect(ws_scripts=[[], []])

    async def on_callback(frame):
        pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = DingtalkStreamClient(
                client_id="c",
                client_secret="s",
                on_callback=on_callback,
                http_client=http_client,
                ws_connect=ws_connect,
                # Tiny backoff so the test doesn't pay the full 1s+ jitter
                # window — production defaults are still used everywhere else.
                backoff_base=0.01,
                backoff_max=0.05,
            )
            task = asyncio.create_task(client.run())
            await asyncio.sleep(0.2)
            await client.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(run())

    assert len(handler.calls) >= 2, "expected at least two open_connection calls"
    tickets = [urlsplit(u).query.split("ticket=")[1] for u in ws_connect.urls]
    assert tickets[0] != tickets[1], "reconnect must use a fresh ticket"


def test_system_disconnect_frame_is_dispatched_and_acked():
    """SYSTEM/topic=disconnect frames must still be ACKed so the server's
    bookkeeping stays consistent. The on_system callback is invoked too."""
    sys_frame = {
        "type": "SYSTEM",
        "specVersion": "1",
        "headers": {"messageId": "sys-1", "topic": "disconnect"},
        "data": "{}",
    }
    handler = _open_connection_handler(["t-1"])
    ws_connect = _FakeWsConnect(ws_scripts=[[json.dumps(sys_frame)]])

    seen: list[Any] = []

    async def on_callback(frame):
        pass

    async def on_system(frame):
        seen.append(frame)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = DingtalkStreamClient(
                client_id="c",
                client_secret="s",
                on_callback=on_callback,
                on_system=on_system,
                http_client=http_client,
                ws_connect=ws_connect,
            )
            task = asyncio.create_task(client.run())
            await asyncio.sleep(0.1)
            await client.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(run())

    assert len(seen) == 1
    assert seen[0].headers.topic == "disconnect"
    ack = json.loads(ws_connect.sent_per_connection[0][0])
    assert ack["headers"]["messageId"] == "sys-1"


def test_frame_parse_error_does_not_crash_loop():
    """A malformed frame is logged + skipped; the loop survives to handle
    the next frame in the same session."""
    good_frame = {
        "type": "CALLBACK",
        "specVersion": "1",
        "headers": {"messageId": "good-1", "topic": "/v1.0/im/bot/messages/get"},
        "data": "{}",
    }
    handler = _open_connection_handler(["t-1"])
    ws_connect = _FakeWsConnect(ws_scripts=[["not-json", json.dumps(good_frame)]])

    received: list[Any] = []

    async def on_callback(frame):
        received.append(frame)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = DingtalkStreamClient(
                client_id="c",
                client_secret="s",
                on_callback=on_callback,
                http_client=http_client,
                ws_connect=ws_connect,
            )
            task = asyncio.create_task(client.run())
            await asyncio.sleep(0.1)
            await client.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(run())

    assert [f.headers.messageId for f in received] == ["good-1"]
