"""Per-``clientId`` access_token cache for DingTalk new-style APIs.

The cache lives only inside a ``DingtalkTokenManager`` instance (not in
``os.environ``) so sub-agent child processes can't accidentally inherit
``clientSecret`` or a live token via the environment.

Refresh policy: a cached entry is considered valid while ``now + 60 < expiry``
— that 60s padding makes sure we don't hand out a token that's about to die.
On HTTP 401 the caller should ``invalidate(client_id)`` to force a refresh on
the next call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from nano_openclaw.logger import get_logger


log = get_logger(__name__)


ACCESS_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
EXPIRY_PADDING_SECONDS = 60


class DingtalkTokenManager:
    """Bucketed access_token cache keyed by ``clientId``.

    Multiple accounts running in the same daemon must not share token state —
    each ``clientId`` gets its own slot. The lock is global rather than
    per-key for simplicity; token refresh is rare enough that the contention
    doesn't matter.
    """

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None) -> None:
        # clientId → (token, expiry_epoch_seconds)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._client = client

    async def get_access_token(self, client_id: str, client_secret: str) -> str:
        """Return a cached or freshly-fetched access_token for ``client_id``.

        Double-checked locking so concurrent callers for the same ``client_id``
        share one refresh round-trip.
        """
        now = time.time()
        cached = self._cache.get(client_id)
        if cached is not None and now + EXPIRY_PADDING_SECONDS < cached[1]:
            return cached[0]

        async with self._lock:
            cached = self._cache.get(client_id)
            now = time.time()
            if cached is not None and now + EXPIRY_PADDING_SECONDS < cached[1]:
                return cached[0]

            token, expire_in = await self._fetch_token(client_id, client_secret)
            expiry = time.time() + float(expire_in)
            self._cache[client_id] = (token, expiry)
            log.info(
                "dingtalk.token.refreshed",
                f"client_id={client_id[:8]}… expire_in={expire_in}s",
            )
            return token

    def invalidate(self, client_id: str) -> None:
        """Drop the cached token; next ``get_access_token`` will refetch.

        Call this when an API responds 401 — a previously-cached token may
        have been revoked server-side ahead of its nominal expiry.
        """
        self._cache.pop(client_id, None)

    async def _fetch_token(
        self,
        client_id: str,
        client_secret: str,
    ) -> tuple[str, int]:
        """POST to ``/v1.0/oauth2/accessToken``. Returns ``(token, expireIn_seconds)``."""
        payload = {"appKey": client_id, "appSecret": client_secret}
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.post(ACCESS_TOKEN_URL, json=payload)
            response.raise_for_status()
            body = response.json()
        finally:
            if self._client is None:
                await client.aclose()

        token = str(body.get("accessToken") or "")
        if not token:
            raise RuntimeError(
                f"dingtalk access_token response missing 'accessToken' field: {body!r}"
            )
        expire_in = int(body.get("expireIn") or 7200)
        return token, expire_in
