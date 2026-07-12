"""Web attachment handling for nano-openclaw.

Images are converted to model content blocks by the agent loop. Non-image
attachments are persisted as local files and exposed to the model as paths so
it can decide whether an installed skill or tool can process them.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


MAX_ATTACHMENTS_PER_TURN = 5
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 250 * 1024 * 1024

IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})
IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

DOCUMENT_TEXT_MIME_TYPES = frozenset({
    "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
    "text/markdown",
    "text/plain",
})
DOCUMENT_TEXT_SUFFIXES = frozenset({".csv", ".json", ".md", ".txt"})
# Keep enough extracted text for long academic papers while still bounding the
# aggregate document context.  Podcast paper mode retrieves a small evidence
# window per round, so retaining the full extraction here does not mean sending
# all of it to the model on every turn.
MAX_EXTRACTED_DOCUMENT_CHARS = 400_000
MAX_DOCX_DOCUMENT_XML_BYTES = 10 * 1024 * 1024


@dataclass
class PromptAttachment:
    name: str
    mime: str
    size: int
    data: bytes


@dataclass
class SavedAttachment:
    name: str
    mime: str
    size: int
    path: Path
    display_path: str


@dataclass
class AttachmentAttached:
    refs: list[str]


@dataclass
class AttachmentError:
    ref: str
    error: str


def decode_attachment_payloads(raw_items: list[Any]) -> list[PromptAttachment]:
    """Validate and decode WebSocket attachment payloads."""
    if len(raw_items) > MAX_ATTACHMENTS_PER_TURN:
        raise ValueError(f"too many attachments: {len(raw_items)} > {MAX_ATTACHMENTS_PER_TURN}")

    attachments: list[PromptAttachment] = []
    total_size = 0
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("attachment must be an object")

        name = _safe_display_name(str(item.get("name") or "attachment"))
        mime = _normalise_mime(str(item.get("mime") or "application/octet-stream"))
        declared_size = int(item.get("size") or 0)
        encoded = str(item.get("data") or "")
        if not encoded:
            raise ValueError(f"attachment {name!r} is missing data")

        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"attachment {name!r} is not valid base64") from exc

        actual_size = len(data)
        if declared_size and declared_size != actual_size:
            raise ValueError(
                f"attachment {name!r} size mismatch: declared {declared_size}, got {actual_size}"
            )
        if actual_size <= 0:
            raise ValueError(f"attachment {name!r} is empty")
        if actual_size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment {name!r} is too large: {actual_size} > {MAX_ATTACHMENT_BYTES}"
            )

        total_size += actual_size
        if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachments are too large in total: {total_size} > {MAX_TOTAL_ATTACHMENT_BYTES}"
            )

        attachments.append(PromptAttachment(name=name, mime=mime, size=actual_size, data=data))

    return attachments


def is_image_mime(mime: str) -> bool:
    return _normalise_mime(mime) in IMAGE_MIME_TYPES


def attachment_image_mime(attachment: PromptAttachment) -> str | None:
    """Return a supported image MIME, falling back to the safe filename suffix."""
    mime = _normalise_mime(attachment.mime)
    if mime in IMAGE_MIME_TYPES:
        return mime
    return IMAGE_MIME_BY_SUFFIX.get(Path(attachment.name).suffix.lower())


def extract_document_text(attachment: PromptAttachment) -> str:
    """Extract bounded plain text from a document uploaded to group chat."""
    mime = _normalise_mime(attachment.mime)
    suffix = Path(attachment.name).suffix.lower()
    if mime == "application/pdf" or suffix == ".pdf":
        text = _extract_pdf_text(attachment.data)
    elif (
        mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    ):
        text = _extract_docx_text(attachment.data)
    elif mime in DOCUMENT_TEXT_MIME_TYPES or suffix in DOCUMENT_TEXT_SUFFIXES:
        text = _decode_text_document(attachment.data)
    else:
        raise ValueError(f"unsupported group-chat document type: {attachment.name}")

    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError(f"no readable text found in document: {attachment.name}")
    if len(text) > MAX_EXTRACTED_DOCUMENT_CHARS:
        text = text[:MAX_EXTRACTED_DOCUMENT_CHARS].rstrip() + "\n[文档内容已截断]"
    return text


def document_context_text(text: str, attachments: list[PromptAttachment]) -> str:
    """Combine typed input and extracted documents into podcast context."""
    parts: list[str] = []
    clean_text = text.strip()
    if clean_text:
        parts.append(clean_text)
    per_document_chars = max(600, MAX_EXTRACTED_DOCUMENT_CHARS // max(1, len(attachments)))
    for attachment in attachments:
        extracted = extract_document_text(attachment)
        if len(extracted) > per_document_chars:
            extracted = extracted[:per_document_chars].rstrip() + "\n[文档内容已截断]"
        parts.append(f"[参考文档：{attachment.name}]\n{extracted}")
    return "\n\n".join(parts)


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[第 {index} 页]\n{text}")
        return "\n\n".join(pages)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("could not read PDF document") from exc


def _extract_docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_DOCUMENT_XML_BYTES:
                raise ValueError(
                    "DOCX document XML is too large: "
                    f"{info.file_size} > {MAX_DOCX_DOCUMENT_XML_BYTES}"
                )
            with archive.open(info) as document:
                document_xml = document.read(MAX_DOCX_DOCUMENT_XML_BYTES + 1)
            if len(document_xml) > MAX_DOCX_DOCUMENT_XML_BYTES:
                raise ValueError(
                    "DOCX document XML is too large: "
                    f"> {MAX_DOCX_DOCUMENT_XML_BYTES}"
                )
    except ValueError:
        raise
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ValueError("could not read DOCX document") from exc

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("could not parse DOCX document") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        value = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
        if value:
            paragraphs.append(value)
    return "\n".join(paragraphs)


def _decode_text_document(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("could not decode text document")


def save_non_image_attachment(
    attachment: PromptAttachment,
    *,
    root: Path,
    session_id: str,
    turn_id: str,
) -> SavedAttachment:
    """Persist a non-image attachment inside the session attachment directory."""
    safe_name = safe_filename(attachment.name)
    target_dir = root / ".nano-openclaw" / "web-attachments" / safe_path_part(session_id) / safe_path_part(turn_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    path = target_dir / safe_name
    if path.exists():
        stem = path.stem
        suffix = path.suffix
        for index in range(2, 1000):
            candidate = target_dir / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                path = candidate
                break

    path.write_bytes(attachment.data)
    display_path = path.relative_to(root).as_posix()
    return SavedAttachment(
        name=attachment.name,
        mime=attachment.mime,
        size=attachment.size,
        path=path,
        display_path=display_path,
    )


def attachment_prompt_block(saved: SavedAttachment) -> str:
    return (
        "[Attached file]\n"
        f"name: {saved.name}\n"
        f"type: {saved.mime}\n"
        f"size: {saved.size} bytes\n"
        f"path: {saved.display_path}\n\n"
        "This non-image attachment is available as a local file path. If the task "
        "requires reading it, decide whether an installed skill or tool can process "
        "this file type. If no suitable skill/tool is available, tell the user which "
        "skill or capability is missing, skip parsing this attachment, and continue "
        "with any answerable parts."
    )


def safe_filename(name: str) -> str:
    leaf = Path(name.replace("\\", "/")).name.strip()
    leaf = re.sub(r"[^A-Za-z0-9._ -]+", "_", leaf)
    leaf = leaf.strip(" .")
    if not leaf:
        leaf = "attachment"
    return leaf[:120]


def safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return safe[:80] or "unknown"


def _safe_display_name(name: str) -> str:
    return safe_filename(name)


def _normalise_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower() or "application/octet-stream"
