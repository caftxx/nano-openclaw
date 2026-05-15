"""Decode a DingTalk callback payload into an :class:`ExtractedMessage`.

Handles the inbound subset the bot responds to: ``text``, the text parts
of ``richText``, and media payloads (``picture`` / ``audio`` / ``video`` /
``file``, plus inline media inside ``richText``). Reply/quote chains are
flattened to at most three levels — beyond that the context is noisy.

The returned dataclass is the only structure downstream code (policy, bot,
sender, dispatcher) is allowed to touch — that keeps the wire format
isolated to this file when DingTalk inevitably adds new ``msgtype`` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_QUOTE_DEPTH = 3


# Bytes-to-MIME guesses by msgtype. DingTalk doesn't put MIME on the wire,
# so we default to plausible values per msgtype; ``file`` falls back to
# ``application/octet-stream`` and lets the agent figure out the rest from
# the filename / contents.
_MIME_BY_MSGTYPE = {
    "picture": "image/jpeg",
    "audio": "audio/amr",
    "video": "video/mp4",
}


@dataclass
class MediaItem:
    """One downloadable attachment from an inbound DingTalk message.

    Use :func:`nano_openclaw.dingtalk.media.download_media` to materialize
    ``download_code`` into bytes; this dataclass is purely the description.
    """

    download_code: str
    mime: str
    name: str  # display name (derived if not provided)
    msgtype: str  # 'picture' | 'audio' | 'video' | 'file' | 'richText:image'


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
    media: list[MediaItem] = field(default_factory=list)


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


def _extract_media(data: dict[str, Any]) -> list[MediaItem]:
    """Pull every downloadable media reference out of a payload.

    Coverage:
    - ``msgtype == "picture"`` → ``content.downloadCode``
    - ``msgtype == "audio"`` → ``content.downloadCode``
    - ``msgtype == "video"`` → ``content.downloadCode``
    - ``msgtype == "file"`` → ``content.downloadCode`` + ``content.fileName``
    - ``msgtype == "richText"`` → each ``richText[].downloadCode`` (images)

    Order preserved as a list (not a dict-keyed map) because richText messages
    can carry multiple images and the agent might want them in order.
    """
    items: list[MediaItem] = []
    msgtype = str(data.get("msgtype") or "")
    content = data.get("content") or {}

    if msgtype in ("picture", "audio", "video"):
        code = str(content.get("downloadCode") or "")
        if code:
            mime = _MIME_BY_MSGTYPE.get(msgtype, "application/octet-stream")
            ext = {"picture": "jpg", "audio": "amr", "video": "mp4"}.get(msgtype, "bin")
            items.append(MediaItem(
                download_code=code,
                mime=mime,
                name=f"dingtalk-{msgtype}-{code[:8]}.{ext}",
                msgtype=msgtype,
            ))
    elif msgtype == "file":
        code = str(content.get("downloadCode") or "")
        name = str(content.get("fileName") or content.get("fileType") or "file") or "file"
        if code:
            items.append(MediaItem(
                download_code=code,
                mime="application/octet-stream",
                name=name,
                msgtype="file",
            ))
    elif msgtype == "richText":
        for idx, part in enumerate(content.get("richText") or []):
            if not isinstance(part, dict):
                continue
            code = str(part.get("downloadCode") or "")
            if not code:
                continue
            items.append(MediaItem(
                download_code=code,
                mime="image/jpeg",
                name=f"dingtalk-richtext-{idx}-{code[:8]}.jpg",
                msgtype="richText:image",
            ))
    return items


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
        media=_extract_media(data),
    )
