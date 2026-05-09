"""Background cron scheduler for nano-openclaw.

Polls every 60 s (aligned with OpenClaw's timer cadence).
On startup: marks interrupted jobs, runs missed jobs up to missedJobsLimit.
Main loop: force-reloads state, finds due jobs, executes concurrently.
"""

from __future__ import annotations

import asyncio
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from nano_openclaw.logger import get_logger
from nano_openclaw.memory.dreaming import _last_cron_occurrence, _next_cron_occurrence
from nano_openclaw.schedule.store import CronStore
from nano_openclaw.schedule.types import CronJob, CronJobState, CronRunRecord

log = get_logger(__name__)


def start_cron_scheduler(
    cron_dir: Path,
    state_dir: Path,
    session_dir: Path,
    workspace_dir: Path | None,
    client: Any,
    base_cfg: Any,          # LoopConfig — imported lazily to avoid circular deps
    max_concurrent: int,
    missed_jobs_limit: int,
    stop_event: threading.Event,
) -> asyncio.Task:
    """Start the cron scheduler as a background asyncio task.

    Returns the Task so the caller can cancel it on shutdown.
    """

    async def _loop() -> None:
        store = CronStore(cron_dir)
        cron_dir.mkdir(parents=True, exist_ok=True)

        _recover_interrupted(store)
        await _run_missed_jobs(store, state_dir, session_dir, workspace_dir, client, base_cfg, missed_jobs_limit)

        active_tasks: set[asyncio.Task] = set()

        while not stop_event.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            if stop_event.is_set():
                return

            jobs = store.load_jobs()
            states = store.load_state()
            now_ms = int(time.time() * 1000)

            active_tasks = {t for t in active_tasks if not t.done()}
            available = max(0, max_concurrent - len(active_tasks))
            due = _collect_due(jobs, states, now_ms)[:available]

            for job in due:
                task = asyncio.create_task(
                    _execute_job(job, store, state_dir, session_dir, workspace_dir, client, base_cfg),
                    name=f"cron-{job.id[:8]}",
                )
                active_tasks.add(task)

        # Wait for in-flight jobs on shutdown
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

    return asyncio.create_task(_loop(), name="cron-scheduler")


# ── Startup recovery ─────────────────────────────────────────────────────────

def _recover_interrupted(store: CronStore) -> None:
    """Mark jobs that were running at shutdown as interrupted."""
    states = store.load_state()
    changed = False
    for state in states.values():
        if state.running_at_ms is not None:
            state.running_at_ms = None
            state.last_status = "interrupted"
            state.last_error = "cron: job interrupted by restart"
            state.consecutive_errors += 1
            changed = True
    if changed:
        store.save_state(states)


async def _run_missed_jobs(
    store: CronStore,
    state_dir: Path,
    session_dir: Path,
    workspace_dir: Path | None,
    client: Any,
    base_cfg: Any,
    limit: int,
) -> None:
    """Execute jobs whose nextRunAtMs has already passed (up to `limit`)."""
    jobs = store.load_jobs()
    states = store.load_state()

    # Recompute nextRunAtMs for all jobs on startup
    now_ms = int(time.time() * 1000)
    now_dt = datetime.now()
    for job in jobs.values():
        if not job.enabled:
            continue
        state = states.setdefault(job.id, CronJobState(job_id=job.id))
        _set_next_run(job, state, now_dt)
    store.save_state(states)

    # Reload and find missed
    states = store.load_state()
    due = _collect_due(jobs, states, now_ms)

    # Stagger excess missed jobs instead of running all at once
    immediate = due[:limit]
    staggered = due[limit:]

    tasks = [
        asyncio.create_task(
            _execute_job(j, store, state_dir, session_dir, workspace_dir, client, base_cfg),
            name=f"cron-missed-{j.id[:8]}",
        )
        for j in immediate
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # For staggered jobs, just update nextRunAtMs to the true next future occurrence
    if staggered:
        states = store.load_state()
        future_dt = datetime.now()
        for job in staggered:
            state = states.get(job.id)
            if state:
                _set_next_run(job, state, future_dt, force_future=True)
        store.save_state(states)


# ── Job selection ─────────────────────────────────────────────────────────────

def _collect_due(
    jobs: dict[str, CronJob],
    states: dict[str, CronJobState],
    now_ms: int,
) -> list[CronJob]:
    """Return enabled jobs whose nextRunAtMs <= now and not currently running."""
    due = []
    for job in jobs.values():
        if not job.enabled:
            continue
        state = states.get(job.id)
        if state is None:
            continue
        if state.running_at_ms is not None:
            continue
        nxt = state.next_run_at_ms
        if nxt is not None and nxt <= now_ms:
            due.append(job)
    return due


# ── Job execution ─────────────────────────────────────────────────────────────

async def _execute_job(
    job: CronJob,
    store: CronStore,
    state_dir: Path,
    session_dir: Path,
    workspace_dir: Path | None,
    client: Any,
    base_cfg: Any,
) -> None:
    """Run one cron job and update state + run log."""
    from dataclasses import replace as dc_replace
    from nano_openclaw.loop import AgentSession, Message
    from nano_openclaw.tools import build_core_registry

    run_id = str(uuid.uuid4())
    started_at = datetime.now()
    started_ms = int(started_at.timestamp() * 1000)
    log.info("cron.job.start", f"Cron job '{job.name}' ({job.id[:8]}) started")

    # Mark running
    states = store.load_state()
    state = states.setdefault(job.id, CronJobState(job_id=job.id))
    state.running_at_ms = started_ms
    store.save_state(states)

    status = "ok"
    error: str | None = None

    try:
        registry = build_core_registry()
        if workspace_dir:
            registry.set_workspace_dir(workspace_dir)

        cfg = dc_replace(
            base_cfg,
            session_key=f"cron-{job.id[:8]}-{run_id[:8]}",
            workspace_dir=workspace_dir,
            active_memory_config=None,
            dreaming_config=None,
            hook_registry=None,
        )

        history: list[Message] = [Message("user", [{"type": "text", "text": job.prompt}])]
        session = AgentSession(
            history=history,
            registry=registry,
            on_event=lambda _: None,
            client=client,
            cfg=cfg,
        )
        await session.run_turn(job.prompt)

    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    ended_at = datetime.now()
    elapsed_ms = int((ended_at - started_at).total_seconds() * 1000)
    log.info("cron.job.complete", f"Cron job '{job.name}' ({job.id[:8]}) completed: status={status}, elapsed={elapsed_ms}ms")

    # Update state
    states = store.load_state()
    state = states.setdefault(job.id, CronJobState(job_id=job.id))
    state.running_at_ms = None
    state.last_run_at_ms = int(ended_at.timestamp() * 1000)
    state.last_status = status
    state.last_error = error
    if status == "error":
        state.consecutive_errors += 1
    else:
        state.consecutive_errors = 0

    # Schedule next run (or delete one-shot)
    jobs = store.load_jobs()
    if job.one_shot:
        store.remove_job(job.id)
        store.remove_state(job.id)
        # Reload states (remove_state already saved)
        states = store.load_state()
    else:
        _set_next_run(job, state, ended_at)
        store.save_state(states)

    store.append_run(CronRunRecord(
        job_id=job.id,
        run_id=run_id,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        status=status,
        error=error,
        elapsed_ms=elapsed_ms,
    ))

    # ── Directed WeChat notification ────────────────────────────────────────────

    if job.notify_wechat and job.created_by.startswith("wechat:"):
        target_uid = job.created_by.split(":", 1)[1]
        should_notify = (status == "ok" and job.notify_on_success) or \
                        (status == "error" and job.notify_on_error)
        if should_notify:
            from nano_openclaw.wechat.notify import NotifyQueue, NotifyItem
            # Use state_dir for notify-queue path (same as wechat bot)
            notify_path = state_dir / "notify-queue.jsonl"
            queue = NotifyQueue(notify_path)

            summary = f"定时任务「{job.name}」执行完成\n状态: {status}\n耗时: {elapsed_ms}ms"
            if error:
                summary += f"\n错误: {error}"

            queue.append(NotifyItem(
                job_id=job.id,
                job_name=job.name,
                status=status,
                result_summary=summary,
                created_at=ended_at.isoformat(),
                target_uid=target_uid,
                sent=False,
            ))
            log.info("cron.notify.queued", f"Notification queued for {target_uid:.16} (job={job.name})")


# ── Next-run calculation ──────────────────────────────────────────────────────

def _set_next_run(
    job: CronJob,
    state: CronJobState,
    reference: datetime,
    *,
    force_future: bool = False,
) -> None:
    """Set state.next_run_at_ms based on job schedule."""
    if job.one_shot:
        state.next_run_at_ms = job.fire_at_ms
        return

    if not job.expression:
        state.next_run_at_ms = None
        return

    if force_future:
        nxt = _next_cron_occurrence(job.expression, reference)
    else:
        # Use last occurrence so missed jobs show up as due on startup
        last = _last_cron_occurrence(job.expression, reference)
        nxt = last if last is not None else _next_cron_occurrence(job.expression, reference)

    state.next_run_at_ms = int(nxt.timestamp() * 1000) if nxt else None