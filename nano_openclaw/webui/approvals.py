"""Web approval bridge for tool calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nano_openclaw.approvals.types import ApprovalDecision, ApprovalRequest


ApprovalEmitter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalDecision]


class WebApprovalBroker:
    """Turns synchronous-looking tool approval into WebSocket decisions."""

    def __init__(self, emit: ApprovalEmitter) -> None:
        self._emit = emit
        self._pending: dict[str, PendingApproval] = {}

    async def request_decision(
        self,
        request: ApprovalRequest,
        cancellation_token: Any | None = None,
    ) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[request.request_id] = PendingApproval(request=request, future=future)
        await self._emit({
            "type": "approval.requested",
            "request_id": request.request_id,
            "tool_name": request.tool_name,
            "tool_args": request.tool_args,
            "risk_level": request.risk_level,
            "reason": request.reason,
            "timestamp": request.timestamp,
        })

        if cancellation_token is None:
            return await future

        cancel_task = asyncio.create_task(_wait_for_cancellation(cancellation_token))
        try:
            done, pending = await asyncio.wait(
                {future, cancel_task},
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
            if not cancel_task.done():
                cancel_task.cancel()

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

    def deny_all(self) -> None:
        for request_id in list(self._pending):
            self.decide(request_id, ApprovalDecision.DENY.value)


async def _wait_for_cancellation(cancellation_token: Any) -> None:
    cancel_event = getattr(cancellation_token, "_cancelled", None)
    if cancel_event is not None and callable(getattr(cancel_event, "wait", None)):
        await asyncio.to_thread(cancel_event.wait)
        return

    while not getattr(cancellation_token, "is_cancelled", False):
        await asyncio.sleep(0.2)
