"""Backend Protocol — the single contract for all frontends.

The TUI REPL, the WebUI server, and the WebSocket client all talk to the
agent runtime exclusively through this interface. Two implementations
satisfy it:

- ``EmbeddedBackend`` (backend_embedded.py) — direct calls into a local
  ``AgentRuntime``. Used when nano-openclaw runs single-process.
- ``WebSocketBackend`` (cli/backend_websocket.py, Phase 5) — JSON-RPC over a
  WebSocket to a remote daemon.

Frontends MUST NOT touch ``AgentRuntime`` / ``AgentSession`` directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from nano_openclaw.core.attachments import PromptAttachment


# ────────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────────


class BackendError(Exception):
    """Base for all Backend errors."""


class VoiceError(BackendError):
    """Voice feature request failed with structured fallback metadata."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "",
        fallback_eligible: bool = False,
        status_code: int = 503,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.fallback_eligible = fallback_eligible
        self.status_code = status_code


class BusyError(BackendError):
    """Operation cannot proceed because conflicting work is in flight.

    ``chat_send`` raises this when the session lock is held by another turn.
    ``runtime_update`` raises this when any turn is in flight. The frontend
    should surface a "try again in N ms" hint, never silently retry.
    """

    def __init__(self, message: str, *, retry_after_ms: int = 500, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms
        self.details = details or {}


class NotFoundError(BackendError):
    pass


# ────────────────────────────────────────────────────────────────────────────
# Push events
# ────────────────────────────────────────────────────────────────────────────


PushEventKind = Literal[
    "agent.event",       # Wraps loop / stream events; payload includes session_key + turn_id
    "approval.request",  # Tool wants human approval (interactive turns only)
    "approval.resolved", # Approval was decided (any client can observe)
    "session.changed",   # Session metadata or list changed
    "channel.changed",   # Channel start/stop/error
    "runtime.changed",   # ``runtime_update`` completed (model / agent / thinking swap)
    "gap",               # Subscriber's bounded queue overflowed; client should chat_history
]


@dataclass(frozen=True)
class PushEvent:
    """One push frame on the event stream.

    ``seq`` is monotonic per Backend instance; ``gap`` events let clients
    detect drops and reconcile via ``chat_history(after_seq=...)``.
    """

    event: PushEventKind
    payload: dict[str, Any]
    seq: int


# ────────────────────────────────────────────────────────────────────────────
# Data classes (return types)
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    title: str
    preview: str
    created_at: float
    updated_at: float
    model: str
    message_count: int
    compaction_count: int
    current: bool
    active_turn_id: str | None = None


@dataclass(frozen=True)
class SessionList:
    sessions: list[SessionInfo]
    last_session_id: str | None


@dataclass(frozen=True)
class SessionDetails:
    session_id: str
    title: str
    history: list[dict[str, Any]]      # message_to_json output
    activities: list[dict[str, Any]]
    model: str
    active_turn_id: str | None = None


@dataclass(frozen=True)
class HistoryPayload:
    """Snapshot returned by chat_history. Lets a reconnecting client rebuild UI."""

    session_id: str
    history: list[dict[str, Any]]
    activities: list[dict[str, Any]]
    last_seq: int                      # Last seq seen by Backend for this session


@dataclass(frozen=True)
class CompactionResult:
    success: bool
    summary: str | None
    tokens_before: int
    tokens_after: int


@dataclass(frozen=True)
class SessionUsageReport:
    """Per-session token + cache + compaction snapshot returned by ``/usage``.

    Sources: ``AgentBackendSession.usage_stats`` (cumulative + last-turn
    counters maintained by the loop) plus budget / cache_ttl from the
    active runtime config.

    Note on the "% of budget" indicator: callers should compute it from
    ``last_prompt_tokens`` (= total prompt the model saw last turn:
    input + cache_read + cache_creation). NOT just billable input —
    cached tokens still count against the model's context window even
    though they're not billed at full rate. This stays aligned with
    what ``compact_if_needed`` uses as its trigger signal.
    """
    session_id: str | None
    last_prompt_tokens: int             # input + cache_read + cache_creation, last turn
    last_output_tokens: int
    last_cache_read_tokens: int
    last_cache_creation_tokens: int
    total_prompt_tokens: int            # cumulative input + cache_read + cache_creation
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_creation_tokens: int
    compactions_fired: int
    turns_recorded: int
    cache_hit_ratio: float | None         # None when no caching traffic yet
    context_budget: int
    context_window: int                   # 0 when unknown
    cache_ttl: str | None                 # None when caching disabled / OpenAI


@dataclass(frozen=True)
class PendingApproval:
    request_id: str
    tool_name: str
    tool_args: dict[str, Any]
    risk_level: str
    reason: str
    timestamp: float
    origin: str | None = None          # "tui" / "webui:tab123" / "wechat:default:uid" / "cron:job:run"
    turn_id: str | None = None


ApprovalScope = Literal["once", "session", "always"]


@dataclass(frozen=True)
class ModelChoice:
    ref: str                           # "anthropic/claude-sonnet-4-5"
    id: str                            # "claude-sonnet-4-5"
    provider: str                      # "anthropic"
    context_window: int | None = None
    is_default: bool = False
    # Phase 1: extended catalog fields. ``input`` is a tuple (frozen=True needs
    # hashable; jsonable() coerces to list at serialize time).
    name: str | None = None
    input: tuple[str, ...] = ()
    reasoning: bool = False
    max_tokens: int | None = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    agent_id: str
    model_ref: str
    model_id: str
    image_model_ref: str | None
    thinking_level: str
    workspace_dir: str
    state_dir: str
    # Context budget + threshold for ``/context`` rendering — added so the
    # remote-mode TUI doesn't need a separate context.stats RPC.
    context_budget: int = 0
    context_threshold: float = 0.0
    context_recent_turns: int = 0
    context_window: int = 0


@dataclass(frozen=True)
class ChannelStatusEntry:
    channel_id: str                    # "wechat"
    account_id: str                    # "default" / "work"
    state: Literal["starting", "running", "stopped", "error"]
    error: str | None = None
    started_at: float | None = None


@dataclass(frozen=True)
class SlashRunResult:
    handled: bool
    text: str = ""
    session_key: str = ""
    session_changed: bool = False


@dataclass(frozen=True)
class SubagentInfo:
    run_id: str
    label: str | None
    task: str
    status: str
    started_at: float | None = None


@dataclass(frozen=True)
class HealthSummary:
    runtime_ready: bool
    channels_running: int
    sessions_loaded: int
    in_flight_turns: int
    extra: dict[str, Any] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Backend Protocol
# ────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class Backend(Protocol):
    """The single interface frontends use to drive the agent runtime.

    All methods are async. Implementations must:

    - Raise ``BusyError`` (not silently queue) when ``chat_send`` hits a
      session-level lock or ``runtime_update`` finds in-flight turns.
    - Tag every ``agent.event`` push with ``session_key`` and ``turn_id`` so
      multi-session clients can demux.
    - Apply per-subscriber bounded queueing (default 256); on overflow, drop
      events and emit a ``gap`` push event.
    """

    # ─── Chat ───
    async def chat_send(
        self,
        *,
        session_key: str,
        text: str,
        attachments: list[PromptAttachment] | None = None,
        turn_source: str = "tui",
        response_style: str = "",
    ) -> str:
        """Start a turn. Returns ``turn_id``.

        ``turn_source`` identifies the originating frontend (``tui`` /
        ``webui`` / ``wechat`` / ``cron`` / ``channel_auto``) and is forwarded
        to ``LoopConfig.turn_source`` so plugins (e.g. the stop-hook memory
        extractor) can filter by source. Defaults to ``"tui"``.

        ``response_style`` is an orthogonal per-turn style hint forwarded to
        ``LoopConfig.response_style``. ``"voice"`` appends a spoken-conversation
        directive (concise, no markdown/emoji) for the web voice mode. Empty =
        normal. Kept separate from ``turn_source`` so memory/analytics are
        unaffected.

        Raises ``BusyError`` if the session already has a turn in flight.
        Events for this turn arrive on ``subscribe`` streams.
        """
        ...

    async def chat_abort(self, *, turn_id: str) -> None:
        """Cancel an in-flight turn. No-op if turn already finished."""
        ...

    async def chat_history(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
    ) -> HistoryPayload:
        """Snapshot for a (re)connecting client. ``after_seq`` reserved for delta replay."""
        ...

    # ─── Sessions ───
    async def sessions_list(self) -> SessionList: ...
    async def sessions_get(self, session_id: str) -> SessionDetails: ...
    async def sessions_delete(self, session_id: str) -> None: ...
    async def sessions_reset(
        self,
        session_key: str,
        *,
        reason: Literal["new", "reset"] = "reset",
    ) -> SessionInfo:
        """``reset`` clears the current session in place; ``new`` starts a fresh session."""
        ...
    async def sessions_compact(self, session_key: str) -> CompactionResult: ...
    async def sessions_usage(self, session_key: str) -> SessionUsageReport: ...
    async def get_todos(self, session_key: str) -> list[dict[str, Any]]:
        """Return the current TODO list for the given session.

        Empty list when the session is fresh or has no todos. Resolves
        ``session_key`` the same way ``sessions_usage`` does (empty string
        / None → most recent session).
        """
        ...

    # ─── Approvals ───
    async def approvals_list(self) -> list[PendingApproval]: ...
    async def approvals_respond(
        self,
        request_id: str,
        *,
        allow: bool,
        scope: ApprovalScope = "once",
        reason: str = "",
    ) -> None: ...

    # ─── Models / runtime control ───
    async def models_list(self) -> list[ModelChoice]: ...
    async def runtime_get(self) -> RuntimeSnapshot: ...
    async def runtime_update(
        self,
        *,
        agent_id: str | None = None,
        model_ref: str | None = None,
        image_model_ref: str | None = None,
        thinking_level: str | None = None,
    ) -> RuntimeSnapshot:
        """Hot-reload runtime config. Raises ``BusyError`` if any turn is in flight."""
        ...

    # ─── Channels ───
    async def channels_status(self) -> list[ChannelStatusEntry]: ...
    async def channels_start(
        self,
        channel_id: str,
        account_id: str | None = None,
    ) -> ChannelStatusEntry: ...
    async def channels_stop(
        self,
        channel_id: str,
        account_id: str | None = None,
    ) -> ChannelStatusEntry: ...

    # ─── Slash commands ───
    async def slash_run(self, command: str, session_key: str = "") -> SlashRunResult: ...

    # ─── Subagents ───
    async def subagents_list(self) -> list[SubagentInfo]: ...
    async def subagents_kill(self, run_id: str) -> None: ...

    # ─── Features (active-memory / dreaming / review-fork / curator / checkpoint) ───
    async def active_memory_get(self) -> dict[str, Any]: ...
    async def active_memory_set(self, **fields: Any) -> dict[str, Any]: ...
    async def dreaming_get(self) -> dict[str, Any]: ...
    async def dreaming_set(self, **fields: Any) -> dict[str, Any]: ...
    async def dreaming_run(self) -> dict[str, Any]: ...
    async def review_fork_get(self) -> dict[str, Any]: ...
    async def review_fork_set(self, **fields: Any) -> dict[str, Any]: ...
    async def review_fork_run(self, session_key: str | None = None) -> dict[str, Any]: ...
    async def curator_get(self) -> dict[str, Any]: ...
    async def curator_set(self, **fields: Any) -> dict[str, Any]: ...
    async def curator_run(self, dry_run: bool = False) -> dict[str, Any]: ...
    async def checkpoint_list(self) -> dict[str, Any]: ...
    async def checkpoint_create(self, reason: str = "manual") -> dict[str, Any]: ...
    async def checkpoint_restore(self, checkpoint_id: str) -> dict[str, Any]: ...

    # ─── Introspection (tools / skills / plugins / hooks) ───
    # Lightweight readers consumed by ``/tools`` ``/skills`` ``/plugins`` ``/hooks``
    # slash commands in remote-mode TUI. Embedded TUI reads these straight off
    # ``runtime.registry`` / ``runtime.cfg``; the RPC layer parity matters so
    # both modes render the same panels.
    async def tools_list(self) -> list[dict[str, Any]]: ...
    async def skills_list(self) -> list[dict[str, Any]]: ...
    async def plugins_list(self) -> list[dict[str, Any]]: ...
    async def hooks_list(self) -> dict[str, Any]: ...
        # Per-event details: ``{event: {"count": N, "plugins": [...], "priorities": [...]}}``.
        # ``count`` preserves the count-only signal the simpler /hooks renderer used.

    # ─── Health ───
    async def health(self) -> HealthSummary: ...

    # ─── Gateway lifecycle ───
    async def gateway_restart(self) -> dict[str, Any]:
        """Restart the daemon. Immediate — does not wait for in-flight turns.

        Returns ``{"strategy": "exec"|"exit", "pid": int}`` synchronously
        BEFORE the actual restart fires (the swap happens on a short delay
        so the response can flush back to the caller). After that the
        connection drops; clients should reconnect on the same port.

        For deferred (turn-end) restart, the LLM-facing ``restart`` tool
        sets ``runtime.pending_restart`` instead — see daemon/server.py.
        """
        ...

    # ─── WebUI/voice service projections ───
    async def webui_state(self) -> dict[str, Any]: ...
    async def voice_config(self) -> dict[str, Any]: ...
    async def voice_token(self) -> dict[str, Any]: ...
    async def talk_speak(self, **params: Any) -> dict[str, Any]: ...

    # ─── Push event subscription ───
    def subscribe(
        self,
        *,
        session_key: str | None = None,
        events: list[PushEventKind] | None = None,
    ) -> AsyncIterator[PushEvent]:
        """Stream push events.

        ``session_key=None`` → all sessions; ``events=None`` → all event kinds.
        Each subscriber gets its own bounded queue; slow consumers see ``gap``
        events rather than blocking the producer.
        """
        ...

    # ─── Lifecycle ───
    async def aclose(self) -> None:
        """Release subscribers, stop tasks, close client. Idempotent."""
        ...
