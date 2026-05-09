"""Type definitions for the cron schedule module."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CronJob:
    """A scheduled cron job definition (stored in jobs.json, no runtime state)."""

    id: str
    name: str
    expression: str       # cron expression "minute hour * * *"; empty for one-shot
    prompt: str
    enabled: bool
    created_at: str       # ISO datetime string
    one_shot: bool = False
    fire_at_ms: int | None = None  # one-shot absolute fire time in epoch-ms
    # 通知相关字段
    created_by: str = ""  # 创建者标识: "cli" / "webui:session-id" / "wechat:uid"
    notify_wechat: bool = False
    notify_on_success: bool = True
    notify_on_error: bool = True


@dataclass
class CronJobState:
    """Runtime state for one cron job (stored in jobs-state.json)."""

    job_id: str
    next_run_at_ms: int | None = None
    running_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: str | None = None   # "ok" | "error" | "interrupted"
    last_error: str | None = None
    consecutive_errors: int = 0


@dataclass
class CronRunRecord:
    """One execution log entry (appended to runs/{jobId}.jsonl)."""

    job_id: str
    run_id: str
    started_at: str       # ISO datetime
    ended_at: str | None
    status: str           # "ok" | "error" | "interrupted"
    error: str | None
    elapsed_ms: int | None
