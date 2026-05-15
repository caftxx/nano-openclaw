"""AI Card HTTP payload shapes: create/deliver, stream, finish, QpsLimit retry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nano_openclaw.dingtalk import ai_card
from nano_openclaw.dingtalk.ai_card import (
    AI_CARD_TEMPLATE_ID,
    AICardInstance,
    AICardTarget,
    TokenBucket,
    build_deliver_body,
    create_ai_card,
    finish_ai_card,
    is_qps_limit_error,
    stream_ai_card,
)
from nano_openclaw.dingtalk.token import ACCESS_TOKEN_URL, DingtalkTokenManager


# ── Common test scaffolding ────────────────────────────────────────────────


class _Recorder:
    """Capture every HTTP request the bot makes, with scriptable responses.

    ``script`` is a mapping of ``(method, path)`` → callable returning the
    response. Falls back to 200 ok for unmocked paths so the test surface
    stays narrow.
    """

    def __init__(self, script: dict[tuple[str, str], Any] | None = None) -> None:
        self.calls: list[dict] = []
        self.script = script or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = None
        if request.content:
            try:
                body = json.loads(request.read())
            except (ValueError, UnicodeDecodeError):
                body = request.read()
        self.calls.append({"method": method, "path": path, "body": body})

        key = (method, path)
        if key in self.script:
            return self.script[key](request)

        # token endpoint default
        if path.endswith("/v1.0/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "tok-abc", "expireIn": 7200})
        return httpx.Response(200, json={"errcode": 0})


@pytest.fixture(autouse=True)
def _reset_bucket():
    """Make the global bucket fast so card tests don't burn 1s per stream."""
    ai_card.reset_global_bucket(max_qps=1000, backoff=0.01)
    yield
    ai_card.reset_global_bucket()


def _setup(script=None) -> tuple[_Recorder, httpx.AsyncClient, DingtalkTokenManager]:
    rec = _Recorder(script)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    mgr = DingtalkTokenManager(client=client)
    return rec, client, mgr


# ── build_deliver_body ─────────────────────────────────────────────────────


def test_build_deliver_body_for_user():
    body = build_deliver_body("card-1", AICardTarget(type="user", user_id="staff-9"), "ding-bot")
    assert body["openSpaceId"] == "dtv1.card//IM_ROBOT.staff-9"
    assert body["imRobotOpenDeliverModel"]["robotCode"] == "ding-bot"
    assert "imGroupOpenDeliverModel" not in body


def test_build_deliver_body_for_group():
    body = build_deliver_body(
        "card-1",
        AICardTarget(type="group", open_conversation_id="grp-7"),
        "ding-bot",
    )
    assert body["openSpaceId"] == "dtv1.card//IM_GROUP.grp-7"
    assert body["imGroupOpenDeliverModel"]["robotCode"] == "ding-bot"


def test_build_deliver_body_rejects_missing_target_id():
    with pytest.raises(ValueError):
        build_deliver_body("c", AICardTarget(type="group"), "r")
    with pytest.raises(ValueError):
        build_deliver_body("c", AICardTarget(type="user"), "r")


# ── create_ai_card ─────────────────────────────────────────────────────────


def test_create_ai_card_posts_create_then_deliver():
    rec, client, mgr = _setup()

    async def run():
        async with client:
            card = await create_ai_card(
                client,
                token_mgr=mgr,
                client_id="ding-a",
                client_secret="sec",
                target=AICardTarget(type="user", user_id="u-1"),
                robot_code="bot-1",
            )
            assert card is not None
            assert card.card_instance_id.startswith("card_")

    asyncio.run(run())

    paths = [c["path"] for c in rec.calls]
    assert "/v1.0/card/instances" in paths
    assert "/v1.0/card/instances/deliver" in paths

    create_call = next(c for c in rec.calls if c["path"] == "/v1.0/card/instances")
    assert create_call["body"]["cardTemplateId"] == AI_CARD_TEMPLATE_ID
    assert create_call["body"]["callbackType"] == "STREAM"


def test_create_ai_card_returns_none_on_create_failure():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "boom"})

    rec, client, mgr = _setup({("POST", "/v1.0/card/instances"): fail})

    async def run():
        async with client:
            card = await create_ai_card(
                client,
                token_mgr=mgr,
                client_id="x",
                client_secret="y",
                target=AICardTarget(type="user", user_id="u"),
                robot_code="b",
            )
            assert card is None

    asyncio.run(run())


# ── stream_ai_card ─────────────────────────────────────────────────────────


def test_stream_ai_card_emits_inputing_then_streaming():
    rec, client, mgr = _setup()

    async def run():
        async with client:
            card = AICardInstance(card_instance_id="card-1")
            await stream_ai_card(
                client, card, "hello",
                token_mgr=mgr, client_id="x", client_secret="y",
            )
            # Second call must NOT re-emit INPUTING — the flag is sticky.
            await stream_ai_card(
                client, card, "hello world",
                token_mgr=mgr, client_id="x", client_secret="y",
            )

    asyncio.run(run())

    paths = [c["path"] for c in rec.calls if c["path"].startswith("/v1.0/card/")]
    # PUT /instances (INPUTING) once, PUT /streaming twice
    instance_puts = [c for c in rec.calls if c["path"] == "/v1.0/card/instances" and c["method"] == "PUT"]
    stream_puts = [c for c in rec.calls if c["path"] == "/v1.0/card/streaming"]
    assert len(instance_puts) == 1
    assert len(stream_puts) == 2

    stream_body = stream_puts[1]["body"]
    assert stream_body["key"] == "msgContent"
    assert stream_body["content"] == "hello world"
    assert stream_body["isFinalize"] is False


def test_stream_ai_card_retries_once_on_qps_limit():
    bucket = TokenBucket(max_qps=1000, backoff=0.01)

    # First /streaming call → 403 QpsLimit; second succeeds.
    calls = {"n": 0}

    def streaming(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, json={"code": "QpsLimit.RateLimit", "msg": "qps"})
        return httpx.Response(200, json={"errcode": 0})

    rec, client, mgr = _setup({("PUT", "/v1.0/card/streaming"): streaming})

    async def run():
        async with client:
            card = AICardInstance(card_instance_id="c-1")
            await stream_ai_card(
                client, card, "x",
                token_mgr=mgr, client_id="x", client_secret="y",
                bucket=bucket,
            )

    asyncio.run(run())
    assert calls["n"] == 2


# ── finish_ai_card ─────────────────────────────────────────────────────────


def test_finish_ai_card_emits_finalize_then_finished_status():
    rec, client, mgr = _setup()

    async def run():
        async with client:
            card = AICardInstance(card_instance_id="c-1")
            await finish_ai_card(
                client, card, "final",
                token_mgr=mgr, client_id="x", client_secret="y",
            )

    asyncio.run(run())

    stream_puts = [c for c in rec.calls if c["path"] == "/v1.0/card/streaming"]
    instance_puts = [c for c in rec.calls if c["path"] == "/v1.0/card/instances" and c["method"] == "PUT"]
    assert any(p["body"]["isFinalize"] is True for p in stream_puts)
    # Should have a FINISHED-status update at the end (flowStatus = "3").
    finished = [p for p in instance_puts if
                p["body"]["cardData"]["cardParamMap"]["flowStatus"] == "3"]
    assert finished, "expected a FINISHED state PUT"


# ── is_qps_limit_error ─────────────────────────────────────────────────────


def test_is_qps_limit_error_truthy_only_on_403_plus_qps_code():
    resp_403_qps = httpx.Response(403, json={"code": "QpsLimit.RateLimit"})
    resp_403_other = httpx.Response(403, json={"code": "Unauthorized"})
    resp_500 = httpx.Response(500, json={"code": "QpsLimit.X"})

    err1 = httpx.HTTPStatusError("x", request=httpx.Request("GET", "/"), response=resp_403_qps)
    err2 = httpx.HTTPStatusError("x", request=httpx.Request("GET", "/"), response=resp_403_other)
    err3 = httpx.HTTPStatusError("x", request=httpx.Request("GET", "/"), response=resp_500)

    assert is_qps_limit_error(err1) is True
    assert is_qps_limit_error(err2) is False
    assert is_qps_limit_error(err3) is False
    assert is_qps_limit_error(ValueError("not http")) is False
