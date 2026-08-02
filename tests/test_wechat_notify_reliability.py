"""Reliability checks for proactive WeChat notification delivery."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from nano_openclaw.adapters.channels.chunking import chunk_text
from nano_openclaw.wechat import bot as bot_module
from nano_openclaw.wechat.bot import WechatBot
from nano_openclaw.wechat.ilink import _headers, send_text
from nano_openclaw.wechat.notify import NotifyItem, NotifyQueue


def _item(*, text: str = "daily digest") -> NotifyItem:
    return NotifyItem(
        job_id="job-1",
        job_name="daily",
        status="ok",
        result_summary=text,
        created_at="2026-08-02T09:00:00+08:00",
        target_uid="user-1",
    )


def _cancel_after_first_poll(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_sleep(_delay: float) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)


def test_ilink_headers_include_fresh_wechat_uin(monkeypatch):
    monkeypatch.setattr("nano_openclaw.wechat.ilink.secrets.randbits", lambda _bits: 123456)

    headers = _headers("token", b"{}")

    assert base64.b64decode(headers["X-WECHAT-UIN"]).decode("ascii") == "123456"
    assert headers["Authorization"] == "Bearer token"


def test_send_text_rejects_http_200_business_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ret": -1, "errcode": -1, "errmsg": "context token required"},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await send_text(client, "https://example", "token", "user-1", "hello")

    with pytest.raises(RuntimeError, match="context token required"):
        asyncio.run(run())


def test_context_token_persists_and_wakes_deferred_notification(tmp_path):
    queue = NotifyQueue(tmp_path / "notify.jsonl")
    queue.append(_item())
    queue.mark_failed("job-1", _item().created_at, "no context", retry_delay=3600)
    assert queue.get_pending(now=0) == []

    token_path = tmp_path / "context-tokens.json"
    bot = WechatBot(
        base_url="https://example",
        token="token",
        notify_queue=queue,
        context_token_path=token_path,
        account_id="work",
    )
    bot._remember_context_token("user-1", "ctx-new")

    assert json.loads(token_path.read_text(encoding="utf-8")) == {"user-1": "ctx-new"}
    assert queue.get_pending(now=0)[0].target_uid == "user-1"

    restarted = WechatBot(
        base_url="https://example",
        token="token",
        context_token_path=token_path,
        account_id="work",
    )
    assert restarted._get_context_token("user-1") == "ctx-new"


def test_missing_context_keeps_notification_pending(tmp_path, monkeypatch):
    queue = NotifyQueue(tmp_path / "notify.jsonl")
    queue.append(_item())
    sends: list[str] = []

    async def fake_send_text(*_args, **_kwargs):
        sends.append("called")

    monkeypatch.setattr(bot_module, "send_text", fake_send_text)
    _cancel_after_first_poll(monkeypatch)
    bot = WechatBot(
        base_url="https://example",
        token="token",
        notify_queue=queue,
        context_token_path=tmp_path / "missing.json",
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._poll_notifications())

    row = json.loads(queue.path.read_text(encoding="utf-8"))
    assert sends == []
    assert row["sent"] is False
    assert row["attempts"] == 1
    assert "context_token" in row["last_error"]


def test_notification_retry_resumes_at_failed_chunk(tmp_path, monkeypatch):
    text = "a " * 2500
    chunks = chunk_text(text)
    assert len(chunks) == 2

    queue = NotifyQueue(tmp_path / "notify.jsonl")
    queue.append(_item(text=text))
    token_path = tmp_path / "context-tokens.json"
    first_bot = WechatBot(
        base_url="https://example",
        token="token",
        notify_queue=queue,
        context_token_path=token_path,
        account_id="work",
    )
    first_bot._remember_context_token("user-1", "ctx-1")

    first_attempt: list[tuple[str, str | None, str | None]] = []

    async def fail_second_chunk(
        _client, _base_url, _token, _uid, body, ctx=None, client_id=None,
    ):
        first_attempt.append((body, ctx, client_id))
        if len(first_attempt) == 2:
            raise RuntimeError("temporary send failure")

    monkeypatch.setattr(bot_module, "send_text", fail_second_chunk)
    _cancel_after_first_poll(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(first_bot._poll_notifications())

    failed = json.loads(queue.path.read_text(encoding="utf-8"))
    assert [entry[0] for entry in first_attempt] == chunks
    assert all(entry[1] == "ctx-1" for entry in first_attempt)
    assert failed["sent"] is False
    assert failed["next_chunk_index"] == 1
    assert failed["attempts"] == 1

    queue.retry_now_for_target("user-1")
    second_bot = WechatBot(
        base_url="https://example",
        token="token",
        notify_queue=queue,
        context_token_path=token_path,
        account_id="work",
    )
    second_attempt: list[str] = []

    async def succeed(
        _client, _base_url, _token, _uid, body, ctx=None, client_id=None,
    ):
        assert ctx == "ctx-1"
        assert client_id
        second_attempt.append(body)

    monkeypatch.setattr(bot_module, "send_text", succeed)
    _cancel_after_first_poll(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(second_bot._poll_notifications())

    delivered = json.loads(queue.path.read_text(encoding="utf-8"))
    assert second_attempt == [chunks[1]]
    assert delivered["sent"] is True
    assert delivered["next_chunk_index"] == 2
    assert delivered["last_error"] == ""
