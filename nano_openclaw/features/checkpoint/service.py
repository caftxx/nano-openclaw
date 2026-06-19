"""Lightweight filesystem checkpoints for nano-openclaw.

This intentionally starts simpler than Hermes' shared git object store. It
creates directory snapshots under {state_dir}/checkpoints/snapshots and can
restore one snapshot back into the workspace. The implementation is small,
auditable, and good enough to protect early self-maintenance work.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".nano-openclaw",
    ".nano-openclaw-dev",
    "dist",
    "build",
    "target",
}


@dataclass
class Checkpoint:
    id: str
    created_at: str
    workspace_dir: str
    reason: str
    path: str


def _root(state_dir: str | Path) -> Path:
    return Path(state_dir) / "checkpoints" / "snapshots"


def _ignore(dir_path: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in DEFAULT_EXCLUDES}
    ignored.update(name for name in names if name.endswith(".pyc") or name.endswith(".log"))
    return ignored


def create_checkpoint(
    state_dir: str | Path | None,
    workspace_dir: str | Path | None,
    *,
    reason: str = "manual",
) -> Checkpoint | None:
    if not state_dir or not workspace_dir:
        return None
    workspace = Path(workspace_dir).resolve()
    if not workspace.exists() or not workspace.is_dir():
        return None
    cp_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    dest = _root(state_dir) / cp_id / "workspace"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(workspace, dest, ignore=_ignore, symlinks=False)
    cp = Checkpoint(
        id=cp_id,
        created_at=datetime.now().isoformat(),
        workspace_dir=str(workspace),
        reason=reason,
        path=str(dest.parent),
    )
    (dest.parent / "meta.json").write_text(
        json.dumps(cp.__dict__, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return cp


def list_checkpoints(state_dir: str | Path | None) -> list[Checkpoint]:
    if not state_dir:
        return []
    root = _root(state_dir)
    if not root.exists():
        return []
    out: list[Checkpoint] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        meta = entry / "meta.json"
        if not meta.exists():
            continue
        try:
            raw = json.loads(meta.read_text(encoding="utf-8"))
            out.append(Checkpoint(**raw))
        except Exception:
            continue
    return out


def restore_checkpoint(
    state_dir: str | Path | None,
    checkpoint_id: str,
    *,
    workspace_dir: str | Path | None = None,
    create_pre_restore: bool = True,
) -> Checkpoint | None:
    if not state_dir or not checkpoint_id:
        return None
    matches = [cp for cp in list_checkpoints(state_dir) if cp.id.startswith(checkpoint_id)]
    if len(matches) != 1:
        return None
    cp = matches[0]
    target = Path(workspace_dir or cp.workspace_dir).resolve()
    source = Path(cp.path) / "workspace"
    if not source.exists():
        return None
    if create_pre_restore:
        create_checkpoint(state_dir, target, reason=f"pre-restore:{cp.id}")

    tmp = target.parent / f".{target.name}.restore-{uuid.uuid4().hex[:8]}"
    shutil.copytree(source, tmp, ignore=_ignore, symlinks=False)
    backup = target.parent / f".{target.name}.restore-backup-{int(time.time())}"
    if target.exists():
        target.rename(backup)
    tmp.rename(target)
    shutil.rmtree(backup, ignore_errors=True)
    return cp
