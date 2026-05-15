"""Outbound message senders for DingTalk.

Two paths, picked by what the caller has on hand:

- **sessionWebhook** (PR2): per-message short-lived URL the server gives us
  on every inbound callback. Works without an access_token, preserves
  DingTalk's "replied to a message" linkage. Used for in-turn replies.

- **Active-message API** (PR4): ``/v1.0/robot/oToMessages/batchSend`` for
  1:1 and ``/v1.0/robot/groupMessages/send`` for groups. Needs an
  access_token from :class:`DingtalkTokenManager`. Used for cron
  completion notifications and any reply that arrives after the
  sessionWebhook has expired.

Each payload follows DingTalk's documented msgtype envelopes:

- text:     ``{msgtype: "text", text: {content}}``
- markdown: ``{msgtype: "markdown", markdown: {title, text}}``

Active-message bodies wrap the same content but encode it as a
``msgKey`` (``sampleText``/``sampleMarkdown``) + JSON-encoded ``msgParam``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger


log = get_logger(__name__)

DINGTALK_API = "https://api.dingtalk.com"
PROACTIVE_DM_ENDPOINT = "/v1.0/robot/oToMessages/batchSend"
PROACTIVE_GROUP_ENDPOINT = "/v1.0/robot/groupMessages/send"

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


# ── Active (proactive) message API ────────────────────────────────────────


async def send_proactive_to_user(
    client: httpx.AsyncClient,
    *,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
    user_id: str,
    text: str,
    title: str = "通知",
    markdown: bool = False,
    robot_code: Optional[str] = None,
) -> None:
    """Send a 1:1 message to ``user_id`` (a DingTalk staffId).

    Used for cron-completion notifications: the originating turn's
    ``sessionWebhook`` is long gone by the time the job finishes, so we go
    in cold via the proactive endpoint. ``robot_code`` defaults to
    ``client_id`` (they're usually interchangeable; old apps may differ).
    """
    if not user_id or not text:
        return
    token = await token_mgr.get_access_token(client_id, client_secret)
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }
    if markdown:
        msg_key = "sampleMarkdown"
        msg_param = json.dumps({"title": title, "text": text})
    else:
        msg_key = "sampleText"
        msg_param = json.dumps({"content": text})
    payload = {
        "robotCode": robot_code or client_id,
        "userIds": [user_id],
        "msgKey": msg_key,
        "msgParam": msg_param,
    }
    try:
        resp = await client.post(
            f"{DINGTALK_API}{PROACTIVE_DM_ENDPOINT}",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dingtalk.proactive.dm.error",
            f"user={user_id[:8]}… {type(exc).__name__}: {exc}",
        )


async def send_proactive_to_group(
    client: httpx.AsyncClient,
    *,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
    open_conversation_id: str,
    text: str,
    title: str = "通知",
    markdown: bool = False,
    robot_code: Optional[str] = None,
) -> None:
    """Send a message into a group conversation by its ``openConversationId``.

    Same shape as the DM endpoint, different URL + field name. The same
    ``robotCode`` rules apply.
    """
    if not open_conversation_id or not text:
        return
    token = await token_mgr.get_access_token(client_id, client_secret)
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }
    if markdown:
        msg_key = "sampleMarkdown"
        msg_param = json.dumps({"title": title, "text": text})
    else:
        msg_key = "sampleText"
        msg_param = json.dumps({"content": text})
    payload = {
        "robotCode": robot_code or client_id,
        "openConversationId": open_conversation_id,
        "msgKey": msg_key,
        "msgParam": msg_param,
    }
    try:
        resp = await client.post(
            f"{DINGTALK_API}{PROACTIVE_GROUP_ENDPOINT}",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dingtalk.proactive.group.error",
            f"conv={open_conversation_id[:12]}… {type(exc).__name__}: {exc}",
        )
