"""``ReplyDispatcher`` — typewriter-effect reply via AI Card with text fallback.

State machine (mirrors the TS connector's ``reply-dispatcher.ts``):

::

    on_start()            → create_ai_card  (target = user | group)
    on_partial(chunk)*    → buffer + throttle 800ms → stream_ai_card
    on_final()            → finish_ai_card    on success
                            send_text_via_webhook on failure / no card

The throttle is purely client-side (per-dispatcher), independent of the
global card-API token bucket. They serve different purposes: the throttle
keeps a single conversation from spamming its own card 50×/second; the
token bucket caps total card-API traffic across all conversations.

If the card never came up (``on_start`` failed), every ``on_partial`` is a
no-op and ``on_final`` falls back to the inbound ``sessionWebhook``. Same
fallback path triggers on any exception during ``finish_ai_card`` so a
partial reply still reaches the user.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from nano_openclaw.dingtalk.ai_card import (
    AICardInstance,
    AICardTarget,
    create_ai_card,
    finish_ai_card,
    stream_ai_card,
)
from nano_openclaw.dingtalk.extract import ExtractedMessage
from nano_openclaw.dingtalk.sender import send_text_via_webhook
from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


THROTTLE_SECONDS = 0.8
"""Min interval between consecutive AI Card streaming updates per dispatcher."""


def _build_target(msg: ExtractedMessage) -> AICardTarget:
    """Pick the right ``AICardTarget`` shape for the inbound message.

    Mirrors the TS connector: groups deliver into the conversation, DMs
    deliver to the sender's staffId. Bots receive ``senderStaffId`` from
    DingTalk for 1:1 cards regardless of whether the user authenticated
    via mobile or unified-app.
    """
    if msg.is_group:
        return AICardTarget(type="group", open_conversation_id=msg.conversation_id)
    return AICardTarget(type="user", user_id=msg.sender_staff_id)


class ReplyDispatcher:
    """Coordinates a single turn's reply across AI Card streaming + fallback.

    One dispatcher per turn — instantiate, ``await dispatcher.on_start()``,
    feed ``on_partial`` from ``TextDelta`` events, then call ``on_final``
    once the agent's ``run_turn`` completes.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        msg: ExtractedMessage,
        token_mgr: DingtalkTokenManager,
        client_id: str,
        client_secret: str,
        robot_code: Optional[str] = None,
        throttle_seconds: float = THROTTLE_SECONDS,
    ) -> None:
        self._client = http_client
        self._msg = msg
        self._token_mgr = token_mgr
        self._client_id = client_id
        self._client_secret = client_secret
        # ``robot_code`` defaults to ``clientId`` — DingTalk often makes them
        # interchangeable, but the message-time ``robotCode`` is more
        # authoritative when present (legacy AppKey/RobotCode split).
        self._robot_code = robot_code or client_id
        self._throttle = throttle_seconds
        self._card: Optional[AICardInstance] = None
        self._buffer = ""
        self._last_emit_at = 0.0
        self._card_failed = False
        self._started = False

    async def on_start(self) -> None:
        """Create + deliver the card. Idempotent."""
        if self._started:
            return
        self._started = True
        target = _build_target(self._msg)
        try:
            self._card = await create_ai_card(
                self._client,
                token_mgr=self._token_mgr,
                client_id=self._client_id,
                client_secret=self._client_secret,
                target=target,
                robot_code=self._robot_code,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dingtalk.reply.create.error",
                f"{type(exc).__name__}: {exc}",
            )
            self._card = None
        if self._card is None:
            self._card_failed = True
            log.info(
                "dingtalk.reply.card.unavailable",
                f"conv={self._msg.conversation_id[:12]}…: falling back to sessionWebhook text",
            )

    async def on_partial(self, chunk: str) -> None:
        """Append ``chunk`` to the buffer and conditionally push to the card.

        Skipped entirely when the card failed to create — we accumulate into
        the buffer regardless so ``on_final`` has full text to fall back on.
        """
        if not chunk:
            return
        self._buffer += chunk
        if self._card_failed or self._card is None:
            return
        now = time.monotonic()
        if now - self._last_emit_at < self._throttle:
            return
        self._last_emit_at = now
        try:
            await stream_ai_card(
                self._client,
                self._card,
                self._buffer,
                token_mgr=self._token_mgr,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
        except Exception as exc:  # noqa: BLE001
            # Partial stream failures aren't fatal — keep buffering and let
            # on_final decide whether to fall back. This matches the TS
            # connector: a transient streaming error doesn't crash the turn.
            log.warning(
                "dingtalk.reply.stream.error",
                f"conv={self._msg.conversation_id[:12]}… {type(exc).__name__}: {exc}",
            )

    async def on_final(self) -> None:
        """Finalize the reply. Card path on success, webhook text otherwise."""
        final_text = self._buffer.strip()
        if not final_text:
            return

        if not self._card_failed and self._card is not None:
            try:
                await finish_ai_card(
                    self._client,
                    self._card,
                    final_text,
                    token_mgr=self._token_mgr,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                )
                return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "dingtalk.reply.finish.error",
                    f"conv={self._msg.conversation_id[:12]}… falling back to webhook: "
                    f"{type(exc).__name__}: {exc}",
                )

        await self._fallback_webhook(final_text)

    async def on_error(self, err_text: str) -> None:
        """Report a turn failure to the user with the best channel available.

        Card path delivers an error-tagged finish; otherwise plain text
        webhook. Either way the user sees *something* — silent failure on
        a long-running tool turn is worse than a noisy one.
        """
        if not err_text:
            err_text = "(internal error)"
        if not self._card_failed and self._card is not None:
            try:
                await finish_ai_card(
                    self._client,
                    self._card,
                    err_text,
                    token_mgr=self._token_mgr,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                )
                return
            except Exception:  # noqa: BLE001
                pass
        await self._fallback_webhook(err_text)

    async def _fallback_webhook(self, text: str) -> None:
        if not self._msg.session_webhook:
            log.warning(
                "dingtalk.reply.no_webhook",
                f"conv={self._msg.conversation_id[:12]}…: no sessionWebhook, dropping reply",
            )
            return
        if (
            self._msg.session_webhook_expire_ms
            and self._msg.session_webhook_expire_ms < time.time() * 1000
        ):
            log.warning(
                "dingtalk.reply.webhook_expired",
                f"conv={self._msg.conversation_id[:12]}…: sessionWebhook expired",
            )
            return
        await send_text_via_webhook(self._client, self._msg.session_webhook, text)
