"""Outbound message senders for DingTalk.

PR2 sends replies through the per-message ``sessionWebhook`` URL that the
server attaches to every inbound callback. That URL is short-lived (a few
hours) and scoped to the originating conversation, but it's the only path
that doesn't need an access_token and that preserves DingTalk's UI
"replied to a message" linkage. Active-message endpoints
(``/v1.0/robot/oToMessages/batchSend``, ``…/groupMessages/send``) land in
PR4 alongside cron notification re-delivery, since those need the
DingtalkTokenManager.

Each payload follows DingTalk's documented msgtype envelopes:
- text:     ``{msgtype: "text", text: {content}}``
- markdown: ``{msgtype: "markdown", markdown: {title, text}}``
- ``at.atUserIds`` is a parallel field for @-mentions.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from nano_openclaw.logger import get_logger


log = get_logger(__name__)

MAX_TEXT_SEGMENT = 1800
"""Soft cap per outbound message. DingTalk's hard limit is around 2000 chars
for text/markdown; we stay under to leave headroom for at-mention tags."""


def _chunk_text(text: str, limit: int = MAX_TEXT_SEGMENT) -> list[str]:
    """Split a long string at line boundaries, then hard-cut if necessary.

    Splitting on ``\\n`` first keeps Markdown code fences readable; only
    when a single line exceeds the limit do we cut mid-line.
    """
    if len(text) <= limit:
        return [text] if text else []
    segments: list[str] = []
    current: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        if cur_len + len(line) + 1 > limit and current:
            segments.append("\n".join(current))
            current = []
            cur_len = 0
        if len(line) > limit:
            # Single line too long — hard-slice.
            for i in range(0, len(line), limit):
                segments.append(line[i : i + limit])
            continue
        current.append(line)
        cur_len += len(line) + 1
    if current:
        segments.append("\n".join(current))
    return segments


async def send_text_via_webhook(
    client: httpx.AsyncClient,
    webhook_url: str,
    content: str,
    *,
    at_user_ids: Optional[list[str]] = None,
) -> None:
    """POST a text reply via the per-message ``sessionWebhook`` URL.

    ``content`` longer than the segment cap is fanned out across multiple
    POSTs so each one stays under DingTalk's hard limit. Long-running
    sessions therefore appear as multiple bubbles, mirroring WeChat's
    chunking behavior.
    """
    if not webhook_url or not content:
        return
    for segment in _chunk_text(content):
        payload: dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": segment},
        }
        if at_user_ids:
            payload["at"] = {"atUserIds": list(at_user_ids), "isAtAll": False}
        try:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dingtalk.send.text.error",
                f"{type(exc).__name__}: {exc}; webhook={webhook_url[:40]}…",
            )
            return


async def send_markdown_via_webhook(
    client: httpx.AsyncClient,
    webhook_url: str,
    title: str,
    text: str,
    *,
    at_user_ids: Optional[list[str]] = None,
) -> None:
    """POST a Markdown reply via the per-message ``sessionWebhook`` URL.

    ``title`` is shown as the bubble subject in the chat list / push
    notification; ``text`` is the rendered body.
    """
    if not webhook_url or not text:
        return
    for segment in _chunk_text(text):
        payload: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": segment},
        }
        if at_user_ids:
            payload["at"] = {"atUserIds": list(at_user_ids), "isAtAll": False}
        try:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dingtalk.send.markdown.error",
                f"{type(exc).__name__}: {exc}; webhook={webhook_url[:40]}…",
            )
            return
