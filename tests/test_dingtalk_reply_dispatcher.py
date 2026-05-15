"""ReplyDispatcher: throttle + AI Card pathway + text fallback."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nano_openclaw.dingtalk import ai_card
from nano_openclaw.dingtalk.extract import ExtractedMessage
from nano_openclaw.dingtalk.reply_dispatcher import ReplyDispatcher
from nano_openclaw.dingtalk.token import DingtalkTokenManager


@pytest.fixture(autouse=True)
def _fast_bucket():
    ai_card.reset_global_bucket(max_qps=1000, backoff=0.01)
    yield
    ai_card.reset_global_bucket()


def _msg(*, group: bool = False) -> ExtractedMessage:
    return ExtractedMessage(
        text="hi",
        sender_staff_id="u-1",
        sender_nick="A",
        conversation_id="c-1",
        is_group=group,
        at_self=group,
        msg_id="m-1",
        session_webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
        session_webhook_expire_ms=0,
        msgtype="text",
        robot_code="bot-x",
    )


class _Recorder:
    def __init__(self, *, card_create_succeeds: bool = True, all_succeed: bool = True) -> None:
        self.calls: list[dict] = []
        self.card_create_succeeds = card_create_succeeds
        self.all_succeed = all_succeed

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = None
        if request.content:
            try:
                body = json.loads(request.read())
            except (ValueError, UnicodeDecodeError):
                body = None
        self.calls.append({"method": request.method, "path": path, "body": body, "url": str(request.url)})

        if path.endswith("/v1.0/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "tok", "expireIn": 7200})
        if path == "/v1.0/card/instances" and request.method == "POST":
            if self.card_create_succeeds:
                return httpx.Response(200, json={"errcode": 0})
            return httpx.Response(500, json={"errcode": -1})
        if not self.all_succeed:
            return httpx.Response(500, json={"errcode": -1})
        return httpx.Response(200, json={"errcode": 0})


def _run(coro):
    return asyncio.run(coro)


# ── happy path: AI Card create → stream → finish ───────────────────────────


def test_ai_card_path_create_stream_finish():
    rec = _Recorder()
    transport = httpx.MockTransport(rec.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            mgr = DingtalkTokenManager(client=client)
            d = ReplyDispatcher(
                http_client=client,
                msg=_msg(group=False),
                token_mgr=mgr,
                client_id="ding-a",
                client_secret="sec",
                throttle_seconds=0.0,  # disable throttle so every chunk emits
            )
            await d.on_start()
            await d.on_partial("hel")
            await d.on_partial("lo")
            await d.on_final()

    _run(run())

    paths = [c["path"] for c in rec.calls]
    # Each lifecycle stage hit at least once.
    assert "/v1.0/card/instances" in paths            # create + finish
    assert "/v1.0/card/instances/deliver" in paths    # deliver
    assert "/v1.0/card/streaming" in paths            # stream + final stream


def test_throttle_drops_close_partials():
    rec = _Recorder()
    transport = httpx.MockTransport(rec.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            mgr = DingtalkTokenManager(client=client)
            d = ReplyDispatcher(
                http_client=client,
                msg=_msg(),
                token_mgr=mgr,
                client_id="x", client_secret="y",
                throttle_seconds=0.5,
            )
            await d.on_start()
            # Three rapid chunks within the throttle window → at most one
            # streaming PUT before on_final.
            await d.on_partial("a")
            await d.on_partial("b")
            await d.on_partial("c")
            await d.on_final()

    _run(run())

    stream_calls = [c for c in rec.calls if c["path"] == "/v1.0/card/streaming"]
    # 0 mid-stream PUTs (all coalesced) + 1 final-stream (from finish path)
    # OR 1 mid-stream + 1 final, both ≤ 2.
    assert 1 <= len(stream_calls) <= 2


# ── fallback path: card create fails → text webhook ────────────────────────


def test_card_create_failure_falls_back_to_text_webhook():
    rec = _Recorder(card_create_succeeds=False)
    transport = httpx.MockTransport(rec.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            mgr = DingtalkTokenManager(client=client)
            d = ReplyDispatcher(
                http_client=client,
                msg=_msg(),
                token_mgr=mgr,
                client_id="x", client_secret="y",
                throttle_seconds=0.0,
            )
            await d.on_start()
            await d.on_partial("only reply")
            await d.on_final()

    _run(run())

    # No /streaming or /deliver because card never came up.
    paths = [c["path"] for c in rec.calls]
    assert "/v1.0/card/streaming" not in paths

    # Reply landed on the sessionWebhook URL instead.
    webhook_hits = [
        c for c in rec.calls
        if c["url"].startswith("https://oapi.dingtalk.com/robot/send")
    ]
    assert webhook_hits, f"expected webhook fallback; calls={paths}"
    assert webhook_hits[0]["body"]["msgtype"] == "text"
    assert webhook_hits[0]["body"]["text"]["content"] == "only reply"


def test_empty_final_skips_send_entirely():
    rec = _Recorder()
    transport = httpx.MockTransport(rec.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            mgr = DingtalkTokenManager(client=client)
            d = ReplyDispatcher(
                http_client=client,
                msg=_msg(),
                token_mgr=mgr,
                client_id="x", client_secret="y",
            )
            await d.on_start()
            # No on_partial at all
            await d.on_final()

    _run(run())

    # Card was created (on_start ran) but no finalize/stream PUT for content.
    stream_calls = [c for c in rec.calls if c["path"] == "/v1.0/card/streaming"]
    assert stream_calls == []


def test_on_error_reports_through_card_or_webhook():
    rec = _Recorder(card_create_succeeds=False)
    transport = httpx.MockTransport(rec.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            mgr = DingtalkTokenManager(client=client)
            d = ReplyDispatcher(
                http_client=client,
                msg=_msg(),
                token_mgr=mgr,
                client_id="x", client_secret="y",
            )
            await d.on_start()
            await d.on_error("boom!")

    _run(run())

    # Card failed → error message went through the webhook fallback.
    webhook_hits = [
        c for c in rec.calls
        if c["url"].startswith("https://oapi.dingtalk.com/robot/send")
    ]
    assert webhook_hits
    assert "boom!" in webhook_hits[0]["body"]["text"]["content"]


def test_group_message_uses_group_target():
    rec = _Recorder()
    transport = httpx.MockTransport(rec.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            mgr = DingtalkTokenManager(client=client)
            d = ReplyDispatcher(
                http_client=client,
                msg=_msg(group=True),
                token_mgr=mgr,
                client_id="x", client_secret="y",
            )
            await d.on_start()

    _run(run())

    deliver_call = next(c for c in rec.calls if c["path"] == "/v1.0/card/instances/deliver")
    assert deliver_call["body"]["openSpaceId"].startswith("dtv1.card//IM_GROUP.")
