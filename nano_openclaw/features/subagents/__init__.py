"""Sub-agent module for nano-openclaw.

Provides background agent runs spawned from the main agent loop.
"""

from nano_openclaw.features.subagents.types import (
    SubagentConfig,
    SubagentContextMode,
    SubagentCleanupMode,
    SubagentRunRecord,
    SubagentStatus,
    SpawnParams,
    new_subagent_run_id,
    new_subagent_session_id,
    build_subagent_session_key,
    parse_session_key,
)
from nano_openclaw.features.subagents.registry import (
    SubagentRegistry,
    get_registry,
    reset_registry,
)
from nano_openclaw.features.subagents.runner import (
    SubagentRunner,
    SubagentRunnerResult,
    get_runner,
    reset_runner,
)
from nano_openclaw.features.subagents.announce import (
    AnnounceEvent,
    build_announce_content,
    build_announce_message,
    format_announce_for_display,
    should_announce,
    create_announce_event,
)

__all__ = [
    "SubagentConfig",
    "SubagentContextMode",
    "SubagentCleanupMode",
    "SubagentRunRecord",
    "SubagentStatus",
    "SpawnParams",
    "new_subagent_run_id",
    "new_subagent_session_id",
    "build_subagent_session_key",
    "parse_session_key",
    "SubagentRegistry",
    "get_registry",
    "reset_registry",
    "SubagentRunner",
    "SubagentRunnerResult",
    "get_runner",
    "reset_runner",
    "AnnounceEvent",
    "build_announce_content",
    "build_announce_message",
    "format_announce_for_display",
    "should_announce",
    "create_announce_event",
]