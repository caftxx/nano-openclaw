"""Three-layer persistent store for cron jobs.

Layout under {stateDir}/cron/:
  jobs.json         – job definitions
  jobs-state.json   – runtime state per job
  runs/{jobId}.jsonl – per-job execution log (JSONL, newest last)
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from nano_openclaw.features.schedule.types import CronJob, CronJobState, CronRunRecord


class CronStore:
    def __init__(self, cron_dir: Path) -> None:
        self.cron_dir = cron_dir
        self.jobs_path = cron_dir / "jobs.json"
        self.state_path = cron_dir / "jobs-state.json"
        self.runs_dir = cron_dir / "runs"

    def _ensure_dirs(self) -> None:
        self.cron_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ── jobs.json ────────────────────────────────────────────────────────────

    def load_jobs(self) -> dict[str, CronJob]:
        if not self.jobs_path.exists():
            return {}
        try:
            raw: list[dict] = json.loads(self.jobs_path.read_text())
            return {d["id"]: _job_from_dict(d) for d in raw}
        except (json.JSONDecodeError, KeyError, OSError):
            return {}

    def save_jobs(self, jobs: dict[str, CronJob]) -> None:
        self._ensure_dirs()
        payload = json.dumps([asdict(j) for j in jobs.values()], indent=2)
        _atomic_write(self.jobs_path, payload)

    def add_job(self, job: CronJob) -> None:
        jobs = self.load_jobs()
        jobs[job.id] = job
        self.save_jobs(jobs)

    def remove_job(self, job_id: str) -> bool:
        jobs = self.load_jobs()
        if job_id not in jobs:
            matches = [k for k in jobs if k.startswith(job_id)]
            if len(matches) != 1:
                return False
            job_id = matches[0]
        del jobs[job_id]
        self.save_jobs(jobs)
        return True

    # ── jobs-state.json ──────────────────────────────────────────────────────

    def load_state(self) -> dict[str, CronJobState]:
        if not self.state_path.exists():
            return {}
        try:
            raw: dict[str, dict] = json.loads(self.state_path.read_text())
            return {k: _state_from_dict(v) for k, v in raw.items()}
        except (json.JSONDecodeError, KeyError, OSError):
            return {}

    def save_state(self, states: dict[str, CronJobState]) -> None:
        self._ensure_dirs()
        payload = json.dumps({k: asdict(v) for k, v in states.items()}, indent=2)
        _atomic_write(self.state_path, payload)

    def update_state(self, state: CronJobState) -> None:
        states = self.load_state()
        states[state.job_id] = state
        self.save_state(states)

    def remove_state(self, job_id: str) -> None:
        states = self.load_state()
        if job_id not in states:
            matches = [k for k in states if k.startswith(job_id)]
            if len(matches) == 1:
                job_id = matches[0]
        states.pop(job_id, None)
        self.save_state(states)

    # ── runs/{jobId}.jsonl ───────────────────────────────────────────────────

    def append_run(self, record: CronRunRecord) -> None:
        self._ensure_dirs()
        path = self.runs_dir / f"{record.job_id}.jsonl"
        line = json.dumps(asdict(record)) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    def get_runs(self, job_id: str, limit: int = 20) -> list[CronRunRecord]:
        path = self.runs_dir / f"{job_id}.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            records = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        records.append(_run_from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        pass
            return records[-limit:]
        except OSError:
            return []


# ── Deserialization helpers ───────────────────────────────────────────────────

def _job_from_dict(d: dict) -> CronJob:
    return CronJob(
        id=d["id"],
        name=d["name"],
        expression=d.get("expression", ""),
        prompt=d["prompt"],
        enabled=d.get("enabled", True),
        created_at=d.get("created_at", ""),
        one_shot=d.get("one_shot", False),
        fire_at_ms=d.get("fire_at_ms"),
        created_by=d.get("created_by", ""),
        notify_wechat=d.get("notify_wechat", False),
        notify_on_success=d.get("notify_on_success", True),
        notify_on_error=d.get("notify_on_error", True),
    )


def _state_from_dict(d: dict) -> CronJobState:
    return CronJobState(
        job_id=d["job_id"],
        next_run_at_ms=d.get("next_run_at_ms"),
        running_at_ms=d.get("running_at_ms"),
        last_run_at_ms=d.get("last_run_at_ms"),
        last_status=d.get("last_status"),
        last_error=d.get("last_error"),
        consecutive_errors=d.get("consecutive_errors", 0),
    )


def _run_from_dict(d: dict) -> CronRunRecord:
    return CronRunRecord(
        job_id=d["job_id"],
        run_id=d["run_id"],
        started_at=d.get("started_at", ""),
        ended_at=d.get("ended_at"),
        status=d.get("status", "ok"),
        error=d.get("error"),
        elapsed_ms=d.get("elapsed_ms"),
    )


# ── Utility ──────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via a temp file and rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def new_job_id() -> str:
    return str(uuid.uuid4())
