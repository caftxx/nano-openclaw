"""Slash registrations for runtime/model features."""

from __future__ import annotations

from typing import Any


def register_slash(registry: Any) -> None:
    from nano_openclaw.services import slash as handlers

    registry.register("/runtime", handlers._cmd_runtime, "Active runtime summary")
    registry.register("/models", handlers._cmd_models, "List configured models")
    registry.register(
        "/model",
        handlers._cmd_model,
        "Show / switch active model",
        "<provider/model-id>",
    )
    registry.register(
        "/thinking",
        handlers._cmd_thinking,
        "Show / set thinking level",
        "off|minimal|low|medium|high|xhigh|adaptive|max",
    )
    registry.register("/restart", handlers._cmd_restart, "Restart the gateway")
