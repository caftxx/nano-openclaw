"""Proactive (cron-completion) send: DM + group payload shapes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from nano_openclaw.dingtalk.sender import (
    PROACTIVE_DM_ENDPOINT,
    PROACTIVE_GROUP_ENDPOINT,
    send_proactive_to_group,
    send_proactive_to_user,
)
from nano_openclaw.dingtalk.token import DingtalkTokenManager


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.read())
            except (ValueError, UnicodeDecodeError):
                body = None
        self.calls.append({"method": request.method, "path": path, "body": body, "headers": dict(request.headers)})

        if path.endswith("/v1.0/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "tok-zzz", "expireIn": 7200})
        return httpx.Response(200, json={"errcode": 0})


def test_dm_proactive_send_payload_shape():
    rec = _Recorder()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            await send_proactive_to_user(
                client,
                token_mgr=mgr,
                client_id="ding-app",
                client_secret="sec",
                user_id="staff-7",
                text="ping",
            )

    asyncio.run(run())

    call = next(c for c in rec.calls if c["path"] == PROACTIVE_DM_ENDPOINT)
    assert call["body"]["robotCode"] == "ding-app"
    assert call["body"]["userIds"] == ["staff-7"]
    assert call["body"]["msgKey"] == "sampleText"
    # msgParam is a JSON-encoded string per DingTalk's contract.
    inner = json.loads(call["body"]["msgParam"])
    assert inner == {"content": "ping"}
    # Auth header carries the token from /oauth2/accessToken.
    assert call["headers"]["x-acs-dingtalk-access-token"] == "tok-zzz"


def test_group_proactive_send_uses_open_conversation_id():
    rec = _Recorder()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            await send_proactive_to_group(
                client,
                token_mgr=mgr,
                client_id="ding-app",
                client_secret="sec",
                open_conversation_id="grp-9",
                text="hi group",
                markdown=True,
                title="Heads up",
            )

    asyncio.run(run())

    call = next(c for c in rec.calls if c["path"] == PROACTIVE_GROUP_ENDPOINT)
    assert call["body"]["openConversationId"] == "grp-9"
    assert call["body"]["msgKey"] == "sampleMarkdown"
    inner = json.loads(call["body"]["msgParam"])
    assert inner == {"title": "Heads up", "text": "hi group"}


def test_robot_code_override_used_when_provided():
    rec = _Recorder()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            await send_proactive_to_user(
                client,
                token_mgr=mgr,
                client_id="ding-app",
                client_secret="sec",
                user_id="u",
                text="x",
                robot_code="dingtalk-bot-legacy",
            )

    asyncio.run(run())

    call = next(c for c in rec.calls if c["path"] == PROACTIVE_DM_ENDPOINT)
    assert call["body"]["robotCode"] == "dingtalk-bot-legacy"


def test_skips_when_missing_user_id_or_text():
    rec = _Recorder()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            await send_proactive_to_user(
                client, token_mgr=mgr,
                client_id="x", client_secret="y",
                user_id="", text="hi",
            )
            await send_proactive_to_user(
                client, token_mgr=mgr,
                client_id="x", client_secret="y",
                user_id="u", text="",
            )

    asyncio.run(run())
    # No proactive call should have happened — only no-op early returns.
    proactive = [c for c in rec.calls if c["path"] == PROACTIVE_DM_ENDPOINT]
    assert proactive == []
