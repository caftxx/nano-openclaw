"""WeChat bot main loop for nano-openclaw.

Connects to iLink WeChat API via long-polling and routes each message through
AgentSession.run_turn(), sending the reply back via iLink.

Each WeChat user gets an isolated history list (per-user session).

Supports directed notifications: scheduled jobs created via WeChat will notify
the creator when completed.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import base64

import httpx

from nano_openclaw.attachments import PromptAttachment
from nano_openclaw.logger import get_logger
from nano_openclaw.loop import AgentSession, TextDelta
from nano_openclaw._stream_events import ToolUseEnd, ToolUseStart
from nano_openclaw.runtime import AgentRuntime
from nano_openclaw.tools import ToolRegistry, Tool
from nano_openclaw.wechat.ilink import (
    download_wechat_file,
    download_wechat_image,
    extract_file_items,
    extract_image_items,
    extract_text,
    get_typing_ticket,
    get_updates,
    is_session_expired,
    send_text,
    send_typing,
)
from nano_openclaw.wechat.notify import NotifyQueue, NotifyItem

log = get_logger(__name__)


def _clone_registry(registry: ToolRegistry, uid: str, account_id: str = "default") -> ToolRegistry:
    """Clone registry and inject sender into cron_create / schedule_wakeup.

    Phase 2 changed the ``created_by`` format from two-segment ``wechat:{uid}``
    to three-segment ``wechat:{account_id}:{uid}`` so the channel registry
    can route notifications to the right account on multi-account daemons.
    The cron scheduler accepts both formats (legacy two-segment is mapped to
    account ``"default"``).
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

    created_by = f"wechat:{account_id}:{uid}"

    # Wrap cron_create to auto-bind created_by and enable notification
    if "cron_create" in clone._tools:
        cron_create = clone._tools["cron_create"]
        def wrapped_run(args: dict[str, Any]) -> str:
            args["created_by"] = created_by
            args.setdefault("notify_wechat", True)  # 微信创建默认开启通知
            return cron_create.run(args)

        clone._tools["cron_create"] = Tool(
            name="cron_create",
            description=cron_create.description,
            input_schema=cron_create.input_schema,
            run=wrapped_run,
        )

    # Wrap schedule_wakeup similarly
    if "schedule_wakeup" in clone._tools:
        schedule_wakeup = clone._tools["schedule_wakeup"]
        def wrapped_wakeup(args: dict[str, Any]) -> str:
            args["created_by"] = created_by
            args.setdefault("notify_wechat", True)
            return schedule_wakeup.run(args)

        clone._tools["schedule_wakeup"] = Tool(
            name="schedule_wakeup",
            description=schedule_wakeup.description,
            input_schema=schedule_wakeup.input_schema,
            run=wrapped_wakeup,
        )

    return clone


@dataclass
class WechatBot:
    runtime: AgentRuntime
    base_url: str
    token: str
    poll_timeout: int = 35
    typing_interval: int = 5
    notify_queue: NotifyQueue | None = None
    notify_poll_interval: int = 30
    heartbeat_interval: float = 30.0  # seconds of silence before a tool-status line is sent
    # Account identifier for created_by markers on cron jobs created by this bot's
    # users. Multi-account daemons spawn one WechatBot per account, each with a
    # distinct ``account_id`` so cron notifications route correctly.
    account_id: str = "default"
    # ``session_manager`` (set in daemon mode) routes per-uid conversations
    # through ``BackendSessionManager`` — same store the WebUI/TUI ``/sessions``
    # surface reads. ``None`` falls back to the legacy in-memory dict (only
    # the deprecated standalone path which Phase 3 already removed).
    session_manager: Any | None = None
    # ``backend`` is the daemon's shared EmbeddedBackend; slash commands
    # require it so WeChat uses the same dispatcher as TUI/WebUI. ``None`` is
    # only tolerated by low-level tests that don't exercise slash handling.
    backend: Any | None = None
    # Persistence path for the uid → session_id mapping; without it, restarts
    # forget which uid maps to which session and the next message creates a
    # fresh empty session.
    uid_map_path: Path | None = None
    # typing_ticket cache TTL — iLink tickets are stable for some minutes; we
    # default to 30 min to avoid a getconfig RTT on every reply while still
    # rotating well before any plausible server-side expiry.
    typing_ticket_ttl: float = 1800.0
    # uid -> history list (legacy fallback when session_manager is None)
    _sessions: dict[str, list[Any]] = field(default_factory=dict)
    # uid -> session_id (loaded from uid_map_path on first use)
    _uid_to_session_id: dict[str, str] = field(default_factory=dict)
    _uid_map_loaded: bool = False
    # uid -> (ticket, expires_at_monotonic). Populated by _keep_typing on miss
    # and invalidated on send_typing failure.
    _typing_ticket_cache: dict[str, tuple[str, float]] = field(default_factory=dict)
    # monotonic timestamp of the most recent errcode=-14 from getUpdates;
    # surfaced by daemon channel reporting / future health checks.
    _session_expired_at: float | None = None

    def _ensure_uid_map_loaded(self) -> None:
        """Lazy-load the uid → session_id JSON mapping. Idempotent."""
        if self._uid_map_loaded or self.uid_map_path is None:
            self._uid_map_loaded = True
            return
        try:
            if self.uid_map_path.exists():
                import json
                self._uid_to_session_id = json.loads(self.uid_map_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            log.warning(
                "wechat.uid_map.load.error",
                f"failed to load {self.uid_map_path}: {type(exc).__name__}: {exc}",
            )
            self._uid_to_session_id = {}
        self._uid_map_loaded = True

    def _save_uid_map(self) -> None:
        """Atomic write of the uid → session_id mapping. No-op if no path."""
        if self.uid_map_path is None:
            return
        try:
            import json
            self.uid_map_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.uid_map_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._uid_to_session_id, indent=2), encoding="utf-8")
            os.replace(tmp, self.uid_map_path)
        except OSError as exc:
            log.warning(
                "wechat.uid_map.save.error",
                f"failed to save {self.uid_map_path}: {exc}",
            )

    def _resolve_session(self, uid: str):
        """Return an ``AgentBackendSession`` for a wechat uid (daemon mode).

        First contact → manager.create() and persist the mapping.
        Subsequent → manager.get_or_load(stored_id); if the file vanished
        somehow (manual deletion etc.) → fall through to a fresh session.

        ``None`` only when ``session_manager`` isn't wired (legacy path).
        """
        if self.session_manager is None:
            return None
        self._ensure_uid_map_loaded()

        existing_id = self._uid_to_session_id.get(uid)
        if existing_id:
            try:
                return self.session_manager.get_or_load(existing_id)
            except KeyError:
                # mapping points at a deleted session — fall through to create
                self._uid_to_session_id.pop(uid, None)

        new_session = self.session_manager.create()
        self._uid_to_session_id[uid] = new_session.session_id
        self._save_uid_map()
        return new_session

    def _get_or_create_history(self, uid: str) -> list[Any]:
        # Legacy fallback only — daemon path uses _resolve_session(uid) directly.
        if uid not in self._sessions:
            self._sessions[uid] = []
        return self._sessions[uid]

    async def _poll_notifications(self) -> None:
        """Poll notify-queue and send directed notifications to creators."""
        if not self.notify_queue:
            return

        while True:
            await asyncio.sleep(self.notify_poll_interval)

            pending = self.notify_queue.get_pending(limit=10)
            if not pending:
                continue

            async with httpx.AsyncClient() as client:
                for item in pending:
                    target_uid = item.target_uid
                    if not target_uid:
                        continue
                    try:
                        await send_text(client, self.base_url, self.token, target_uid, item.result_summary)
                        log.info("wechat.notify.sent", f"notification sent to {target_uid:.16} for job {item.job_name}")
                    except Exception as exc:
                        log.warning("wechat.notify.failed", f"send notification to {target_uid:.16} failed: {exc}")

                    self.notify_queue.mark_sent(item.job_id, item.created_at)

    async def _handle_slash_command(self, uid: str, cmd: str) -> str | None:
        """Defer to the shared ``gateway/slash.py`` dispatcher via a
        ``PlainRenderer`` so WeChat sees the exact same surface as TUI / WebUI
        (single source of truth, including ``/help`` ordering + content).

        Returns reply text if the command was handled, ``None`` if it should
        be routed to the agent loop instead (skill-shaped or unknown).
        """
        cmd_stripped = cmd.strip()
        cmd_lower = cmd_stripped.lower()

        if cmd_lower in ("/quit", "/exit", "/q"):
            return "⚠️ **Bot cannot quit via WeChat command.** Send `/help` for available commands."

        if self.backend is None:
            log.error(
                "wechat.slash.no_backend",
                f"slash command {cmd_lower!r} received without daemon backend",
            )
            return "⚠️ Slash commands require the daemon backend. Start WeChat through `gateway run`."

        from nano_openclaw.gateway.slash import handle_slash, QuitREPL
        from nano_openclaw.gateway.slash_renderer import PlainRenderer

        # Bind a session for this uid so /clear / /context / etc. operate on
        # it; pre-resolve so handle_slash sees a non-empty session_key.
        sess = self._resolve_session(uid)
        session_key = sess.session_id if sess is not None else ""
        slash_state = {"session_key": session_key, "session_changed": False}
        renderer = PlainRenderer(emoji=True, max_chars=1500, width=50)
        try:
            handled = await handle_slash(cmd_stripped, self.backend, renderer, slash_state)
        except QuitREPL:
            return "⚠️ Bot cannot quit via WeChat command."
        if not handled:
            return None
        # /new and /clear may have rebound which session_key we should carry
        # forward for this uid; persist so the next inbound message routes into
        # the new transcript instead of recreating the old.
        if slash_state.get("session_changed"):
            new_key = slash_state.get("session_key") or ""
            self._ensure_uid_map_loaded()
            if new_key:
                self._uid_to_session_id[uid] = new_key
            else:
                self._uid_to_session_id.pop(uid, None)
            self._save_uid_map()
        text = renderer.collect()
        return text or "(done)"

    # Long-poll backoff schedule. Mirrors openilink-sdk-python/monitor.py
    # constants so behavior matches the reference SDK: a couple of fast
    # retries to absorb transient blips, then a 30s cool-off so we don't
    # hammer iLink (or fill the log) when the server is genuinely down.
    POLL_RETRY_DELAY: float = 2.0
    POLL_BACKOFF_DELAY: float = 30.0
    POLL_MAX_CONSECUTIVE_FAILURES: int = 3
    # Session-expired (errcode=-14) means the bot token is invalid; only a
    # re-login can fix it. Wait long enough that we don't spam the server but
    # short enough that a fresh token (e.g. via `wechat login`) is picked up
    # without restarting the daemon.
    POLL_SESSION_EXPIRED_BACKOFF: float = 300.0

    async def run(self) -> None:
        """Main long-poll loop. Runs until cancelled.

        Maintains a consecutive-failure counter so steady-state errors
        backoff to ``POLL_BACKOFF_DELAY`` instead of tight-looping at the
        per-failure ``POLL_RETRY_DELAY`` cadence.
        """
        buf = ""
        failures = 0
        log.info("wechat.start", f"WeChat bot started (base_url={self.base_url})")

        # Start notification polling task
        notify_task = asyncio.create_task(self._poll_notifications())

        async def _backoff(reason: str) -> None:
            """Sleep based on consecutive-failure count, then advance counter."""
            nonlocal failures
            failures += 1
            if failures >= self.POLL_MAX_CONSECUTIVE_FAILURES:
                delay = self.POLL_BACKOFF_DELAY
                # Reset so the *next* unbroken streak rebuilds before the
                # next long sleep — same semantics as the reference SDK.
                failures = 0
            else:
                delay = self.POLL_RETRY_DELAY
            log.warning(
                "wechat.poll.error",
                f"{reason} (consecutive={failures}/{self.POLL_MAX_CONSECUTIVE_FAILURES}, sleep={delay}s)",
            )
            await asyncio.sleep(delay)

        async with httpx.AsyncClient() as client:
            while True:
                try:
                    resp = await get_updates(
                        client, self.base_url, self.token, buf, self.poll_timeout
                    )
                except asyncio.CancelledError:
                    notify_task.cancel()
                    try:
                        await notify_task
                    except BaseException:
                        pass
                    raise
                except httpx.HTTPStatusError as exc:
                    await _backoff(f"iLink HTTP error {exc.response.status_code}")
                    continue
                except Exception as exc:  # noqa: BLE001 — catch-all is the long-poll story
                    await _backoff(f"poll error: {type(exc).__name__}: {exc}")
                    continue

                if is_session_expired(resp):
                    self._session_expired_at = time.monotonic()
                    log.error(
                        "wechat.session.expired",
                        f"iLink session expired (errcode/ret=-14) for account={self.account_id!r}; "
                        f"run `nano-openclaw wechat login --account={self.account_id}` to re-login",
                    )
                    await asyncio.sleep(self.POLL_SESSION_EXPIRED_BACKOFF)
                    continue

                ret = resp.get("ret")
                if ret not in (0, None):
                    await _backoff(f"iLink getUpdates ret={ret}")
                    continue

                # Successful long-poll: clear failure streak and dispatch.
                failures = 0
                buf = resp.get("get_updates_buf", buf)
                for msg in resp.get("msgs", []):
                    asyncio.create_task(self._handle_message(msg))

    async def _resolve_typing_ticket(
        self,
        client: httpx.AsyncClient,
        uid: str,
        ctx: str,
    ) -> str:
        """Return a valid typing_ticket for ``uid``, reusing a cached one when fresh.

        Cache miss / expiry → call ``getconfig``; failures invalidate so the
        next caller retries. Empty result is cached briefly via the same path
        (we just re-fetch next time since we only store non-empty values).
        """
        now = time.monotonic()
        cached = self._typing_ticket_cache.get(uid)
        if cached is not None and cached[1] > now:
            return cached[0]
        ticket = await get_typing_ticket(client, self.base_url, self.token, uid, ctx)
        if ticket:
            self._typing_ticket_cache[uid] = (ticket, now + self.typing_ticket_ttl)
        return ticket

    async def _keep_typing(
        self,
        client: httpx.AsyncClient,
        uid: str,
        ctx: str,
        stop: asyncio.Event,
    ) -> None:
        """Send typing indicator and keep it alive until stop is set."""
        ticket = ""
        try:
            ticket = await self._resolve_typing_ticket(client, uid, ctx)
            if not ticket:
                return
            while not stop.is_set():
                try:
                    await send_typing(client, self.base_url, self.token, uid, ticket, status=1)
                except Exception as exc:
                    log.debug("wechat.typing.error", f"typing keepalive failed for {uid:.16}: {exc}")
                    # Drop the cached ticket so the next turn re-fetches; the
                    # current keepalive loop just keeps trying the same one
                    # since stop is the only exit condition.
                    self._typing_ticket_cache.pop(uid, None)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.typing_interval)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            log.debug("wechat.typing.error", f"typing setup failed for {uid:.16}: {exc}")
            self._typing_ticket_cache.pop(uid, None)
        finally:
            if ticket:
                try:
                    await send_typing(client, self.base_url, self.token, uid, ticket, status=2)
                except Exception:
                    pass

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        uid = msg.get("from_user_id", "")
        ctx = msg.get("context_token", "")
        items = msg.get("item_list", [])
        text = extract_text(items)

        item_types = ",".join(str(it.get("type", 0)) for it in items)
        log.info("wechat.message.received", f"msg from {uid:.16} items={len(items)} types={item_types} text={text[:80] if text else ''!r}")

        # Handle slash commands before attachments
        if text.strip().startswith("/"):
            reply = await self._handle_slash_command(uid, text.strip())
            if reply:
                async with httpx.AsyncClient() as send_client:
                    await send_text(send_client, self.base_url, self.token, uid, reply, ctx)
                return

        # Download image attachments (type=2) with AES decryption
        attachments: list[PromptAttachment] = []
        image_items = extract_image_items(items)
        if image_items:
            log.info("wechat.image.count", f"found {len(image_items)} image item(s) to download")
        async with httpx.AsyncClient() as dl_client:
            for img_item in image_items:
                try:
                    data, mime = await download_wechat_image(dl_client, img_item, self.token)
                    aeskey = img_item.get("aeskey", "")
                    name = f"wechat-image-{aeskey[:8] if aeskey else 'raw'}.jpg"
                    log.debug("wechat.image.downloaded", f"downloaded image: aeskey={aeskey[:8] if aeskey else 'none'} mime={mime} size={len(data)}, b64[:30]={base64.b64encode(data[:30]).decode() if data else 'none'}")
                    attachments.append(
                        PromptAttachment(name=name, mime=mime, size=len(data), data=data)
                    )
                    log.info("wechat.image.downloaded", f"image downloaded: name={name} mime={mime} size={len(data)}")
                except Exception as exc:
                    log.warning("wechat.image.failed", f"image download failed: {exc}")

            # Download file attachments (type=4/5, PDFs, docs, etc.)
            file_items = extract_file_items(items)
            if file_items:
                log.info("wechat.file.count", f"found {len(file_items)} file item(s) to download")
            for file_item in file_items:
                try:
                    data, mime, filename = await download_wechat_file(dl_client, file_item, self.token)
                    log.debug("wechat.file.downloaded", f"downloaded file: filename={filename} mime={mime} size={len(data)}, b64[:30]={base64.b64encode(data[:30]).decode() if data else 'none'}")
                    attachments.append(
                        PromptAttachment(name=filename, mime=mime, size=len(data), data=data)
                    )
                    log.info("wechat.file.downloaded", f"file downloaded: name={filename} mime={mime} size={len(data)}")
                except Exception as exc:
                    log.warning("wechat.file.failed", f"file download failed: {exc}")

        if not text.strip() and not attachments:
            return

        # Prefer the daemon's BackendSessionManager when wired (Phase 9): each
        # uid gets a real persisted session that shows up in /sessions / WebUI.
        # The legacy in-memory dict is only used by the deprecated standalone
        # path which Phase 3 already removed from the CLI.
        backend_session = self._resolve_session(uid)
        if backend_session is not None:
            history = backend_session.history
            transcript_writer = backend_session.writer
            session_id_for_cfg = backend_session.session_id
            session_lock = backend_session.lock
        else:
            history = self._get_or_create_history(uid)
            transcript_writer = None
            session_id_for_cfg = None
            session_lock = None
        # Buffer of TextDeltas that have not yet been flushed as a wechat message.
        # On every ToolUseStart we cut here and ship the buffered text as one
        # standalone message — that gives the user incremental visibility into the
        # model's reasoning between tool calls instead of one big blob at the end.
        text_buf: list[str] = []
        send_queue: asyncio.Queue[str | None] = asyncio.Queue()

        # Heartbeat state: when the model is silently grinding through tools we
        # ship one throttled status line so the user knows work is still happening.
        active_tools: dict[str, str] = {}        # tool_use_id -> name (currently running)
        last_activity_at = time.monotonic()       # bumped whenever we enqueue any segment

        def mark_activity() -> None:
            nonlocal last_activity_at
            last_activity_at = time.monotonic()

        def flush_buf() -> None:
            chunk = "".join(text_buf).strip()
            if not chunk:
                return
            text_buf.clear()
            send_queue.put_nowait(chunk)
            mark_activity()

        def on_event(event: Any) -> None:
            event_type = type(event).__name__
            log.debug("event.received", "", event_type=event_type)
            if isinstance(event, TextDelta):
                text_buf.append(event.text)
            elif isinstance(event, ToolUseStart):
                # Model finished talking and is invoking a tool — ship what it said
                # so far as a separate message, then start a new segment.
                flush_buf()
                active_tools[event.id] = event.name
            elif isinstance(event, ToolUseEnd):
                active_tools.pop(event.id, None)

        async with httpx.AsyncClient() as wechat_client:
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(
                self._keep_typing(wechat_client, uid, ctx, stop_typing)
            )

            async def sender() -> None:
                """Serially drain send_queue; None is the sentinel to stop."""
                while True:
                    segment = await send_queue.get()
                    if segment is None:
                        return
                    try:
                        await send_text(wechat_client, self.base_url, self.token, uid, segment, ctx)
                    except Exception as exc:
                        log.error("wechat.send.segment.failed", f"send_text failed for {uid:.16}: {exc}")

            heartbeat_stop = asyncio.Event()

            async def heartbeat() -> None:
                """Emit a throttled status line when tools have been running silently.

                Wakes every 5s; only enqueues a heartbeat when (a) tools are
                currently running, and (b) no segment has been produced for at
                least `heartbeat_interval` seconds.
                """
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    if heartbeat_stop.is_set():
                        return
                    if not active_tools:
                        continue
                    if time.monotonic() - last_activity_at < self.heartbeat_interval:
                        continue
                    # Dedupe names while preserving insertion order.
                    names = list(dict.fromkeys(active_tools.values()))
                    line = "⏳ " + " · ".join(names)
                    send_queue.put_nowait(line)
                    mark_activity()

            sender_task = asyncio.create_task(sender())
            heartbeat_task = asyncio.create_task(heartbeat())

            try:
                # Build the session config — when daemon-mode, point cfg at
                # the resolved session_id so any code reading cfg.session_key
                # (cron, subagent context, etc.) sees the real id.
                from dataclasses import replace as _dc_replace
                turn_cfg = self.runtime.cfg
                if session_id_for_cfg is not None:
                    turn_cfg = _dc_replace(self.runtime.cfg, session_key=session_id_for_cfg)

                # Hold the session lock so two concurrent messages from the
                # same wechat user serialize on the same backend session
                # (otherwise they'd race on history mutation). Skip when no
                # backend (legacy path).
                if session_lock is not None:
                    if session_lock.locked():
                        log.info(
                            "wechat.session.busy",
                            f"uid={uid:.16}: previous turn still running, will queue",
                        )
                    await session_lock.acquire()
                try:
                    # Share long-lived per-conversation state by reference so
                    # cumulative tokens, last_prompt_tokens, and previous_summary
                    # survive across WeChat turns — otherwise /usage from
                    # WebUI/TUI sees zeros and Stage 3 iterative summary
                    # updates never fire for WeChat sessions.
                    shared_kwargs: dict[str, Any] = {}
                    if backend_session is not None:
                        shared_kwargs["usage_stats"] = backend_session.usage_stats
                        shared_kwargs["compaction_state"] = backend_session.compaction_state
                    agent_session = AgentSession(
                        history=history,
                        registry=_clone_registry(self.runtime.registry, uid, self.account_id),
                        on_event=on_event,
                        client=self.runtime.client,
                        cfg=turn_cfg,
                        transcript_writer=transcript_writer,
                        **shared_kwargs,
                    )
                    await agent_session.run_turn(
                        text or "(no text, maybe just attachments)",
                        attachments=attachments or None,
                    )
                    # Persist sessions.json metadata so /sessions sees the
                    # updated message count after each turn.
                    if backend_session is not None and self.session_manager is not None:
                        try:
                            self.session_manager.save_metadata(backend_session)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("wechat.save_metadata.error", f"{type(exc).__name__}: {exc}")
                finally:
                    if session_lock is not None:
                        session_lock.release()
            except Exception as exc:
                log.error("wechat.turn.failed", f"run_turn failed for {uid:.16}: {exc}")
            finally:
                stop_typing.set()
                heartbeat_stop.set()
                await typing_task
                await heartbeat_task
                # Final segment: whatever text remained after the last tool call
                # (or the entire reply if there were no tool calls at all).
                flush_buf()
                send_queue.put_nowait(None)
                await sender_task
