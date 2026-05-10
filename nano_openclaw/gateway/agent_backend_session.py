"""In-memory session entity owned by the Backend.

This is the per-conversation handle the Backend hands out: history, transcript
writer, activity log, and a per-session lock that ``EmbeddedBackend.chat_send``
holds across one ``run_turn`` to keep two concurrent turns from racing on
``history``.

Originally lived in ``webui/sessions.py``; promoted here in Phase 0 so the
TUI, WebUI, and (future) WebSocket clients share one session entity.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nano_openclaw.loop import Message
from nano_openclaw.session import (
    TranscriptReader,
    TranscriptWriter,
    list_sessions,
    load_session_store,
    new_session_id,
    save_session_store,
    update_session,
)


def message_to_json(message: Message) -> dict[str, Any]:
    return {"role": message.role, "content": message.content}


def message_text(message: Message) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(part for part in parts if part)


def is_subagent_announcement(message: Message) -> bool:
    if message.role != "user":
        return False
    text = message_text(message).strip()
    return text.startswith("<subagent_completion") and text.endswith("</subagent_completion>")


def display_history(history: list[Message]) -> list[Message]:
    return [message for message in history if not is_subagent_announcement(message)]


def session_title(history: list[Message], fallback: str) -> str:
    visible_history = display_history(history)
    for message in visible_history:
        if message.role != "user":
            continue
        text = _one_line(message_text(message))
        if text:
            return _truncate(text, 42)
    for message in visible_history:
        text = _one_line(message_text(message))
        if text:
            return _truncate(text, 42)
    return fallback


def session_preview(history: list[Message]) -> str:
    for message in reversed(display_history(history)):
        text = _one_line(message_text(message))
        if text:
            return _truncate(text, 96)
    return ""


def session_search_text(history: list[Message]) -> str:
    parts = [_one_line(message_text(message)) for message in display_history(history)]
    return "\n".join(part for part in parts if part)[:6000].lower()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class AgentBackendSession:
    """One conversation. Hold ``lock`` across a turn to keep ``history`` safe."""

    session_id: str
    transcript_path: Path
    history: list[Message]
    writer: TranscriptWriter
    activities: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_turn_id: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class SessionSummary:
    history: list[Message]
    activities: list[dict[str, Any]]
    msg_count: int
    comp_count: int
    mtime_ns: int


class BackendSessionManager:
    """Loads / lists / persists ``AgentBackendSession`` instances on top of the on-disk store."""

    def __init__(self, *, session_dir: Path, store_path: Path, model: str, cwd: str = "") -> None:
        self.session_dir = session_dir
        self.store_path = store_path
        self.model = model
        self.cwd = cwd
        self._loaded: dict[str, AgentBackendSession] = {}
        self._summary_cache: dict[str, SessionSummary] = {}
        self._store_lock = threading.RLock()
        # Tracks sessions created in memory but not yet written to disk.
        # Each id is cleared independently once that session's first message
        # is persisted, so multiple new WebUI sessions can coexist safely.
        self._pending_session_ids: set[str] = set()
        self._pending_session_order: list[str] = []

    def list(self) -> list[dict[str, Any]]:
        store = load_session_store(self.store_path)
        last_id = store.get("lastSessionId")
        result = []
        for item in list_sessions(store):
            path = self.session_dir / f"{item.session_id}.jsonl"
            if item.session_id not in self._loaded and not path.exists():
                continue
            history, actual_msg_count, actual_comp_count, stored_summary = self._summary_for_list(store, item.session_id)
            if actual_msg_count == 0:
                continue
            loaded_session = self._loaded.get(item.session_id)
            result.append({
                "session_id": item.session_id,
                "title": stored_summary.get("title") or session_title(history, item.session_id[:8]),
                "preview": stored_summary.get("preview") or session_preview(history),
                "search_text": stored_summary.get("search_text") or session_search_text(history),
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "model": item.model,
                "message_count": actual_msg_count,
                "compaction_count": actual_comp_count,
                "current": item.session_id == last_id,
                "active_turn_id": loaded_session.active_turn_id if loaded_session else None,
            })
        result.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return result

    def create(self) -> AgentBackendSession:
        session_id = new_session_id()
        path = self.session_dir / f"{session_id}.jsonl"
        writer = TranscriptWriter(path)
        writer.start(model=self.model, cwd=self.cwd, session_id=session_id)
        session = AgentBackendSession(session_id=session_id, transcript_path=path, history=[], writer=writer)
        self._loaded[session_id] = session
        self._summary_cache.pop(session_id, None)
        self._mark_pending(session_id)
        # Persist to sessions.json only when the first message is written.
        def _on_first_write() -> None:
            self._unmark_pending(session_id)
            self.save_metadata(session)
        writer._on_first_write = _on_first_write
        return session

    def get_or_load(self, session_id: str | None = None) -> AgentBackendSession:
        if not session_id:
            # Return the most recently created in-memory pending session before
            # consulting the store. Explicit session_id lookups still bypass
            # this and load/select the requested session.
            pending = self._latest_pending_session()
            if pending is not None:
                return pending
            store = load_session_store(self.store_path)
            session_id = store.get("lastSessionId")
            if not session_id:
                return self.create()
            if session_id not in self._loaded and self._load_existing(session_id) is None:
                for item in list_sessions(store):
                    if item.session_id in self._loaded or self._load_existing(item.session_id) is not None:
                        session_id = item.session_id
                        break
                else:
                    return self.create()
        if session_id in self._loaded:
            return self._loaded[session_id]

        loaded = self._load_existing(session_id)
        if loaded is None:
            raise KeyError(f"session not found or transcript is invalid: {session_id}")
        canonical_id, path, history, activities, msg_count, comp_count, last_msg_id = loaded
        writer = TranscriptWriter.resume(path, canonical_id, msg_count, comp_count, last_msg_id)
        session = AgentBackendSession(
            session_id=canonical_id,
            transcript_path=path,
            history=history,
            writer=writer,
            activities=activities,
        )
        self._loaded[canonical_id] = session
        self.save_metadata(session, update_time=False)
        return session

    def select(self, session_id: str) -> AgentBackendSession:
        session = self.get_or_load(session_id)
        # Abandon other pending sessions only when they are truly blank and not
        # running. A pending session that has an active turn will persist itself
        # as soon as its first user message is written.
        self._prune_blank_pending_sessions(except_session_id=session.session_id)
        if self._is_persisted(session):
            self.save_metadata(session, update_time=False)
        return session

    async def clear(self, session_id: str) -> AgentBackendSession:
        session = self.get_or_load(session_id)
        if session.active_turn_id:
            raise RuntimeError("cannot clear a session while a turn is running")
        async with session.lock:
            session.history.clear()
            session.activities.clear()
            session.writer.clear()
            self._summary_cache.pop(session.session_id, None)
            self.save_metadata(session)
        return session

    def save_metadata(self, session: AgentBackendSession, *, update_time: bool = True) -> None:
        if not self._is_persisted(session):
            return
        self._unmark_pending(session.session_id)
        session.writer._on_first_write = None
        with self._store_lock:
            store = load_session_store(self.store_path)
            existed = session.session_id in store.get("sessions", {})
            update_session(
                store,
                session.session_id,
                model=self.model,
                message_count=session.writer.message_count,
                compaction_count=session.writer.compaction_count,
                update_time=update_time,
            )
            if not existed:
                store["sessions"][session.session_id]["created_at"] = session.created_at
            store["sessions"][session.session_id]["webui_summary"] = _session_summary_metadata(
                session.history,
                fallback=session.session_id[:8],
            )
            save_session_store(self.store_path, store)
        self._summary_cache.pop(session.session_id, None)

    def _mark_pending(self, session_id: str) -> None:
        self._pending_session_ids.add(session_id)
        self._pending_session_order = [sid for sid in self._pending_session_order if sid != session_id]
        self._pending_session_order.append(session_id)

    def _unmark_pending(self, session_id: str) -> None:
        self._pending_session_ids.discard(session_id)
        self._pending_session_order = [sid for sid in self._pending_session_order if sid != session_id]

    def _latest_pending_session(self) -> AgentBackendSession | None:
        for session_id in reversed(self._pending_session_order):
            if session_id not in self._pending_session_ids:
                continue
            session = self._loaded.get(session_id)
            if session is not None:
                return session
        return None

    def _is_persisted(self, session: AgentBackendSession) -> bool:
        return session.transcript_path.exists()

    def _prune_blank_pending_sessions(self, *, except_session_id: str) -> None:
        for session_id in list(self._pending_session_order):
            if session_id == except_session_id or session_id not in self._pending_session_ids:
                continue
            session = self._loaded.get(session_id)
            if session is None:
                self._unmark_pending(session_id)
                continue
            if session.active_turn_id or self._is_persisted(session):
                continue
            self._loaded.pop(session_id, None)
            self._unmark_pending(session_id)

    def history_json(self, session: AgentBackendSession) -> list[dict[str, Any]]:
        return [message_to_json(message) for message in display_history(session.history)]

    def activity_json(self, session: AgentBackendSession) -> list[dict[str, Any]]:
        return [_jsonable_activity(activity) for activity in session.activities]

    def _summary_for_list(self, store: dict[str, Any], session_id: str) -> tuple[list[Message], int, int, dict[str, Any]]:
        entry = store.get("sessions", {}).get(session_id, {})
        stored_summary = entry.get("webui_summary") if isinstance(entry, dict) else None
        if isinstance(stored_summary, dict):
            loaded_session = self._loaded.get(session_id)
            if loaded_session:
                return (
                    [],
                    loaded_session.writer.message_count,
                    loaded_session.writer.compaction_count,
                    stored_summary,
                )
            return (
                [],
                int(entry.get("message_count", 0) or 0),
                int(entry.get("compaction_count", 0) or 0),
                stored_summary,
            )
        history, msg_count, comp_count = self._summary_history_and_counts(session_id)
        return history, msg_count, comp_count, {}

    def _summary_history_and_counts(self, session_id: str) -> tuple[list[Message], int, int]:
        if session_id in self._loaded:
            session = self._loaded[session_id]
            return session.history, session.writer.message_count, session.writer.compaction_count
        path = self.session_dir / f"{session_id}.jsonl"
        summary = self._cached_summary(path)
        if summary is None:
            return [], 0, 0
        return summary.history, summary.msg_count, summary.comp_count

    def _load_existing(
        self,
        session_id: str,
    ) -> tuple[str, Path, list[Message], list[dict[str, Any]], int, int, str] | None:
        direct_path = self.session_dir / f"{session_id}.jsonl"
        direct = self._read_transcript(direct_path)
        if direct is not None:
            history, activities, _header_id, msg_count, comp_count, last_msg_id = direct
            return session_id, direct_path, history, activities, msg_count, comp_count, last_msg_id

        # Compatibility for earlier WebUI builds that accidentally wrote
        # transcript header IDs into sessions.json. Those IDs have no matching
        # file, but can be resolved by scanning transcript headers.
        for path in self.session_dir.glob("*.jsonl"):
            loaded = self._read_transcript(path)
            if loaded is None:
                continue
            history, activities, header_id, msg_count, comp_count, last_msg_id = loaded
            if header_id == session_id:
                return path.stem, path, history, activities, msg_count, comp_count, last_msg_id
        return None

    def _read_transcript(self, path: Path) -> tuple[list[Message], list[dict[str, Any]], str, int, int, str] | None:
        history, loaded_id, msg_count, comp_count, last_msg_id = TranscriptReader(path).load_history()
        if not loaded_id:
            return None
        return history, _read_activity_entries(path), loaded_id, msg_count, comp_count, last_msg_id

    def _cached_summary(self, path: Path) -> SessionSummary | None:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._summary_cache.get(path.stem)
        if cached and cached.mtime_ns == mtime_ns:
            return cached
        loaded = self._read_transcript(path)
        if loaded is None:
            self._summary_cache.pop(path.stem, None)
            return None
        history, activities, _loaded_id, msg_count, comp_count, _last_msg_id = loaded
        summary = SessionSummary(
            history=history,
            activities=activities,
            msg_count=msg_count,
            comp_count=comp_count,
            mtime_ns=mtime_ns,
        )
        self._summary_cache[path.stem] = summary
        return summary


def _read_activity_entries(path: Path) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    if not path.exists():
        return activities
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "activity":
                activities.append({k: v for k, v in entry.items() if k != "type"})
    return activities


def _jsonable_activity(activity: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(activity, ensure_ascii=False, default=str))


def _session_summary_metadata(history: list[Message], *, fallback: str) -> dict[str, str]:
    return {
        "title": session_title(history, fallback),
        "preview": session_preview(history),
        "search_text": session_search_text(history),
    }
