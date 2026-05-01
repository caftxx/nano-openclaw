"""Transcript writer and reader for .jsonl session files.

Mirrors OpenClaw's `src/config/sessions/transcript.ts`:
- TranscriptWriter: append entries to .jsonl
- TranscriptReader: parse .jsonl back to Message objects

The .jsonl format uses one JSON object per line:
- Header: {"type":"session", "version":1, "id":"uuid", ...}
- Message: {"type":"message", "id":"msg-xxx", "role":"user", "content":[...]}
- Compaction: {"type":"compaction", "id":"comp-xxx", "summary":"..."}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..loop import Message
from .truncate import truncate_tool_result
from .types import (
    SessionHeader,
    TranscriptCompaction,
    TranscriptMessage,
    TranscriptEntry,
)


@dataclass
class TranscriptWriter:
    """Append entries to a .jsonl transcript file."""
    path: Path
    _session_id: str = ""
    _last_message_id: str = ""
    _message_count: int = 0
    _compaction_count: int = 0

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
        return writer

    @property
    def session_id(self) -> str:
        return self._session_id

    def start(self, *, model: str = "", cwd: str = "") -> str:
        """Write header entry and return the session ID."""
        header = SessionHeader(model=model, cwd=cwd)
        self._session_id = header.id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._append(header)
        return self._session_id

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

    def clear(self) -> None:
        """Rewrite the transcript keeping only the session header; reset counters."""
        if not self.path.exists():
            self._message_count = 0
            self._compaction_count = 0
            self._last_message_id = ""
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
        line = json.dumps(asdict(entry), ensure_ascii=False)
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

        return history, session_id, message_count, compaction_count, last_message_id
