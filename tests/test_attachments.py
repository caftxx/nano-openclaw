from __future__ import annotations

import asyncio
import base64
import shutil
import uuid
from pathlib import Path

import pytest

from nano_openclaw.core.attachments import (
    AttachmentAttached,
    PromptAttachment,
    decode_attachment_payloads,
)
from nano_openclaw.core.loop import AgentSession, LoopConfig, Message
from nano_openclaw.core.provider import MessageEnd, TextDelta
from nano_openclaw.core.tools import ToolRegistry


def test_decode_attachment_payloads_accepts_base64():
    data = b"%PDF-1.7"
    attachments = decode_attachment_payloads([{
        "name": "demo.pdf",
        "mime": "application/pdf",
        "size": len(data),
        "data": base64.b64encode(data).decode(),
    }])

    assert attachments == [PromptAttachment("demo.pdf", "application/pdf", len(data), data)]


def test_decode_attachment_payloads_rejects_bad_base64():
    with pytest.raises(ValueError, match="not valid base64"):
        decode_attachment_payloads([{
            "name": "demo.pdf",
            "mime": "application/pdf",
            "size": 10,
            "data": "not base64!",
        }])


def test_agent_session_persists_non_image_attachment_and_injects_path(monkeypatch):
    tmp_dir = Path("tests") / f".tmp-attachments-{uuid.uuid4().hex}"
    events = []

    async def fake_stream_response(**_kwargs):
        yield TextDelta(text="ok")
        yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    try:
        tmp_dir.mkdir(parents=True)
        history: list[Message] = []
        attachment = PromptAttachment(
            name="../demo.pdf",
            mime="application/pdf",
            size=8,
            data=b"%PDF-1.7",
        )

        session = AgentSession(
            history=history,
            registry=ToolRegistry(),
            on_event=events.append,
            client=object(),
            cfg=LoopConfig(workspace_dir=tmp_dir, session_key="session-1"),
        )
        asyncio.run(session.run_turn(
            "summarize this",
            attachments=[attachment],
            attachment_turn_id="turn-1",
        ))

        assert any(isinstance(event, AttachmentAttached) for event in events)
        saved = tmp_dir / ".nano-openclaw" / "web-attachments" / "session-1" / "turn-1" / "demo.pdf"
        assert saved.read_bytes() == b"%PDF-1.7"
        user_text = "\n".join(
            block["text"] for block in history[0].content if block.get("type") == "text"
        )
        assert "path: .nano-openclaw/web-attachments/session-1/turn-1/demo.pdf" in user_text
        assert "If no suitable skill/tool is available" in user_text
        assert "summarize this" in user_text
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
