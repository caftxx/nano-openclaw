"""Notification queue for WeChat push notifications.

Stores pending notifications in notify-queue.jsonl. Each item has a target_uid
for directed delivery to the job creator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


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

    def get_pending(self, limit: int = 10) -> list[NotifyItem]:
        """Get pending (unsent) notifications, up to `limit`."""
        if not self.path.exists():
            return []
        items: list[NotifyItem] = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not d.get("sent"):
                    items.append(NotifyItem(
                        job_id=d.get("job_id", ""),
                        job_name=d.get("job_name", ""),
                        status=d.get("status", "ok"),
                        result_summary=d.get("result_summary", ""),
                        created_at=d.get("created_at", ""),
                        target_uid=d.get("target_uid", ""),
                        sent=d.get("sent", False),
                    ))
            except (json.JSONDecodeError, KeyError):
                pass
        return items[:limit]

    def mark_sent(self, job_id: str, created_at: str) -> None:
        """Mark a notification as sent by rewriting the file."""
        if not self.path.exists():
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        updated: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("job_id") == job_id and d.get("created_at") == created_at:
                    d["sent"] = True
                updated.append(json.dumps(d) + "\n")
            except (json.JSONDecodeError, KeyError):
                pass
        self.path.write_text("".join(updated), encoding="utf-8")

    def purge_sent(self, keep_recent: int = 100) -> None:
        """Remove sent notifications, keep recent N for audit."""
        if not self.path.exists():
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not d.get("sent"):
                    kept.append(json.dumps(d) + "\n")
            except (json.JSONDecodeError, KeyError):
                pass
        # Keep recent sent for audit (optional)
        if len(kept) > keep_recent:
            kept = kept[-keep_recent:]
        self.path.write_text("".join(kept), encoding="utf-8")