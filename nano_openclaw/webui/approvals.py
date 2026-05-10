"""Compatibility shim — module moved to gateway.approval_broker.

Phase 0 of the gateway port (plan: /home/caft/.claude/plans/1-5000-2-tender-dusk.md)
promoted the WebUI approval broker to be the shared interactive-approval path
for all frontends. Old name re-exported here for back-compat.

Renames:
    WebApprovalBroker -> ApprovalBroker
"""

from nano_openclaw.gateway.approval_broker import (
    ApprovalBroker as WebApprovalBroker,
    NonInteractiveApprovalHandler,
    PendingApproval,
)

__all__ = [
    "WebApprovalBroker",
    "NonInteractiveApprovalHandler",
    "PendingApproval",
]
