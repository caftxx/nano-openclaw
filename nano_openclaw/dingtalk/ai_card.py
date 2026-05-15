"""DingTalk AI Card — three-stage streaming reply (create → stream → finish).

Ported from the TS connector's ``services/messaging/card.ts`` with the same
shape so the API payloads stay byte-compatible (template id, ``msgContent``
field name, ``flowStatus`` numeric codes). The only Python-side liberty is
shared state: the global rate limiter is one ``asyncio.Lock``-protected
token bucket living at module scope, equivalent to the TS one's
``_queueTail`` serialization.

Why a shared (not per-account) limiter: DingTalk's QPS limit applies
**per-app** but with multiple co-resident channels we still want to keep
total card-API traffic predictable. Using one bucket process-wide is the
safe default; if individual accounts ever need higher headroom we can
key the bucket by ``clientId`` later.

Error handling: ``put_with_backoff`` retries exactly once on ``403 QpsLimit``;
beyond that the caller's reply-dispatcher decides whether to drop the
update or fall back to a plain webhook text message.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx

from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


DINGTALK_API = "https://api.dingtalk.com"
AI_CARD_TEMPLATE_ID = "02fcf2f4-5e02-4a85-b672-46d1f715543e.schema"
CARD_API_MAX_QPS = 20
QPS_BACKOFF_SECONDS = 2.0

# AICardStatus enum mirrors the TS connector — DingTalk's docs are sparse so
# we keep the strings rather than rename them.
AI_CARD_STATUS_INPUTING = "2"
AI_CARD_STATUS_FINISHED = "3"


# ── Global token bucket (shared across all reply dispatchers) ─────────────


class TokenBucket:
    """Async-safe leaky-bucket-style limiter.

    The bucket refills continuously at ``max_qps`` tokens/second up to a
    ``max_qps`` cap. ``acquire`` blocks under contention (a lock keeps
    concurrent callers from each "winning" a fractional token simultaneously,
    which would let actual QPS drift above the configured cap).

    ``trigger_backoff`` is called when DingTalk responds 403 QpsLimit —
    drops all tokens and pauses the bucket for ``backoff`` seconds.
    """

    def __init__(
        self,
        max_qps: float = CARD_API_MAX_QPS,
        backoff: float = QPS_BACKOFF_SECONDS,
    ) -> None:
        self._max_qps = float(max_qps)
        self._backoff = backoff
        self._tokens = float(max_qps)
        self._last_refill = time.monotonic()
        self._backoff_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Acquire one token. Returns the milliseconds slept (for telemetry)."""
        async with self._lock:
            slept = 0.0
            now = time.monotonic()
            if now < self._backoff_until:
                wait = self._backoff_until - now
                await asyncio.sleep(wait)
                slept += wait
            self._refill()
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._max_qps
                await asyncio.sleep(wait)
                slept += wait
                self._refill()
            self._tokens -= 1.0
            return slept * 1000.0

    def trigger_backoff(self) -> None:
        """Mark the bucket as paused for ``self._backoff`` seconds."""
        now = time.monotonic()
        self._backoff_until = now + self._backoff
        self._tokens = 0.0
        self._last_refill = self._backoff_until

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._max_qps, self._tokens + elapsed * self._max_qps)
            self._last_refill = now


# Module-level singleton; reset via `reset_global_bucket` in tests.
_global_bucket = TokenBucket()


def get_global_bucket() -> TokenBucket:
    return _global_bucket


def reset_global_bucket(*, max_qps: float = CARD_API_MAX_QPS, backoff: float = QPS_BACKOFF_SECONDS) -> None:
    """Test hook: replace the global bucket with a fresh one."""
    global _global_bucket
    _global_bucket = TokenBucket(max_qps=max_qps, backoff=backoff)


# ── Data types ─────────────────────────────────────────────────────────────


@dataclass
class AICardTarget:
    """Where to deliver a card: 1:1 to a user, or into a group conversation."""

    type: Literal["user", "group"]
    user_id: Optional[str] = None
    open_conversation_id: Optional[str] = None


@dataclass
class AICardInstance:
    """One in-flight card; mutated by ``stream_ai_card`` as state advances."""

    card_instance_id: str
    inputing_started: bool = False
    cumulative_content: str = ""


# ── QPS-limit detection ────────────────────────────────────────────────────


def is_qps_limit_error(exc: BaseException) -> bool:
    """True if ``exc`` is DingTalk's 403 + ``code`` containing ``QpsLimit``."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    resp = exc.response
    if resp.status_code != 403:
        return False
    try:
        body = resp.json()
    except (ValueError, AttributeError):
        return False
    code = str(body.get("code", "") if isinstance(body, dict) else "")
    return "QpsLimit" in code


# ── HTTP helpers ───────────────────────────────────────────────────────────


async def _put_with_qps_backoff(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    bucket: Optional[TokenBucket] = None,
) -> httpx.Response:
    """PUT with a single QpsLimit retry that respects the bucket's backoff."""
    bucket = bucket or get_global_bucket()
    try:
        resp = await client.put(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp
    except httpx.HTTPStatusError as exc:
        if not is_qps_limit_error(exc):
            raise
        bucket.trigger_backoff()
        log.warning(
            "dingtalk.ai_card.qps_limit",
            f"PUT {url}: hit QpsLimit, backing off {QPS_BACKOFF_SECONDS}s and retrying once",
        )
        await bucket.acquire()
        # Re-randomize ``guid`` if present so the server doesn't dedupe the retry.
        if "guid" in body:
            body = {**body, "guid": f"{int(time.time() * 1000)}_{secrets.token_hex(3)}"}
        resp = await client.put(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "x-acs-dingtalk-access-token": access_token,
        "Content-Type": "application/json",
    }


# ── Card lifecycle ─────────────────────────────────────────────────────────


def build_deliver_body(
    card_instance_id: str,
    target: AICardTarget,
    robot_code: str,
) -> dict[str, Any]:
    """Build the ``/v1.0/card/instances/deliver`` request payload.

    Single source of truth for the openSpaceId formatting — it's templated
    differently for group vs DM and easy to get wrong.
    """
    base: dict[str, Any] = {"outTrackId": card_instance_id, "userIdType": 1}
    if target.type == "group":
        if not target.open_conversation_id:
            raise ValueError("AICardTarget(type='group') requires open_conversation_id")
        return {
            **base,
            "openSpaceId": f"dtv1.card//IM_GROUP.{target.open_conversation_id}",
            "imGroupOpenDeliverModel": {"robotCode": robot_code},
        }
    if not target.user_id:
        raise ValueError("AICardTarget(type='user') requires user_id")
    return {
        **base,
        "openSpaceId": f"dtv1.card//IM_ROBOT.{target.user_id}",
        "imRobotOpenDeliverModel": {
            "spaceType": "IM_ROBOT",
            "robotCode": robot_code,
            "extension": {"dynamicSummary": "true"},
        },
    }


async def create_ai_card(
    client: httpx.AsyncClient,
    *,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
    target: AICardTarget,
    robot_code: str,
) -> Optional[AICardInstance]:
    """Create a fresh AI Card and deliver it to ``target``.

    Returns ``None`` if either the create or the deliver step fails — the
    caller (reply dispatcher) treats that as "AI Card unavailable, fall back
    to plain text webhook reply".
    """
    token = await token_mgr.get_access_token(client_id, client_secret)
    card_id = f"card_{int(time.time() * 1000)}_{secrets.token_hex(4)}"

    create_body: dict[str, Any] = {
        "cardTemplateId": AI_CARD_TEMPLATE_ID,
        "outTrackId": card_id,
        "cardData": {
            "cardParamMap": {"config": json.dumps({"autoLayout": True})},
        },
        "callbackType": "STREAM",
        "imGroupOpenSpaceModel": {"supportForward": True},
        "imRobotOpenSpaceModel": {"supportForward": True},
    }
    headers = _auth_headers(token)
    try:
        resp = await client.post(
            f"{DINGTALK_API}/v1.0/card/instances",
            json=create_body,
            headers=headers,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dingtalk.ai_card.create.failed",
            f"target={target.type} {type(exc).__name__}: {exc}",
        )
        return None

    deliver_body = build_deliver_body(card_id, target, robot_code)
    try:
        resp = await client.post(
            f"{DINGTALK_API}/v1.0/card/instances/deliver",
            json=deliver_body,
            headers=headers,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dingtalk.ai_card.deliver.failed",
            f"card={card_id} {type(exc).__name__}: {exc}",
        )
        return None

    return AICardInstance(card_instance_id=card_id)


async def stream_ai_card(
    client: httpx.AsyncClient,
    card: AICardInstance,
    content: str,
    *,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
    finished: bool = False,
    bucket: Optional[TokenBucket] = None,
) -> None:
    """Push an incremental update onto an existing card.

    On the first call we additionally flip the card into ``INPUTING`` so the
    DingTalk UI shows the typing indicator; subsequent calls just send the
    growing ``msgContent`` body. ``finished=True`` sets the ``isFinalize``
    flag on the streaming envelope but does **not** transition to the
    ``FINISHED`` state — call ``finish_ai_card`` for that.
    """
    token = await token_mgr.get_access_token(client_id, client_secret)
    headers = _auth_headers(token)
    bucket = bucket or get_global_bucket()

    if not card.inputing_started:
        await bucket.acquire()
        status_body: dict[str, Any] = {
            "outTrackId": card.card_instance_id,
            "cardData": {
                "cardParamMap": {
                    "flowStatus": AI_CARD_STATUS_INPUTING,
                    "msgContent": content,
                    "staticMsgContent": "",
                    "sys_full_json_obj": json.dumps({"order": ["msgContent"]}),
                    "config": json.dumps({"autoLayout": True}),
                }
            },
        }
        await _put_with_qps_backoff(
            client,
            f"{DINGTALK_API}/v1.0/card/instances",
            status_body,
            headers,
            bucket=bucket,
        )
        card.inputing_started = True

    body: dict[str, Any] = {
        "outTrackId": card.card_instance_id,
        "guid": f"{int(time.time() * 1000)}_{secrets.token_hex(3)}",
        "key": "msgContent",
        "content": content,
        "isFull": True,
        "isFinalize": finished,
        "isError": False,
    }
    await bucket.acquire()
    await _put_with_qps_backoff(
        client,
        f"{DINGTALK_API}/v1.0/card/streaming",
        body,
        headers,
        bucket=bucket,
    )
    card.cumulative_content = content


async def finish_ai_card(
    client: httpx.AsyncClient,
    card: AICardInstance,
    content: str,
    *,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
    bucket: Optional[TokenBucket] = None,
) -> None:
    """Mark a card as ``FINISHED`` with its final content.

    Wraps two calls: a last streaming update (with ``isFinalize=true``) and
    a status PUT that flips ``flowStatus`` to ``FINISHED``. The DingTalk UI
    transitions out of the typing/streaming state only after the second
    call lands.
    """
    bucket = bucket or get_global_bucket()
    await stream_ai_card(
        client,
        card,
        content,
        token_mgr=token_mgr,
        client_id=client_id,
        client_secret=client_secret,
        finished=True,
        bucket=bucket,
    )

    token = await token_mgr.get_access_token(client_id, client_secret)
    headers = _auth_headers(token)
    finish_body: dict[str, Any] = {
        "outTrackId": card.card_instance_id,
        "cardData": {
            "cardParamMap": {
                "flowStatus": AI_CARD_STATUS_FINISHED,
                "msgContent": content,
                "staticMsgContent": "",
                "sys_full_json_obj": json.dumps({"order": ["msgContent"]}),
                "config": json.dumps({"autoLayout": True}),
            }
        },
        "cardUpdateOptions": {"updateCardDataByKey": True},
    }
    await bucket.acquire()
    await _put_with_qps_backoff(
        client,
        f"{DINGTALK_API}/v1.0/card/instances",
        finish_body,
        headers,
        bucket=bucket,
    )
