"""``DingtalkBot`` — glue between :class:`DingtalkStreamClient` and
:class:`AgentSession`.

For each inbound CALLBACK frame on the bot-messages topic:

1. Decode the payload into an :class:`ExtractedMessage`.
2. Apply the per-account engagement :func:`policy.should_respond`.
3. Resolve or create the agent session keyed by ``conversationId``.
4. Run one ``AgentSession.run_turn`` with the user's text, collecting the
   model's reply via ``TextDelta`` events.
5. POST the assembled reply through the inbound message's ``sessionWebhook``.

The per-``conversationId`` → ``session_id`` mapping persists to
``state_dir/dingtalk-sessions.{clientId}.json`` so daemon restarts keep
talking to the same backend session as before.

PR2 deliberately skips:

- AI Card streaming "typing" feedback → PR3 owns the reply dispatcher.
- ``picture`` / ``richText`` media segments / file attachments → PR4.
- Cron-completion routing → PR4 (``decorate_tools`` already exists from PR1
  but its body is a passthrough until then).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nano_openclaw.dingtalk.extract import extract_message
from nano_openclaw.dingtalk.frames import CallbackFrame
from nano_openclaw.dingtalk.policy import DingtalkPolicy, should_respond
from nano_openclaw.dingtalk.sender import send_text_via_webhook
from nano_openclaw.dingtalk.stream_client import DingtalkStreamClient
from nano_openclaw.logger import get_logger
from nano_openclaw.loop import AgentSession
from nano_openclaw.runtime import AgentRuntime
from nano_openclaw.tools import Tool, ToolRegistry
from nano_openclaw._stream_events import TextDelta


log = get_logger(__name__)


def _clone_registry_for_dingtalk(
    registry: ToolRegistry,
    *,
    account_id: str,
    conversation_id: str,
) -> ToolRegistry:
    """Per-turn shallow clone of ``registry`` with ``created_by`` injected.

    Cron jobs and wakeups created during this turn are tagged with the
    three-segment ``dingtalk:{account_id}:{conversation_id}`` marker so the
    channel registry can route completion notifications back to the same
    DingTalk conversation later. Mirrors the wechat helper but uses
    ``conversation_id`` (DingTalk's natural session granularity) instead of
    a user id.
    """
    clone = ToolRegistry(
        _tools=dict(registry._tools),
        approval_manager=registry.approval_manager,
        console=registry.console,
        _workspace_dir=registry._workspace_dir,
        _state_dir=registry._state_dir,
        _allow_global_pip=registry._allow_global_pip,
    )
    clone.set_session_status_context(**registry._session_status_context)
    clone.set_eligible_skills(dict(registry._eligible_skills))
    hook_reg = registry.hook_registry()
    if hook_reg is not None:
        clone.set_hook_registry(hook_reg)

    created_by = f"dingtalk:{account_id}:{conversation_id}"

    if "cron_create" in clone._tools:
        cron_create = clone._tools["cron_create"]

        def wrapped_cron_create(args: dict[str, Any]) -> str:
            args["created_by"] = created_by
            return cron_create.run(args)

        clone._tools["cron_create"] = Tool(
            name="cron_create",
            description=cron_create.description,
            input_schema=cron_create.input_schema,
            run=wrapped_cron_create,
        )

    if "schedule_wakeup" in clone._tools:
        schedule_wakeup = clone._tools["schedule_wakeup"]

        def wrapped_wakeup(args: dict[str, Any]) -> str:
            args["created_by"] = created_by
            return schedule_wakeup.run(args)

        clone._tools["schedule_wakeup"] = Tool(
            name="schedule_wakeup",
            description=schedule_wakeup.description,
            input_schema=schedule_wakeup.input_schema,
            run=wrapped_wakeup,
        )

    return clone


@dataclass
class DingtalkBot:
    """One ``DingtalkStreamClient`` plus the message-handling glue.

    Instantiated by :class:`DingtalkChannel.start` after credentials are
    loaded; ``run()`` blocks until ``stop()`` is invoked from the channel's
    teardown path. All conversation state lives on this instance — no
    module-level globals — so multi-account daemons keep their per-account
    state cleanly isolated.
    """

    runtime: AgentRuntime
    account_id: str  # == clientId
    client_id: str
    client_secret: str
    policy: DingtalkPolicy
    session_manager: Any | None = None
    backend: Any | None = None
    conv_map_path: Path | None = None
    # conversationId → session_id mapping (loaded lazily on first use).
    _conv_to_session_id: dict[str, str] = field(default_factory=dict)
    _conv_map_loaded: bool = False
    # Per-conversation lock so two concurrent messages from the same group
    # don't race on history mutation — matches wechat's session_lock model.
    _conv_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _stream: DingtalkStreamClient | None = None
    _http_client: httpx.AsyncClient | None = None

    # ── Conversation → session bookkeeping ─────────────────────────────────

    def _ensure_conv_map_loaded(self) -> None:
        if self._conv_map_loaded or self.conv_map_path is None:
            self._conv_map_loaded = True
            return
        try:
            if self.conv_map_path.exists():
                self._conv_to_session_id = (
                    json.loads(self.conv_map_path.read_text(encoding="utf-8")) or {}
                )
        except (OSError, ValueError) as exc:
            log.warning(
                "dingtalk.conv_map.load.error",
                f"failed to load {self.conv_map_path}: {type(exc).__name__}: {exc}",
            )
            self._conv_to_session_id = {}
        self._conv_map_loaded = True

    def _save_conv_map(self) -> None:
        if self.conv_map_path is None:
            return
        try:
            self.conv_map_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.conv_map_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._conv_to_session_id, indent=2), encoding="utf-8")
            os.replace(tmp, self.conv_map_path)
        except OSError as exc:
            log.warning(
                "dingtalk.conv_map.save.error",
                f"failed to save {self.conv_map_path}: {exc}",
            )

    def _resolve_session(self, conversation_id: str) -> Any | None:
        """Find or create the backend session for ``conversation_id``.

        Returns ``None`` in standalone (no daemon) mode — callers fall back
        to a transient in-memory history list when that happens.
        """
        if self.session_manager is None:
            return None
        self._ensure_conv_map_loaded()

        existing_id = self._conv_to_session_id.get(conversation_id)
        if existing_id:
            try:
                return self.session_manager.get_or_load(existing_id)
            except KeyError:
                self._conv_to_session_id.pop(conversation_id, None)

        new_session = self.session_manager.create()
        self._conv_to_session_id[conversation_id] = new_session.session_id
        self._save_conv_map()
        return new_session

    def _get_conv_lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._conv_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_locks[conversation_id] = lock
        return lock

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Drive the stream loop until ``stop()`` is called.

        The ``httpx.AsyncClient`` is held open for the bot's lifetime so
        outbound webhook posts share a connection pool — DingTalk's
        sessionWebhook host is fronted by a CDN that benefits from keepalive.
        """
        self._stream = DingtalkStreamClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            on_callback=self._on_callback,
        )
        async with httpx.AsyncClient(timeout=30.0) as http:
            self._http_client = http
            try:
                await self._stream.run()
            finally:
                self._http_client = None

    async def stop(self) -> None:
        if self._stream is not None:
            await self._stream.stop()

    # ── Inbound dispatch ───────────────────────────────────────────────────

    async def _on_callback(self, frame: CallbackFrame) -> None:
        try:
            data = json.loads(frame.data) if isinstance(frame.data, str) else dict(frame.data)
        except (ValueError, TypeError) as exc:
            log.warning(
                "dingtalk.bot.parse_data.error",
                f"messageId={frame.headers.messageId} {type(exc).__name__}: {exc}",
            )
            return

        msg = extract_message(data)
        if not should_respond(msg, self.policy):
            log.debug(
                "dingtalk.bot.policy.skip",
                f"conv={msg.conversation_id[:12]}… sender={msg.sender_staff_id[:8]}… "
                f"group={msg.is_group} at_self={msg.at_self}",
            )
            return

        # Empty text + no attachments → nothing for the agent to do (PR4
        # will surface media attachments here and let media-only messages
        # through).
        if not msg.text.strip():
            log.debug("dingtalk.bot.empty_text.skip", f"conv={msg.conversation_id[:12]}…")
            return

        # Per-conversation serialization — two near-simultaneous messages in
        # the same group must not race on the shared session history.
        lock = self._get_conv_lock(msg.conversation_id)
        async with lock:
            await self._handle_message(msg)

    async def _handle_message(self, msg) -> None:  # type: ignore[no-untyped-def]
        backend_session = self._resolve_session(msg.conversation_id)
        if backend_session is not None:
            history = backend_session.history
            transcript_writer = backend_session.writer
            session_id_for_cfg = backend_session.session_id
        else:
            history = []
            transcript_writer = None
            session_id_for_cfg = None

        from dataclasses import replace as _dc_replace

        turn_cfg = self.runtime.cfg
        if session_id_for_cfg is not None:
            turn_cfg = _dc_replace(self.runtime.cfg, session_key=session_id_for_cfg)

        text_buf: list[str] = []

        def on_event(event: Any) -> None:
            if isinstance(event, TextDelta):
                text_buf.append(event.text)

        shared_kwargs: dict[str, Any] = {}
        if backend_session is not None:
            shared_kwargs["usage_stats"] = backend_session.usage_stats
            shared_kwargs["compaction_state"] = backend_session.compaction_state
            shared_kwargs["todo_store"] = backend_session.todo_store

        agent_session = AgentSession(
            history=history,
            registry=_clone_registry_for_dingtalk(
                self.runtime.registry,
                account_id=self.account_id,
                conversation_id=msg.conversation_id,
            ),
            on_event=on_event,
            client=self.runtime.client,
            cfg=turn_cfg,
            transcript_writer=transcript_writer,
            **shared_kwargs,
        )

        try:
            await agent_session.run_turn(msg.text)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "dingtalk.turn.failed",
                f"conv={msg.conversation_id[:12]}… {type(exc).__name__}: {exc}",
            )
            return
        finally:
            if backend_session is not None and self.session_manager is not None:
                try:
                    self.session_manager.save_metadata(backend_session)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "dingtalk.save_metadata.error",
                        f"{type(exc).__name__}: {exc}",
                    )

        reply = "".join(text_buf).strip()
        if not reply:
            return
        if not msg.session_webhook:
            log.warning(
                "dingtalk.reply.no_webhook",
                f"conv={msg.conversation_id[:12]}…: cannot reply, sessionWebhook missing",
            )
            return
        if msg.session_webhook_expire_ms and msg.session_webhook_expire_ms < time.time() * 1000:
            log.warning(
                "dingtalk.reply.webhook_expired",
                f"conv={msg.conversation_id[:12]}…: sessionWebhook already expired",
            )
            return

        client = self._http_client
        if client is None:
            # Falling back to a fresh client keeps the bot useful in unit
            # tests where ``run()`` hasn't initialized the shared pool.
            async with httpx.AsyncClient(timeout=30.0) as fallback:
                await send_text_via_webhook(fallback, msg.session_webhook, reply)
        else:
            await send_text_via_webhook(client, msg.session_webhook, reply)
