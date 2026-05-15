"""``send_text_via_webhook`` / ``send_markdown_via_webhook`` payload shapes."""

from __future__ import annotations

import asyncio
import json

import httpx

from nano_openclaw.dingtalk.sender import (
    MAX_TEXT_SEGMENT,
    send_markdown_via_webhook,
    send_text_via_webhook,
)


class _Capturer:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({
            "url": str(request.url),
            "body": json.loads(request.read()),
        })
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})


def _run(coro):
    return asyncio.run(coro)


def test_send_text_basic_payload_shape():
    cap = _Capturer()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(cap.handler)) as client:
            await send_text_via_webhook(client, "https://dt/hook", "hello world")

    _run(run())
    assert len(cap.requests) == 1
    body = cap.requests[0]["body"]
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "hello world"
    assert "at" not in body


def test_send_text_with_at_user_ids():
    cap = _Capturer()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(cap.handler)) as client:
            await send_text_via_webhook(
                client,
                "https://dt/hook",
                "hi",
                at_user_ids=["staff-1", "staff-2"],
            )

    _run(run())
    body = cap.requests[0]["body"]
    assert body["at"] == {"atUserIds": ["staff-1", "staff-2"], "isAtAll": False}


def test_send_text_chunks_long_payloads():
    cap = _Capturer()
    long = "\n".join("line " + str(i) * 100 for i in range(50))
    assert len(long) > MAX_TEXT_SEGMENT

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(cap.handler)) as client:
            await send_text_via_webhook(client, "https://dt/hook", long)

    _run(run())
    assert len(cap.requests) >= 2, "long text should fan out across multiple sends"
    for req in cap.requests:
        assert len(req["body"]["text"]["content"]) <= MAX_TEXT_SEGMENT


def test_send_text_skips_empty_payload():
    cap = _Capturer()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(cap.handler)) as client:
            await send_text_via_webhook(client, "https://dt/hook", "")
            await send_text_via_webhook(client, "", "non-empty")

    _run(run())
    assert cap.requests == []


def test_send_markdown_payload_shape():
    cap = _Capturer()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(cap.handler)) as client:
            await send_markdown_via_webhook(
                client,
                "https://dt/hook",
                "Title!",
                "## body\nline",
            )

    _run(run())
    body = cap.requests[0]["body"]
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["title"] == "Title!"
    assert "body" in body["markdown"]["text"]


def test_send_text_swallows_http_errors():
    """Sender failures must not crash the agent turn — they're best-effort."""
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errcode": -1, "errmsg": "boom"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
            await send_text_via_webhook(client, "https://dt/hook", "hi")  # no raise

    _run(run())
