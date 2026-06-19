"""``RuntimeUpdateGuard`` — coordinate ``runtime.update`` against in-flight turns.

The agent runtime (model client, registry, hooks) is mutable: changing the
model or agent_id mid-flight would let an in-progress ``run_turn`` observe
a half-swapped client and crash. This guard provides a fail-fast
coordinator so ``runtime.update`` returns ``BusyError`` immediately when a
turn is in flight, rather than racing or blocking indefinitely.

Two contexts:

- ``reader()``: held by every long-running operation that observes runtime
  state — ``chat.send``'s ``_run_turn``, the cron scheduler's ``_execute_job``.
  Multiple readers may hold concurrently.
- ``writer()``: held by ``runtime.update``. Acquired only when zero readers
  are active and no other writer holds; otherwise raises ``BusyError`` with
  the count + a 2s retry hint.

This is **fail-fast**, not blocking: writers never wait for readers, and
new readers fail immediately if a writer is mid-update. That matches the
UX intent ("tell the user to retry") and keeps the implementation tiny —
pure asyncio counters, no asyncio.Lock needed because increment/check
sequences run between awaits on the single event loop.

Mirrors openclaw's ``runtime_lock`` semantics in ``server-runtime-state.ts``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from nano_openclaw.services.backend import BusyError


class RuntimeUpdateGuard:
    """Reader/writer coordinator with fail-fast semantics.

    Not thread-safe — single asyncio event loop only. The counter mutations
    are safe between awaits because asyncio does not preempt mid-statement.
    """

    def __init__(self) -> None:
        self._n_readers = 0
        self._writer_held = False

    @property
    def n_readers(self) -> int:
        return self._n_readers

    @property
    def writer_held(self) -> bool:
        return self._writer_held

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[None]:
        """Hold a reader slot for the wrapped block.

        Raises ``BusyError`` if a writer (``runtime.update``) is currently
        applying changes — the caller should retry after a moment.
        """
        if self._writer_held:
            raise BusyError(
                "runtime update is in progress",
                retry_after_ms=2000,
                details={"reason": "writer-held"},
            )
        self._n_readers += 1
        try:
            yield
        finally:
            self._n_readers -= 1

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[None]:
        """Acquire the exclusive writer slot for the wrapped block.

        Raises ``BusyError`` immediately if any reader holds or another
        writer is already in progress. Releases on context exit.
        """
        if self._n_readers > 0:
            raise BusyError(
                f"{self._n_readers} turn(s) in flight",
                retry_after_ms=2000,
                details={"reason": "readers-active", "n_readers": self._n_readers},
            )
        if self._writer_held:
            raise BusyError(
                "another runtime update is in progress",
                retry_after_ms=2000,
                details={"reason": "writer-held"},
            )
        self._writer_held = True
        try:
            yield
        finally:
            self._writer_held = False
