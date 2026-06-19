"""RuntimeUpdateGuard + ApprovalManager allowlist-lock tests.

Two layers (mirroring Phase 7's two safety nets):

1. ``RuntimeUpdateGuard`` — fail-fast reader/writer mutual exclusion.
2. ``ApprovalManager._allowlist_lock`` — concurrent ``record_decision``
   calls do not lose entries when persisting allow-always allowlist
   updates.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
)
from nano_openclaw.services.backend import BusyError
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard


# ────────────────────────────────────────────────────────────────────────────
# RuntimeUpdateGuard primitives
# ────────────────────────────────────────────────────────────────────────────


def test_starts_with_zero_readers_no_writer():
    g = RuntimeUpdateGuard()
    assert g.n_readers == 0
    assert g.writer_held is False


def test_reader_increments_and_decrements():
    async def run():
        g = RuntimeUpdateGuard()
        async with g.reader():
            assert g.n_readers == 1
            async with g.reader():
                assert g.n_readers == 2
            assert g.n_readers == 1
        assert g.n_readers == 0

    asyncio.run(run())


def test_writer_acquires_exclusively():
    async def run():
        g = RuntimeUpdateGuard()
        async with g.writer():
            assert g.writer_held is True
            assert g.n_readers == 0
        assert g.writer_held is False

    asyncio.run(run())


def test_writer_raises_busy_when_readers_active():
    async def run():
        g = RuntimeUpdateGuard()
        async with g.reader():
            with pytest.raises(BusyError) as exc_info:
                async with g.writer():
                    pass
            assert exc_info.value.retry_after_ms == 2000
            assert exc_info.value.details.get("reason") == "readers-active"
            assert exc_info.value.details.get("n_readers") == 1

    asyncio.run(run())


def test_reader_raises_busy_when_writer_held():
    """A second reader trying to acquire while writer holds gets BUSY."""
    async def run():
        g = RuntimeUpdateGuard()

        # Force writer-held state by entering its CM, then peek
        writer_cm = g.writer()
        await writer_cm.__aenter__()
        try:
            with pytest.raises(BusyError) as exc_info:
                async with g.reader():
                    pass
            assert exc_info.value.details.get("reason") == "writer-held"
        finally:
            await writer_cm.__aexit__(None, None, None)

    asyncio.run(run())


def test_writer_raises_busy_when_another_writer_held():
    async def run():
        g = RuntimeUpdateGuard()
        cm = g.writer()
        await cm.__aenter__()
        try:
            with pytest.raises(BusyError) as exc_info:
                async with g.writer():
                    pass
            assert exc_info.value.details.get("reason") == "writer-held"
        finally:
            await cm.__aexit__(None, None, None)

    asyncio.run(run())


def test_concurrent_readers_can_hold_simultaneously():
    """Two reader contexts in concurrent tasks should both be live at once."""
    async def run():
        g = RuntimeUpdateGuard()
        observed = []

        async def reader_task(i: int):
            async with g.reader():
                observed.append(("acquire", i, g.n_readers))
                await asyncio.sleep(0.05)
                observed.append(("release", i, g.n_readers))

        await asyncio.gather(reader_task(1), reader_task(2), reader_task(3))
        # After all release, n_readers back to 0
        assert g.n_readers == 0
        # At some point during the run, n_readers should have hit > 1
        max_readers = max(n for kind, _, n in observed if kind == "acquire")
        assert max_readers >= 2

    asyncio.run(run())


def test_writer_after_readers_finish_succeeds():
    async def run():
        g = RuntimeUpdateGuard()
        async with g.reader():
            pass
        # Now zero readers — writer should succeed
        async with g.writer():
            assert g.writer_held is True

    asyncio.run(run())


def test_busyerror_release_path_resets_state():
    """When a writer raises BUSY (readers active), state must remain clean."""
    async def run():
        g = RuntimeUpdateGuard()
        async with g.reader():
            with pytest.raises(BusyError):
                async with g.writer():
                    pass  # not reached
            # writer was never held, just attempted
            assert g.writer_held is False
        assert g.n_readers == 0

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# ApprovalManager._allowlist_lock — no lost writes under concurrent record
# ────────────────────────────────────────────────────────────────────────────


def _build_manager(tmp_path: Path) -> ApprovalManager:
    """Build an ApprovalManager wired to a real on-disk allow-always store.

    Uses ``ask_mode='always'`` + dangerous bash so every patterned bash
    request enters the allowlist write path on ALLOW_ALWAYS.
    """
    store_path = tmp_path / "exec-approvals.json"
    policy = ApprovalPolicy(
        agent_id="test",
        ask_mode="always",
        security_mode="allowlist",
        dangerous_tools=["bash"],
        allow_always_store=str(store_path),
    )
    return ApprovalManager(policy)


def _read_allowlist(tmp_path: Path) -> list[dict]:
    store_path = tmp_path / "exec-approvals.json"
    if not store_path.exists():
        return []
    data = json.loads(store_path.read_text(encoding="utf-8"))
    return data.get("agents", {}).get("test", {}).get("allowlist", [])


def test_concurrent_record_decisions_persist_all_entries(tmp_path: Path):
    """Spin up N threads each issuing a unique ALLOW_ALWAYS — all must land
    in the on-disk allowlist after the dust settles.
    """
    manager = _build_manager(tmp_path)
    N = 24
    ready = threading.Event()
    completed = threading.Barrier(N + 1)

    def worker(i: int) -> None:
        # Each worker creates a unique bash command so the patterns differ.
        req = manager.create_request(
            "bash",
            {"command": f"/usr/bin/uniq{i:02d} --flag"},
        )
        ready.wait()  # release everyone at once for max contention
        manager.record_decision(req.request_id, ApprovalDecision.ALLOW_ALWAYS)
        completed.wait()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()

    # Hold workers until all are spun up, then release in a flurry
    time.sleep(0.05)
    ready.set()
    completed.wait()
    for t in threads:
        t.join()

    persisted = _read_allowlist(tmp_path)
    patterns = {entry["pattern"] for entry in persisted}
    expected_patterns = {f"/usr/bin/uniq{i:02d}" for i in range(N)}
    missing = expected_patterns - patterns
    assert not missing, (
        f"lost {len(missing)} of {N} allowlist entries: {sorted(missing)[:5]}…"
    )


def test_repeated_record_decision_for_same_pattern_overwrites(tmp_path: Path):
    """Calling record_decision twice for the same pattern shouldn't duplicate."""
    manager = _build_manager(tmp_path)
    for _ in range(3):
        req = manager.create_request("bash", {"command": "/usr/bin/echo hi"})
        manager.record_decision(req.request_id, ApprovalDecision.ALLOW_ALWAYS)

    persisted = _read_allowlist(tmp_path)
    matching = [e for e in persisted if e["pattern"] == "/usr/bin/echo"]
    assert len(matching) == 1, f"expected 1 entry, got {len(matching)}"


def test_lock_attribute_exists_and_is_rlock_compatible():
    """Sanity: the lock attr is present and re-entrant (so a method that
    holds it can call another method that also tries to hold it).
    """
    manager = _build_manager(Path("/tmp"))  # path doesn't matter for this check
    lock = manager._allowlist_lock
    # threading.RLock is acquired with .acquire() — verify re-entry works
    lock.acquire()
    try:
        lock.acquire()  # would deadlock with non-reentrant Lock
        lock.release()
    finally:
        lock.release()
