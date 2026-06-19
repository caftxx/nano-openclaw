"""Cron recovery / dedupe tests.

The bug: ``_run_missed_jobs`` recomputed ``next_run_at_ms`` on daemon
startup using ``_last_cron_occurrence``, which sets it to the most recent
past slot — even if the job already ran at that slot. The next pass of
``_collect_due`` then re-fired the just-completed job.

Fix: ``_set_next_run_recovery`` checks ``last_run_at_ms``: if the job ran
at or after the most-recent scheduled slot, advance to the NEXT future
occurrence. Tests below cover periodic vs one-shot, never-run vs ran-once
vs missed-by-down-time scenarios.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.features.schedule.scheduler import _execute_job, _set_next_run_recovery
from nano_openclaw.features.schedule.store import CronStore
from nano_openclaw.features.schedule.types import CronJob, CronJobState
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard


def _periodic_job(expression: str = "0 9 * * *") -> CronJob:
    """Daily at 9:00 by default."""
    return CronJob(
        id="job-1234567890",
        name="daily-9am",
        expression=expression,
        prompt="hello",
        enabled=True,
        created_at="2026-01-01T00:00:00",
    )


def _one_shot(fire_at_ms: int) -> CronJob:
    return CronJob(
        id="oneshot-12345",
        name="oneshot",
        expression="",
        prompt="hello",
        enabled=True,
        created_at="2026-01-01T00:00:00",
        one_shot=True,
        fire_at_ms=fire_at_ms,
    )


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ────────────────────────────────────────────────────────────────────────────
# Periodic jobs — the main bug shape
# ────────────────────────────────────────────────────────────────────────────


def test_already_ran_today_advances_to_tomorrow():
    """The classic bug: job ran at 9:00 today, daemon restarts at 11:00.
    Without the fix, next_run_at_ms would be set to 9:00 today and re-fire.
    """
    job = _periodic_job("0 9 * * *")
    today_9 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    if today_9 > datetime.now():
        # Test machine time is before 9 AM; pretend yesterday so 9 AM is past.
        today_9 -= timedelta(days=1)
    state = CronJobState(job_id=job.id, last_run_at_ms=_ms(today_9))

    reference = today_9 + timedelta(hours=2)  # 11 AM same day
    _set_next_run_recovery(job, state, reference)

    assert state.next_run_at_ms is not None
    next_run_dt = datetime.fromtimestamp(state.next_run_at_ms / 1000)
    # next run is in the future relative to ``reference``
    assert next_run_dt > reference
    # And it's the NEXT 9 AM, not today's 9 AM.
    assert next_run_dt.hour == 9


def test_never_ran_catches_up_missed_slot():
    """Job has never run (last_run_at_ms is None). Daemon starts at 11 AM,
    today's 9 AM slot is past — schedule it as missed (catch-up behavior).
    """
    job = _periodic_job("0 9 * * *")
    state = CronJobState(job_id=job.id, last_run_at_ms=None)

    today_11 = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
    if today_11 < datetime.now().replace(hour=9, minute=0, second=0, microsecond=0):
        today_11 += timedelta(days=1)
    _set_next_run_recovery(job, state, today_11)

    assert state.next_run_at_ms is not None
    next_run_dt = datetime.fromtimestamp(state.next_run_at_ms / 1000)
    assert next_run_dt <= today_11  # past slot → catch up
    assert next_run_dt.hour == 9


def test_ran_yesterday_catches_up_today_if_due():
    """Last run was yesterday at 9 AM; daemon restart at 11 AM today.
    Most-recent slot = today 9 AM, job didn't run for it → catch up.
    """
    job = _periodic_job("0 9 * * *")
    yesterday_9 = (datetime.now() - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    state = CronJobState(job_id=job.id, last_run_at_ms=_ms(yesterday_9))

    today_11 = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
    _set_next_run_recovery(job, state, today_11)

    assert state.next_run_at_ms is not None
    next_run_dt = datetime.fromtimestamp(state.next_run_at_ms / 1000)
    # today's 9 AM is the most recent slot, not yet run → schedule it
    assert next_run_dt.day == today_11.day
    assert next_run_dt.hour == 9


def test_ran_for_minute_slot_within_grace_period():
    """Tolerate small clock skew: a run that completed within ~60 seconds
    of the scheduled slot still counts as "ran" — same minute granularity
    cron uses.
    """
    job = _periodic_job("0 9 * * *")
    today_9 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    if today_9 > datetime.now():
        today_9 -= timedelta(days=1)
    # last_run is 30s before the scheduled slot (clock skew / pre-fire offset)
    state = CronJobState(job_id=job.id, last_run_at_ms=_ms(today_9) - 30_000)

    reference = today_9 + timedelta(hours=1)
    _set_next_run_recovery(job, state, reference)

    next_run_dt = datetime.fromtimestamp(state.next_run_at_ms / 1000)
    assert next_run_dt > reference  # advanced to tomorrow, not stuck on today


# ────────────────────────────────────────────────────────────────────────────
# Disabled / no-expression jobs
# ────────────────────────────────────────────────────────────────────────────


def test_no_expression_clears_next_run():
    job = CronJob(
        id="x" * 8,
        name="noop",
        expression="",
        prompt="",
        enabled=True,
        created_at="2026-01-01T00:00:00",
    )
    state = CronJobState(job_id=job.id, last_run_at_ms=None, next_run_at_ms=12345)
    _set_next_run_recovery(job, state, datetime.now())
    assert state.next_run_at_ms is None


# ────────────────────────────────────────────────────────────────────────────
# One-shot jobs
# ────────────────────────────────────────────────────────────────────────────


def test_one_shot_never_fired_keeps_fire_at():
    fire = _ms(datetime.now() + timedelta(hours=1))
    job = _one_shot(fire_at_ms=fire)
    state = CronJobState(job_id=job.id, last_run_at_ms=None)
    _set_next_run_recovery(job, state, datetime.now())
    assert state.next_run_at_ms == fire


def test_one_shot_already_fired_clears_next_run():
    """Defensive: one-shots are deleted post-fire (so this state shouldn't
    persist), but if the deletion was interrupted we shouldn't re-fire.
    """
    fire = _ms(datetime.now() - timedelta(hours=1))
    job = _one_shot(fire_at_ms=fire)
    state = CronJobState(job_id=job.id, last_run_at_ms=_ms(datetime.now() - timedelta(minutes=30)))
    _set_next_run_recovery(job, state, datetime.now())
    assert state.next_run_at_ms is None


def test_execute_job_defers_during_runtime_update(tmp_path):
    async def run():
        store = CronStore(tmp_path / "cron")
        job = _periodic_job("* * * * *")
        store.save_jobs({job.id: job})
        guard = RuntimeUpdateGuard()
        async with guard.writer():
            await _execute_job(
                job,
                store,
                tmp_path / "state",
                tmp_path / "sessions",
                tmp_path / "workspace",
                client=object(),
                base_cfg=LoopConfig(model="test-model"),
                run_registry=None,
                approval_manager=None,
                runtime_guard=guard,
            )
        state = store.load_state()[job.id]
        assert state.running_at_ms is None
        assert state.last_status is None
        assert state.last_error is None
        assert state.last_run_at_ms is None

    asyncio.run(run())


def test_execute_job_binds_skill_runtime_for_fallback_registry(tmp_path, monkeypatch):
    async def fake_run_turn(self, prompt):
        assert prompt == "hello"
        assert self.registry.skill_installer is not None
        assert self.registry.skill_usage_recorder is not None

    monkeypatch.setattr("nano_openclaw.core.loop.AgentSession.run_turn", fake_run_turn)

    async def run():
        store = CronStore(tmp_path / "cron")
        job = _periodic_job("* * * * *")
        store.save_jobs({job.id: job})
        await _execute_job(
            job,
            store,
            tmp_path / "state",
            tmp_path / "sessions",
            tmp_path / "workspace",
            client=object(),
            base_cfg=LoopConfig(model="test-model"),
            run_registry=None,
            approval_manager=None,
            runtime_guard=None,
        )

    asyncio.run(run())
