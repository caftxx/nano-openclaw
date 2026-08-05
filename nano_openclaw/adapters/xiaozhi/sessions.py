"""Stable Device-Id to nano session mapping."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from nano_openclaw.logger import get_logger


log = get_logger(__name__)


class DeviceSessionStore:
    def __init__(self, path: Path, backend: Any, *, idle_minutes: int = 360) -> None:
        self.path = path
        self.backend = backend
        self.idle_minutes = idle_minutes
        self._lock = threading.RLock()

    def resolve(self, device_id: str) -> str:
        key = device_id.strip().lower()
        if not key:
            raise ValueError("Device-Id is required")
        with self._lock:
            mapping = self._load()
            session_id = str(mapping.get(key) or "")
            if session_id:
                try:
                    session = self.backend.manager.get_or_load(session_id)
                    if not self.backend.manager.is_idle(session, self.idle_minutes):
                        self.backend.manager.mark_interaction(session)
                        return session.session_id
                    log.info(
                        "xiaozhi.session.idle_rollover",
                        f"device={key} old_session={session.session_id} idle_minutes={self.idle_minutes}",
                    )
                except KeyError:
                    pass
            session = self.backend.manager.create()
            self.backend.manager.mark_interaction(session)
            session_id = session.session_id
            mapping[key] = session_id
            self._save(mapping)
            return session_id

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self, mapping: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
