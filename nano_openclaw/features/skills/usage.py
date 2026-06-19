"""Skill usage telemetry for nano-openclaw.

The curator needs evidence before it can maintain skills safely. This module
keeps that evidence in a small sidecar JSON file under the active state dir:

    {state_dir}/skills/.usage.json

The file is operational telemetry, not skill content. Failures are best-effort
and must never break a user turn.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}


def _skills_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / "skills"


def usage_file(state_dir: str | Path) -> Path:
    return _skills_dir(state_dir) / ".usage.json"


def archive_dir(state_dir: str | Path) -> Path:
    return _skills_dir(state_dir) / ".archive"


@contextmanager
def _locked_usage_file(state_dir: str | Path):
    """Serialize read-modify-write cycles on Unix; degrade gracefully elsewhere."""
    path = usage_file(state_dir)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fcntl  # type: ignore
    except Exception:  # pragma: no cover - Windows / restricted platforms
        yield
        return

    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_usage(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    path = usage_file(state_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): v
        for k, v in raw.items()
        if isinstance(v, dict)
    }


def save_usage(state_dir: str | Path, data: dict[str, dict[str, Any]]) -> None:
    path = usage_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_record(name: str, *, source: str = "", path: str = "") -> dict[str, Any]:
    now = _now_iso()
    return {
        "name": name,
        "source": source,
        "path": path,
        "state": STATE_ACTIVE,
        "pinned": False,
        "agent_created": source in {"workspace", "agents-project", "managed"},
        "created_at": now,
        "last_loaded_at": None,
        "last_used_at": None,
        "last_viewed_at": None,
        "last_patched_at": None,
        "load_count": 0,
        "use_count": 0,
        "view_count": 0,
        "patch_count": 0,
    }


def ensure_record(
    state_dir: str | Path,
    name: str,
    *,
    source: str = "",
    path: str = "",
) -> dict[str, Any]:
    with _locked_usage_file(state_dir):
        data = load_usage(state_dir)
        rec = data.get(name) or _default_record(name, source=source, path=path)
        rec["name"] = name
        if source:
            rec["source"] = source
        if path:
            rec["path"] = path
        data[name] = rec
        save_usage(state_dir, data)
        return dict(rec)


def record_event(
    state_dir: str | Path | None,
    name: str,
    event: str,
    *,
    source: str = "",
    path: str = "",
) -> None:
    """Bump a usage counter. Best-effort by design."""
    if not state_dir or not name:
        return
    try:
        with _locked_usage_file(state_dir):
            data = load_usage(state_dir)
            rec = data.get(name) or _default_record(name, source=source, path=path)
            if source:
                rec["source"] = source
            if path:
                rec["path"] = path
            now = _now_iso()
            if event == "load":
                rec["load_count"] = int(rec.get("load_count") or 0) + 1
                rec["last_loaded_at"] = now
            elif event == "use":
                rec["use_count"] = int(rec.get("use_count") or 0) + 1
                rec["last_used_at"] = now
                if rec.get("state") == STATE_STALE:
                    rec["state"] = STATE_ACTIVE
            elif event == "view":
                rec["view_count"] = int(rec.get("view_count") or 0) + 1
                rec["last_viewed_at"] = now
            elif event == "patch":
                rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
                rec["last_patched_at"] = now
                if rec.get("state") == STATE_STALE:
                    rec["state"] = STATE_ACTIVE
            data[name] = rec
            save_usage(state_dir, data)
    except Exception:
        return


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def latest_activity_at(record: dict[str, Any]) -> str | None:
    latest_dt: datetime | None = None
    latest_raw: str | None = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at", "last_loaded_at"):
        raw = record.get(key)
        dt = _parse_iso(raw)
        if dt is not None and (latest_dt is None or dt > latest_dt):
            latest_dt = dt
            latest_raw = str(raw)
    return latest_raw


def activity_count(record: dict[str, Any]) -> int:
    total = 0
    for key in ("use_count", "view_count", "patch_count", "load_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def report(state_dir: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, rec in load_usage(state_dir).items():
        row = dict(rec)
        row["name"] = name
        row["state"] = row.get("state") if row.get("state") in VALID_STATES else STATE_ACTIVE
        row["activity_count"] = activity_count(row)
        row["last_activity_at"] = latest_activity_at(row)
        rows.append(row)
    rows.sort(key=lambda r: (r.get("state", ""), r.get("name", "")))
    return rows


def set_state(state_dir: str | Path, name: str, state: str) -> bool:
    if state not in VALID_STATES:
        return False
    with _locked_usage_file(state_dir):
        data = load_usage(state_dir)
        if name not in data:
            return False
        data[name]["state"] = state
        save_usage(state_dir, data)
    return True


def set_pinned(state_dir: str | Path, name: str, pinned: bool) -> bool:
    with _locked_usage_file(state_dir):
        data = load_usage(state_dir)
        if name not in data:
            return False
        data[name]["pinned"] = bool(pinned)
        save_usage(state_dir, data)
    return True
