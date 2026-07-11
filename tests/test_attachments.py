from __future__ import annotations

import asyncio
import base64
import shutil
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from nano_openclaw.core.attachments import (
    AttachmentAttached,
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    PromptAttachment,
    decode_attachment_payloads,
    document_context_text,
    extract_document_text,
)
from nano_openclaw.core.loop import AgentSession, ImageError, LoopConfig, Message
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


def test_web_attachment_limits_allow_fifty_megabytes_per_file():
    assert MAX_ATTACHMENT_BYTES == 50 * 1024 * 1024
    assert MAX_TOTAL_ATTACHMENT_BYTES == 5 * MAX_ATTACHMENT_BYTES


def test_decode_attachment_payloads_rejects_bad_base64():
    with pytest.raises(ValueError, match="not valid base64"):
        decode_attachment_payloads([{
            "name": "demo.pdf",
            "mime": "application/pdf",
            "size": 10,
            "data": "not base64!",
        }])


def test_extract_text_document_for_group_chat():
    data = "第一段\n第二段".encode()
    attachment = PromptAttachment("notes.md", "text/markdown", len(data), data)

    assert extract_document_text(attachment) == "第一段\n第二段"
    assert document_context_text("请总结", [attachment]) == (
        "请总结\n\n[参考文档：notes.md]\n第一段\n第二段"
    )


def test_extract_docx_document_for_group_chat():
    xml = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Product brief</w:t></w:r></w:p>
      <w:p><w:r><w:t>Launch in July</w:t></w:r></w:p></w:body>
    </w:document>'''
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    data = buffer.getvalue()

    assert extract_document_text(PromptAttachment(
        "brief.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        len(data),
        data,
    )) == "Product brief\nLaunch in July"


def test_image_attachment_describe_error_keeps_visible_context(monkeypatch):
    async def fail_describe(*_args, **_kwargs):
        raise RuntimeError("vision service rejected image")

    monkeypatch.setattr("nano_openclaw.core.loop.describe_image", fail_describe)

    events = []
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    session = AgentSession(
        history=[],
        registry=ToolRegistry(),
        on_event=events.append,
        client=object(),
        cfg=LoopConfig(image_model="image-model", model_input=("text",)),
    )

    content = asyncio.run(session._build_user_content(
        "what is this?",
        "what is this?",
        None,
        events.append,
        attachments=[PromptAttachment("tiny.png", "image/png", len(png), png)],
    ))

    text = "\n".join(block["text"] for block in content if block.get("type") == "text")
    assert any(isinstance(event, ImageError) for event in events)
    assert "[Image: tiny.png]" in text
    assert "processing error: vision service rejected image" in text
    assert "what is this?" in text


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
