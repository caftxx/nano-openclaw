"""``WebSocketBackend`` — Backend Protocol implementation that talks to a
remote ``run_daemon`` over JSON-RPC/WebSocket.

The remote daemon's ``/rpc`` endpoint (``api/ws_route.py``) speaks the
exact same Protocol vocabulary as ``EmbeddedBackend`` so the TUI's CLI
``repl()`` can swap the implementation without knowing.

Connection layout:

- One asyncio task = receive loop. It demultiplexes incoming JSON frames:
  ``{id, ok, ...}`` Responses resolve a Future from ``_pending_requests``;
  ``{event, payload, seq}`` PushFrames fan out to subscriber queues.
- Each method call serializes a Request, registers a Future, sends, and
  awaits the Future.
- ``subscribe()`` returns an ``AsyncIterator[PushEvent]`` backed by a
  bounded queue. Slow consumers see a synthetic ``gap`` event (mirroring
  ``EmbeddedBackend._Subscriber``).

v1 has no auth (per user decision) and no automatic reconnect — when the
daemon dies, the next call raises ``ConnectionError``. Reconnect logic
ships when Phase 7 ladders in lifecycle hardening.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import websockets
from websockets.exceptions import ConnectionClosed

from nano_openclaw.core.attachments import PromptAttachment
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
    SubagentInfo,
)
from nano_openclaw.api.protocol import ErrorCode
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


SUBSCRIBER_QUEUE_MAX = 256
SUBSCRIBER_GAP_DROP = 5
DEFAULT_REQUEST_TIMEOUT = 60.0  # seconds — tuned for chat.send + abort, not long turns


# ────────────────────────────────────────────────────────────────────────────
# Subscriber: per-iterator bounded queue (mirrors EmbeddedBackend's helper)
# ────────────────────────────────────────────────────────────────────────────


class _Subscriber:
    """One ``subscribe()`` consumer of remote push events."""

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
            for _ in range(SUBSCRIBER_GAP_DROP):
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self.queue.put_nowait(
                    PushEvent(
                        event="gap",
                        payload={"dropped": SUBSCRIBER_GAP_DROP, "expected_next_seq": event.seq},
                        seq=event.seq,
                    )
                )
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("ws_backend.subscriber.full", "queue still full after gap drop")

    def close(self) -> None:
        self._closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


# ────────────────────────────────────────────────────────────────────────────
# WebSocketBackend
# ────────────────────────────────────────────────────────────────────────────


class WebSocketBackend(Backend):
    """Thin client that talks JSON-RPC to a remote ``run_daemon`` over WS."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.url = url
        self.token = token  # reserved; v1 daemon has no auth
        self.request_timeout = request_timeout
        self._ws: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: list[_Subscriber] = []
        self._closed = False
        self._closing = False
        self._connect_lock = asyncio.Lock()

    # ─── Lifecycle ───

    async def aopen(self) -> None:
        """Connect to the daemon. Idempotent."""
        async with self._connect_lock:
            if self._ws is not None:
                return
            connect_kwargs: dict[str, Any] = {"max_size": 2**24}
            if self.url.startswith("wss://"):
                # Daemon TLS certs are typically self-signed (LAN / phone mic),
                # and v1 has no auth anyway — skip verification so the local TUI
                # can still dial the wss endpoint.
                import ssl

                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                connect_kwargs["ssl"] = ssl_ctx
            self._ws = await websockets.connect(self.url, **connect_kwargs)
            self._receive_task = asyncio.create_task(self._receive_loop(), name="ws-backend-recv")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True

        # Cancel receive loop first so it doesn't reject the close handshake.
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, BaseException):
                pass
            self._receive_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — already-closed connection is fine
                pass
            self._ws = None

        # Resolve any in-flight requests with a connection-closed error so
        # awaiters don't hang.
        for fut in list(self._pending_requests.values()):
            if not fut.done():
                fut.set_exception(BackendError("connection closed"))
        self._pending_requests.clear()

        # Close subscribers.
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()

    # ─── Receive loop: demux Response vs PushFrame ───

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("ws_backend.recv.bad_json", str(exc))
                    continue

                # PushFrame: {event, payload, seq}
                if isinstance(obj, dict) and "event" in obj:
                    self._dispatch_push(obj)
                    continue

                # Response: {id, ok, payload?, error?}
                if isinstance(obj, dict) and "id" in obj:
                    self._resolve_response(obj)
                    continue

                log.warning("ws_backend.recv.unknown_frame", repr(obj)[:200])
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("ws_backend.recv.error", f"{type(exc).__name__}: {exc}")
        finally:
            # Notify in-flight requests + subscribers if the connection
            # dropped unexpectedly (not via aclose, which already cleared).
            if not self._closing:
                for fut in list(self._pending_requests.values()):
                    if not fut.done():
                        fut.set_exception(BackendError("connection lost"))
                self._pending_requests.clear()
                for sub in list(self._subscribers):
                    sub.close()
                self._subscribers.clear()

    def _resolve_response(self, obj: dict[str, Any]) -> None:
        req_id = str(obj.get("id") or "")
        fut = self._pending_requests.pop(req_id, None)
        if fut is None or fut.done():
            return
        fut.set_result(obj)

    def _dispatch_push(self, obj: dict[str, Any]) -> None:
        event = str(obj.get("event") or "")
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        seq = int(obj.get("seq") or 0)
        evt = PushEvent(event=event, payload=payload, seq=seq)  # type: ignore[arg-type]
        for sub in list(self._subscribers):
            if sub.matches(evt):
                sub.push(evt)

    # ─── Send a request, await its response ───

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._closed:
            raise BackendError("backend is closed")
        if self._ws is None:
            await self.aopen()

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = fut

        frame = json.dumps(
            {"id": req_id, "method": method, "params": params or {}},
            ensure_ascii=False,
        )
        try:
            await self._ws.send(frame)  # type: ignore[union-attr]
        except (ConnectionClosed, RuntimeError) as exc:
            self._pending_requests.pop(req_id, None)
            raise BackendError(f"send failed: {exc}") from exc

        try:
            obj = await asyncio.wait_for(fut, timeout=self.request_timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise BackendError(f"timeout waiting for {method!r} response") from None

        if obj.get("ok"):
            return obj.get("payload")

        err = obj.get("error") or {}
        code = str(err.get("code") or ErrorCode.INTERNAL.value)
        message = str(err.get("message") or method)
        if code == ErrorCode.BUSY.value:
            raise BusyError(
                message,
                retry_after_ms=int(err.get("retry_after_ms") or 0),
                details=err.get("details") or {},
            )
        if code == ErrorCode.NOT_FOUND.value:
            raise NotFoundError(message)
        raise BackendError(f"[{code}] {message}")

    # ─── Backend Protocol implementation ───

    async def chat_send(
        self,
        *,
        session_key: str,
        text: str,
        attachments: list[PromptAttachment] | None = None,
        # Embedded-only kwargs — accepted but ignored on the wire path.
        # Esc-to-cancel for remote TUI lives in ws_repl._abort_on_escape,
        # which translates the token flip into a chat.abort RPC instead.
        on_local_event: Any = None,
        cancellation_token: Any = None,
        turn_source: str = "tui",
        response_style: str = "",
    ) -> str:
        params: dict[str, Any] = {"session_key": session_key, "text": text, "turn_source": turn_source, "response_style": response_style}
        if attachments:
            import base64
            params["attachments"] = [
                {
                    "name": a.name,
                    "mime": a.mime,
                    "size": a.size,
                    "data": base64.b64encode(a.data).decode("ascii"),
                }
                for a in attachments
            ]
        result = await self._call("chat.send", params)
        return str(result["turn_id"]) if result else ""

    async def chat_abort(self, *, turn_id: str) -> None:
        await self._call("chat.abort", {"turn_id": turn_id})

    async def chat_history(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
    ) -> HistoryPayload:
        params: dict[str, Any] = {"session_id": session_id}
        if after_seq is not None:
            params["after_seq"] = after_seq
        payload = await self._call("chat.history", params)
        return HistoryPayload(
            session_id=str(payload.get("session_id") or session_id),
            history=list(payload.get("history") or []),
            activities=list(payload.get("activities") or []),
            last_seq=int(payload.get("last_seq") or 0),
        )

    async def sessions_list(self) -> SessionList:
        payload = await self._call("sessions.list")
        sessions = [
            SessionInfo(
                session_id=str(s["session_id"]),
                title=str(s.get("title") or ""),
                preview=str(s.get("preview") or ""),
                created_at=float(s.get("created_at") or 0),
                updated_at=float(s.get("updated_at") or 0),
                model=str(s.get("model") or ""),
                message_count=int(s.get("message_count") or 0),
                compaction_count=int(s.get("compaction_count") or 0),
                current=bool(s.get("current")),
                active_turn_id=s.get("active_turn_id"),
            )
            for s in (payload.get("sessions") or [])
        ]
        return SessionList(sessions=sessions, last_session_id=payload.get("last_session_id"))

    async def sessions_get(self, session_id: str) -> SessionDetails:
        payload = await self._call("sessions.get", {"session_id": session_id})
        return SessionDetails(
            session_id=str(payload.get("session_id") or session_id),
            title=str(payload.get("title") or ""),
            history=list(payload.get("history") or []),
            activities=list(payload.get("activities") or []),
            model=str(payload.get("model") or ""),
            active_turn_id=payload.get("active_turn_id"),
        )

    async def sessions_delete(self, session_id: str) -> None:
        await self._call("sessions.delete", {"session_id": session_id})

    async def sessions_reset(
        self,
        session_key: str,
        *,
        reason: Literal["new", "reset"] = "reset",
    ) -> SessionInfo:
        payload = await self._call(
            "sessions.reset", {"session_key": session_key, "reason": reason},
        )
        return SessionInfo(
            session_id=str(payload.get("session_id") or ""),
            title=str(payload.get("title") or ""),
            preview=str(payload.get("preview") or ""),
            created_at=float(payload.get("created_at") or 0),
            updated_at=float(payload.get("updated_at") or 0),
            model=str(payload.get("model") or ""),
            message_count=int(payload.get("message_count") or 0),
            compaction_count=int(payload.get("compaction_count") or 0),
            current=bool(payload.get("current")),
            active_turn_id=payload.get("active_turn_id"),
        )

    async def sessions_compact(self, session_key: str) -> CompactionResult:
        payload = await self._call("sessions.compact", {"session_key": session_key})
        return CompactionResult(
            success=bool(payload.get("success")),
            summary=payload.get("summary"),
            tokens_before=int(payload.get("tokens_before") or 0),
            tokens_after=int(payload.get("tokens_after") or 0),
        )

    async def sessions_usage(self, session_key: str) -> SessionUsageReport:
        payload = await self._call("sessions.usage", {"session_key": session_key})
        ratio_raw = payload.get("cache_hit_ratio")
        return SessionUsageReport(
            session_id=payload.get("session_id"),
            last_prompt_tokens=int(payload.get("last_prompt_tokens") or 0),
            last_output_tokens=int(payload.get("last_output_tokens") or 0),
            last_cache_read_tokens=int(payload.get("last_cache_read_tokens") or 0),
            last_cache_creation_tokens=int(payload.get("last_cache_creation_tokens") or 0),
            total_prompt_tokens=int(payload.get("total_prompt_tokens") or 0),
            total_output_tokens=int(payload.get("total_output_tokens") or 0),
            total_cache_read_tokens=int(payload.get("total_cache_read_tokens") or 0),
            total_cache_creation_tokens=int(payload.get("total_cache_creation_tokens") or 0),
            compactions_fired=int(payload.get("compactions_fired") or 0),
            turns_recorded=int(payload.get("turns_recorded") or 0),
            cache_hit_ratio=float(ratio_raw) if ratio_raw is not None else None,
            context_budget=int(payload.get("context_budget") or 0),
            context_window=int(payload.get("context_window") or 0),
            cache_ttl=payload.get("cache_ttl"),
        )

    async def get_todos(self, session_key: str) -> list[dict[str, Any]]:
        payload = await self._call("todos.get", {"session_key": session_key})
        items = payload.get("todos") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    async def approvals_list(self) -> list[PendingApproval]:
        payload = await self._call("approvals.list")
        return [
            PendingApproval(
                request_id=str(p.get("request_id") or ""),
                tool_name=str(p.get("tool_name") or ""),
                tool_args=dict(p.get("tool_args") or {}),
                risk_level=str(p.get("risk_level") or "medium"),
                reason=str(p.get("reason") or ""),
                timestamp=float(p.get("timestamp") or 0),
                origin=p.get("origin"),
                turn_id=p.get("turn_id"),
            )
            for p in (payload.get("approvals") or [])
        ]

    async def approvals_respond(
        self,
        request_id: str,
        *,
        allow: bool,
        scope: Literal["once", "session", "always"] = "once",
        reason: str = "",
    ) -> None:
        await self._call("approvals.respond", {
            "request_id": request_id,
            "allow": allow,
            "scope": scope,
            "reason": reason,
        })

    async def models_list(self) -> list[ModelChoice]:
        payload = await self._call("models.list")
        result: list[ModelChoice] = []
        for m in (payload.get("models") or []):
            inputs = m.get("input") or ()
            if isinstance(inputs, list):
                inputs = tuple(str(x) for x in inputs)
            elif not isinstance(inputs, tuple):
                inputs = ()
            result.append(
                ModelChoice(
                    ref=str(m.get("ref") or ""),
                    id=str(m.get("id") or ""),
                    provider=str(m.get("provider") or ""),
                    context_window=m.get("context_window"),
                    is_default=bool(m.get("is_default")),
                    name=m.get("name"),
                    input=inputs,
                    reasoning=bool(m.get("reasoning") or False),
                    max_tokens=m.get("max_tokens"),
                )
            )
        return result

    async def runtime_get(self) -> RuntimeSnapshot:
        payload = await self._call("runtime.get")
        return RuntimeSnapshot(
            agent_id=str(payload.get("agent_id") or ""),
            model_ref=str(payload.get("model_ref") or ""),
            model_id=str(payload.get("model_id") or ""),
            image_model_ref=payload.get("image_model_ref"),
            thinking_level=str(payload.get("thinking_level") or "off"),
            workspace_dir=str(payload.get("workspace_dir") or ""),
            state_dir=str(payload.get("state_dir") or ""),
            context_budget=int(payload.get("context_budget") or 0),
            context_threshold=float(payload.get("context_threshold") or 0.0),
            context_recent_turns=int(payload.get("context_recent_turns") or 0),
            context_window=int(payload.get("context_window") or 0),
        )

    async def runtime_update(
        self,
        *,
        agent_id: str | None = None,
        model_ref: str | None = None,
        image_model_ref: str | None = None,
        thinking_level: str | None = None,
    ) -> RuntimeSnapshot:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if model_ref is not None:
            params["model_ref"] = model_ref
        if image_model_ref is not None:
            params["image_model_ref"] = image_model_ref
        if thinking_level is not None:
            params["thinking_level"] = thinking_level
        payload = await self._call("runtime.update", params)
        return RuntimeSnapshot(
            agent_id=str(payload.get("agent_id") or ""),
            model_ref=str(payload.get("model_ref") or ""),
            model_id=str(payload.get("model_id") or ""),
            image_model_ref=payload.get("image_model_ref"),
            thinking_level=str(payload.get("thinking_level") or "off"),
            workspace_dir=str(payload.get("workspace_dir") or ""),
            state_dir=str(payload.get("state_dir") or ""),
            context_budget=int(payload.get("context_budget") or 0),
            context_threshold=float(payload.get("context_threshold") or 0.0),
            context_recent_turns=int(payload.get("context_recent_turns") or 0),
            context_window=int(payload.get("context_window") or 0),
        )

    async def channels_status(self) -> list[ChannelStatusEntry]:
        payload = await self._call("channels.status")
        return [
            ChannelStatusEntry(
                channel_id=str(c.get("channel_id") or ""),
                account_id=str(c.get("account_id") or ""),
                state=c.get("state") or "stopped",  # type: ignore[arg-type]
                error=c.get("error"),
                started_at=c.get("started_at"),
            )
            for c in (payload.get("channels") or [])
        ]

    async def channels_start(
        self,
        channel_id: str,
        account_id: str | None = None,
    ) -> ChannelStatusEntry:
        params = {"channel_id": channel_id}
        if account_id:
            params["account_id"] = account_id
        c = await self._call("channels.start", params)
        return ChannelStatusEntry(
            channel_id=str(c.get("channel_id") or channel_id),
            account_id=str(c.get("account_id") or account_id or "default"),
            state=c.get("state") or "stopped",  # type: ignore[arg-type]
            error=c.get("error"),
            started_at=c.get("started_at"),
        )

    async def channels_stop(
        self,
        channel_id: str,
        account_id: str | None = None,
    ) -> ChannelStatusEntry:
        params = {"channel_id": channel_id}
        if account_id:
            params["account_id"] = account_id
        c = await self._call("channels.stop", params)
        return ChannelStatusEntry(
            channel_id=str(c.get("channel_id") or channel_id),
            account_id=str(c.get("account_id") or account_id or "default"),
            state=c.get("state") or "stopped",  # type: ignore[arg-type]
            error=c.get("error"),
            started_at=c.get("started_at"),
        )

    async def subagents_list(self) -> list[SubagentInfo]:
        payload = await self._call("subagents.list")
        return [
            SubagentInfo(
                run_id=str(s.get("run_id") or ""),
                label=s.get("label"),
                task=str(s.get("task") or ""),
                status=str(s.get("status") or ""),
                started_at=s.get("started_at"),
            )
            for s in (payload.get("subagents") or [])
        ]

    async def subagents_kill(self, run_id: str) -> None:
        await self._call("subagents.kill", {"run_id": run_id})

    # ─── Features (active-memory / dreaming) ───

    async def active_memory_get(self) -> dict[str, Any]:
        return await self._call("active_memory.get") or {"configured": False, "enabled": False}

    async def active_memory_set(self, **fields: Any) -> dict[str, Any]:
        return await self._call("active_memory.set", dict(fields)) or {"configured": False, "enabled": False}

    async def dreaming_get(self) -> dict[str, Any]:
        return await self._call("dreaming.get") or {"configured": False, "enabled": False}

    async def dreaming_set(self, **fields: Any) -> dict[str, Any]:
        return await self._call("dreaming.set", dict(fields)) or {"configured": False, "enabled": False}

    async def dreaming_run(self) -> dict[str, Any]:
        # Dreaming sweeps can take 10s+; allow a generous timeout for this one call.
        prev_timeout = self.request_timeout
        self.request_timeout = max(prev_timeout, 120.0)
        try:
            return await self._call("dreaming.run") or {}
        finally:
            self.request_timeout = prev_timeout

    async def review_fork_get(self) -> dict[str, Any]:
        return await self._call("review_fork.get") or {"configured": False, "enabled": False}

    async def review_fork_set(self, **fields: Any) -> dict[str, Any]:
        return await self._call("review_fork.set", dict(fields)) or {"configured": False, "enabled": False}

    async def review_fork_run(self, session_key: str | None = None) -> dict[str, Any]:
        # Review-fork run dispatches a fire-and-forget subagent so the call itself
        # is fast; default RPC timeout is fine.
        params: dict[str, Any] = {}
        if session_key:
            params["session_key"] = session_key
        return await self._call("review_fork.run", params) or {}

    async def curator_get(self) -> dict[str, Any]:
        return await self._call("curator.get") or {"configured": False}

    async def curator_set(self, **fields: Any) -> dict[str, Any]:
        return await self._call("curator.set", dict(fields)) or {"configured": False}

    async def curator_run(self, dry_run: bool = False) -> dict[str, Any]:
        return await self._call("curator.run", {"dry_run": bool(dry_run)}) or {}

    async def checkpoint_list(self) -> dict[str, Any]:
        return await self._call("checkpoint.list") or {"checkpoints": []}

    async def checkpoint_create(self, reason: str = "manual") -> dict[str, Any]:
        return await self._call("checkpoint.create", {"reason": reason}) or {}

    async def checkpoint_restore(self, checkpoint_id: str) -> dict[str, Any]:
        return await self._call("checkpoint.restore", {"checkpoint_id": checkpoint_id}) or {}

    # ─── Introspection (tools / skills / plugins / hooks) ───

    async def tools_list(self) -> list[dict[str, Any]]:
        payload = await self._call("tools.list")
        return list(payload.get("tools") or [])

    async def skills_list(self) -> list[dict[str, Any]]:
        payload = await self._call("skills.list")
        return list(payload.get("skills") or [])

    async def plugins_list(self) -> list[dict[str, Any]]:
        payload = await self._call("plugins.list")
        return list(payload.get("plugins") or [])

    async def hooks_list(self) -> dict[str, Any]:
        payload = await self._call("hooks.list")
        return dict(payload.get("hooks") or {})

    async def health(self) -> HealthSummary:
        payload = await self._call("health")
        extra_keys = {k: v for k, v in payload.items() if k not in {
            "runtime_ready", "channels_running", "sessions_loaded", "in_flight_turns",
        }}
        return HealthSummary(
            runtime_ready=bool(payload.get("runtime_ready")),
            channels_running=int(payload.get("channels_running") or 0),
            sessions_loaded=int(payload.get("sessions_loaded") or 0),
            in_flight_turns=int(payload.get("in_flight_turns") or 0),
            extra=extra_keys,
        )

    async def gateway_restart(self) -> dict[str, Any]:
        # The daemon restarts ~0.3s after acking; the WebSocket will drop.
        # The caller is expected to print "restarting…" and (optionally) reconnect.
        return await self._call("gateway.restart") or {}

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
