"""chat.* RPC handlers — send / abort / history.

Attachments are inlined as base64 in ``chat.send`` for v1 (matches the
existing webui WebSocket protocol). Phase later may add a separate
``chat.upload`` HTTP endpoint with attachment_id references.
"""

from __future__ import annotations

import base64
from typing import Any

from nano_openclaw.core.attachments import PromptAttachment
from nano_openclaw.api.context import GatewayContext


async def chat_send(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_key = str(params.get("session_key") or "")
    text = str(params.get("text") or "")
    turn_source = str(params.get("turn_source") or "tui")
    response_style = str(params.get("response_style") or "")
    raw_attachments = params.get("attachments") or []

    attachments: list[PromptAttachment] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        data_b64 = item.get("data") or item.get("data_b64") or ""
        try:
            data = base64.b64decode(data_b64) if data_b64 else b""
        except Exception:
            data = b""
        attachments.append(PromptAttachment(
            name=str(item.get("name") or "attachment"),
            mime=str(item.get("mime") or "application/octet-stream"),
            size=int(item.get("size") or len(data)),
            data=data,
        ))

    turn_id = await ctx.backend.chat_send(
        session_key=session_key,
        text=text,
        attachments=attachments or None,
        turn_source=turn_source,
        response_style=response_style,
    )
    return {"turn_id": turn_id}


async def chat_abort(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    turn_id = str(params.get("turn_id") or "")
    if not turn_id:
        return {"ok": False, "reason": "missing turn_id"}
    await ctx.backend.chat_abort(turn_id=turn_id)
    return {"ok": True}


async def chat_history(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_id = str(params.get("session_id") or "")
    after_seq_raw = params.get("after_seq")
    after_seq = int(after_seq_raw) if after_seq_raw is not None else None
    payload = await ctx.backend.chat_history(session_id, after_seq=after_seq)
    return {
        "session_id": payload.session_id,
        "history": payload.history,
        "activities": payload.activities,
        "last_seq": payload.last_seq,
    }


HANDLERS = {
    "chat.send": chat_send,
    "chat.abort": chat_abort,
    "chat.history": chat_history,
}
