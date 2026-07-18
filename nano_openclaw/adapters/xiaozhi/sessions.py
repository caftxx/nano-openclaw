"""Stable Device-Id to nano session mapping."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class DeviceSessionStore:
    def __init__(self, path: Path, backend: Any) -> None:
        self.path = path
        self.backend = backend
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
                    return self.backend.manager.get_or_load(session_id).session_id
                except KeyError:
                    pass
            session_id = self.backend.manager.create().session_id
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
