"""Session store management — sessions.json index.

Mirrors OpenClaw's `src/config/sessions/store.ts`:
- load_session_store: read JSON, return dict
- update_session_store: write with lock (single-process simplified)
- prune / cap / rotate (omitted for nano simplicity)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import SessionInfo


def load_session_store(store_path: Path) -> dict[str, Any]:
    """Load sessions.json or return empty structure."""
    if not store_path.exists():
        return {"lastSessionId": None, "sessions": {}}
    data = json.loads(store_path.read_text(encoding="utf-8"))
    return {
        "lastSessionId": data.get("lastSessionId"),
        "sessions": data.get("sessions", {}),
    }


def save_session_store(store_path: Path, store: dict[str, Any]) -> None:
    """Write sessions.json atomically."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = store_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(store_path)


def get_last_session(store: dict[str, Any]) -> SessionInfo | None:
    """Return the last active session's metadata, or None."""
    last_id = store.get("lastSessionId")
    if not last_id:
        return None
    session_data = store.get("sessions", {}).get(last_id)
    if not session_data:
        return None
    return SessionInfo(
        session_id=last_id,
        created_at=session_data.get("created_at", 0),
        updated_at=session_data.get("updated_at", 0),
        model=session_data.get("model", ""),
        message_count=session_data.get("message_count", 0),
        compaction_count=session_data.get("compaction_count", 0),
    )


def update_session(
    store: dict[str, Any],
    session_id: str,
    *,
    model: str = "",
    message_count: int = 0,
    compaction_count: int = 0,
) -> None:
    """Add or update a session entry in the store."""
    import time
    now = time.time()
    sessions = store.setdefault("sessions", {})

    if session_id in sessions:
        entry = sessions[session_id]
        entry["updated_at"] = now
        if model:
            entry["model"] = model
        entry["message_count"] = message_count
        entry["compaction_count"] = compaction_count
    else:
        sessions[session_id] = {
            "created_at": now,
            "updated_at": now,
            "model": model,
            "message_count": message_count,
            "compaction_count": compaction_count,
        }

    store["lastSessionId"] = session_id


def list_sessions(store: dict[str, Any]) -> list[SessionInfo]:
    """Return all sessions sorted by updated_at (most recent first)."""
    sessions = store.get("sessions", {})
    result = []
    for sid, data in sessions.items():
        result.append(SessionInfo(
            session_id=sid,
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
            model=data.get("model", ""),
            message_count=data.get("message_count", 0),
            compaction_count=data.get("compaction_count", 0),
        ))
    result.sort(key=lambda s: s.updated_at, reverse=True)
    return result
