"""Cron schedule tools: cron_create, cron_delete, cron_list, schedule_wakeup."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nano_openclaw.schedule.store import CronStore, new_job_id
from nano_openclaw.schedule.types import CronJob, CronJobState
from nano_openclaw.tools import Tool


def build_cron_tools(cron_dir: Path) -> list[Tool]:
    store = CronStore(cron_dir)
    return [
        _cron_create_tool(store),
        _cron_delete_tool(store),
        _cron_list_tool(store),
        _schedule_wakeup_tool(store),
    ]


# ── cron_create ───────────────────────────────────────────────────────────────

def _cron_create_tool(store: CronStore) -> Tool:
    def run(args: dict[str, Any]) -> str:
        name = args.get("name", "").strip()
        expression = args.get("schedule", "").strip()
        prompt = args.get("prompt", "").strip()
        enabled = bool(args.get("enabled", True))
        created_by = args.get("created_by", "cli").strip()
        notify_wechat = bool(args.get("notify_wechat", False))
        notify_on_success = bool(args.get("notify_on_success", True))
        notify_on_error = bool(args.get("notify_on_error", True))

        if not name:
            return "Error: 'name' is required"
        if not expression:
            return "Error: 'schedule' is required"
        if not prompt:
            return "Error: 'prompt' is required"

        # Validate cron expression
        from nano_openclaw.memory.dreaming import _next_cron_occurrence
        nxt = _next_cron_occurrence(expression, datetime.now())
        if nxt is None:
            return (
                f"Error: invalid or unsupported cron expression '{expression}'. "
                "Use 'minute hour * * *' format (e.g. '0 9 * * *', '*/30 * * * *')."
            )

        job_id = new_job_id()
        job = CronJob(
            id=job_id,
            name=name,
            expression=expression,
            prompt=prompt,
            enabled=enabled,
            created_at=datetime.now().isoformat(),
            created_by=created_by,
            notify_wechat=notify_wechat,
            notify_on_success=notify_on_success,
            notify_on_error=notify_on_error,
        )
        store.add_job(job)

        # Write initial state with nextRunAtMs so scheduler picks it up
        state = CronJobState(job_id=job_id, next_run_at_ms=int(nxt.timestamp() * 1000))
        store.update_state(state)

        next_run_str = nxt.strftime("%Y-%m-%d %H:%M")
        notify_str = ""
        if notify_wechat:
            notify_str = f"\nWeChat通知: 启用 (成功: {notify_on_success}, 失败: {notify_on_error})"
        return (
            f"Cron job created.\n"
            f"ID: {job_id}\n"
            f"Name: {name}\n"
            f"Schedule: {expression}\n"
            f"Next run: {next_run_str}"
            f"{notify_str}"
        )

    return Tool(
        name="cron_create",
        description=(
            "Create a recurring scheduled task. The task runs the given prompt as a "
            "background agent on the specified cron schedule. "
            "Supports 'minute hour * * *' format (day/month/weekday must be '*'). "
            "Examples: '0 9 * * *' (daily 9am), '*/30 * * * *' (every 30 min), "
            "'0 */6 * * *' (every 6 hours). "
            "Use created_by to bind the job to a specific user for notification routing. "
            "Use notify_wechat to enable WeChat push notification on completion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short display name for the job"},
                "schedule": {
                    "type": "string",
                    "description": "Cron expression in 'minute hour * * *' format",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task prompt the agent will run each time",
                },
                "enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether the job is active",
                },
                "created_by": {
                    "type": "string",
                    "default": "cli",
                    "description": "Creator identifier for notification routing (e.g. 'wechat:uid')",
                },
                "notify_wechat": {
                    "type": "boolean",
                    "default": False,
                    "description": "Send WeChat notification on job completion",
                },
                "notify_on_success": {
                    "type": "boolean",
                    "default": True,
                    "description": "Notify on successful execution",
                },
                "notify_on_error": {
                    "type": "boolean",
                    "default": True,
                    "description": "Notify on execution error",
                },
            },
            "required": ["name", "schedule", "prompt"],
        },
        run=run,
    )


# ── cron_delete ───────────────────────────────────────────────────────────────

def _cron_delete_tool(store: CronStore) -> Tool:
    def run(args: dict[str, Any]) -> str:
        job_id = args.get("job_id", "").strip()
        if not job_id:
            return "Error: 'job_id' is required"

        removed = store.remove_job(job_id)
        if not removed:
            return f"Error: no job with ID '{job_id}'"

        store.remove_state(job_id)
        return f"Cron job {job_id} deleted."

    return Tool(
        name="cron_delete",
        description="Delete a scheduled cron job by ID. Use cron_list to find IDs.",
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID from cron_list"},
            },
            "required": ["job_id"],
        },
        run=run,
    )


# ── cron_list ─────────────────────────────────────────────────────────────────

def _cron_list_tool(store: CronStore) -> Tool:
    def run(args: dict[str, Any]) -> str:
        jobs = store.load_jobs()
        states = store.load_state()

        if not jobs:
            return "No cron jobs scheduled."

        lines = [f"{'ID'[:8]:<8}  {'Name':<20}  {'Schedule':<16}  {'Status':<12}  Next Run"]
        lines.append("-" * 80)

        for job in sorted(jobs.values(), key=lambda j: j.created_at):
            state = states.get(job.id)
            status = state.last_status or "never run" if state else "no state"
            if state and state.running_at_ms:
                status = "running"
            if not job.enabled:
                status = "disabled"

            next_run = "—"
            if state and state.next_run_at_ms:
                nxt_dt = datetime.fromtimestamp(state.next_run_at_ms / 1000)
                next_run = nxt_dt.strftime("%m-%d %H:%M")

            schedule_label = "one-shot" if job.one_shot else job.expression
            lines.append(
                f"{job.id[:8]:<8}  {job.name[:20]:<20}  {schedule_label[:16]:<16}  "
                f"{status[:12]:<12}  {next_run}"
            )
            if state and state.consecutive_errors > 0:
                lines.append(f"          ⚠ {state.consecutive_errors} consecutive error(s): {state.last_error or ''}")

        return "\n".join(lines)

    return Tool(
        name="cron_list",
        description="List all scheduled cron jobs with their status and next run time.",
        input_schema={"type": "object", "properties": {}},
        run=run,
    )


# ── schedule_wakeup ───────────────────────────────────────────────────────────

def _schedule_wakeup_tool(store: CronStore) -> Tool:
    def run(args: dict[str, Any]) -> str:
        delay_seconds = args.get("delay_seconds")
        prompt = args.get("prompt", "").strip()
        reason = args.get("reason", "").strip()
        created_by = args.get("created_by", "cli").strip()
        notify_wechat = bool(args.get("notify_wechat", False))
        notify_on_success = bool(args.get("notify_on_success", True))
        notify_on_error = bool(args.get("notify_on_error", True))

        if delay_seconds is None:
            return "Error: 'delay_seconds' is required"
        try:
            delay_seconds = int(delay_seconds)
        except (TypeError, ValueError):
            return "Error: 'delay_seconds' must be an integer"
        if delay_seconds < 60:
            return "Error: minimum delay is 60 seconds (cron scheduler polls every 60s)"
        if not prompt:
            return "Error: 'prompt' is required"

        fire_at_ms = int((time.time() + delay_seconds) * 1000)
        job_id = new_job_id()
        name = reason[:40] if reason else f"wakeup-{job_id[:8]}"

        job = CronJob(
            id=job_id,
            name=name,
            expression="",
            prompt=prompt,
            enabled=True,
            created_at=datetime.now().isoformat(),
            one_shot=True,
            fire_at_ms=fire_at_ms,
            created_by=created_by,
            notify_wechat=notify_wechat,
            notify_on_success=notify_on_success,
            notify_on_error=notify_on_error,
        )
        store.add_job(job)

        state = CronJobState(job_id=job_id, next_run_at_ms=fire_at_ms)
        store.update_state(state)

        fire_dt = datetime.fromtimestamp(fire_at_ms / 1000)
        notify_str = ""
        if notify_wechat:
            notify_str = f"\nWeChat通知: 启用"
        return (
            f"One-shot task scheduled.\n"
            f"ID: {job_id}\n"
            f"Fire at: {fire_dt.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(in ~{delay_seconds}s)"
            f"{notify_str}"
        )

    return Tool(
        name="schedule_wakeup",
        description=(
            "Schedule a one-shot task to run after a delay. "
            "The prompt runs once as a background agent, then the job is removed. "
            "Minimum delay is 60 seconds. Useful for self-pacing within a /loop "
            "or deferring work to a future time. "
            "Use created_by to bind the job to a specific user for notification routing. "
            "Use notify_wechat to enable WeChat push notification on completion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "integer",
                    "description": "Seconds from now to fire (minimum 60)",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task the agent will run",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short label explaining why (used as job name)",
                },
                "created_by": {
                    "type": "string",
                    "default": "cli",
                    "description": "Creator identifier for notification routing (e.g. 'wechat:uid')",
                },
                "notify_wechat": {
                    "type": "boolean",
                    "default": False,
                    "description": "Send WeChat notification on completion",
                },
                "notify_on_success": {
                    "type": "boolean",
                    "default": True,
                    "description": "Notify on successful execution",
                },
                "notify_on_error": {
                    "type": "boolean",
                    "default": True,
                    "description": "Notify on execution error",
                },
            },
            "required": ["delay_seconds", "prompt"],
        },
        run=run,
    )
