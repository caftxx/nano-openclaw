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

from nano_openclaw.attachments import PromptAttachment
from nano_openclaw.dingtalk.extract import extract_message
from nano_openclaw.dingtalk.frames import CallbackFrame
from nano_openclaw.dingtalk.media import download_media
from nano_openclaw.dingtalk.policy import DingtalkPolicy, should_respond
from nano_openclaw.dingtalk.reply_dispatcher import ReplyDispatcher
from nano_openclaw.dingtalk.sender import (
    send_proactive_to_group,
    send_proactive_to_user,
)
from nano_openclaw.dingtalk.stream_client import DingtalkStreamClient
from nano_openclaw.dingtalk.token import DingtalkTokenManager
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
    # One DingtalkTokenManager per bot — token cache is keyed by clientId so
    # technically a process-wide singleton would also work, but a per-bot one
    # keeps the lifetime tied to the channel for clean teardown.
    _token_mgr: DingtalkTokenManager = field(default_factory=DingtalkTokenManager)
    # conversationId → metadata needed to send proactive notifications
    # (``cron_create`` completion ping-back). We learn (is_group, sender_id)
    # whenever a user message comes in; that's the only path that gives us
    # the full identity. Persists alongside the conv_map so daemon restarts
    # don't lose the routing context.
    _conv_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    _conv_metadata_loaded: bool = False

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

    # ── Conversation metadata (for proactive routing) ─────────────────────

    def _conv_metadata_path(self) -> Path | None:
        """Sibling file to the conv_map; same suffix scheme."""
        if self.conv_map_path is None:
            return None
        return self.conv_map_path.with_name(
            self.conv_map_path.name.replace("dingtalk-sessions", "dingtalk-conv-meta")
        )

    def _ensure_conv_metadata_loaded(self) -> None:
        if self._conv_metadata_loaded:
            return
        self._conv_metadata_loaded = True
        path = self._conv_metadata_path()
        if path is None or not path.exists():
            return
        try:
            self._conv_metadata = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            log.warning(
                "dingtalk.conv_meta.load.error",
                f"failed to load {path}: {type(exc).__name__}: {exc}",
            )
            self._conv_metadata = {}

    def _save_conv_metadata(self) -> None:
        path = self._conv_metadata_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._conv_metadata, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning(
                "dingtalk.conv_meta.save.error",
                f"failed to save {path}: {exc}",
            )

    def _record_conv_metadata(self, msg) -> None:  # type: ignore[no-untyped-def]
        """Stash (is_group, sender_staff_id) for proactive routing later.

        Only persist when ``conv_map_path`` is wired (daemon mode). Standalone
        tests just hold this in memory — they don't go through cron anyway.
        """
        self._ensure_conv_metadata_loaded()
        prev = self._conv_metadata.get(msg.conversation_id)
        new = {
            "is_group": bool(msg.is_group),
            "sender_staff_id": msg.sender_staff_id,
            "robot_code": msg.robot_code or self.client_id,
        }
        if prev == new:
            return
        self._conv_metadata[msg.conversation_id] = new
        self._save_conv_metadata()

    # ── Proactive send (cron notification entry point) ────────────────────

    async def send_proactive(self, conversation_id: str, text: str) -> None:
        """Push a message into ``conversation_id`` outside of a user turn.

        Uses the proactive REST API instead of the (long-gone)
        sessionWebhook. The bot routes DM vs group based on the metadata
        captured from the originating user message; if we have no record
        of this conversation, we fall back to assuming a 1:1 with the
        ``conversation_id`` itself as the user id, which is correct for
        DingTalk's "1:1 with a robot" case and harmless otherwise (the API
        returns an error we log and move on).
        """
        if not conversation_id or not text:
            return
        self._ensure_conv_metadata_loaded()
        meta = self._conv_metadata.get(conversation_id, {})
        client = self._http_client
        if client is None:
            # Open a one-shot client for the off-turn case (cron-driven).
            async with httpx.AsyncClient(timeout=30.0) as fallback:
                await self._dispatch_proactive(fallback, conversation_id, text, meta)
            return
        await self._dispatch_proactive(client, conversation_id, text, meta)

    async def _dispatch_proactive(
        self,
        client: httpx.AsyncClient,
        conversation_id: str,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        robot_code = str(meta.get("robot_code") or self.client_id)
        if meta.get("is_group"):
            await send_proactive_to_group(
                client,
                token_mgr=self._token_mgr,
                client_id=self.client_id,
                client_secret=self.client_secret,
                open_conversation_id=conversation_id,
                text=text,
                markdown=True,
                robot_code=robot_code,
            )
            return
        user_id = str(meta.get("sender_staff_id") or "")
        if not user_id:
            log.warning(
                "dingtalk.proactive.no_user_id",
                f"conv={conversation_id[:12]}…: no sender metadata recorded; "
                f"cron notification cannot be delivered",
            )
            return
        await send_proactive_to_user(
            client,
            token_mgr=self._token_mgr,
            client_id=self.client_id,
            client_secret=self.client_secret,
            user_id=user_id,
            text=text,
            markdown=True,
            robot_code=robot_code,
        )

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

        # Empty text + no media → nothing for the agent to do.
        if not msg.text.strip() and not msg.media:
            log.debug("dingtalk.bot.empty.skip", f"conv={msg.conversation_id[:12]}…")
            return

        # Capture (is_group, sender) for future proactive notifications
        # (cron completion routing). Cheap to do here; persists on first
        # contact and updates if the message changes the routing target
        # (e.g. user rejoins after deletion).
        self._record_conv_metadata(msg)

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

        # Materialize media attachments before we hit run_turn. The download
        # API returns presigned URLs that expire fast, so we do this inline
        # rather than lazily during inference.
        attachments: list[PromptAttachment] = []
        if msg.media:
            client = self._ensure_http_client()
            robot_code = msg.robot_code or self.client_id
            for item in msg.media:
                data = await download_media(
                    client,
                    download_code=item.download_code,
                    robot_code=robot_code,
                    token_mgr=self._token_mgr,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                )
                if data is None:
                    log.warning(
                        "dingtalk.media.skip",
                        f"conv={msg.conversation_id[:12]}… name={item.name}: download failed",
                    )
                    continue
                attachments.append(PromptAttachment(
                    name=item.name,
                    mime=item.mime,
                    size=len(data),
                    data=data,
                ))

        from dataclasses import replace as _dc_replace

        turn_cfg = self.runtime.cfg
        if session_id_for_cfg is not None:
            turn_cfg = _dc_replace(self.runtime.cfg, session_key=session_id_for_cfg)

        # Reply dispatcher owns the AI Card lifecycle plus the webhook
        # fallback. We create it up-front so each TextDelta can stream into
        # the live card in real time (subject to the per-dispatcher throttle
        # and the global card-API token bucket).
        client = self._ensure_http_client()
        dispatcher = ReplyDispatcher(
            http_client=client,
            msg=msg,
            token_mgr=self._token_mgr,
            client_id=self.client_id,
            client_secret=self.client_secret,
            robot_code=msg.robot_code or self.client_id,
        )
        await dispatcher.on_start()

        # Stream model output into the dispatcher. Coalescing into chunks
        # avoids one HTTP PUT per character; the throttle inside
        # ``on_partial`` enforces the 800ms cap.
        async def _emit(chunk: str) -> None:
            try:
                await dispatcher.on_partial(chunk)
            except Exception as exc:  # noqa: BLE001 — never let reply errors abort the turn
                log.warning(
                    "dingtalk.reply.partial.error",
                    f"{type(exc).__name__}: {exc}",
                )

        pending_emits: list[asyncio.Task] = []

        def on_event(event: Any) -> None:
            if isinstance(event, TextDelta) and event.text:
                # Fire-and-forget; the reply dispatcher serializes its own
                # internal HTTP calls so concurrent tasks here are safe.
                pending_emits.append(asyncio.create_task(_emit(event.text)))

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

        turn_error: str | None = None
        try:
            await agent_session.run_turn(
                msg.text or "(no text, media-only)",
                attachments=attachments or None,
            )
        except Exception as exc:  # noqa: BLE001
            turn_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "dingtalk.turn.failed",
                f"conv={msg.conversation_id[:12]}… {turn_error}",
            )
        finally:
            if backend_session is not None and self.session_manager is not None:
                try:
                    self.session_manager.save_metadata(backend_session)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "dingtalk.save_metadata.error",
                        f"{type(exc).__name__}: {exc}",
                    )

        # Drain any in-flight emit tasks before finalizing so the card
        # finishes with the complete buffer, not a mid-stream snapshot.
        if pending_emits:
            await asyncio.gather(*pending_emits, return_exceptions=True)

        if turn_error is not None:
            await dispatcher.on_error(f"⚠️ {turn_error}")
        else:
            await dispatcher.on_final()

    def _ensure_http_client(self) -> httpx.AsyncClient:
        """Return the shared client held by ``run()``.

        Tests can inject one by setting ``self._http_client`` before
        invoking ``_handle_message`` directly; raising rather than
        lazily-creating one avoids leaking unclosed clients in unexpected
        code paths.
        """
        if self._http_client is None:
            raise RuntimeError(
                "DingtalkBot._http_client not initialized — _handle_message "
                "must be called from inside run() or after the test sets it."
            )
        return self._http_client
