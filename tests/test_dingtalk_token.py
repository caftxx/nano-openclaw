"""DingtalkTokenManager: clientId-bucketed cache, TTL refresh, invalidate."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from nano_openclaw.dingtalk.token import (
    ACCESS_TOKEN_URL,
    EXPIRY_PADDING_SECONDS,
    DingtalkTokenManager,
)


class _MockServer:
    def __init__(self, *, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(ACCESS_TOKEN_URL)
        body = request.read()
        import json as _json
        self.calls.append(_json.loads(body))
        if self.responses:
            payload = self.responses.pop(0)
        else:
            payload = {"accessToken": "tok-x", "expireIn": 7200}
        return httpx.Response(200, json=payload)


def _client_for(server: _MockServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server.handler))


def test_cache_hit_skips_http_on_second_call():
    server = _MockServer(
        responses=[
            {"accessToken": "tok-aaa", "expireIn": 7200},
            {"accessToken": "tok-bbb", "expireIn": 7200},
        ]
    )

    async def run() -> None:
        async with _client_for(server) as client:
            mgr = DingtalkTokenManager(client=client)
            t1 = await mgr.get_access_token("ding-a", "sec-a")
            t2 = await mgr.get_access_token("ding-a", "sec-a")
            assert t1 == "tok-aaa"
            assert t2 == "tok-aaa", "second call should hit the cache"
            assert len(server.calls) == 1, "expected exactly one /accessToken HTTP call"

    asyncio.run(run())


def test_distinct_client_ids_get_independent_tokens():
    server = _MockServer(
        responses=[
            {"accessToken": "tok-A", "expireIn": 7200},
            {"accessToken": "tok-B", "expireIn": 7200},
        ]
    )

    async def run() -> None:
        async with _client_for(server) as client:
            mgr = DingtalkTokenManager(client=client)
            ta = await mgr.get_access_token("ding-a", "sec-a")
            tb = await mgr.get_access_token("ding-b", "sec-b")
            assert ta == "tok-A"
            assert tb == "tok-B"
            assert len(server.calls) == 2

            # No cross-contamination on re-fetch.
            ta2 = await mgr.get_access_token("ding-a", "sec-a")
            tb2 = await mgr.get_access_token("ding-b", "sec-b")
            assert ta2 == "tok-A"
            assert tb2 == "tok-B"
            assert len(server.calls) == 2, "still cached"

    asyncio.run(run())


def test_expired_entry_triggers_refresh():
    server = _MockServer(
        responses=[
            {"accessToken": "tok-old", "expireIn": 1},   # expires almost immediately
            {"accessToken": "tok-new", "expireIn": 7200},
        ]
    )

    async def run() -> None:
        async with _client_for(server) as client:
            mgr = DingtalkTokenManager(client=client)
            t1 = await mgr.get_access_token("ding-a", "sec-a")
            assert t1 == "tok-old"

            # Force the cache entry to look stale (within the 60s padding).
            tok, _ = mgr._cache["ding-a"]
            mgr._cache["ding-a"] = (tok, time.time() + EXPIRY_PADDING_SECONDS - 5)

            t2 = await mgr.get_access_token("ding-a", "sec-a")
            assert t2 == "tok-new"
            assert len(server.calls) == 2

    asyncio.run(run())


def test_invalidate_forces_refetch_on_next_call():
    server = _MockServer(
        responses=[
            {"accessToken": "tok-1", "expireIn": 7200},
            {"accessToken": "tok-2", "expireIn": 7200},
        ]
    )

    async def run() -> None:
        async with _client_for(server) as client:
            mgr = DingtalkTokenManager(client=client)
            await mgr.get_access_token("ding-a", "sec-a")
            mgr.invalidate("ding-a")
            t = await mgr.get_access_token("ding-a", "sec-a")
            assert t == "tok-2"
            assert len(server.calls) == 2

    asyncio.run(run())


def test_concurrent_callers_share_one_refresh():
    """Double-checked locking: two coroutines racing for the same uncached
    clientId must end up with one shared token (and one HTTP call)."""
    server = _MockServer(
        responses=[{"accessToken": "tok-once", "expireIn": 7200}]
    )

    async def run() -> None:
        async with _client_for(server) as client:
            mgr = DingtalkTokenManager(client=client)
            t1, t2 = await asyncio.gather(
                mgr.get_access_token("ding-a", "sec-a"),
                mgr.get_access_token("ding-a", "sec-a"),
            )
            assert t1 == t2 == "tok-once"
            assert len(server.calls) == 1

    asyncio.run(run())


def test_missing_token_field_raises():
    server = _MockServer(responses=[{"expireIn": 7200}])  # no accessToken

    async def run() -> None:
        async with _client_for(server) as client:
            mgr = DingtalkTokenManager(client=client)
            with pytest.raises(RuntimeError, match="accessToken"):
                await mgr.get_access_token("ding-a", "sec-a")

    asyncio.run(run())
