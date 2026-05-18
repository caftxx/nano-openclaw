"""Curator Lite: rule-based skill lifecycle maintenance.

This is the conservative first half of Hermes' curator idea. It does not call
an LLM and it never deletes skills. It only updates telemetry state and writes
reports, giving nano-openclaw an observable self-maintenance loop.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nano_openclaw.skills import usage


@dataclass
class CuratorConfig:
    stale_after_days: int = 30
    archive_after_days: int = 90


def _state_file(state_dir: str | Path) -> Path:
    return Path(state_dir) / "skills" / ".curator_state.json"


def _reports_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / "logs" / "curator"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_state(state_dir: str | Path) -> dict[str, Any]:
    path = _state_file(state_dir)
    if not path.exists():
        return {
            "enabled": True,
            "paused": False,
            "run_count": 0,
            "last_run_at": None,
            "last_run_summary": None,
            "last_report_path": None,
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    base = {
        "enabled": True,
        "paused": False,
        "run_count": 0,
        "last_run_at": None,
        "last_run_summary": None,
        "last_report_path": None,
    }
    if isinstance(raw, dict):
        base.update(raw)
    return base


def _save_state(state_dir: str | Path, state: dict[str, Any]) -> None:
    path = _state_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".curator-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
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


def status(state_dir: str | Path, cfg: CuratorConfig | None = None) -> dict[str, Any]:
    cfg = cfg or CuratorConfig()
    rows = usage.report(state_dir)
    state = _load_state(state_dir)
    counts: dict[str, int] = {"active": 0, "stale": 0, "archived": 0}
    for row in rows:
        counts[row.get("state", "active")] = counts.get(row.get("state", "active"), 0) + 1
    return {
        "configured": True,
        "enabled": bool(state.get("enabled", True)),
        "paused": bool(state.get("paused", False)),
        "run_count": int(state.get("run_count") or 0),
        "last_run_at": state.get("last_run_at"),
        "last_run_summary": state.get("last_run_summary"),
        "last_report_path": state.get("last_report_path"),
        "stale_after_days": cfg.stale_after_days,
        "archive_after_days": cfg.archive_after_days,
        "total": len(rows),
        "counts": counts,
        "least_recent": sorted(
            rows,
            key=lambda r: r.get("last_activity_at") or r.get("created_at") or "",
        )[:8],
    }


def set_enabled(state_dir: str | Path, enabled: bool) -> dict[str, Any]:
    state = _load_state(state_dir)
    state["enabled"] = bool(enabled)
    if enabled:
        state["paused"] = False
    _save_state(state_dir, state)
    return status(state_dir)


def set_paused(state_dir: str | Path, paused: bool) -> dict[str, Any]:
    state = _load_state(state_dir)
    state["paused"] = bool(paused)
    _save_state(state_dir, state)
    return status(state_dir)


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


def apply_transitions(
    state_dir: str | Path,
    cfg: CuratorConfig | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = cfg or CuratorConfig()
    now = _now()
    stale_cutoff = now - timedelta(days=cfg.stale_after_days)
    archive_cutoff = now - timedelta(days=cfg.archive_after_days)
    rows = usage.report(state_dir)
    changed: list[dict[str, Any]] = []
    counts = {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0}

    for row in rows:
        counts["checked"] += 1
        name = row["name"]
        current = row.get("state", usage.STATE_ACTIVE)
        if row.get("pinned"):
            continue
        anchor_raw = row.get("last_activity_at") or row.get("created_at")
        anchor = _parse_iso(anchor_raw)
        if anchor is None:
            continue
        target = current
        if anchor <= archive_cutoff:
            target = usage.STATE_ARCHIVED
        elif anchor <= stale_cutoff and current == usage.STATE_ACTIVE:
            target = usage.STATE_STALE
        elif anchor > stale_cutoff and current == usage.STATE_STALE:
            target = usage.STATE_ACTIVE

        if target == current:
            continue
        if target == usage.STATE_STALE:
            counts["marked_stale"] += 1
        elif target == usage.STATE_ARCHIVED:
            counts["archived"] += 1
        elif target == usage.STATE_ACTIVE:
            counts["reactivated"] += 1
        changed.append({
            "name": name,
            "from": current,
            "to": target,
            "last_activity_at": anchor_raw,
        })
        if not dry_run:
            usage.set_state(state_dir, name, target)

    return {"counts": counts, "changed": changed, "dry_run": dry_run}


def write_report(
    state_dir: str | Path,
    payload: dict[str, Any],
) -> Path:
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    root = _reports_dir(state_dir) / stamp
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "run.json"
    md_path = root / "REPORT.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Curator Lite Report",
        "",
        f"- dry_run: {payload.get('dry_run')}",
        f"- checked: {payload.get('counts', {}).get('checked', 0)}",
        f"- marked_stale: {payload.get('counts', {}).get('marked_stale', 0)}",
        f"- archived: {payload.get('counts', {}).get('archived', 0)}",
        f"- reactivated: {payload.get('counts', {}).get('reactivated', 0)}",
        "",
        "## Changes",
        "",
    ]
    changed = payload.get("changed") or []
    if changed:
        for item in changed:
            lines.append(
                f"- {item.get('name')}: {item.get('from')} -> {item.get('to')} "
                f"(last activity: {item.get('last_activity_at') or 'unknown'})"
            )
    else:
        lines.append("(none)")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def run(
    state_dir: str | Path,
    cfg: CuratorConfig | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = _load_state(state_dir)
    if not state.get("enabled", True):
        return {"skipped": True, "reason": "disabled", **status(state_dir, cfg)}
    if state.get("paused", False):
        return {"skipped": True, "reason": "paused", **status(state_dir, cfg)}

    result = apply_transitions(state_dir, cfg, dry_run=dry_run)
    report_path = write_report(state_dir, result)
    counts = result["counts"]
    summary = (
        f"checked={counts['checked']} stale={counts['marked_stale']} "
        f"archived={counts['archived']} reactivated={counts['reactivated']}"
    )
    state["last_run_summary"] = ("dry-run " if dry_run else "") + summary
    state["last_report_path"] = str(report_path)
    if not dry_run:
        state["last_run_at"] = _now().isoformat()
        state["run_count"] = int(state.get("run_count") or 0) + 1
    _save_state(state_dir, state)
    return {"skipped": False, "report_path": str(report_path), **result}


def restore_archived_skill(state_dir: str | Path, name: str) -> bool:
    """Restore a skill directory from the curator archive if present."""
    src = usage.archive_dir(state_dir) / name
    dst = Path(state_dir) / "skills" / name
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    usage.set_state(state_dir, name, usage.STATE_ACTIVE)
    return True
