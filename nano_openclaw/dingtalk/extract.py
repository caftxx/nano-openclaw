"""Decode a DingTalk callback payload into an :class:`ExtractedMessage`.

Handles the inbound subset that PR2 actually responds to: ``text`` and the
text parts of ``richText`` (with image/audio/video/file segments deferred
to PR4 along with media downloads). Reply/quote chains are flattened to at
most three levels — beyond that the context is noisy and we drop it.

The returned dataclass is the only structure downstream code (policy, bot,
sender) is allowed to touch — that keeps the wire format isolated to this
file when DingTalk inevitably adds new ``msgtype`` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_QUOTE_DEPTH = 3


@dataclass
class ExtractedMessage:
    """Channel-neutral view of one inbound DingTalk message.

    ``at_self`` reflects ``isInAtList`` (whether the bot is in the @-target
    list) — group-chat policy uses this to decide whether to engage.
    """

    text: str
    sender_staff_id: str
    sender_nick: str
    conversation_id: str
    is_group: bool
    at_self: bool
    msg_id: str
    session_webhook: str
    session_webhook_expire_ms: int
    msgtype: str
    at_user_staff_ids: list[str] = field(default_factory=list)
    # ``robot_code`` carries the legacy AppKey-vs-RobotCode split. AI Card
    # APIs prefer the message-time value over the configured clientId when
    # set — they can differ for apps onboarded through the older console.
    robot_code: str = ""
    chatbot_user_id: str = ""


def _text_from_text_msg(data: dict[str, Any]) -> str:
    text_obj = data.get("text") or {}
    return str(text_obj.get("content") or "").strip()


def _text_from_rich_text(data: dict[str, Any]) -> str:
    """Concatenate the textual parts of a richText payload.

    Media segments are skipped — they're either ``downloadCode``s (PR4) or
    unrenderable in plain text. Empty segments are filtered so we don't end
    up with stray blank lines from layout-only spans.
    """
    content = data.get("content") or {}
    parts = content.get("richText") or []
    pieces = []
    for part in parts:
        if isinstance(part, dict):
            t = str(part.get("text") or "").strip()
            if t:
                pieces.append(t)
    return "\n".join(pieces)


def _primary_text(data: dict[str, Any]) -> str:
    msgtype = str(data.get("msgtype") or "")
    if msgtype == "text":
        return _text_from_text_msg(data)
    if msgtype == "richText":
        return _text_from_rich_text(data)
    return ""


def _quote_chain_text(data: dict[str, Any], *, depth: int = 0) -> str:
    """Recursively pull text out of replied/quoted messages.

    Each level is prefixed with the sender's nickname so the agent can tell
    who said what. Depth is capped at :data:`MAX_QUOTE_DEPTH` — deeper
    chains are common in busy group chats but rarely useful, and the token
    cost grows linearly.
    """
    if depth >= MAX_QUOTE_DEPTH:
        return ""
    if not data.get("isReplyMsg"):
        return ""
    replied = data.get("repliedMsg")
    if not isinstance(replied, dict):
        return ""

    text = _primary_text(replied)
    sender = str(replied.get("senderNick") or replied.get("senderStaffId") or "user")
    deeper = _quote_chain_text(replied, depth=depth + 1)
    chunks = []
    if deeper:
        chunks.append(deeper)
    if text:
        chunks.append(f"> @{sender}: {text}")
    return "\n".join(chunks)


def extract_message(data: dict[str, Any]) -> ExtractedMessage:
    """Decode a CALLBACK frame's ``data`` JSON into an ``ExtractedMessage``.

    ``data`` should already be parsed from JSON (``CallbackFrame.data`` is a
    string, the caller runs ``json.loads`` first).
    """
    text = _primary_text(data)
    quoted = _quote_chain_text(data)
    if quoted and text:
        text = f"{quoted}\n\n{text}"
    elif quoted:
        text = quoted

    at_users_raw = data.get("atUsers") or []
    at_staff_ids: list[str] = []
    for entry in at_users_raw:
        if isinstance(entry, dict):
            sid = str(entry.get("staffId") or "")
            if sid:
                at_staff_ids.append(sid)

    conv_type = str(data.get("conversationType") or "1")
    return ExtractedMessage(
        text=text,
        sender_staff_id=str(data.get("senderStaffId") or ""),
        sender_nick=str(data.get("senderNick") or ""),
        conversation_id=str(data.get("conversationId") or ""),
        is_group=conv_type == "2",
        at_self=bool(data.get("isInAtList")),
        msg_id=str(data.get("msgId") or ""),
        session_webhook=str(data.get("sessionWebhook") or ""),
        session_webhook_expire_ms=int(data.get("sessionWebhookExpiredTime") or 0),
        msgtype=str(data.get("msgtype") or ""),
        at_user_staff_ids=at_staff_ids,
        robot_code=str(data.get("robotCode") or ""),
        chatbot_user_id=str(data.get("chatbotUserId") or ""),
    )
