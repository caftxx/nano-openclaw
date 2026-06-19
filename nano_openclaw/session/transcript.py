"""Transcript writer and reader for .jsonl session files.

Mirrors OpenClaw's `src/config/sessions/transcript.ts`:
- TranscriptWriter: append entries to .jsonl
- TranscriptReader: parse .jsonl back to Message objects

The .jsonl format uses one JSON object per line:
- Header: {"type":"session", "version":1, "id":"uuid", ...}
- Message: {"type":"message", "id":"msg-xxx", "role":"user", "content":[...]}
- Compaction: {"type":"compaction", "id":"comp-xxx", "summary":"..."}
- Activity: {"type":"activity", "turn_id":"...", "payloads":[...]}
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from nano_openclaw.core.loop import Message
from .truncate import truncate_tool_result
from .types import (
    SessionHeader,
    TranscriptCompaction,
    TranscriptMessage,
    TranscriptEntry,
)


SUMMARY_PREFIX = "[Previous conversation summary]\n"


def is_synthetic_summary(message: Message) -> bool:
    """True when the message is the synthetic summary injected by compact_if_needed."""
    if message.role != "user" or not message.content:
        return False
    block = message.content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return False
    text = block.get("text", "")
    return isinstance(text, str) and text.startswith(SUMMARY_PREFIX)


def _summary_text_from_message(message: Message) -> str:
    if not is_synthetic_summary(message):
        return ""
    return message.content[0].get("text", "")[len(SUMMARY_PREFIX):]


def _build_synthetic_summary_message(summary: str) -> Message:
    return Message(
        role="user",
        content=[{"type": "text", "text": f"{SUMMARY_PREFIX}{summary}"}],
    )


@dataclass
class TranscriptWriter:
    """Append entries to a .jsonl transcript file."""
    path: Path
    _session_id: str = ""
    _last_message_id: str = ""
    _message_count: int = 0
    _compaction_count: int = 0
    # Lazy-write state: header is only written to disk on the first append so
    # that sessions with no messages leave no files behind.
    _started: bool = False
    _lazy_header: Any = field(default=None, repr=False)
    _on_first_write: Any = field(default=None, repr=False)

    @classmethod
    def resume(
        cls,
        path: Path,
        session_id: str,
        msg_count: int,
        comp_count: int,
        last_message_id: str,
    ) -> "TranscriptWriter":
        """Create a writer that appends to an existing transcript."""
        writer = cls(path)
        writer._session_id = session_id
        writer._message_count = msg_count
        writer._compaction_count = comp_count
        writer._last_message_id = last_message_id
        writer._started = path.exists()
        return writer

    @property
    def session_id(self) -> str:
        return self._session_id

    def start(self, *, model: str = "", cwd: str = "", session_id: str | None = None) -> str:
        """Prepare session header for lazy write; return the session ID.

        The header is written to disk only when the first message is appended,
        so sessions with no messages leave no files behind.
        """
        header = SessionHeader(model=model, cwd=cwd)
        if session_id:
            header.id = session_id
        self._session_id = header.id
        self._lazy_header = header
        return self._session_id

    def _ensure_started(self) -> None:
        """Write the session header on first call, then fire _on_first_write once."""
        if self._started:
            return
        self._started = True
        if self._lazy_header is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(self._lazy_header), ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._lazy_header = None
        if self._on_first_write is not None:
            cb = self._on_first_write
            self._on_first_write = None
            cb()

    def append_message(self, message: Message) -> None:
        """Append a message entry to the transcript."""
        parent_id = self._last_message_id
        entry = TranscriptMessage(
            parent_id=parent_id,
            role=message.role,
            content=_prepare_content_for_persistence(message.content),
        )
        self._last_message_id = entry.id
        self._message_count += 1
        self._append(entry)

    def append_compaction(self, summary: str) -> None:
        """Append a compaction entry to the transcript."""
        entry = TranscriptCompaction(
            parent_id=self._last_message_id,
            summary=summary,
        )
        self._compaction_count += 1
        self._append(entry)

    def append_activity(self, activity: dict[str, Any]) -> None:
        """Append WebUI-only activity metadata to the transcript."""
        self._append_raw({"type": "activity", **activity})

    def rotate(self, summary: str, kept_messages: list[Message]) -> None:
        """Atomically rewrite the transcript to ``header + compaction(summary) + kept_messages``.

        Used after context compaction to keep the on-disk transcript in sync with
        the in-memory post-compaction history. Without this, the file keeps
        growing forever and a daemon restart re-loads the full pre-compaction
        history (defeating compaction). Activity entries from before the
        rotation are dropped — the WebUI activity log only reflects events
        produced after the most recent rotation.
        """
        # Resolve a header even if the file was never started (so rotate() works
        # before the lazy header has been flushed). Otherwise reuse what's on disk.
        header_obj: Any = self._lazy_header
        if header_obj is None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session":
                    header_obj = entry
                    break
        if header_obj is None:
            header_obj = SessionHeader(id=self._session_id or "")

        header_dict = header_obj if isinstance(header_obj, dict) else asdict(header_obj)
        compaction = TranscriptCompaction(parent_id="", summary=summary)

        new_entries: list[dict[str, Any]] = [header_dict, asdict(compaction)]
        last_id = compaction.id
        for msg in kept_messages:
            entry = TranscriptMessage(
                parent_id=last_id,
                role=msg.role,
                content=_prepare_content_for_persistence(msg.content),
            )
            new_entries.append(asdict(entry))
            last_id = entry.id

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in new_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self.path)

        self._lazy_header = None
        self._started = True
        self._message_count = len(kept_messages)
        self._compaction_count = 1
        self._last_message_id = last_id

    def clear(self) -> None:
        """Rewrite the transcript keeping only the session header; reset counters."""
        if not self.path.exists():
            self._message_count = 0
            self._compaction_count = 0
            self._last_message_id = ""
            # Keep _started=False and _lazy_header intact so next append re-creates the file.
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        header_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session":
                header_lines.append(stripped)
        self.path.write_text("\n".join(header_lines) + ("\n" if header_lines else ""), encoding="utf-8")
        self._message_count = 0
        self._compaction_count = 0
        self._last_message_id = ""

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def compaction_count(self) -> int:
        return self._compaction_count

    def _append(self, entry: TranscriptEntry) -> None:
        self._ensure_started()
        line = json.dumps(asdict(entry), ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _append_raw(self, entry: dict[str, Any]) -> None:
        self._ensure_started()
        line = json.dumps(entry, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _prepare_content_for_persistence(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize content blocks before writing to transcript."""
    result = []
    for block in content:
        if not isinstance(block, dict):
            result.append(block)
            continue

        block_type = block.get("type")
        if block_type == "tool_result":
            # Truncate text content in tool results
            text_content = block.get("content", [])
            if isinstance(text_content, list):
                block = {**block, "content": truncate_tool_result(text_content)}
            result.append(block)
        elif block_type == "image":
            # Skip image blocks from persistence (they're expensive to store)
            # The image description text should already be in a text block
            continue
        else:
            result.append(block)
    return result


@dataclass
class TranscriptReader:
    """Parse a .jsonl transcript file back into Message objects."""
    path: Path

    def load_history(self) -> tuple[list[Message], str, int, int, str]:
        """Load transcript and return (history, session_id, message_count, compaction_count, last_message_id).

        If the file doesn't exist or is empty, returns empty history.
        """
        if not self.path.exists():
            return [], "", 0, 0, ""

        history: list[Message] = []
        session_id = ""
        message_count = 0
        compaction_count = 0
        last_message_id = ""

        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")
                if entry_type == "session":
                    session_id = entry.get("id", "")
                elif entry_type == "message":
                    msg = Message(
                        role=entry.get("role", "user"),
                        content=entry.get("content", []),
                    )
                    history.append(msg)
                    message_count += 1
                    last_message_id = entry.get("id", "")
                elif entry_type == "compaction":
                    compaction_count += 1
                    # Only materialize compactions written by rotate(): those
                    # appear before any message in the file, so the in-memory
                    # shape matches what compact_if_needed would produce.
                    # Mid-file compactions in legacy (non-rotated) transcripts
                    # stay as markers — the original messages are still on
                    # disk and would be the load source.
                    summary = entry.get("summary", "")
                    if summary and message_count == 0:
                        history.append(_build_synthetic_summary_message(summary))

        return history, session_id, message_count, compaction_count, last_message_id
