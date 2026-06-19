"""Slash registrations for skills features."""

from __future__ import annotations

from typing import Any


def register_slash(registry: Any) -> None:
    from nano_openclaw.services import slash as handlers

    registry.register("/skills", handlers._cmd_skills, "Available skills")
    registry.register(
        "/curator",
        handlers._cmd_curator,
        "Skill lifecycle curator",
        "status|on|off|pause|resume|run|dry-run",
    )
