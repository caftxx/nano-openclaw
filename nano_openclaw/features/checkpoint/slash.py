"""Slash registrations for checkpoint features."""

from __future__ import annotations

from typing import Any


def register_slash(registry: Any) -> None:
    from nano_openclaw.services import slash as handlers

    registry.register(
        "/checkpoint",
        handlers._cmd_checkpoint,
        "Workspace checkpoints",
        "list|create|restore <id>",
    )
