"""WeChat bot main loop for nano-openclaw.

Connects to iLink WeChat API via long-polling and routes each message through
BackendService, sending the reply back via iLink.

Each WeChat user gets an isolated history list (per-user session).

Supports directed notifications: scheduled jobs created via WeChat will notify
the creator when completed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import base64

import httpx

from nano_openclaw.core.attachments import PromptAttachment
from nano_openclaw.adapters.channels.chunking import chunk_text
from nano_openclaw.logger import get_logger
from nano_openclaw.core.provider import TextDelta
from nano_openclaw.core._stream_events import ToolUseEnd, ToolUseStart
from nano_openclaw.core.tools import ToolRegistry, Tool
from nano_openclaw.services.backend import BusyError
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


async def _send_chunked_text(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    to_user: str,
    text: str,
    ctx: str | None = None,
) -> None:
    """Send text through iLink using the channel's outbound chunking policy."""
    for segment in chunk_text(text):
        await send_text(client, base_url, token, to_user, segment, ctx)


def _clone_registry(registry: ToolRegistry, uid: str, account_id: str = "default") -> ToolRegistry:
    """Clone registry and inject sender into cron_create / schedule_wakeup.

    Phase 2 changed the ``created_by`` format from two-segment ``wechat:{uid}``
    to three-segment ``wechat:{account_id}:{uid}`` so the channel registry
    can route notifications to the right account on multi-account daemons.
    The cron scheduler accepts both formats (legacy two-segment is mapped to
    account ``"default"``).
    """
    clone = registry.clone()

    created_by = f"wechat:{account_id}:{uid}"

    # Wrap cron_create to auto-bind created_by and enable notification
    if "cron_create" in clone._tools:
        cron_create = clone._tools["cron_create"]
        async def wrapped_run(args: dict[str, Any]) -> str:
            args["created_by"] = created_by
            args.setdefault("notify_wechat", True)  # 微信创建默认开启通知
            result = cron_create.run(args)
            return await result if asyncio.iscoroutine(result) else result

        clone._tools["cron_create"] = Tool(
            name="cron_create",
            description=cron_create.description,
            input_schema=cron_create.input_schema,
            run=wrapped_run,
        )

    # Wrap schedule_wakeup similarly
    if "schedule_wakeup" in clone._tools:
        schedule_wakeup = clone._tools["schedule_wakeup"]
        async def wrapped_wakeup(args: dict[str, Any]) -> str:
            args["created_by"] = created_by
            args.setdefault("notify_wechat", True)
            result = schedule_wakeup.run(args)
            return await result if asyncio.iscoroutine(result) else result

        clone._tools["schedule_wakeup"] = Tool(
            name="schedule_wakeup",
            description=schedule_wakeup.description,
            input_schema=schedule_wakeup.input_schema,
            run=wrapped_wakeup,
        )

    return clone


@dataclass
class WechatBot:
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
    # ``session_manager`` routes per-uid conversations through
    # ``BackendSessionManager`` — same store the WebUI/TUI ``/sessions``
    # surface reads.
    session_manager: Any | None = None
    # ``backend`` is the daemon's shared BackendService; all WeChat turns and
    # slash commands go through it so channel delivery cannot bypass service
    # orchestration.
    backend: Any | None = None
    # Persistence path for the uid → session_id mapping; without it, restarts
    # forget which uid maps to which session and the next message creates a
    # fresh empty session.
    uid_map_path: Path | None = None
    # Latest iLink context_token for each uid.  Scheduled/proactive delivery
    # happens outside an inbound turn, so it cannot rely on a stack-local token.
    # Persisting the map also keeps delivery working across gateway restarts.
    context_token_path: Path | None = None
    # typing_ticket cache TTL — iLink tickets are stable for some minutes; we
    # default to 30 min to avoid a getconfig RTT on every reply while still
    # rotating well before any plausible server-side expiry.
    typing_ticket_ttl: float = 1800.0
    # Auto-bound uid sessions roll over lazily when a new request arrives
    # after this much inactivity. 0 disables rollover.
    session_idle_minutes: int = 360
    # uid -> session_id (loaded from uid_map_path on first use)
    _uid_to_session_id: dict[str, str] = field(default_factory=dict)
    _uid_map_loaded: bool = False
    _context_tokens: dict[str, str] = field(default_factory=dict)
    _context_tokens_loaded: bool = False
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

    def _ensure_context_tokens_loaded(self) -> None:
        """Load the per-user iLink context-token cache once."""
        if self._context_tokens_loaded:
            return
        self._context_tokens_loaded = True
        if self.context_token_path is None:
            return
        try:
            if self.context_token_path.exists():
                import json
                raw = json.loads(self.context_token_path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    self._context_tokens = {
                        str(uid): token
                        for uid, token in raw.items()
                        if isinstance(token, str) and token
                    }
        except (OSError, ValueError) as exc:
            log.warning(
                "wechat.context_tokens.load.error",
                f"failed to load {self.context_token_path}: {type(exc).__name__}: {exc}",
            )
            self._context_tokens = {}

    def _save_context_tokens(self) -> None:
        if self.context_token_path is None:
            return
        try:
            import json
            self.context_token_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.context_token_path.with_suffix(self.context_token_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._context_tokens, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.context_token_path)
            os.chmod(self.context_token_path, 0o600)
        except OSError as exc:
            log.warning(
                "wechat.context_tokens.save.error",
                f"failed to save {self.context_token_path}: {exc}",
            )

    def _remember_context_token(self, uid: str, ctx: str) -> None:
        """Remember the freshest inbound context token and wake queued sends."""
        if not uid or not ctx:
            return
        self._ensure_context_tokens_loaded()
        changed = self._context_tokens.get(uid) != ctx
        self._context_tokens[uid] = ctx
        if changed:
            self._save_context_tokens()
        if self.notify_queue is not None:
            woken = self.notify_queue.retry_now_for_target(uid)
            if woken:
                log.info(
                    "wechat.notify.retry.woken",
                    f"account={self.account_id} target={uid:.16} pending={woken}",
                )

    def _get_context_token(self, uid: str) -> str:
        self._ensure_context_tokens_loaded()
        return self._context_tokens.get(uid, "")

    def _resolve_session(self, uid: str):
        """Return an ``AgentBackendSession`` for a wechat uid (daemon mode).

        First contact → manager.create() and persist the mapping.
        Subsequent → manager.get_or_load(stored_id); if the file vanished
        somehow (manual deletion etc.) → fall through to a fresh session.

        ``None`` only when ``session_manager`` isn't wired by a low-level test.
        """
        if self.session_manager is None:
            return None
        self._ensure_uid_map_loaded()

        existing_id = self._uid_to_session_id.get(uid)
        if existing_id:
            try:
                session = self.session_manager.get_or_load(existing_id)
                if not self.session_manager.is_idle(session, self.session_idle_minutes):
                    self.session_manager.mark_interaction(session)
                    return session
                log.info(
                    "wechat.session.idle_rollover",
                    f"uid={uid:.16} old_session={session.session_id} idle_minutes={self.session_idle_minutes}",
                )
            except KeyError:
                # mapping points at a deleted session — fall through to create
                pass
            self._uid_to_session_id.pop(uid, None)

        new_session = self.session_manager.create()
        self.session_manager.mark_interaction(new_session)
        self._uid_to_session_id[uid] = new_session.session_id
        self._save_uid_map()
        return new_session

    def _get_or_create_history(self, uid: str) -> list[Any]:
        raise RuntimeError("WeChat history is owned by BackendSessionManager")

    async def _poll_notifications(self) -> None:
        """Poll notify-queue and send directed notifications to creators."""
        if not self.notify_queue:
            return

        while True:
            await asyncio.sleep(self.notify_poll_interval)

            try:
                pending = self.notify_queue.get_pending(limit=10)
            except Exception as exc:  # keep the background worker supervised
                log.error(
                    "wechat.notify.queue.read.failed",
                    f"account={self.account_id}: {type(exc).__name__}: {exc}",
                )
                continue
            if not pending:
                continue

            async with httpx.AsyncClient() as client:
                for item in pending:
                    target_uid = item.target_uid
                    if not target_uid:
                        self.notify_queue.mark_failed(
                            item.job_id,
                            item.created_at,
                            "notification has no target uid",
                            retry_delay=300.0,
                        )
                        continue
                    ctx = self._get_context_token(target_uid)
                    if not ctx:
                        self.notify_queue.mark_failed(
                            item.job_id,
                            item.created_at,
                            "missing iLink context_token; waiting for the user to message the bot",
                            retry_delay=300.0,
                        )
                        log.warning(
                            "wechat.notify.deferred.no_context",
                            f"account={self.account_id} target={target_uid:.16} job={item.job_name}",
                        )
                        continue
                    try:
                        chunks = chunk_text(item.result_summary)
                        start_index = min(item.next_chunk_index, len(chunks))
                        for index in range(start_index, len(chunks)):
                            stable_key = (
                                f"{self.account_id}\0{item.job_id}\0{item.created_at}\0{index}"
                            ).encode("utf-8")
                            client_id = "nano-notify-" + hashlib.sha256(stable_key).hexdigest()[:24]
                            await send_text(
                                client,
                                self.base_url,
                                self.token,
                                target_uid,
                                chunks[index],
                                ctx,
                                client_id=client_id,
                            )
                            self.notify_queue.mark_chunk_sent(
                                item.job_id,
                                item.created_at,
                                index + 1,
                            )
                        self.notify_queue.mark_sent(item.job_id, item.created_at)
                        log.info("wechat.notify.sent", f"notification sent to {target_uid:.16} for job {item.job_name}")
                    except Exception as exc:
                        log.warning("wechat.notify.failed", f"send notification to {target_uid:.16} failed: {exc}")
                        retry_delay = min(900.0, 30.0 * (2 ** min(item.attempts, 5)))
                        self.notify_queue.mark_failed(
                            item.job_id,
                            item.created_at,
                            f"{type(exc).__name__}: {exc}",
                            retry_delay=retry_delay,
                        )

    async def _handle_slash_command(self, uid: str, cmd: str) -> str | None:
        """Defer to the shared ``services/slash.py`` dispatcher via a
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

        from nano_openclaw.services.slash import handle_slash, QuitREPL
        from nano_openclaw.services.slash_renderer import PlainRenderer

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
        self._remember_context_token(uid, ctx)
        items = msg.get("item_list", [])
        text = extract_text(items)

        item_types = ",".join(str(it.get("type", 0)) for it in items)
        log.info("wechat.message.received", f"msg from {uid:.16} items={len(items)} types={item_types} text={text[:80] if text else ''!r}")

        # Handle slash commands before attachments
        if text.strip().startswith("/"):
            reply = await self._handle_slash_command(uid, text.strip())
            if reply:
                async with httpx.AsyncClient() as send_client:
                    await _send_chunked_text(
                        send_client,
                        self.base_url,
                        self.token,
                        uid,
                        reply,
                        ctx,
                    )
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

        backend_session = self._resolve_session(uid)
        if backend_session is None or self.backend is None:
            log.error(
                "wechat.backend.missing",
                f"message from {uid:.16} received without backend/session manager",
            )
            async with httpx.AsyncClient() as send_client:
                await _send_chunked_text(
                    send_client,
                    self.base_url,
                    self.token,
                    uid,
                    "⚠️ WeChat channel requires the daemon backend. Start it through `gateway run`.",
                    ctx,
                )
            return
        session_id_for_cfg = backend_session.session_id
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
            buffered = "".join(text_buf).strip()
            if not buffered:
                return
            text_buf.clear()
            send_queue.put_nowait(buffered)
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
                        await _send_chunked_text(
                            wechat_client,
                            self.base_url,
                            self.token,
                            uid,
                            segment,
                            ctx,
                        )
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
                while True:
                    try:
                        turn_id = await self.backend.chat_send(
                            session_key=session_id_for_cfg,
                            text=text or "(no text, maybe just attachments)",
                            attachments=attachments or None,
                            on_local_event=on_event,
                            turn_source="wechat",
                            channel_id="wechat",
                            channel_account_id=self.account_id,
                            channel_sender_key=uid,
                        )
                        break
                    except BusyError as exc:
                        log.info(
                            "wechat.session.busy",
                            f"uid={uid:.16}: previous turn still running, will queue",
                        )
                        await asyncio.sleep(max(0.05, (exc.retry_after_ms or 500) / 1000))
                await self.backend.await_turn(turn_id)
                try:
                    refreshed = self.session_manager.get_or_load(session_id_for_cfg)
                    self.session_manager.save_metadata(refreshed)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "wechat.save_metadata.error",
                        f"{type(exc).__name__}: {exc}",
                    )
            except Exception as exc:
                log.error("wechat.turn.failed", f"backend turn failed for {uid:.16}: {exc}")
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
