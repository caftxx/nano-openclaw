"""Process-wide registry of in-flight ``turn_id`` → cancellation handle.

Single source of truth for ``chat.abort(turn_id)``: chat-triggered runs
register here on ``chat_send``, and **cron-triggered runs do too** (Phase 6),
so the same RPC abort path works for both. Mirrors openclaw's
``server-methods/chat.ts:chatAbortControllers``.

Lives on ``AgentRuntime.run_registry`` rather than as a process singleton —
test isolation is easier when each ``AgentRuntime`` (one per test fixture)
has its own. The daemon happens to construct one runtime so it's effectively
a singleton there.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from nano_openclaw.loop import CancellationToken


RunOrigin = Literal["chat", "cron", "channel", "subagent"]


@dataclass
class RunEntry:
    """One in-flight turn — ``cancel()`` flips its CancellationToken."""

    turn_id: str
    origin: RunOrigin
    cancellation_token: "CancellationToken"
    started_at: float
    session_key: str | None = None
    label: str | None = None  # human-readable, e.g. "cron job: backup nightly"
    task: asyncio.Task[Any] | None = None


class RunRegistry:
    """Holds active ``RunEntry`` items keyed by ``turn_id``.

    Not thread-safe (asyncio-only); all mutators run on the event loop.
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunEntry] = {}

    def register(
        self,
        *,
        turn_id: str,
        origin: RunOrigin,
        cancellation_token: "CancellationToken",
        session_key: str | None = None,
        label: str | None = None,
        task: asyncio.Task[Any] | None = None,
    ) -> RunEntry:
        """Idempotent on the same ``turn_id``: re-registration overwrites the
        previous entry (a turn can only be in one run at a time).
        """
        entry = RunEntry(
            turn_id=turn_id,
            origin=origin,
            cancellation_token=cancellation_token,
            started_at=time.time(),
            session_key=session_key,
            label=label,
            task=task,
        )
        self._runs[turn_id] = entry
        return entry

    def unregister(self, turn_id: str) -> RunEntry | None:
        """Drop the entry. Idempotent."""
        return self._runs.pop(turn_id, None)

    def get(self, turn_id: str) -> RunEntry | None:
        return self._runs.get(turn_id)

    def cancel(self, turn_id: str) -> bool:
        """Flip the cancellation token if the turn is registered.

        Returns True if the cancel was issued (entry existed), False
        otherwise. Doesn't unregister — the turn's own runner does that
        in its finally clause when it observes the cancellation.
        """
        entry = self._runs.get(turn_id)
        if entry is None:
            return False
        entry.cancellation_token.cancel()
        return True

    def list(self) -> list[RunEntry]:
        return list(self._runs.values())

    def __len__(self) -> int:
        return len(self._runs)

    def __contains__(self, turn_id: str) -> bool:
        return turn_id in self._runs


def cron_turn_id(job_id: str, run_id: str) -> str:
    """Deterministic ``turn_id`` for cron-triggered turns: ``cron:{job:8}:{run:8}``.

    Stable derivation matters because ``chat.abort`` must be able to target
    a cron turn by id without the caller having seen it spawn — typical
    flow is: ``cron.list`` → user picks one → ``chat.abort(turn_id)``.
    """
    return f"cron:{job_id[:8]}:{run_id[:8]}"
