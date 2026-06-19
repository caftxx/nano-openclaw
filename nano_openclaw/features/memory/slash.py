"""Slash registrations for memory features."""

from __future__ import annotations

from typing import Any


def register_slash(registry: Any) -> None:
    from nano_openclaw.services import slash as handlers

    registry.register(
        "/active-memory",
        handlers._cmd_active_memory,
        "Active memory config",
        "status|on|off|mode|style",
    )
    registry.register(
        "/dreaming",
        handlers._cmd_dreaming,
        "Dreaming config",
        "status|on|off|run",
    )
