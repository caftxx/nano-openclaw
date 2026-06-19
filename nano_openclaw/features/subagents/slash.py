"""Slash registrations for subagent features."""

from __future__ import annotations

from typing import Any


def register_slash(registry: Any) -> None:
    from nano_openclaw.services import slash as handlers

    registry.register(
        "/subagents",
        handlers._cmd_subagents,
        "Active subagent runs",
        "list|kill <id>|all",
    )
    registry.register(
        "/review-fork",
        handlers._cmd_review_fork,
        "Background review fork",
        "status|on|off|run",
    )
