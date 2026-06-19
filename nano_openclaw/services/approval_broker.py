"""Approval broker — turns sync-looking tool approval into async decisions.

The ``ApprovalBroker`` is the human-prompt path: it pushes an
``approval.requested`` event out via ``emit`` and parks the tool dispatch on
a future until ``decide`` resolves it. Used by the WebUI WebSocket and the
TUI when ``--connect``-ed to a daemon.

The ``NonInteractiveApprovalHandler`` is the cron / channel-bot path: it
consults the existing ``ApprovalManager`` allowlist synchronously — never
prompts. If the tool is in the allowlist (or the policy says it doesn't
require approval at all) it returns ALLOW; otherwise DENY. This is the
fix for "cron triggered a tool needing approval and there's no human to
answer the prompt → daemon hangs forever".

Originally ``adapters/webui/approvals.py`` (Phase 0 promotion); since the
``webui/`` → ``adapters/webui/`` move the shim has been removed and callers
import ``ApprovalBroker`` directly from this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalDecision, ApprovalRequest


ApprovalEmitter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class PendingApproval:
    """One in-flight approval request waiting for a decision.

    ``origin`` and ``turn_id`` let the broker route ``approval.requested`` push
    events to the right subscribers (the frontend that started the turn) when
    multiple WS clients are connected. v1 broadcasts to all subscribers.
    """

    request: ApprovalRequest
    future: asyncio.Future[ApprovalDecision]
    origin: str | None = None        # "tui" / "webui:tab123" / "wechat:default:uid"
    turn_id: str | None = None


class ApprovalBroker:
    """Bridges synchronous-looking tool approval to async decisions.

    Used by interactive frontends (WebUI WebSocket, TUI). The cron / channel
    non-interactive path uses ``NonInteractiveApprovalHandler`` instead, which
    never goes through this broker.
    """

    def __init__(self, emit: ApprovalEmitter) -> None:
        self._emit = emit
        self._pending: dict[str, PendingApproval] = {}

    async def request_decision(
        self,
        request: ApprovalRequest,
        cancellation_token: Any | None = None,
        *,
        origin: str | None = None,
        turn_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ApprovalDecision:
        """Wait for a decision, optionally bounded by timeout / cancellation.

        ``timeout_seconds=None`` (default) waits forever. The cron / channel path
        does not use this broker at all; instead they use
        ``NonInteractiveApprovalHandler`` which never blocks.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[request.request_id] = PendingApproval(
            request=request,
            future=future,
            origin=origin,
            turn_id=turn_id,
        )
        await self._emit({
            "type": "approval.requested",
            "request_id": request.request_id,
            "tool_name": request.tool_name,
            "tool_args": request.tool_args,
            "risk_level": request.risk_level,
            "reason": request.reason,
            "timestamp": request.timestamp,
            "origin": origin,
            "turn_id": turn_id,
        })

        wait_tasks: list[asyncio.Task[Any]] = []
        if cancellation_token is not None:
            wait_tasks.append(asyncio.create_task(_wait_for_cancellation(cancellation_token)))
        if timeout_seconds is not None:
            wait_tasks.append(asyncio.create_task(asyncio.sleep(timeout_seconds)))

        if not wait_tasks:
            return await future

        try:
            done, pending = await asyncio.wait(
                {future, *wait_tasks},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if future in done:
                return future.result()

            self._pending.pop(request.request_id, None)
            if not future.done():
                future.cancel()
            return ApprovalDecision.DENY
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()

    def decide(self, request_id: str, decision: str) -> bool:
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        try:
            parsed = ApprovalDecision(decision)
        except ValueError:
            parsed = ApprovalDecision.DENY
        pending.future.set_result(parsed)
        return True

    def list_pending(self) -> list[PendingApproval]:
        return list(self._pending.values())

    def deny_all(self) -> None:
        for request_id in list(self._pending):
            self.decide(request_id, ApprovalDecision.DENY.value)


class NonInteractiveApprovalHandler:
    """Approval handler for cron / channel turns that have no human attached.

    Consults the existing ``ApprovalManager`` allowlist exactly the same way as
    interactive turns — but instead of prompting, returns ALLOW if the tool
    doesn't require approval (allowlist match / safe path / ask=off) and DENY
    otherwise. Never blocks, never raises.

    Wire this onto a per-turn ``ToolRegistry`` clone in cron / channel code paths:

        registry.approval_handler = NonInteractiveApprovalHandler(runtime.registry.approval_manager)

    The dispatcher in ``tools.py`` only invokes ``approval_handler`` after
    ``check_request`` already says ``requires_approval=True``, so this handler's
    job is exactly: "we got here because the allowlist said ask — say no
    instead." Hence ``ALWAYS DENY``.

    A future variant could grow a per-channel allowlist override for
    "auto-allow extra patterns when triggered by trusted account X", but v1
    keeps it simple: one allowlist, one rule.
    """

    def __init__(self, manager: ApprovalManager | None) -> None:
        self.manager = manager

    def __call__(
        self,
        request: ApprovalRequest,
        cancellation_token: Any | None = None,
    ) -> ApprovalDecision:
        # If we got here, dispatch already determined requires_approval=True
        # (see ToolRegistry.dispatch in tools.py). Non-interactive context
        # cannot answer that question, so we always deny.
        return ApprovalDecision.DENY


async def _wait_for_cancellation(cancellation_token: Any) -> None:
    while not getattr(cancellation_token, "is_cancelled", False):
        await asyncio.sleep(0.05)
