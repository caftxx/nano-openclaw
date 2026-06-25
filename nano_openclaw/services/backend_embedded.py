"""EmbeddedBackend — Backend implementation that talks directly to a local AgentRuntime.

Used when nano-openclaw runs single-process (the default ``tui`` invocation
when no daemon is detected), and inside the daemon itself: the WebSocket and
HTTP route handlers wrap the same EmbeddedBackend that's powering the
in-process TUI.

Mirrors openclaw's ``tui/embedded-backend.ts`` — same interface as the
WebSocket backend, no remote IPC.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from nano_openclaw.core.attachments import PromptAttachment
from nano_openclaw.services.event_payload import (
    event_to_payload,
    is_replayable_activity_payload,
    jsonable,
)
from nano_openclaw.services.agent_session import (
    AgentBackendSession,
    BackendSessionManager,
    display_history,
    message_text,
)
from nano_openclaw.services.approval_broker import ApprovalBroker
from nano_openclaw.services.backend import (
    Backend,
    BackendError,
    BusyError,
    ChannelStatusEntry,
    CompactionResult,
    HealthSummary,
    HistoryPayload,
    ModelChoice,
    NotFoundError,
    PendingApproval,
    PushEvent,
    PushEventKind,
    RuntimeSnapshot,
    SessionDetails,
    SessionInfo,
    SessionList,
    SessionUsageReport,
    SlashRunResult,
    SubagentInfo,
    VoiceError,
)
from nano_openclaw.logger import get_logger
from nano_openclaw.core.loop import (
    AgentSession,
    CancellationToken,
    Message,
    TurnCancelled,
    append_active_todo_reminder,
)
from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.features.voice.voice_catalog import is_emotion_voice

if TYPE_CHECKING:
    from nano_openclaw.core.runtime import AgentRuntime
    from nano_openclaw.services.channels import ChannelManager


log = get_logger(__name__)


SUBSCRIBER_QUEUE_MAX = 256
SUBSCRIBER_GAP_DROP = 5


def _new_runtime_guard():
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard

    return RuntimeUpdateGuard()


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────────────
# Subscriber: per-iterator bounded queue with gap detection
# ────────────────────────────────────────────────────────────────────────────


class _Subscriber:
    """One ``subscribe()`` consumer.

    Bounded queue keeps a hung consumer from holding back the producer; on
    overflow, drop the oldest events and emit a synthetic ``gap`` PushEvent
    so the client can ``chat_history(after_seq=)`` to reconcile.
    """

    def __init__(
        self,
        *,
        session_key: str | None,
        events: set[PushEventKind] | None,
    ) -> None:
        self.session_key = session_key
        self.events = events
        self.queue: asyncio.Queue[PushEvent | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._closed = False

    def matches(self, event: PushEvent) -> bool:
        if self.events is not None and event.event not in self.events:
            return False
        if self.session_key is None:
            return True
        sk = event.payload.get("session_key") or event.payload.get("session_id")
        return sk == self.session_key

    def push(self, event: PushEvent) -> None:
        if self._closed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            dropped = 0
            for _ in range(SUBSCRIBER_GAP_DROP):
                try:
                    self.queue.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
            try:
                self.queue.put_nowait(
                    PushEvent(
                        event="gap",
                        payload={"dropped": dropped, "expected_next_seq": event.seq},
                        seq=event.seq,
                    )
                )
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Pathological: producer outpaces drain entirely. Drop event silently.
                log.warning("subscriber.queue.full", "subscriber queue still full after gap drop")

    def close(self) -> None:
        self._closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


# ────────────────────────────────────────────────────────────────────────────
# EmbeddedBackend
# ────────────────────────────────────────────────────────────────────────────


class EmbeddedBackend(Backend):
    """In-process Backend. Holds an AgentRuntime and drives turns directly.

    In-flight turns live in ``runtime.run_registry`` (Phase 6) — that lets
    cron-triggered turns share the same abort path as chat-triggered ones,
    and lets ``chat.abort(turn_id)`` over RPC target either kind uniformly.
    """

    def __init__(
        self,
        runtime: "AgentRuntime",
        *,
        manager: BackendSessionManager | None = None,
        channel_manager: "ChannelManager | None" = None,
    ) -> None:
        self.runtime = runtime
        self.channel_manager = channel_manager
        self.manager = manager or BackendSessionManager(
            session_dir=runtime.session_dir,
            store_path=runtime.store_path,
            model=runtime.model_id,
            cwd=str(runtime.workspace_dir),
        )
        # Global ApprovalBroker for this Backend; emits as approval.request
        # push events. Subscribers (TUI / WebUI / WS) listen.
        self._approval_broker = ApprovalBroker(self._emit_approval_event)
        self._subscribers: list[_Subscriber] = []
        self._seq = 0
        self._closed = False
        # Thinking-level change requested by the model via ``set_thinking``
        # mid-turn. Can't apply immediately: the turn holds the
        # ``RuntimeUpdateGuard`` reader, so ``runtime_update`` (writer) would
        # raise BusyError. Stash it here and flush after the turn's reader
        # releases — matching ``/thinking``'s semantics (global runtime.cfg
        # change, effective from the next turn).
        self._pending_thinking_level: str | None = None
        self._voice_token_provider: Any | None = None
        self._podcast_runs: dict[str, dict[str, Any]] = {}
        # Wire LLM-facing runtime introspection tools (list_models /
        # switch_model / get_runtime / …). These need a live ``Backend``
        # reference, which only exists once we've built ``self`` — that's why
        # registration happens here rather than in build_agent_runtime.
        # Skip when the runtime opted out via ``no_tools`` so prompt-only
        # / pure-chat configs stay tool-free.
        cfg = getattr(runtime, "config", None)
        no_tools = bool(getattr(cfg, "noTools", False)) or bool(
            getattr(getattr(cfg, "tools", None), "noTools", False)
        )
        if not no_tools:
            try:
                from nano_openclaw.services.tool_hooks import install_checkpoint_write_hook
                install_checkpoint_write_hook(runtime.registry)
            except Exception as exc:  # noqa: BLE001
                log.warning("backend.tool_hooks.register_failed", f"{type(exc).__name__}: {exc}")
            try:
                from nano_openclaw.core.tools_runtime import register_runtime_tools
                register_runtime_tools(runtime.registry, self)
            except Exception as exc:  # noqa: BLE001 — tool wiring is non-fatal
                log.warning("backend.runtime_tools.register_failed", f"{type(exc).__name__}: {exc}")
        self._register_plugin_channels(runtime)

    @property
    def _run_registry(self):
        """Convenience proxy — every Backend instance shares the runtime's registry."""
        return self.runtime.run_registry

    def _register_plugin_channels(self, runtime: "AgentRuntime") -> None:
        if self.channel_manager is None:
            return
        hook_registry = getattr(runtime, "hook_registry", None)
        if hook_registry is None or not hasattr(hook_registry, "channels"):
            return
        for channel in hook_registry.channels():
            self.channel_manager.register(channel, replace=True)

    async def _restart_running_channels(self, runtime: "AgentRuntime") -> None:
        if self.channel_manager is None:
            return
        await self.channel_manager.restart_all(
            runtime,
            SimpleNamespace(
                backend=self,
                runtime=runtime,
                channel_manager=self.channel_manager,
            ),
        )

    # ─── Push event plumbing ───

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event: PushEvent) -> None:
        """Fan out to matching subscribers."""
        if not self._subscribers:
            return
        for sub in list(self._subscribers):
            if sub.matches(event):
                sub.push(event)

    async def _emit_approval_event(self, payload: dict[str, Any]) -> None:
        # ApprovalBroker hands us a dict with ``type=approval.requested`` plus
        # tool args. Wrap into a PushEvent.
        evt = PushEvent(event="approval.request", payload=payload, seq=self._next_seq())
        self._emit(evt)

    # ─── Chat ───

    async def chat_send(
        self,
        *,
        session_key: str,
        text: str,
        attachments: list[PromptAttachment] | None = None,
        on_local_event: Any = None,
        cancellation_token: CancellationToken | None = None,
        turn_source: str = "tui",
        response_style: str = "",
        voice_id: str = "",
        voice_output: str = "",
        channel_id: str = "",
        channel_account_id: str = "",
        channel_sender_key: str = "",
    ) -> str:
        """Start a turn. ``on_local_event`` and ``cancellation_token`` are
        EmbeddedBackend-only extensions (not part of the Backend Protocol):

        - ``on_local_event``: called synchronously with the typed event
          dataclass for each step. Lets a local TUI feed Rich Live without
          the round-trip through ``subscribe()``'s PushEvent serialization.
          Remote (WebSocket) backends ignore this argument.
        - ``cancellation_token``: lets the caller wire up its own ESC handler
          (the TUI uses this for inline ctrl+c). When omitted, a fresh token
          is created and the only abort path is ``chat_abort``.
        """
        if self._closed:
            raise RuntimeError("backend is closed")

        session = self._resolve_session(session_key)
        if session.lock.locked() or session.active_turn_id is not None:
            raise BusyError(
                "session has an active turn",
                retry_after_ms=500,
                details={"session_id": session.session_id, "active_turn_id": session.active_turn_id},
            )

        turn_id = uuid.uuid4().hex
        token = cancellation_token or CancellationToken()
        session.active_turn_id = turn_id

        task = asyncio.create_task(
            self._run_turn(
                turn_id=turn_id,
                session=session,
                token=token,
                text=text,
                attachments=list(attachments or []),
                on_local_event=on_local_event,
                turn_source=turn_source,
                response_style=response_style,
                voice_id=voice_id,
                voice_output=voice_output,
                channel_id=channel_id,
                channel_account_id=channel_account_id,
                channel_sender_key=channel_sender_key,
            ),
            name=f"backend.chat_send:{turn_id}",
        )
        self._run_registry.register(
            turn_id=turn_id,
            origin="chat",
            cancellation_token=token,
            session_key=session.session_id,
            task=task,
        )
        return turn_id

    async def chat_abort(self, *, turn_id: str) -> None:
        """Cancel any in-flight turn — chat- or cron-triggered — by id."""
        self._run_registry.cancel(turn_id)

    async def await_turn(self, turn_id: str) -> None:
        """EmbeddedBackend-only: block until a turn finishes (success / cancel / error).

        Useful for the local TUI which wants ``await backend.chat_send(...)``
        followed by ``await backend.await_turn(turn_id)`` to mirror the
        original synchronous ``await session.run_turn(...)`` semantics. No-op
        if the turn is already gone.
        """
        entry = self._run_registry.get(turn_id)
        if entry is None or entry.task is None:
            return
        try:
            await entry.task
        except (asyncio.CancelledError, BaseException):
            # Errors are surfaced via push events; await_turn never re-raises.
            pass

    async def chat_history(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
    ) -> HistoryPayload:
        session = self.manager.get_or_load(session_id)
        return HistoryPayload(
            session_id=session.session_id,
            history=self.manager.history_json(session),
            activities=self.manager.activity_json(session),
            last_seq=self._seq,
        )

    # ─── Turn execution ───

    def _resolve_session(self, session_key: str) -> AgentBackendSession:
        # session_key is the caller's notion of "which conversation". For
        # embedded REPL / WebUI it's the session_id. For wechat it'll be the
        # uid (each uid → its own AgentBackendSession). Manager.get_or_load
        # currently keys by session_id, so wechat will need a separate path
        # added in Phase 2; for now we treat session_key == session_id.
        if not session_key:
            return self.manager.get_or_load(None)
        try:
            return self.manager.get_or_load(session_key)
        except KeyError:
            return self.manager.create()

    def _build_turn_registry(
        self,
        session_id: str,
        *,
        channel_id: str = "",
        channel_account_id: str = "",
        channel_sender_key: str = "",
    ) -> ToolRegistry:
        """Per-turn shallow clone with spawn context wired."""
        base = self.runtime.registry
        if self.channel_manager is not None and channel_id and channel_sender_key:
            account_id = channel_account_id or "default"
            adapter = self.channel_manager.get_instance(channel_id, account_id)
            if adapter is not None:
                return adapter.decorate_tools(base, channel_sender_key)
        return base.clone()

    def _wire_spawn_context(
        self,
        registry: ToolRegistry,
        session_id: str,
        on_event: Any,
    ) -> None:
        if registry.get("sessions_spawn") is None:
            return
        from nano_openclaw.features.subagents.tools import SpawnToolContext
        registry.set_spawn_tool_context(SpawnToolContext(
            requester_session_key=session_id,
            session_dir=self.runtime.session_dir,
            workspace_dir=self.runtime.workspace_dir,
            client=self.runtime.client,
            base_cfg=replace(self.runtime.cfg, session_key=session_id),
            on_event=on_event,
            parent_registry=registry,
        ))

    async def _run_turn(
        self,
        *,
        turn_id: str,
        session: AgentBackendSession,
        token: CancellationToken,
        text: str,
        attachments: list[PromptAttachment],
        on_local_event: Any = None,
        turn_source: str = "tui",
        response_style: str = "",
        voice_id: str = "",
        voice_output: str = "",
        channel_id: str = "",
        channel_account_id: str = "",
        channel_sender_key: str = "",
    ) -> None:
        history_len_before = len(session.history)
        activity_started_at = time.time()
        activity_payloads: list[dict[str, Any]] = []

        turn_registry = self._build_turn_registry(
            session.session_id,
            channel_id=channel_id,
            channel_account_id=channel_account_id,
            channel_sender_key=channel_sender_key,
        )

        async def _request_approval(request: Any, cancellation_token: Any | None = None) -> Any:
            return await self._approval_broker.request_decision(
                request,
                cancellation_token,
                origin="embedded",
                turn_id=turn_id,
            )

        turn_registry.approval_handler = _request_approval

        def on_event(event: Any) -> None:
            # Embedded-only callback receives the typed dataclass first so
            # the local TUI can render Rich Live without payload re-marshaling.
            if on_local_event is not None:
                try:
                    on_local_event(event)
                except Exception as exc:  # noqa: BLE001 — never let renderer break the turn
                    log.warning("backend.on_local_event.error", f"{type(exc).__name__}: {exc}")
            payload = event_to_payload(event, turn_id, session.session_id)
            payload["session_key"] = session.session_id
            if is_replayable_activity_payload(payload):
                activity_payloads.append(payload)
            self._emit(PushEvent(event="agent.event", payload=payload, seq=self._next_seq()))

        self._wire_spawn_context(turn_registry, session.session_id, on_event=on_event)

        cfg = replace(
            self.runtime.cfg,
            session_key=session.session_id,
            turn_source=turn_source,
            response_style=response_style,
            voice_id=voice_id,
            voice_output=voice_output,
        )

        # Notify subscribers turn started.
        self._emit(
            PushEvent(
                event="agent.event",
                payload={
                    "type": "turn.started",
                    "turn_id": turn_id,
                    "session_id": session.session_id,
                    "session_key": session.session_id,
                    "user_text": text,
                    "attachments": [
                        {"name": a.name, "mime": a.mime, "size": a.size}
                        for a in attachments
                    ],
                    "voice_id": voice_id,
                    "voice_output": voice_output,
                    "voice_ssml": response_style == "voice"
                    and voice_output.startswith("aliyun")
                    and is_emotion_voice(voice_id),
                },
                seq=self._next_seq(),
            )
        )

        try:
            # Hold the runtime-update reader for the entire turn so a concurrent
            # ``runtime.update`` is told BUSY rather than silently swapping the
            # client / registry mid-flight.
            async with self.runtime.runtime_guard.reader():
                async with session.lock:
                    agent_session = AgentSession(
                        history=session.history,
                        registry=turn_registry,
                        on_event=on_event,
                        client=self.runtime.client,
                        cfg=cfg,
                        transcript_writer=session.writer,
                        cancellation_token=token,
                        # Share long-lived per-conversation state by reference so
                        # cumulative tokens, last_prompt_tokens, and
                        # previous_summary survive across turns.
                        usage_stats=session.usage_stats,
                        compaction_state=session.compaction_state,
                        todo_store=session.todo_store,
                    )
                    await agent_session.run_turn(
                        text,
                        attachments=attachments or None,
                        attachment_turn_id=turn_id,
                    )
                    self.manager.save_metadata(session)
                    activity = {
                        "turn_id": turn_id,
                        "session_id": session.session_id,
                        "insert_after_index": history_len_before,
                        "duration_ms": max(0, int((time.time() - activity_started_at) * 1000)),
                        "payloads": [
                            *jsonable(activity_payloads),
                            {
                                "type": "turn.done",
                                "turn_id": turn_id,
                                "session_id": session.session_id,
                            },
                        ],
                    }
                    session.activities.append(activity)
                    session.writer.append_activity(activity)

            self._emit(
                PushEvent(
                    event="agent.event",
                    payload={
                        "type": "turn.done",
                        "turn_id": turn_id,
                        "session_id": session.session_id,
                        "session_key": session.session_id,
                    },
                    seq=self._next_seq(),
                )
            )
        except TurnCancelled:
            self._emit(
                PushEvent(
                    event="agent.event",
                    payload={
                        "type": "turn.cancelled",
                        "turn_id": turn_id,
                        "session_id": session.session_id,
                        "session_key": session.session_id,
                    },
                    seq=self._next_seq(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to subscribers
            log.warning("backend.turn.error", f"{type(exc).__name__}: {exc}")
            self._emit(
                PushEvent(
                    event="agent.event",
                    payload={
                        "type": "turn.error",
                        "turn_id": turn_id,
                        "session_id": session.session_id,
                        "session_key": session.session_id,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                    seq=self._next_seq(),
                )
            )
        finally:
            if session.active_turn_id == turn_id:
                session.active_turn_id = None
            self._run_registry.unregister(turn_id)
            # Reader released with the ``async with`` above — now safe to apply
            # any thinking-level change the model queued via ``set_thinking``.
            await self._flush_pending_thinking()
            self._emit(
                PushEvent(
                    event="session.changed",
                    payload={"session_id": session.session_id, "session_key": session.session_id},
                    seq=self._next_seq(),
                )
            )

    # ─── Sessions ───

    async def sessions_list(self) -> SessionList:
        items = self.manager.list()
        sessions = [
            SessionInfo(
                session_id=item["session_id"],
                title=item["title"],
                preview=item["preview"],
                created_at=item.get("created_at") or 0.0,
                updated_at=item.get("updated_at") or 0.0,
                model=item.get("model") or "",
                message_count=item.get("message_count") or 0,
                compaction_count=item.get("compaction_count") or 0,
                current=bool(item.get("current")),
                active_turn_id=item.get("active_turn_id"),
            )
            for item in items
        ]
        last_session_id = next((s.session_id for s in sessions if s.current), None)
        return SessionList(sessions=sessions, last_session_id=last_session_id)

    async def sessions_get(self, session_id: str) -> SessionDetails:
        try:
            session = self.manager.get_or_load(session_id)
        except KeyError as exc:
            raise NotFoundError(str(exc)) from exc
        visible = display_history(session.history)
        title = visible[0].content[0].get("text", session.session_id[:8])[:42] if visible else session.session_id[:8]
        return SessionDetails(
            session_id=session.session_id,
            title=str(title),
            history=self.manager.history_json(session),
            activities=self.manager.activity_json(session),
            model=self.runtime.model_id,
            active_turn_id=session.active_turn_id,
        )

    async def sessions_delete(self, session_id: str) -> None:
        """Remove a session entirely: store entry + transcript file + manager cache.

        Refuses to delete the session that's currently mid-turn (active_turn_id
        set) — the cron / chat path that owns the lock would lose its writer
        out from under it. Emits ``session.changed`` so subscribers can drop
        the entry from their lists.
        """
        from nano_openclaw.session import (
            list_sessions,
            load_session_store,
            save_session_store,
        )

        # Permit deleting either a known-loaded session or a disk-only one.
        loaded = self.manager._loaded.get(session_id)
        if loaded is not None and loaded.active_turn_id is not None:
            raise BusyError(
                f"session {session_id} has an active turn",
                retry_after_ms=2000,
                details={"session_id": session_id, "active_turn_id": loaded.active_turn_id},
            )

        # Drop from manager cache + summary cache
        self.manager._loaded.pop(session_id, None)
        self.manager._summary_cache.pop(session_id, None)
        self.manager._unmark_pending(session_id)

        # Remove transcript file (idempotent — if it never existed, fine)
        transcript_path = self.manager.session_dir / f"{session_id}.jsonl"
        try:
            transcript_path.unlink()
        except FileNotFoundError:
            pass

        # Remove from on-disk session store
        store = load_session_store(self.manager.store_path)
        sessions_dict = store.get("sessions", {})
        existed = sessions_dict.pop(session_id, None) is not None
        if not existed:
            # Some legacy stores keyed by transcript header id rather than file
            # name; fall back to scanning by stem (matches ``_load_existing``
            # rescue path).
            for sid, entry in list(sessions_dict.items()):
                if isinstance(entry, dict) and entry.get("session_id") == session_id:
                    sessions_dict.pop(sid, None)
                    existed = True
                    break
        if store.get("lastSessionId") == session_id:
            store["lastSessionId"] = None
        save_session_store(self.manager.store_path, store)

        if not existed and not transcript_path.exists():
            raise NotFoundError(f"session not found: {session_id}")

        self._emit(
            PushEvent(
                event="session.changed",
                payload={"session_id": session_id, "reason": "deleted"},
                seq=self._next_seq(),
            )
        )

    async def sessions_reset(
        self,
        session_key: str,
        *,
        reason: str = "reset",
    ) -> SessionInfo:
        if reason == "new":
            session = self.manager.create()
        else:
            session = await self.manager.clear(session_key)
        info = SessionInfo(
            session_id=session.session_id,
            title=session.session_id[:8],
            preview="",
            created_at=session.created_at,
            updated_at=session.created_at,
            model=self.runtime.model_id,
            message_count=session.writer.message_count,
            compaction_count=session.writer.compaction_count,
            current=True,
            active_turn_id=session.active_turn_id,
        )
        self._emit(
            PushEvent(
                event="session.changed",
                payload={"session_id": session.session_id, "reason": reason},
                seq=self._next_seq(),
            )
        )
        return info

    async def sessions_compact(self, session_key: str) -> CompactionResult:
        """Force-compact a session's history. Returns tokens-before/after.

        Uses ``compact_if_needed`` with ``threshold_ratio=1.0`` so any history
        that fits the budget triggers a single compaction pass — same
        semantics as the legacy embedded-mode ``/compact`` slash command.
        """
        from nano_openclaw.core.compact import compact_if_needed, estimate_tokens

        try:
            session = self.manager.get_or_load(session_key or None)
        except KeyError as exc:
            raise NotFoundError(str(exc)) from exc

        cfg = self.runtime.cfg
        if len(session.history) < cfg.context_recent_turns * 2:
            return CompactionResult(
                success=False,
                summary="not enough history to compact",
                tokens_before=estimate_tokens(session.history),
                tokens_after=estimate_tokens(session.history),
            )

        tokens_before = estimate_tokens(session.history)
        # Hold the session lock so a concurrent ``chat.send`` can't mutate
        # history mid-compaction. Backend's lock-locked check there will
        # surface BUSY to the user, which is the right outcome.
        async with session.lock:
            _, summary = await compact_if_needed(
                session.history,
                budget=1,
                client=self.runtime.client,
                model=cfg.model,
                api=cfg.api,
                threshold_ratio=1.0,
                recent_turns=cfg.context_recent_turns,
            )
            if summary:
                reminder = append_active_todo_reminder(session.history, session.todo_store)
                session.writer.append_compaction(summary)
                if reminder is not None:
                    session.writer.append_message(reminder)
                self.manager.save_metadata(session)
        tokens_after = estimate_tokens(session.history)
        self._emit(
            PushEvent(
                event="session.changed",
                payload={"session_id": session.session_id, "reason": "compacted"},
                seq=self._next_seq(),
            )
        )
        return CompactionResult(
            success=True,
            summary=None,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    async def sessions_usage(self, session_key: str) -> SessionUsageReport:
        """Snapshot one session's token + cache + compaction counters.

        Reads ``AgentBackendSession.usage_stats`` (maintained by the loop
        on every ``MessageEnd``) and combines it with budget / cache_ttl
        from the active runtime config.
        """
        try:
            session = self.manager.get_or_load(session_key or None)
        except KeyError as exc:
            raise NotFoundError(str(exc)) from exc

        stats = session.usage_stats
        cfg = self.runtime.cfg
        return SessionUsageReport(
            session_id=session.session_id,
            last_prompt_tokens=stats.last_prompt_tokens,
            last_output_tokens=stats.last_output_tokens,
            last_cache_read_tokens=stats.last_cache_read_tokens,
            last_cache_creation_tokens=stats.last_cache_creation_tokens,
            total_prompt_tokens=stats.total_prompt_tokens,
            total_output_tokens=stats.total_output_tokens,
            total_cache_read_tokens=stats.total_cache_read_tokens,
            total_cache_creation_tokens=stats.total_cache_creation_tokens,
            compactions_fired=stats.compactions_fired,
            turns_recorded=stats.turns_recorded,
            cache_hit_ratio=stats.cache_hit_ratio(),
            context_budget=cfg.context_budget,
            context_window=cfg.context_window,
            cache_ttl=cfg.cache_ttl,
        )

    async def get_todos(self, session_key: str) -> list[dict[str, Any]]:
        """Return the current TODO list for the addressed session."""
        try:
            session = self.manager.get_or_load(session_key or None)
        except KeyError as exc:
            raise NotFoundError(str(exc)) from exc
        return session.todo_store.read()

    # ─── Approvals ───

    async def approvals_list(self) -> list[PendingApproval]:
        result: list[PendingApproval] = []
        for pending in self._approval_broker.list_pending():
            req = pending.request
            result.append(
                PendingApproval(
                    request_id=req.request_id,
                    tool_name=req.tool_name,
                    tool_args=dict(req.tool_args),
                    risk_level=req.risk_level,
                    reason=req.reason,
                    timestamp=req.timestamp,
                    origin=pending.origin,
                    turn_id=pending.turn_id,
                )
            )
        return result

    async def approvals_respond(
        self,
        request_id: str,
        *,
        allow: bool,
        scope: str = "once",
        reason: str = "",
    ) -> None:
        wire_decision = "allow-always" if (allow and scope == "always") else ("allow-once" if allow else "deny")
        if not self._approval_broker.decide(request_id, wire_decision):
            raise NotFoundError(f"unknown approval request: {request_id}")

    # ─── Models / runtime ───

    async def models_list(self) -> list[ModelChoice]:
        # Enumerate every model declared under ``config.models.providers``,
        # marking ``is_default=True`` for the row whose ``ref`` matches the
        # active runtime. When providers is empty (e.g. minimal config), fall
        # back to a single-entry list synthesized from the runtime so the
        # caller never sees an empty catalog.
        current_ref = self.runtime.model_ref
        providers = getattr(getattr(self.runtime.config, "models", None), "providers", None) or {}
        choices: list[ModelChoice] = []
        for provider_id, provider in providers.items():
            for model in getattr(provider, "models", []) or []:
                ref = f"{provider_id}/{model.id}"
                choices.append(
                    ModelChoice(
                        ref=ref,
                        id=model.id,
                        provider=provider_id,
                        context_window=model.contextWindow or None,
                        is_default=(ref == current_ref),
                        name=model.name or model.id,
                        input=tuple(model.input or ()),
                        reasoning=bool(model.reasoning),
                        max_tokens=model.maxTokens or None,
                    )
                )
        if not choices:
            choices.append(
                ModelChoice(
                    ref=current_ref,
                    id=self.runtime.model_id,
                    provider=current_ref.split("/", 1)[0] if "/" in current_ref else "",
                    context_window=self.runtime.cfg.context_window or None,
                    is_default=True,
                    name=self.runtime.model_id,
                    input=("text",),
                )
            )
        return choices

    async def runtime_get(self) -> RuntimeSnapshot:
        cfg = self.runtime.cfg
        return RuntimeSnapshot(
            agent_id=self.runtime.agent_id,
            model_ref=self.runtime.model_ref,
            model_id=self.runtime.model_id,
            image_model_ref=self.runtime.image_model_ref,
            thinking_level=cfg.thinking_level,
            workspace_dir=str(self.runtime.workspace_dir),
            state_dir=str(self.runtime.state_dir),
            context_budget=cfg.context_budget,
            context_threshold=cfg.context_threshold,
            context_recent_turns=cfg.context_recent_turns,
            context_window=cfg.context_window,
        )

    def queue_thinking_level(self, level: str) -> None:
        """Queue a thinking-level change to apply once the current turn ends.

        Called by the ``set_thinking`` LLM tool, which always runs inside a
        turn (reader held), so it cannot call ``runtime_update`` directly.
        ``_run_turn``'s finally flushes this after the reader releases, so the
        change lands on the global ``runtime.cfg`` and takes effect from the
        next turn — identical to a user typing ``/thinking <level>``.
        """
        self._pending_thinking_level = level

    async def _flush_pending_thinking(self) -> None:
        """Apply a queued ``set_thinking`` change after the turn's reader is
        released. Re-queues on BusyError (another turn still in flight) so the
        next turn's flush retries; never raises into the turn's finally."""
        level = self._pending_thinking_level
        if level is None:
            return
        self._pending_thinking_level = None
        try:
            await self.runtime_update(thinking_level=level)
        except BusyError:
            # Concurrent turn still holds the reader — keep the request and
            # let the next turn's flush apply it.
            self._pending_thinking_level = level
        except Exception as exc:  # noqa: BLE001 — never break the turn's finally
            log.warning("backend.thinking.flush_failed", f"{type(exc).__name__}: {exc}")

    async def runtime_update(
        self,
        *,
        agent_id: str | None = None,
        model_ref: str | None = None,
        image_model_ref: str | None = None,
        thinking_level: str | None = None,
    ) -> RuntimeSnapshot:
        # Hot-reload: ``RuntimeUpdateGuard.writer()`` raises BusyError
        # immediately if any reader (chat / cron) holds, so callers see a
        # quick "model busy" instead of a half-swapped runtime. Inside the
        # writer block we rebuild via ``build_agent_runtime`` and swap
        # ``self.runtime``. Because ``agent_id`` is preserved (or carried
        # over from the old runtime), ``session_dir`` / ``store_path`` /
        # transcript files are unchanged — every loaded session continues
        # working transparently. We update ``self.manager.model`` (not the
        # manager instance) so any AgentBackendSession that callers already
        # hold remains connected to the same per-session transcripts.
        from nano_openclaw.core.runtime import image_model_id_from_ref
        from nano_openclaw.services.runtime_factory import build_agent_runtime

        async with self.runtime.runtime_guard.writer():
            old = self.runtime
            # Heavy rebuild only when agent_id or model_ref actually change —
            # those swap the model client + tool registry. ``thinking_level``
            # and ``image_model_ref`` are pure config-field updates and don't
            # need build_agent_runtime; doing them in place avoids tearing
            # down the LLM client and dropping in-memory caches every time
            # the user toggles thinking.
            agent_changed = agent_id is not None and agent_id != old.agent_id
            model_changed = model_ref is not None and model_ref != old.model_ref

            if agent_changed or model_changed:
                target_agent = agent_id or old.agent_id
                # ``image_model_ref=None`` means "leave alone"; passing through
                # would zero out the image model in build_agent_runtime.
                target_image_ref = (
                    image_model_ref if image_model_ref is not None else old.image_model_ref
                )
                new_runtime = await build_agent_runtime(
                    config_path=old.config_path,
                    agent_id=target_agent,
                    model_ref_override=model_ref or old.model_ref,
                    image_model_ref_override=target_image_ref,
                    run_registry=old.run_registry,
                    runtime_guard=_new_runtime_guard(),
                )
                if thinking_level is not None:
                    new_runtime.cfg.thinking_level = thinking_level
                try:
                    self._register_plugin_channels(new_runtime)
                except Exception:
                    try:
                        await new_runtime.close()
                    except Exception as close_exc:  # noqa: BLE001
                        log.warning(
                            "runtime_update.new_close",
                            f"{type(close_exc).__name__}: {close_exc}",
                        )
                    raise
                self.runtime = new_runtime
                # Keep the manager instance (callers hold its sessions); just
                # refresh metadata new transcripts will be tagged with.
                self.manager.model = new_runtime.model_id
                self.manager.cwd = str(new_runtime.workspace_dir)
                await self._restart_running_channels(new_runtime)
                close_old = True
            else:
                # Light path — mutate the existing runtime in place. Still
                # under writer() so we don't race a chat turn reading the
                # field mid-flight.
                if thinking_level is not None:
                    old.cfg.thinking_level = thinking_level
                if image_model_ref is not None:
                    old.image_model_ref = image_model_ref
                    old.cfg.image_model = image_model_id_from_ref(image_model_ref)
                close_old = False

            snapshot = await self.runtime_get()
            self._emit(
                PushEvent(
                    event="runtime.changed",
                    payload={
                        "agent_id": snapshot.agent_id,
                        "model_ref": snapshot.model_ref,
                        "model_id": snapshot.model_id,
                        "image_model_ref": snapshot.image_model_ref,
                        "thinking_level": snapshot.thinking_level,
                    },
                    seq=self._next_seq(),
                )
            )
            if close_old:
                try:
                    await old.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("runtime_update.old_close", f"{type(exc).__name__}: {exc}")
            return snapshot

    # ─── Channels ───

    async def channels_status(self) -> list[ChannelStatusEntry]:
        if self.channel_manager is None:
            return []
        return [
            ChannelStatusEntry(
                channel_id=entry.channel_id,
                account_id=entry.account_id,
                state=entry.state,
                error=entry.error,
                started_at=entry.started_at,
            )
            for entry in self.channel_manager.list_status()
        ]

    async def channels_start(
        self,
        channel_id: str,
        account_id: str | None = None,
    ) -> ChannelStatusEntry:
        if self.channel_manager is None:
            raise NotImplementedError("channels_start: channel manager not configured")
        from nano_openclaw.services.channels import ChannelAccount

        instance = await self.channel_manager.start(
            channel_id,
            ChannelAccount(id=account_id or "default", config={}),
            self.runtime,
            SimpleNamespace(
                backend=self,
                runtime=self.runtime,
                channel_manager=self.channel_manager,
            ),
        )
        entry = instance.status()
        return ChannelStatusEntry(
            channel_id=entry.channel_id,
            account_id=entry.account_id,
            state=entry.state,
            error=entry.error,
            started_at=entry.started_at,
        )

    async def channels_stop(
        self,
        channel_id: str,
        account_id: str | None = None,
    ) -> ChannelStatusEntry:
        if self.channel_manager is None:
            raise NotImplementedError("channels_stop: channel manager not configured")
        resolved_account_id = account_id or "default"
        stopped = await self.channel_manager.stop(channel_id, resolved_account_id)
        if not stopped:
            instance = self.channel_manager.get_instance(channel_id, resolved_account_id)
            if instance is not None:
                entry = instance.status()
                return ChannelStatusEntry(
                    channel_id=entry.channel_id,
                    account_id=entry.account_id,
                    state=entry.state,
                    error=entry.error or "stop failed",
                    started_at=entry.started_at,
                )
        return ChannelStatusEntry(
            channel_id=channel_id,
            account_id=resolved_account_id,
            state="stopped",
            error=None,
            started_at=None,
        )

    # ─── Slash commands ───

    async def slash_run(self, command: str, session_key: str = "") -> SlashRunResult:
        from nano_openclaw.services.slash import QuitREPL, handle_slash
        from nano_openclaw.services.slash_renderer import MarkdownRenderer

        renderer = MarkdownRenderer()
        state = {"session_key": session_key, "session_changed": False}
        try:
            handled = await handle_slash(command, self, renderer, state)
        except QuitREPL:
            handled = True
            renderer.text("_(This frontend cannot quit the daemon.)_")
        return SlashRunResult(
            handled=handled,
            text=renderer.collect(),
            session_key=str(state.get("session_key") or session_key),
            session_changed=bool(state.get("session_changed")),
        )

    # ─── Subagents ───

    async def subagents_list(self) -> list[SubagentInfo]:
        from nano_openclaw.features.subagents.runner import get_runner
        runner = get_runner(None)  # type: ignore[arg-type]  # accepts None per existing convention
        if runner is None:
            return []
        out: list[SubagentInfo] = []
        for record in runner.registry.list_active():
            out.append(
                SubagentInfo(
                    run_id=record.run_id,
                    label=getattr(record, "label", None),
                    task=getattr(record, "task", ""),
                    status=getattr(record, "status", "running"),
                    started_at=getattr(record, "started_at", None),
                )
            )
        return out

    async def subagents_kill(self, run_id: str) -> None:
        from nano_openclaw.features.subagents.runner import get_runner
        runner = get_runner(None)  # type: ignore[arg-type]
        if runner is None:
            raise NotFoundError(f"no subagent runner")
        await runner.kill(run_id)

    # ─── Features: active-memory / dreaming ───

    async def active_memory_get(self) -> dict[str, Any]:
        """Snapshot the active-memory config off ``runtime.cfg``.

        Returns the canonical dict shape RPC clients consume; ``configured``
        is False when the agent never had active-memory wired (cfg field is
        None) so the WebUI can show a "not configured" hint instead of zeros.
        """
        cfg = self.runtime.cfg.active_memory_config
        if cfg is None:
            return {"configured": False, "enabled": False}
        return {
            "configured": True,
            "enabled": cfg.enabled,
            "query_mode": cfg.query_mode.value,
            "prompt_style": cfg.prompt_style.value,
            "timeout_ms": cfg.timeout_ms,
            "model": cfg.model,
            "thinking": cfg.thinking,
            "max_summary_chars": cfg.max_summary_chars,
            "recent_user_turns": cfg.recent_user_turns,
            "recent_assistant_turns": cfg.recent_assistant_turns,
            "logging": cfg.logging,
        }

    async def active_memory_set(self, **fields: Any) -> dict[str, Any]:
        """Mutate the active-memory config in place. Unknown fields → ignored.

        Lazily constructs a default ``ActiveMemoryConfig`` if the runtime
        was started without one, so toggling on works for agents that
        merely declined to enable it at boot.
        """
        from nano_openclaw.features.memory.active import ActiveMemoryConfig, PromptStyle, QueryMode

        if self.runtime.cfg.active_memory_config is None:
            self.runtime.cfg.active_memory_config = ActiveMemoryConfig(enabled=False)
        cfg = self.runtime.cfg.active_memory_config

        if "enabled" in fields:
            cfg.enabled = bool(fields["enabled"])
        if "query_mode" in fields:
            try:
                cfg.query_mode = QueryMode(str(fields["query_mode"]))
            except ValueError as exc:
                raise BackendError(f"invalid query_mode: {fields['query_mode']!r}") from exc
        if "prompt_style" in fields:
            try:
                cfg.prompt_style = PromptStyle(str(fields["prompt_style"]))
            except ValueError as exc:
                raise BackendError(f"invalid prompt_style: {fields['prompt_style']!r}") from exc
        if "timeout_ms" in fields:
            cfg.timeout_ms = int(fields["timeout_ms"])
        if "model" in fields:
            cfg.model = fields["model"]
        if "thinking" in fields:
            cfg.thinking = str(fields["thinking"])
        if "max_summary_chars" in fields:
            cfg.max_summary_chars = int(fields["max_summary_chars"])
        if "recent_user_turns" in fields:
            cfg.recent_user_turns = int(fields["recent_user_turns"])
        if "recent_assistant_turns" in fields:
            cfg.recent_assistant_turns = int(fields["recent_assistant_turns"])
        if "logging" in fields:
            cfg.logging = bool(fields["logging"])

        return await self.active_memory_get()

    async def dreaming_get(self) -> dict[str, Any]:
        """Snapshot dreaming config + runtime status (candidates / due / etc).

        ``configured=False`` indicates the agent boot didn't wire dreaming;
        ``status`` is None when there's no workspace dir to scan against.
        """
        cfg = self.runtime.cfg.dreaming_config
        if cfg is None:
            return {"configured": False, "enabled": False}

        payload: dict[str, Any] = {
            "configured": True,
            "enabled": cfg.enabled,
            "frequency": cfg.frequency,
            "min_score": cfg.min_score,
            "min_recall_count": cfg.min_recall_count,
            "min_unique_queries": cfg.min_unique_queries,
            "max_promotions": cfg.max_promotions,
            "diary": cfg.diary,
            "model": cfg.model,
        }

        workspace_dir = str(self.runtime.workspace_dir) if self.runtime.workspace_dir else None
        if workspace_dir:
            from nano_openclaw.features.memory.dreaming import get_dreaming_status
            try:
                payload["status"] = get_dreaming_status(workspace_dir, cfg)
            except Exception as exc:  # noqa: BLE001 — surface but don't crash
                payload["status"] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            payload["status"] = None
        return payload

    async def dreaming_set(self, **fields: Any) -> dict[str, Any]:
        """Mutate dreaming config in place. Lazy-init the same as active_memory_set."""
        from nano_openclaw.features.memory.dreaming import DreamingConfig

        if self.runtime.cfg.dreaming_config is None:
            self.runtime.cfg.dreaming_config = DreamingConfig(enabled=True)
        cfg = self.runtime.cfg.dreaming_config

        if "enabled" in fields:
            cfg.enabled = bool(fields["enabled"])
        if "frequency" in fields:
            cfg.frequency = str(fields["frequency"])
        if "min_score" in fields:
            cfg.min_score = float(fields["min_score"])
        if "min_recall_count" in fields:
            cfg.min_recall_count = int(fields["min_recall_count"])
        if "min_unique_queries" in fields:
            cfg.min_unique_queries = int(fields["min_unique_queries"])
        if "max_promotions" in fields:
            cfg.max_promotions = int(fields["max_promotions"])
        if "diary" in fields:
            cfg.diary = bool(fields["diary"])
        if "model" in fields:
            cfg.model = fields["model"]

        return await self.dreaming_get()

    async def dreaming_run(self) -> dict[str, Any]:
        """Run a dreaming sweep synchronously and return the result.

        Long-running (LLM call) — clients should expect a multi-second
        response. ``NotFoundError`` if dreaming was never configured;
        ``BackendError`` if there's no workspace to scan.
        """
        from nano_openclaw.features.memory.dreaming import run_dreaming

        cfg = self.runtime.cfg.dreaming_config
        if cfg is None:
            raise NotFoundError("dreaming not configured for this agent")
        workspace_dir = str(self.runtime.workspace_dir) if self.runtime.workspace_dir else None
        if not workspace_dir:
            raise BackendError("no workspace_dir on this runtime; dreaming.run unavailable")

        result = await run_dreaming(
            workspace_dir,
            cfg,
            self.runtime.cfg.model,
            api_client=self.runtime.client,
        )
        return {
            "elapsed_ms": result.elapsed_ms,
            "candidates": len(result.candidates),
            "promoted": [
                {
                    "path": entry.path,
                    "start_line": entry.start_line,
                    "score": score,
                    "preview": content[:120],
                }
                for entry, score, content in result.promoted
            ],
        }

    # ─── Review Fork ───

    async def review_fork_get(self) -> dict[str, Any]:
        """Snapshot review-fork config + runtime status.

        ``configured=False`` indicates the plugin never registered (e.g. it's
        not in the plugin loader output). ``enabled`` reflects the live cfg
        flag — flipping it via ``review_fork_set`` takes effect immediately.
        """
        from nano_openclaw.features.review_fork.plugin import get_state

        st = get_state()
        if st is None:
            return {"configured": False, "enabled": False}
        payload = {"configured": True}
        payload.update(st.status())
        return payload

    async def review_fork_set(self, **fields: Any) -> dict[str, Any]:
        """Mutate review-fork config in place.

        Accepts a subset of: enabled / trigger_n / cooldown_s / timeout_s /
        model_aux. Unknown keys are ignored. Returns the updated snapshot.
        ``NotFoundError`` if the plugin never registered.
        """
        from nano_openclaw.features.review_fork.plugin import get_state

        st = get_state()
        if st is None:
            raise NotFoundError("review-fork plugin not loaded")
        cfg = st.cfg
        if "enabled" in fields:
            cfg.enabled = bool(fields["enabled"])
        if "trigger_n" in fields:
            cfg.trigger_n = max(1, int(fields["trigger_n"]))
        if "cooldown_s" in fields:
            cfg.cooldown_s = max(0, int(fields["cooldown_s"]))
        if "timeout_s" in fields:
            cfg.timeout_s = max(1, int(fields["timeout_s"]))
        if "model_aux" in fields:
            v = fields["model_aux"]
            cfg.model_aux = str(v) if v else None
        return await self.review_fork_get()

    async def review_fork_run(self, session_key: str | None = None) -> dict[str, Any]:
        """Force-trigger a review-fork run for the given session, bypassing N + cooldown.

        Returns ``{"run_id": "...", "skipped": False}`` on spawn success, or
        ``{"run_id": None, "skipped": True, "reason": "..."}`` when the plugin
        decided not to spawn (e.g. concurrency cap, plugin disabled).
        ``NotFoundError`` if the plugin never registered.
        """
        from nano_openclaw.features.review_fork.plugin import get_state

        st = get_state()
        if st is None:
            raise NotFoundError("review-fork plugin not loaded")
        if not st.cfg.enabled:
            return {"run_id": None, "skipped": True, "reason": "plugin disabled (set enabled=true first)"}
        target_session_key = session_key or self.runtime.cfg.session_key
        try:
            session = self._resolve_session(target_session_key)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not resolve session {target_session_key!r}: {exc}") from exc
        messages_snapshot = [
            {"role": m.role, "content": m.content} for m in session.history
        ]
        transcript_path = (
            str(session.transcript_path) if getattr(session, "transcript_path", None) else None
        )
        session_dir = str(self.runtime.session_dir) if self.runtime.session_dir else ""
        workspace_dir = str(self.runtime.workspace_dir) if self.runtime.workspace_dir else ""
        if not workspace_dir:
            raise BackendError("no workspace_dir on this runtime; review_fork.run unavailable")
        agent_id = "default"
        try:
            from nano_openclaw.features.subagents.types import parse_session_key
            parsed = parse_session_key(target_session_key)
            agent_id = parsed.get("agentId", "default")
        except Exception:
            pass
        payload = {
            "session_id": session.session_id,
            "agent_id": agent_id,
            "session_key": target_session_key,
            "session_dir": session_dir,
            "transcript_path": transcript_path,
            "workspace_dir": workspace_dir,
            "stop_reason": "manual",
            "iteration_count": 0,
            "tools_used": [],
            "messages_snapshot": messages_snapshot,
            "user_input": "",
            "client": self.runtime.client,
            "loop_config": self.runtime.cfg,
            "tool_registry": self.runtime.registry,
        }
        run_id = await st.force_fork(payload)
        if run_id is None:
            return {
                "run_id": None,
                "skipped": True,
                "reason": st.last_skip_reason or "unknown",
            }
        return {"run_id": run_id, "skipped": False}

    # ─── Curator Lite ───

    async def curator_get(self) -> dict[str, Any]:
        from nano_openclaw.features.skills.curator import status

        state_dir = str(self.runtime.state_dir) if self.runtime.state_dir else ""
        if not state_dir:
            return {"configured": False}
        return status(state_dir)

    async def curator_set(self, **fields: Any) -> dict[str, Any]:
        from nano_openclaw.features.skills.curator import set_enabled, set_paused

        state_dir = str(self.runtime.state_dir) if self.runtime.state_dir else ""
        if not state_dir:
            raise BackendError("no state_dir on this runtime; curator unavailable")
        if "enabled" in fields:
            return set_enabled(state_dir, bool(fields["enabled"]))
        if "paused" in fields:
            return set_paused(state_dir, bool(fields["paused"]))
        return await self.curator_get()

    async def curator_run(self, dry_run: bool = False) -> dict[str, Any]:
        from nano_openclaw.features.skills.curator import run

        state_dir = str(self.runtime.state_dir) if self.runtime.state_dir else ""
        if not state_dir:
            raise BackendError("no state_dir on this runtime; curator unavailable")
        return run(state_dir, dry_run=dry_run)

    # ─── Checkpoints ───

    async def checkpoint_list(self) -> dict[str, Any]:
        from nano_openclaw.features.checkpoint.service import list_checkpoints

        state_dir = str(self.runtime.state_dir) if self.runtime.state_dir else ""
        checkpoints = [cp.__dict__ for cp in list_checkpoints(state_dir)]
        return {"checkpoints": checkpoints}

    async def checkpoint_create(self, reason: str = "manual") -> dict[str, Any]:
        from nano_openclaw.features.checkpoint.service import create_checkpoint

        state_dir = str(self.runtime.state_dir) if self.runtime.state_dir else ""
        workspace_dir = str(self.runtime.workspace_dir) if self.runtime.workspace_dir else ""
        cp = create_checkpoint(state_dir, workspace_dir, reason=reason or "manual")
        if cp is None:
            raise BackendError("checkpoint.create unavailable without state_dir and workspace_dir")
        return {"checkpoint": cp.__dict__}

    async def checkpoint_restore(self, checkpoint_id: str) -> dict[str, Any]:
        from nano_openclaw.features.checkpoint.service import restore_checkpoint

        state_dir = str(self.runtime.state_dir) if self.runtime.state_dir else ""
        workspace_dir = str(self.runtime.workspace_dir) if self.runtime.workspace_dir else ""
        cp = restore_checkpoint(state_dir, checkpoint_id, workspace_dir=workspace_dir)
        if cp is None:
            raise NotFoundError(f"checkpoint not found or ambiguous: {checkpoint_id}")
        return {"restored": cp.__dict__}

    async def mcp_status(self) -> dict[str, Any]:
        from nano_openclaw.features.mcp.plugin import mcp_status_for_runtime

        return mcp_status_for_runtime(self.runtime)

    # ─── Introspection (tools / skills / plugins / hooks) ───

    async def tools_list(self) -> list[dict[str, Any]]:
        """Tool name + description pairs from the active registry.

        Mirrors what ``runtime.registry.names()`` exposes; the description
        field is included so a frontend can render a help table without a
        second round-trip.
        """
        out: list[dict[str, Any]] = []
        for name, tool in self.runtime.registry._tools.items():
            description = (getattr(tool, "description", "") or "").strip()
            out.append({"name": name, "description": description})
        out.sort(key=lambda item: item["name"])
        return out

    async def skills_list(self) -> list[dict[str, Any]]:
        """All skills (eligible + blocked) with status fields for the
        slash-command Table renderer. Empty when no workspace is configured.

        Phase 9 enrichment: returns ALL entries (not just eligible ones) plus
        ``in_prompt`` and ``reason`` so the remote-mode TUI can render the
        same Table cli.py renders.
        """
        from nano_openclaw.features.skills import (
            filter_eligible_skills,
            filter_visible_skills,
            get_or_load_skills,
        )

        cfg = self.runtime.cfg
        workspace = cfg.workspace_dir
        if not workspace:
            return []
        try:
            all_entries = get_or_load_skills(
                workspace,
                cfg.session_key,
                extra_dirs=cfg.extra_skill_dirs,
                max_bytes=cfg.max_skill_file_bytes,
            )
            # filter_eligible_skills mutates entries in place to set ``eligible``
            # + ``eligibilityReason``; filter_visible_skills returns the subset
            # that actually makes it into the prompt.
            filter_eligible_skills(all_entries, skill_filter=cfg.skill_filter)
            visible_skills = set(id(s) for s in filter_visible_skills(all_entries))
        except Exception as exc:  # noqa: BLE001 — surface but don't crash
            log.warning("backend.skills_list", f"{type(exc).__name__}: {exc}")
            return []
        return [
            {
                "name": e.skill.name,
                "description": getattr(e.skill, "description", "") or "",
                "path": getattr(e.skill, "filePath", "") or "",
                "source": getattr(e.skill, "source", "unknown"),
                "eligible": e.eligible,
                "in_prompt": id(e.skill) in visible_skills,
                "reason": e.eligibilityReason or "",
            }
            for e in all_entries
        ]

    async def plugins_list(self) -> list[dict[str, Any]]:
        """Loaded plugin records with full metadata for the slash Table.

        Phase 9 enrichment: id, name, source, entry, tools[], hooks[],
        slash[], channels[], features[] —
        matches what cli.py's _list_plugins renders so remote mode produces
        an identical Table.
        """
        hooks = self.runtime.registry.hook_registry()
        if hooks is None:
            return []
        plugins_fn = getattr(hooks, "plugins", None)
        if plugins_fn is None:
            return []
        result: list[dict[str, Any]] = []
        for plugin in plugins_fn():
            result.append({
                "id": getattr(plugin, "id", ""),
                "name": getattr(plugin, "name", ""),
                "source": getattr(plugin, "source", ""),
                "entry": getattr(plugin, "entry", ""),
                "tools": list(getattr(plugin, "tools", ()) or ()),
                "hooks": list(getattr(plugin, "hooks", ()) or ()),
                "slash": list(getattr(plugin, "slash", ()) or ()),
                "channels": list(getattr(plugin, "channels", ()) or ()),
                "features": list(getattr(plugin, "features", ()) or ()),
            })
        return result

    async def hooks_list(self) -> dict[str, Any]:
        """Per-event hook details for the slash Table.

        Phase 9 changed the wire shape: was ``dict[event, count]``; now
        ``dict[event, {count, plugins[], priorities[]}]``. The ``count``
        field preserves the prior renderer's needs.
        """
        hooks = self.runtime.registry.hook_registry()
        if hooks is None:
            return {}
        by_event_fn = getattr(hooks, "hooks_by_event", None)
        if by_event_fn is None:
            counts_fn = getattr(hooks, "handler_counts", None)
            if counts_fn is None:
                return {}
            return {event: {"count": n, "plugins": [], "priorities": []}
                    for event, n in counts_fn().items()}
        result: dict[str, Any] = {}
        for event, hook_list in by_event_fn().items():
            result[event] = {
                "count": len(hook_list),
                "plugins": [
                    f"{h.plugin_name} ({h.plugin_id})" for h in hook_list
                ],
                "priorities": [h.priority for h in hook_list],
            }
        return result

    # ─── Health ───

    async def health(self) -> HealthSummary:
        return HealthSummary(
            runtime_ready=True,
            channels_running=len(self.channel_manager.list_status()) if self.channel_manager is not None else 0,
            sessions_loaded=len(self.manager._loaded),
            in_flight_turns=len(self._run_registry),
        )

    # ─── Gateway lifecycle ───

    async def gateway_restart(self) -> dict[str, Any]:
        """Schedule an immediate daemon restart.

        Returns synchronously so the caller's response/push can flush. The
        actual restart fires from a 0.3s ``call_later`` — long enough for
        the WebSocket frame and any logger writes to make it out before the
        process is swapped (or exits).
        """
        import asyncio

        strategy = self.runtime.config.gateway.restart_strategy
        pid = os.getpid()
        restart_callback = getattr(self.runtime, "restart_callback", None)
        if restart_callback is None:
            raise RuntimeError("daemon restart callback is not configured")

        loop = asyncio.get_running_loop()
        loop.call_later(0.3, restart_callback, strategy)

        self._emit(
            PushEvent(
                event="session.changed",
                payload={"reason": "gateway-restart", "strategy": strategy},
                seq=self._next_seq(),
            )
        )
        return {"strategy": strategy, "pid": pid}

    # ─── WebUI / voice projections ───

    async def webui_state(self) -> dict[str, Any]:
        from nano_openclaw.services.webui_state import state_payload

        return state_payload(self.runtime)

    async def voice_config(self) -> dict[str, Any]:
        from nano_openclaw.features.voice import build_talk_config

        return build_talk_config(self.runtime.config)

    def _ensure_voice_token_provider(self, cfg: Any) -> Any:
        from nano_openclaw.features.voice import AliyunTokenProvider

        provider = self._voice_token_provider
        if provider is None or not provider.matches(
            access_key_id=cfg.accessKeyId,
            access_key_secret=cfg.accessKeySecret,
            region_id=cfg.region,
        ):
            provider = AliyunTokenProvider(
                access_key_id=cfg.accessKeyId,
                access_key_secret=cfg.accessKeySecret,
                region_id=cfg.region,
            )
            self._voice_token_provider = provider
        return provider

    async def voice_token(self) -> dict[str, Any]:
        from nano_openclaw.features.voice import TokenError

        cfg = self.runtime.config.voice
        if not cfg.available:
            raise VoiceError("阿里云语音识别未配置", status_code=503)
        provider = self._ensure_voice_token_provider(cfg)
        try:
            token_id, expire_time = await asyncio.to_thread(provider.get_token)
        except TokenError as exc:
            raise VoiceError(f"签发阿里云 Token 失败: {exc}", status_code=503) from exc
        return {
            "token": token_id,
            "expire_time": expire_time,
            "appkey": cfg.appkey,
            "endpoint": cfg.resolved_endpoint(),
        }

    async def talk_speak(self, **params: Any) -> dict[str, Any]:
        from nano_openclaw.features.voice import TalkSpeakError, synthesize_talk_speech

        cfg = self.runtime.config.voice
        provider = self._ensure_voice_token_provider(cfg) if cfg.available else None
        try:
            result = await asyncio.to_thread(
                synthesize_talk_speech,
                self.runtime.config,
                text=str(params.get("text") or ""),
                voice_id=_optional_str(params.get("voice_id") or params.get("voiceId") or params.get("voice")),
                sample_rate=_optional_int(params.get("sample_rate") or params.get("sampleRate")),
                speed=_optional_float(params.get("speed")),
                rate_wpm=_optional_int(params.get("rate_wpm") or params.get("rateWpm")),
                token_provider=provider,
            )
        except TalkSpeakError as exc:
            raise VoiceError(
                str(exc),
                reason=exc.reason,
                fallback_eligible=exc.fallback_eligible,
                status_code=503 if exc.fallback_eligible else 502,
            ) from exc
        return {
            "audioBase64": base64.b64encode(result.audio).decode("ascii"),
            "provider": result.provider,
            "outputFormat": result.output_format,
            "voiceCompatible": result.voice_compatible,
            "mimeType": result.mime_type,
            "fileExtension": result.file_extension,
        }

    async def podcast_start(
        self,
        *,
        session_key: str,
        topic: str,
        agents: list[dict[str, Any]],
        rounds: int = 20,
        host_voice_id: str = "",
        host_voice_label: str = "",
    ) -> dict[str, Any]:
        from nano_openclaw.features.voice.podcast import (
            HOST_ROLE,
            HOST_VOICE_ID,
            HOST_VOICE_LABEL,
            assign_agents,
            build_start_summary,
            normalize_rounds,
            podcast_model_options,
            voice_label,
        )

        session = self._resolve_session(session_key)
        topic = topic.strip() or "自由讨论"
        rounds = normalize_rounds(rounds)
        host_voice_id = host_voice_id.strip() or HOST_VOICE_ID
        host_voice_label = host_voice_label.strip() or voice_label(host_voice_id) or HOST_VOICE_LABEL
        run_id = uuid.uuid4().hex
        import random
        model_refs, model_labels = podcast_model_options(self.runtime.config)
        if not model_refs:
            model_refs = [self.runtime.model_ref]
            model_labels = {self.runtime.model_ref: self.runtime.model_id}
        assigned = assign_agents(
            agents,
            topic,
            excluded_voice_id=host_voice_id,
            model_refs=model_refs,
            model_labels=model_labels,
            rng=random.Random(run_id),
        )
        token = CancellationToken()
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        await self._append_podcast_message(
            session,
            "user",
            build_start_summary(
                topic,
                assigned,
                rounds,
                host_voice_id=host_voice_id,
                host_voice_label=host_voice_label,
            ),
        )

        task = asyncio.create_task(
            self._run_podcast(
                run_id=run_id,
                session=session,
                topic=topic,
                agents=assigned,
                rounds=rounds,
                host_voice_id=host_voice_id,
                host_voice_label=host_voice_label,
                token=token,
                input_queue=input_queue,
            ),
            name=f"backend.voice_podcast:{run_id}",
        )
        self._podcast_runs[run_id] = {
            "session_id": session.session_id,
            "token": token,
            "task": task,
            "input_queue": input_queue,
        }
        task.add_done_callback(lambda _task, rid=run_id: self._podcast_runs.pop(rid, None))

        payload = {
            "run_id": run_id,
            "session_id": session.session_id,
            "topic": topic,
            "rounds": rounds,
            "host": {
                "role": HOST_ROLE,
                "voice_id": host_voice_id,
                "voice_label": host_voice_label,
            },
            "agents": [
                {
                    "id": a.id,
                    "role": a.role,
                    "requested_role": a.requested_role,
                    "voice_id": a.voice_id,
                    "voice_label": a.voice_label,
                    "model_ref": a.model_ref,
                    "model_label": a.model_label,
                }
                for a in assigned
            ],
        }
        self._emit_podcast({"type": "podcast.started", **payload})
        return payload

    async def podcast_input(self, *, run_id: str, text: str) -> dict[str, Any]:
        run = self._podcast_runs.get(run_id)
        if run is None:
            raise NotFoundError(f"podcast run not found: {run_id}")
        text = text.strip()
        if not text:
            return {"ok": False, "reason": "empty input"}
        await run["input_queue"].put(text)
        self._emit_podcast({
            "type": "podcast.input.accepted",
            "run_id": run_id,
            "session_id": run.get("session_id"),
            "text": text,
        })
        return {"ok": True}

    async def podcast_stop(self, *, run_id: str) -> dict[str, Any]:
        run = self._podcast_runs.get(run_id)
        if run is None:
            return {"ok": False, "reason": "not_found"}
        run["token"].cancel()
        task = run.get("task")
        if task is not None:
            task.cancel()
        self._emit_podcast({
            "type": "podcast.stopped",
            "run_id": run_id,
            "session_id": run.get("session_id"),
        })
        return {"ok": True}

    async def _run_podcast(
        self,
        *,
        run_id: str,
        session: AgentBackendSession,
        topic: str,
        agents: list[Any],
        rounds: int,
        host_voice_id: str,
        host_voice_label: str,
        token: CancellationToken,
        input_queue: asyncio.Queue[str],
    ) -> None:
        import random

        from nano_openclaw.features.voice.podcast import (
            HOST_ROLE,
            build_host_prompt,
            build_speaker_prompt,
            choose_speakers,
        )

        rng = random.Random(run_id)
        context: list[str] = []
        research_cache: dict[str, str] = {}
        next_utterance_sequence = 1

        def next_sequence() -> int:
            nonlocal next_utterance_sequence
            value = next_utterance_sequence
            next_utterance_sequence += 1
            return value

        try:
            async with self.runtime.runtime_guard.reader():
                first_speaker = rng.choice(agents) if agents else None
                if first_speaker is not None:
                    host_text = await self._generate_podcast_utterance(
                        run_id=run_id,
                        session=session,
                        round_index=0,
                        phase="opening",
                        sequence=next_sequence(),
                        role=HOST_ROLE,
                        voice_id=host_voice_id,
                        voice_label=host_voice_label,
                        system_prompt=build_host_prompt(
                            topic=topic,
                            round_index=0,
                            speakers=[first_speaker],
                            total_rounds=rounds,
                            user_input="",
                        ),
                        user_text=f"请先做一段精简开场白，然后自然 cue {first_speaker.role} 作为第一位主讲人开始。",
                        token=token,
                        use_research_tools=False,
                    )
                    if host_text:
                        context.append(f"{HOST_ROLE}: {host_text}")
                for round_index in range(1, rounds + 1):
                    if token.is_cancelled:
                        break
                    pending_inputs = self._drain_podcast_inputs(input_queue)
                    speakers = choose_speakers(agents, round_index, rng)
                    if round_index == 1 and first_speaker is not None:
                        speakers = [first_speaker] + [
                            agent for agent in speakers
                            if getattr(agent, "id", None) != getattr(first_speaker, "id", None)
                        ]
                        if len(speakers) == 1 and len(agents) > 1:
                            remaining = [
                                agent for agent in agents
                                if getattr(agent, "id", None) != getattr(first_speaker, "id", None)
                            ]
                            if remaining:
                                speakers.append(rng.choice(remaining))
                    if not speakers:
                        break

                    if pending_inputs:
                        user_input = "\n".join(pending_inputs)
                        host_text = await self._generate_podcast_utterance(
                            run_id=run_id,
                            session=session,
                            round_index=round_index,
                            phase="interjection",
                            sequence=next_sequence(),
                            role=HOST_ROLE,
                            voice_id=host_voice_id,
                            voice_label=host_voice_label,
                            system_prompt=build_host_prompt(
                                topic=topic,
                                round_index=round_index,
                                speakers=speakers,
                                total_rounds=rounds,
                                user_input=user_input,
                            ),
                            user_text="请回应用户插话，并自然引出本轮主讲人。",
                            token=token,
                            use_research_tools=False,
                        )
                        if host_text:
                            context.append(f"{HOST_ROLE}: {host_text}")

                    self._emit_podcast({
                        "type": "podcast.round.started",
                        "run_id": run_id,
                        "session_id": session.session_id,
                        "round": round_index,
                        "speaker_count": len(speakers),
                    })
                    round_context = "\n".join(context[-10:])
                    speaker_tasks = [
                        asyncio.create_task(
                            self._run_podcast_speaker_turn(
                                run_id=run_id,
                                session=session,
                                topic=topic,
                                agent=agent,
                                round_index=round_index,
                                sequence=next_sequence(),
                                context=round_context,
                                research_cache=research_cache,
                                token=token,
                            ),
                            name=f"backend.voice_podcast.speaker:{run_id}:{round_index}:{idx}",
                        )
                        for idx, agent in enumerate(speakers)
                    ]
                    speaker_results = await asyncio.gather(*speaker_tasks)
                    spoken_this_round: list[str] = []
                    for agent, speaker_text in speaker_results:
                        if speaker_text:
                            line = f"{agent.role}: {speaker_text}"
                            spoken_this_round.append(line)
                            context.append(line)
                            await self._append_podcast_message(session, "assistant", f"【{agent.role}｜{agent.voice_label}】{speaker_text}")

                    if token.is_cancelled:
                        break
                    summary_prompt = build_host_prompt(
                        topic=topic,
                        round_index=round_index,
                        speakers=speakers,
                        total_rounds=rounds,
                        user_input="",
                    )
                    if round_index >= rounds:
                        summary_prompt += "\n请用一句话精简总结刚才主讲人的观点，并做最终收束。"
                    else:
                        summary_prompt += "\n请用一句话承接刚才主讲人的观点，并自然 cue 下一位或下一轮；禁止输出结束播客、结束节目、感谢收听等收尾语。"
                    host_summary = await self._generate_podcast_utterance(
                        run_id=run_id,
                        session=session,
                        round_index=round_index,
                        phase="summary",
                        sequence=next_sequence(),
                        role=HOST_ROLE,
                        voice_id=host_voice_id,
                        voice_label=host_voice_label,
                        system_prompt=summary_prompt,
                        user_text="\n".join(spoken_this_round) or "请做简短串讲。",
                        token=token,
                        use_research_tools=False,
                    )
                    if host_summary:
                        context.append(f"{HOST_ROLE}: {host_summary}")
                    self._emit_podcast({
                        "type": "podcast.round.done",
                        "run_id": run_id,
                        "session_id": session.session_id,
                        "round": round_index,
                    })

            self._emit_podcast({
                "type": "podcast.done",
                "run_id": run_id,
                "session_id": session.session_id,
            })
        except asyncio.CancelledError:
            self._emit_podcast({
                "type": "podcast.stopped",
                "run_id": run_id,
                "session_id": session.session_id,
            })
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.podcast.error", f"{type(exc).__name__}: {exc}")
            self._emit_podcast({
                "type": "podcast.error",
                "run_id": run_id,
                "session_id": session.session_id,
                "message": f"{type(exc).__name__}: {exc}",
            })

    async def _run_podcast_research_subagent(
        self,
        *,
        run_id: str,
        session: AgentBackendSession,
        topic: str,
        agent: Any,
        round_index: int,
        context: str,
        token: CancellationToken,
    ) -> str:
        from nano_openclaw.features.subagents import (
            SpawnParams,
            SubagentCleanupMode,
            SubagentContextMode,
            get_runner,
        )

        if token.is_cancelled:
            raise asyncio.CancelledError()
        task = f"""\
你是 AI 播客主讲人「{agent.role}」的 research 子 Agent。

播客主题：{topic}
当前轮次：{round_index}
近期讨论上下文：
{context or "暂无。"}

请围绕该身份做深入但聚焦的 research：
- 优先使用 web_search / web_fetch 等工具查找主流、被认可、可验证的信息。
- 只总结和本身份相关的关键事实、共识观点、重要争议边界。
- 不要写播客发言稿；只输出供主讲人使用的研究摘要。
- 输出控制在 500 中文字以内。
"""
        self._emit_podcast({
            "type": "podcast.research.started",
            "run_id": run_id,
            "session_id": session.session_id,
            "round": round_index,
            "role": agent.role,
            "model_ref": getattr(agent, "model_ref", ""),
        })

        def on_subagent_event(event: Any) -> None:
            event_type = type(event).__name__
            if event_type in {"SubagentSpawned", "SubagentProgress", "SubagentAnnounced", "SubagentKilled"}:
                payload: dict[str, Any] = {
                    "type": "podcast.research.event",
                    "run_id": run_id,
                    "session_id": session.session_id,
                    "round": round_index,
                    "role": agent.role,
                    "model_ref": getattr(agent, "model_ref", ""),
                    "event_type": event_type,
                }
                for name in ("run_id", "label", "status", "tool_uses", "input_tokens", "output_tokens", "current_activity", "elapsed_ms", "error_message"):
                    if hasattr(event, name):
                        payload[name] = getattr(event, name)
                self._emit_podcast(payload)

        parent_registry = self.runtime.registry.clone(
            exclude={
                "write_file",
                "apply_patch",
                "bash",
                "skill_install",
                "sessions_spawn",
                "subagents",
                "cron_create",
                "cron_delete",
                "schedule_wakeup",
                "todo",
            },
            console=None,
            approval_handler=None,
        )
        runner = get_runner()
        model_client, model_cfg, close_model_client = self._podcast_model_runtime(getattr(agent, "model_ref", ""))
        record = runner.spawn(
            SpawnParams(
                task=task,
                label=f"podcast research: {agent.role}",
                thinking="high",
                run_timeout_seconds=120,
                cleanup=SubagentCleanupMode.KEEP,
                context=SubagentContextMode.ISOLATED,
            ),
            requester_session_key=session.session_id,
            client=model_client,
            base_cfg=model_cfg,
            session_dir=self.runtime.session_dir,
            workspace_dir=self.runtime.workspace_dir,
            on_event=on_subagent_event,
            parent_registry=parent_registry,
        )
        try:
            result = await runner.wait_for(record.run_id, timeout=125)
        except asyncio.CancelledError:
            await runner.kill(record.run_id)
            raise
        finally:
            if close_model_client:
                await self._close_podcast_model_client(model_client)
        if token.is_cancelled:
            await runner.kill(record.run_id)
            raise asyncio.CancelledError()
        text = ""
        status = ""
        error = ""
        if result is not None:
            status = result.status.value
            text = result.result_text or ""
            error = result.error_message or ""
        else:
            status = "timeout"
            error = "research subagent wait timed out"
        self._emit_podcast({
            "type": "podcast.research.done",
            "run_id": run_id,
            "session_id": session.session_id,
            "round": round_index,
            "role": agent.role,
            "model_ref": getattr(agent, "model_ref", ""),
            "subagent_run_id": record.run_id,
            "status": status,
            "error": error,
        })
        return text or f"research 子 Agent 未返回有效结果：{error or status}"

    async def _run_podcast_speaker_turn(
        self,
        *,
        run_id: str,
        session: AgentBackendSession,
        topic: str,
        agent: Any,
        round_index: int,
        sequence: int,
        context: str,
        research_cache: dict[str, str],
        token: CancellationToken,
    ) -> tuple[Any, str]:
        from nano_openclaw.features.voice.podcast import build_speaker_prompt

        if token.is_cancelled:
            raise asyncio.CancelledError()
        cache_key = str(getattr(agent, "id", "") or getattr(agent, "role", ""))
        research_text = research_cache.get(cache_key, "")
        if not research_text:
            research_text = await self._run_podcast_research_subagent(
                run_id=run_id,
                session=session,
                topic=topic,
                agent=agent,
                round_index=round_index,
                context=context,
                token=token,
            )
            research_cache[cache_key] = research_text
        speaker_text = await self._generate_podcast_utterance(
            run_id=run_id,
            session=session,
            round_index=round_index,
            phase="speaker",
            sequence=sequence,
            role=agent.role,
            voice_id=agent.voice_id,
            voice_label=agent.voice_label,
            system_prompt=build_speaker_prompt(
                topic=topic,
                agent=agent,
                round_index=round_index,
                context=context,
                research=research_text,
            ),
            user_text="请基于你的身份、首次 research 摘要和当前讨论上下文，给出本轮观点。",
            token=token,
            use_research_tools=False,
            persist=False,
            model_ref=getattr(agent, "model_ref", ""),
        )
        return agent, speaker_text

    def _drain_podcast_inputs(self, queue: asyncio.Queue[str]) -> list[str]:
        items: list[str] = []
        while True:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    def _podcast_model_runtime(self, model_ref: str) -> tuple[Any, Any, bool]:
        model_ref = str(model_ref or "").strip()
        if not model_ref or model_ref == self.runtime.model_ref:
            return self.runtime.client, self.runtime.cfg, False

        from nano_openclaw.config import resolve_model_config
        from nano_openclaw.services.runtime_factory import _build_client

        resolved = resolve_model_config(model_ref, self.runtime.config)
        api_type = resolved["api_type"]
        api = "anthropic" if api_type == "anthropic-messages" else "openai"
        model_id = resolved["model_id"]
        max_tokens = int(resolved["max_tokens"] or self.runtime.cfg.max_tokens)
        context_window = int(resolved["context_window"] or 0)
        if context_window > 0 and max_tokens > context_window:
            max_tokens = context_window
        context_budget = self.runtime.cfg.context_budget
        if context_window > 0 and context_budget > context_window:
            context_budget = context_window
        cache_ttl = (
            self.runtime.config.promptCaching.cache_ttl
            if api == "anthropic" and self.runtime.config.promptCaching.enabled
            else None
        )
        cfg = replace(
            self.runtime.cfg,
            model=model_id,
            api=api,
            base_url=resolved["base_url"],
            model_input=tuple(resolved["model_input"] or ["text"]),
            max_tokens=max_tokens,
            context_window=context_window,
            context_budget=context_budget,
            thinking_level=self.runtime.config.resolve_thinking_level(model_ref),
            cache_ttl=cache_ttl,
            session_key=f"{self.runtime.cfg.session_key}:voice-podcast:{model_ref}",
        )
        return _build_client(api, resolved["api_key"], resolved["base_url"]), cfg, True

    async def _close_podcast_model_client(self, client: Any) -> None:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()
            return
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def _generate_podcast_utterance(
        self,
        *,
        run_id: str,
        session: AgentBackendSession,
        round_index: int,
        phase: str,
        sequence: int,
        role: str,
        voice_id: str,
        voice_label: str,
        system_prompt: str,
        user_text: str,
        token: CancellationToken,
        use_research_tools: bool,
        persist: bool = True,
        model_ref: str = "",
    ) -> str:
        from nano_openclaw.features.voice.podcast import generate_utterance

        utterance_id = uuid.uuid4().hex
        self._emit_podcast({
            "type": "podcast.utterance.started",
            "run_id": run_id,
            "utterance_id": utterance_id,
            "session_id": session.session_id,
            "round": round_index,
            "phase": phase,
            "sequence": sequence,
            "role": role,
            "voice_id": voice_id,
            "voice_label": voice_label,
            "model_ref": model_ref,
        })

        def on_delta(text: str) -> None:
            if text:
                self._emit_podcast({
                    "type": "podcast.text.delta",
                    "run_id": run_id,
                    "utterance_id": utterance_id,
                    "session_id": session.session_id,
                    "round": round_index,
                    "phase": phase,
                    "sequence": sequence,
                    "role": role,
                    "voice_id": voice_id,
                    "voice_label": voice_label,
                    "model_ref": model_ref,
                    "text": text,
                })

        exclude = {
            "write_file",
            "apply_patch",
            "bash",
            "skill_install",
            "sessions_spawn",
            "subagents",
            "cron_create",
            "cron_delete",
            "schedule_wakeup",
            "todo",
        }
        if not use_research_tools:
            exclude.update({"web_search", "web_fetch", "read_file", "list_dir", "memory_search", "memory_get"})
        registry = self.runtime.registry.clone(exclude=exclude, console=None, approval_handler=None)
        model_client, model_cfg, close_model_client = self._podcast_model_runtime(model_ref)
        try:
            text = await generate_utterance(
                runtime=self.runtime,
                registry=registry,
                system_prompt=system_prompt,
                user_text=user_text,
                cancellation_token=token,
                on_delta=on_delta,
                client=model_client,
                cfg=model_cfg,
            )
        finally:
            if close_model_client:
                await self._close_podcast_model_client(model_client)
        if text and persist:
            await self._append_podcast_message(session, "assistant", f"【{role}｜{voice_label}】{text}")
        self._emit_podcast({
            "type": "podcast.utterance.done",
            "run_id": run_id,
            "utterance_id": utterance_id,
            "session_id": session.session_id,
            "round": round_index,
            "phase": phase,
            "sequence": sequence,
            "role": role,
            "voice_id": voice_id,
            "voice_label": voice_label,
            "model_ref": model_ref,
            "text": text,
        })
        return text

    async def _append_podcast_message(self, session: AgentBackendSession, role: str, text: str) -> None:
        message = Message(role=role, content=[{"type": "text", "text": text}])
        async with session.lock:
            session.history.append(message)
            session.writer.append_message(message)
            self.manager.save_metadata(session)
        self._emit(
            PushEvent(
                event="session.changed",
                payload={
                    "session_id": session.session_id,
                    "session_key": session.session_id,
                    "history_changed": True,
                },
                seq=self._next_seq(),
            )
        )

    def _emit_podcast(self, payload: dict[str, Any]) -> None:
        self._emit(PushEvent(event="podcast.event", payload=payload, seq=self._next_seq()))

    # ─── Push event subscription ───

    def subscribe(
        self,
        *,
        session_key: str | None = None,
        events: list[PushEventKind] | None = None,
    ) -> AsyncIterator[PushEvent]:
        sub = _Subscriber(
            session_key=session_key,
            events=set(events) if events else None,
        )
        self._subscribers.append(sub)

        async def _iterator() -> AsyncIterator[PushEvent]:
            try:
                while True:
                    item = await sub.queue.get()
                    if item is None:
                        return
                    yield item
            finally:
                sub.close()
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

        return _iterator()

    # ─── Lifecycle ───

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Cancel in-flight turns started by this Backend (chat-origin only —
        # cron turns belong to the scheduler's lifecycle and are torn down
        # when the daemon shuts down independently).
        chat_entries = [e for e in self._run_registry.list() if e.origin == "chat"]
        for entry in chat_entries:
            entry.cancellation_token.cancel()
        for entry in chat_entries:
            if entry.task is None:
                continue
            try:
                await asyncio.wait_for(entry.task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, BaseException):
                pass

        # Drain any pending approval prompts as deny so callers don't hang
        self._approval_broker.deny_all()

        # Close subscribers
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()
