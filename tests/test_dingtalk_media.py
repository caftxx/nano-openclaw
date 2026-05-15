"""``download_media``: two-step exchange downloadCode → URL → bytes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from nano_openclaw.dingtalk.media import (
    DINGTALK_API,
    DOWNLOAD_URL_ENDPOINT,
    download_media,
    fetch_download_url,
)
from nano_openclaw.dingtalk.token import DingtalkTokenManager


class _Recorder:
    def __init__(self, script: dict[str, Any]) -> None:
        self.calls: list[dict] = []
        self.script = script

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.read())
            except (ValueError, UnicodeDecodeError):
                body = None
        self.calls.append({"method": request.method, "path": path, "body": body, "url": str(request.url)})

        if path.endswith("/v1.0/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "tok", "expireIn": 7200})
        if path in self.script:
            return self.script[path](request)
        if str(request.url) in self.script:
            return self.script[str(request.url)](request)
        return httpx.Response(404, json={"err": "unmocked"})


def test_fetch_download_url_returns_presigned_url():
    rec = _Recorder({
        DOWNLOAD_URL_ENDPOINT: lambda r: httpx.Response(
            200, json={"downloadUrl": "https://cdn.example/file?sig=abc"}
        ),
    })

    async def run() -> str | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            return await fetch_download_url(
                client,
                download_code="dl-1",
                robot_code="bot-x",
                token_mgr=mgr,
                client_id="ding-a", client_secret="sec",
            )

    url = asyncio.run(run())
    assert url == "https://cdn.example/file?sig=abc"

    download_call = next(c for c in rec.calls if c["path"] == DOWNLOAD_URL_ENDPOINT)
    assert download_call["body"] == {"downloadCode": "dl-1", "robotCode": "bot-x"}


def test_fetch_download_url_handles_missing_field():
    rec = _Recorder({
        DOWNLOAD_URL_ENDPOINT: lambda r: httpx.Response(200, json={"errcode": -1}),
    })

    async def run() -> str | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            return await fetch_download_url(
                client,
                download_code="dl", robot_code="b",
                token_mgr=mgr,
                client_id="a", client_secret="b",
            )

    assert asyncio.run(run()) is None


def test_download_media_round_trip():
    DOWNLOAD_URL = "https://cdn.example/file?sig=zz"
    payload = b"\x89PNG\r\n\x1a\nfakebytes"

    rec = _Recorder({
        DOWNLOAD_URL_ENDPOINT: lambda r: httpx.Response(200, json={"downloadUrl": DOWNLOAD_URL}),
        DOWNLOAD_URL: lambda r: httpx.Response(200, content=payload),
    })

    async def run() -> bytes | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            return await download_media(
                client,
                download_code="dl-2",
                robot_code="bot-x",
                token_mgr=mgr,
                client_id="a", client_secret="b",
            )

    got = asyncio.run(run())
    assert got == payload


def test_download_media_returns_none_when_url_missing():
    rec = _Recorder({
        DOWNLOAD_URL_ENDPOINT: lambda r: httpx.Response(200, json={}),
    })

    async def run() -> bytes | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)) as client:
            mgr = DingtalkTokenManager(client=client)
            return await download_media(
                client,
                download_code="dl", robot_code="b",
                token_mgr=mgr,
                client_id="a", client_secret="b",
            )

    assert asyncio.run(run()) is None
