"""Web attachment handling for nano-openclaw.

Images are converted to model content blocks by the agent loop. Non-image
attachments are persisted as local files and exposed to the model as paths so
it can decide whether an installed skill or tool can process them.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_ATTACHMENTS_PER_TURN = 5
MAX_NON_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024

IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})


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
        if not is_image_mime(mime) and actual_size > MAX_NON_IMAGE_BYTES:
            raise ValueError(
                f"attachment {name!r} is too large: {actual_size} > {MAX_NON_IMAGE_BYTES}"
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


def save_non_image_attachment(
    attachment: PromptAttachment,
    *,
    root: Path,
    session_id: str,
    turn_id: str,
) -> SavedAttachment:
    """Persist a non-image attachment inside the session attachment directory."""
    safe_name = safe_filename(attachment.name)
    target_dir = root / ".openclaw" / "web-attachments" / safe_path_part(session_id) / safe_path_part(turn_id)
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
