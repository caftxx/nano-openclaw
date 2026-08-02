"""Notification queue for WeChat push notifications.

Stores pending notifications in notify-queue.jsonl. Each item has a target_uid
for directed delivery to the job creator.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class NotifyItem:
    """A pending notification to be sent to a WeChat user."""

    job_id: str
    job_name: str
    status: str  # "ok" | "error"
    result_summary: str  # 执行结果摘要
    created_at: str  # ISO datetime
    target_uid: str  # 目标用户 uid（定向发送）
    sent: bool = False  # 是否已发送
    attempts: int = 0
    last_error: str = ""
    next_retry_at: float = 0.0
    next_chunk_index: int = 0
    sent_at: str = ""


class NotifyQueue:
    """Persistent notification queue backed by JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, item: NotifyItem) -> None:
        """Append a new notification to the queue."""
        line = json.dumps(asdict(item)) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._restrict_permissions()

    def get_pending(self, limit: int = 10, *, now: float | None = None) -> list[NotifyItem]:
        """Get due, unsent notifications up to ``limit``.

        Old queue rows do not have retry fields; tolerant defaults make them
        immediately due and preserve backwards compatibility.
        """
        due_at = time.time() if now is None else now
        items: list[NotifyItem] = []
        for d in self._load_dicts():
            if d.get("sent") or float(d.get("next_retry_at") or 0.0) > due_at:
                continue
            items.append(self._item_from_dict(d))
            if len(items) >= limit:
                break
        return items

    def mark_sent(self, job_id: str, created_at: str) -> None:
        """Mark a notification sent only after every chunk is acknowledged."""
        def mutate(d: dict[str, Any]) -> None:
            d["sent"] = True
            d["sent_at"] = datetime.now().astimezone().isoformat()
            d["last_error"] = ""
            d["next_retry_at"] = 0.0

        self._update_matching(job_id, created_at, mutate)

    def mark_chunk_sent(self, job_id: str, created_at: str, next_chunk_index: int) -> None:
        """Checkpoint chunk progress so retry does not resend prior chunks."""
        def mutate(d: dict[str, Any]) -> None:
            current = int(d.get("next_chunk_index") or 0)
            d["next_chunk_index"] = max(current, next_chunk_index)
            d["last_error"] = ""

        self._update_matching(job_id, created_at, mutate)

    def mark_failed(
        self,
        job_id: str,
        created_at: str,
        error: str,
        *,
        retry_delay: float,
    ) -> None:
        """Record a failed attempt while keeping the notification pending."""
        def mutate(d: dict[str, Any]) -> None:
            d["sent"] = False
            d["attempts"] = int(d.get("attempts") or 0) + 1
            d["last_error"] = error[:1000]
            d["next_retry_at"] = time.time() + max(0.0, retry_delay)

        self._update_matching(job_id, created_at, mutate)

    def retry_now_for_target(self, target_uid: str) -> int:
        """Wake pending rows when a fresh context token arrives for a user."""
        count = 0

        def mutate_all(d: dict[str, Any]) -> None:
            nonlocal count
            if not d.get("sent") and d.get("target_uid") == target_uid:
                d["next_retry_at"] = 0.0
                count += 1

        self._rewrite(mutate_all)
        return count

    def purge_sent(self, keep_recent: int = 100) -> None:
        """Remove sent notifications, keep recent N for audit."""
        rows = self._load_dicts()
        sent_indices = [i for i, row in enumerate(rows) if row.get("sent")]
        recent_count = max(0, keep_recent)
        keep_sent = set(sent_indices[-recent_count:]) if recent_count else set()
        kept = [
            row for i, row in enumerate(rows)
            if not row.get("sent") or i in keep_sent
        ]
        self._write_dicts(kept)

    @staticmethod
    def _item_from_dict(d: dict[str, Any]) -> NotifyItem:
        return NotifyItem(
            job_id=d.get("job_id", ""),
            job_name=d.get("job_name", ""),
            status=d.get("status", "ok"),
            result_summary=d.get("result_summary", ""),
            created_at=d.get("created_at", ""),
            target_uid=d.get("target_uid", ""),
            sent=bool(d.get("sent", False)),
            attempts=int(d.get("attempts") or 0),
            last_error=str(d.get("last_error") or ""),
            next_retry_at=float(d.get("next_retry_at") or 0.0),
            next_chunk_index=int(d.get("next_chunk_index") or 0),
            sent_at=str(d.get("sent_at") or ""),
        )

    def _load_dicts(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def _update_matching(
        self,
        job_id: str,
        created_at: str,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        def apply(d: dict[str, Any]) -> None:
            if d.get("job_id") == job_id and d.get("created_at") == created_at:
                mutate(d)

        self._rewrite(apply)

    def _rewrite(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        if not self.path.exists():
            return
        rows = self._load_dicts()
        for row in rows:
            mutate(row)
        self._write_dicts(rows)

    def _write_dicts(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
