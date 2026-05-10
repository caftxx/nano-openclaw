"""Async QR-code login state machine drives wait → scaned → confirmed correctly.

Uses ``httpx.MockTransport`` to fake the iLink server so we can script the
sequence of get_qrcode_status responses and assert the right ``LoginResult``
falls out — including endpoint URLs, callback firing, and the post-confirm
``base_url`` override.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nano_openclaw.wechat.ilink import (
    LoginCallbacks,
    fetch_qr_code,
    login_with_qr,
    poll_qr_status,
)


class _Server:
    """Minimal scripted iLink server for QR tests."""

    def __init__(self, *, status_seq: list[dict[str, Any]], qr_payloads: list[dict[str, Any]] | None = None) -> None:
        self.status_seq = list(status_seq)
        self.qr_payloads = list(qr_payloads or [{"qrcode": "QR1", "qrcode_img_content": "https://q/1"}])
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/get_bot_qrcode"):
            payload = self.qr_payloads.pop(0) if self.qr_payloads else {"qrcode": "QR0", "qrcode_img_content": "u"}
            return httpx.Response(200, json=payload)
        if path.endswith("/get_qrcode_status"):
            payload = self.status_seq.pop(0) if self.status_seq else {"status": "wait"}
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": f"unmocked path: {path}"})


def test_fetch_qr_code_endpoint_and_payload():
    server = _Server(status_seq=[])
    transport = httpx.MockTransport(server.handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_qr_code(client, "https://ilink.example", bot_type="3")

    qr = asyncio.run(run())
    assert qr["qrcode"] == "QR1"
    assert qr["qrcode_img_content"] == "https://q/1"
    assert server.calls == ["/ilink/bot/get_bot_qrcode"]


def test_login_confirmed_path_returns_token_and_base_url(monkeypatch):
    server = _Server(status_seq=[
        {"status": "wait"},
        {"status": "scaned"},
        {
            "status": "confirmed",
            "bot_token": "TOKEN_XYZ",
            "ilink_bot_id": "bot_42",
            "baseurl": "https://shard-7.ilink.example",
            "ilink_user_id": "uid_99",
        },
    ])
    transport = httpx.MockTransport(server.handler)

    # Speed up the inter-poll sleep so the test doesn't sit on ~3s of real time.
    import nano_openclaw.wechat.ilink as ilink_mod
    real_sleep = asyncio.sleep
    async def fast_sleep(d: float):
        await real_sleep(0)
    monkeypatch.setattr(ilink_mod.asyncio, "sleep", fast_sleep)

    fired = {"qr": [], "scanned": 0}
    callbacks = LoginCallbacks(
        on_qrcode=lambda content: fired["qr"].append(content),
        on_scanned=lambda: fired.update(scanned=fired["scanned"] + 1),
    )

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await login_with_qr(client, "https://ilink.example", callbacks, timeout=30)

    result = asyncio.run(run())
    assert result.connected is True
    assert result.bot_token == "TOKEN_XYZ"
    assert result.bot_id == "bot_42"
    assert result.base_url == "https://shard-7.ilink.example"
    assert result.user_id == "uid_99"
    # on_qrcode fires once at start; on_scanned fires once on first 'scaned'.
    assert fired["qr"] == ["https://q/1"]
    assert fired["scanned"] == 1


def test_login_expired_refreshes_then_confirms(monkeypatch):
    server = _Server(
        status_seq=[
            {"status": "expired"},
            {"status": "wait"},
            {
                "status": "confirmed",
                "bot_token": "TOK2",
                "ilink_bot_id": "b",
                "baseurl": "",
                "ilink_user_id": "u",
            },
        ],
        qr_payloads=[
            {"qrcode": "QR1", "qrcode_img_content": "u1"},
            {"qrcode": "QR2", "qrcode_img_content": "u2"},
        ],
    )
    transport = httpx.MockTransport(server.handler)

    import nano_openclaw.wechat.ilink as ilink_mod
    real_sleep = asyncio.sleep
    async def fast_sleep(d: float):
        await real_sleep(0)
    monkeypatch.setattr(ilink_mod.asyncio, "sleep", fast_sleep)

    expired_calls = []
    cb = LoginCallbacks(
        on_expired=lambda n, mx: expired_calls.append((n, mx)),
    )

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await login_with_qr(client, "https://ilink.example", cb, timeout=30)

    result = asyncio.run(run())
    assert result.connected is True
    assert result.bot_token == "TOK2"
    assert expired_calls == [(2, 3)]
    # Two qrcode fetches (initial + 1 refresh) + 3 status polls.
    assert server.calls.count("/ilink/bot/get_bot_qrcode") == 2


def test_poll_qr_status_swallows_network_error():
    """Long-poll timeouts must not abort the login loop."""

    class _BoomTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ReadTimeout("timeout", request=request)

    async def run():
        async with httpx.AsyncClient(transport=_BoomTransport()) as client:
            return await poll_qr_status(client, "https://ilink.example", "QR")

    resp = asyncio.run(run())
    assert resp == {"status": "wait"}
