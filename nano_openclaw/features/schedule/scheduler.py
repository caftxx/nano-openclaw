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
from nano_openclaw.features.memory.dreaming import _last_cron_occurrence, _next_cron_occurrence
from nano_openclaw.features.schedule.store import CronStore
from nano_openclaw.features.schedule.types import CronJob, CronJobState, CronRunRecord

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
    run_registry: Any | None = None,  # gateway.run_registry.RunRegistry; None → no abort plumbing
    approval_manager: Any | None = None,  # consulted by NonInteractiveApprovalHandler
    runtime_guard: Any | None = None,  # gateway.runtime_lock.RuntimeUpdateGuard
) -> asyncio.Task:
    """Start the cron scheduler as a background asyncio task.

    Returns the Task so the caller can cancel it on shutdown. Phase 6 added
    ``run_registry`` so each cron job's turn is registered under
    ``cron:{job:8}:{run:8}`` and can be aborted via ``chat.abort(turn_id)``.
    """

    async def _loop() -> None:
        store = CronStore(cron_dir)
        cron_dir.mkdir(parents=True, exist_ok=True)

        _recover_interrupted(store)
        await _run_missed_jobs(store, state_dir, session_dir, workspace_dir, client, base_cfg, missed_jobs_limit, run_registry, approval_manager, runtime_guard)

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
                    _execute_job(job, store, state_dir, session_dir, workspace_dir, client, base_cfg, run_registry, approval_manager, runtime_guard),
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
    run_registry: Any | None = None,
    approval_manager: Any | None = None,
    runtime_guard: Any | None = None,
) -> None:
    """Execute jobs whose nextRunAtMs has already passed (up to `limit`)."""
    jobs = store.load_jobs()
    states = store.load_state()

    # Recompute nextRunAtMs for all jobs on startup. Use the recovery-aware
    # variant so a daemon restart doesn't re-fire jobs that already ran for
    # the most recent scheduled slot — see _set_next_run_recovery.
    now_ms = int(time.time() * 1000)
    now_dt = datetime.now()
    for job in jobs.values():
        if not job.enabled:
            continue
        state = states.setdefault(job.id, CronJobState(job_id=job.id))
        _set_next_run_recovery(job, state, now_dt)
    store.save_state(states)

    # Reload and find missed
    states = store.load_state()
    due = _collect_due(jobs, states, now_ms)

    # Stagger excess missed jobs instead of running all at once
    immediate = due[:limit]
    staggered = due[limit:]

    tasks = [
        asyncio.create_task(
            _execute_job(j, store, state_dir, session_dir, workspace_dir, client, base_cfg, run_registry, approval_manager, runtime_guard),
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
    run_registry: Any | None = None,
    approval_manager: Any | None = None,
    runtime_guard: Any | None = None,
) -> None:
    """Run one cron job and update state + run log.

    Phase 6: registers the run under a deterministic ``turn_id`` so
    ``chat.abort(turn_id)`` can target it; uses
    ``NonInteractiveApprovalHandler`` so any tool that would have prompted
    a human gets the allowlist check (allow → run, deny → fail) without
    blocking forever.
    """
    from dataclasses import replace as dc_replace
    from nano_openclaw.services.approval_broker import NonInteractiveApprovalHandler
    from nano_openclaw.services.runs import cron_turn_id
    from nano_openclaw.core.loop import AgentSession, CancellationToken, Message, TurnCancelled
    from nano_openclaw.core.tools import build_core_registry

    run_id = str(uuid.uuid4())
    turn_id = cron_turn_id(job.id, run_id)
    started_at = datetime.now()
    started_ms = int(started_at.timestamp() * 1000)
    log.info("cron.job.start", f"Cron job '{job.name}' ({job.id[:8]}) started turn={turn_id}")

    # Mark running
    states = store.load_state()
    state = states.setdefault(job.id, CronJobState(job_id=job.id))
    state.running_at_ms = started_ms
    store.save_state(states)

    status = "ok"
    error: str | None = None
    history: list[Message] = []

    cancellation_token = CancellationToken()
    if run_registry is not None:
        run_registry.register(
            turn_id=turn_id,
            origin="cron",
            cancellation_token=cancellation_token,
            session_key=f"cron-{job.id[:8]}-{run_id[:8]}",
            label=f"cron job: {job.name}",
        )

    try:
        registry = build_core_registry()
        if workspace_dir:
            registry.set_workspace_dir(workspace_dir)
        # Wire the non-interactive approval path so cron-triggered tool calls
        # never wait on a human. The handler consults the existing per-agent
        # allowlist via ``approval_manager.check_request``: allowlist hit →
        # ALLOW; otherwise DENY (per user decision).
        if approval_manager is not None:
            registry.approval_manager = approval_manager
            registry.approval_handler = NonInteractiveApprovalHandler(approval_manager)

        cfg = dc_replace(
            base_cfg,
            session_key=f"cron-{job.id[:8]}-{run_id[:8]}",
            workspace_dir=workspace_dir,
            active_memory_config=None,
            dreaming_config=None,
            hook_registry=None,
            turn_source="cron",
        )

        history = [Message("user", [{"type": "text", "text": job.prompt}])]
        # cron 跑完即弃，给个 throwaway TodoStore 让 todo 工具在该 turn 内可用。
        from nano_openclaw.todo import TodoStore
        session = AgentSession(
            history=history,
            registry=registry,
            on_event=lambda _: None,
            client=client,
            cfg=cfg,
            cancellation_token=cancellation_token,
            todo_store=TodoStore(),
        )
        # Hold the runtime-update reader for the cron turn — so a daemon-side
        # ``runtime.update`` finds this turn in flight and returns BUSY rather
        # than torpedoing the cron job mid-run.
        if runtime_guard is not None:
            async with runtime_guard.reader():
                await session.run_turn(job.prompt)
        else:
            await session.run_turn(job.prompt)

    except TurnCancelled:
        status = "interrupted"
        error = "cancelled via chat.abort"
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if run_registry is not None:
            run_registry.unregister(turn_id)

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
        # After a run, advance to a strictly-future occurrence — otherwise
        # _last_cron_occurrence would re-select today's slot and the main loop
        # would re-trigger this job on the next 60s tick.
        _set_next_run(job, state, ended_at, force_future=True)
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

    # ── Directed channel notification ───────────────────────────────────────────
    #
    # Phase 2 routes via ChannelManager.dispatch_notify when the originating
    # channel is registered + running (the daemon/Phase 3 path). Falls back to
    # the legacy direct-write path when no channel instance owns the
    # notification — that's the case for jobs created in the standalone
    # `nano-openclaw wechat` subcommand which runs WechatBot without going
    # through the registry.

    should_notify = job.notify_wechat and (
        (status == "ok" and job.notify_on_success)
        or (status == "error" and job.notify_on_error)
    )

    if should_notify:
        from nano_openclaw.services.channels import get_channel_manager

        if status == "error":
            summary = (
                f"定时任务「{job.name}」执行失败\n"
                f"错误: {error}\n"
                f"耗时: {elapsed_ms}ms"
            )
        else:
            model_text = _extract_last_assistant_text(history)
            summary = model_text if model_text else f"定时任务「{job.name}」已完成（无文本输出）"

        record = CronRunRecord(
            job_id=job.id,
            run_id=run_id,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            status=status,
            error=error,
            elapsed_ms=elapsed_ms,
        )

        registry = get_channel_manager()
        delivered = await registry.dispatch_notify(
            created_by=job.created_by,
            status=status,
            summary=summary,
            job=job,
            record=record,
        )

        if not delivered and job.created_by.startswith("wechat:"):
            # Fallback: legacy single-account path. When the daemon isn't
            # running but a standalone `nano-openclaw wechat` bot is, the
            # bot polls the default state_dir/notify-queue.jsonl directly.
            from nano_openclaw.wechat.notify import NotifyItem, NotifyQueue
            parts = job.created_by.split(":", 2)
            target_uid = parts[2] if len(parts) == 3 else parts[1]
            notify_path = state_dir / "notify-queue.jsonl"
            queue = NotifyQueue(notify_path)
            queue.append(NotifyItem(
                job_id=job.id,
                job_name=job.name,
                status=status,
                result_summary=summary,
                created_at=ended_at.isoformat(),
                target_uid=target_uid,
                sent=False,
            ))
            log.info("cron.notify.queued.legacy", f"Notification queued (legacy) for {target_uid:.16} (job={job.name})")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_last_assistant_text(history: list[Any]) -> str:
    """Return concatenated text blocks from the last assistant message, or ''."""
    for msg in reversed(history):
        if getattr(msg, "role", None) != "assistant":
            continue
        parts = [
            block.get("text", "")
            for block in getattr(msg, "content", []) or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(p for p in parts if p).strip()
        if text:
            return text
    return ""


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


def _set_next_run_recovery(
    job: CronJob,
    state: CronJobState,
    reference: datetime,
) -> None:
    """Recompute ``next_run_at_ms`` at daemon startup, **skipping slots
    that already ran**.

    Without this dedupe, a daemon restart after a successful run would set
    ``next_run_at_ms`` back to the just-fired slot — and ``_collect_due``
    would re-trigger the job. We compare the most-recent scheduled
    occurrence against ``last_run_at_ms``: if the job already ran at or
    after that slot, skip ahead to the next future occurrence.

    One-shot jobs are deleted post-fire, so a one-shot still in the store
    with ``last_run_at_ms`` set means the deletion didn't complete (rare
    crash window) — treat it as "already done, don't re-fire".
    """
    if job.one_shot:
        if state.last_run_at_ms is not None:
            state.next_run_at_ms = None
            return
        state.next_run_at_ms = job.fire_at_ms
        return

    if not job.expression:
        state.next_run_at_ms = None
        return

    last_occ = _last_cron_occurrence(job.expression, reference)
    if last_occ is None:
        # No past occurrence yet — schedule the next future one.
        nxt = _next_cron_occurrence(job.expression, reference)
        state.next_run_at_ms = int(nxt.timestamp() * 1000) if nxt else None
        return

    last_occ_ms = int(last_occ.timestamp() * 1000)
    last_run = state.last_run_at_ms or 0
    # last_run within the same minute as last_occ counts as "ran for that slot"
    # — cron expressions resolve to minute granularity, so a slight clock skew
    # between when we computed last_occ and when the job actually ran shouldn't
    # cause a re-fire.
    if last_run + 60_000 > last_occ_ms:
        nxt = _next_cron_occurrence(job.expression, reference)
        state.next_run_at_ms = int(nxt.timestamp() * 1000) if nxt else None
    else:
        state.next_run_at_ms = last_occ_ms