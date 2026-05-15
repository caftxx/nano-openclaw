"""DingTalk Stream protocol client — open_connection → WebSocket → dispatch.

Self-written replacement for the unmaintained ``dingtalk-stream`` pip package.
Implements the minimum protocol surface needed to host a robot:

1. ``POST /v1.0/gateway/connections/open`` with the AppKey/AppSecret pair to
   receive a one-shot ``(endpoint, ticket)`` pair.
2. Open a WebSocket to ``{endpoint}?ticket={ticket}``.
3. For each inbound frame: send an ACK back synchronously, then dispatch to
   the appropriate callback (``on_callback`` / ``on_event`` / ``on_system``).
4. On connection loss, reconnect with exponential backoff (1s → 30s + jitter)
   **after requesting a new ticket** — the old ticket is already burned.

Heartbeat uses the WebSocket protocol's native PING (``ping_interval=60``).
The TS connector layers a 10s/20s application-level heartbeat on top; we
don't bother in v1 — the SDK's original 60s interval has proven enough
under reconnection storms in production.

Credentials never leak into ``os.environ``; they live only on the client
instance and on the ``DingtalkTokenManager`` it owns.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Optional
from urllib.parse import quote

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from nano_openclaw.dingtalk.frames import (
    CallbackFrame,
    EventFrame,
    SystemFrame,
    _BaseFrame,
    make_ack,
    parse_inbound,
)
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


OPEN_CONNECTION_URL = "https://api.dingtalk.com/v1.0/gateway/connections/open"
DEFAULT_SUBSCRIPTIONS = [
    {"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"},
    {"type": "EVENT", "topic": "*"},
]
DEFAULT_UA = "nano-openclaw/0.1"
PING_INTERVAL_SECONDS = 60
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0


CallbackHandler = Callable[[CallbackFrame], Awaitable[None]]
EventHandler = Callable[[EventFrame], Awaitable[None]]
SystemHandler = Callable[[SystemFrame], Awaitable[None]]


def _local_ip() -> str:
    """Best-effort local IP. Falls back to ``127.0.0.1`` because the field is
    informational — DingTalk treats it as connection metadata, not auth."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


class DingtalkStreamClient:
    """One persistent stream connection for one ``(clientId, clientSecret)`` pair.

    Lifecycle:

    1. ``run()`` blocks until ``stop()`` is called or the task is cancelled.
    2. Internally it loops: ``open_connection`` → connect → recv loop →
       on disconnect, back off and try again with a fresh ticket.

    Callbacks are awaited inline on the recv loop; long-running handlers
    should ``asyncio.create_task`` rather than block the loop or the next
    ACK will be late.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        on_callback: CallbackHandler,
        on_event: Optional[EventHandler] = None,
        on_system: Optional[SystemHandler] = None,
        subscriptions: Optional[list[dict[str, str]]] = None,
        ua: str = DEFAULT_UA,
        http_client: Optional[httpx.AsyncClient] = None,
        ws_connect: Any = None,  # injection point for tests
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_callback = on_callback
        self._on_event = on_event
        self._on_system = on_system
        self._subscriptions = subscriptions or DEFAULT_SUBSCRIPTIONS
        self._ua = ua
        self._http_client = http_client
        self._ws_connect = ws_connect or websockets.connect
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._stopped = False
        self._on_status: Optional[Callable[[str], None]] = None

    def set_status_callback(self, cb: Callable[[str], None]) -> None:
        """Register a status callback receiving ``"connecting"`` /
        ``"connected"`` / ``"reconnecting"`` / ``"stopped"`` strings. Used by
        the channel to populate ``/channels`` status."""
        self._on_status = cb

    async def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Main reconnect loop. Returns only when ``stop()`` is called."""
        backoff = self._backoff_base
        while not self._stopped:
            try:
                self._emit_status("connecting")
                endpoint, ticket = await self._open_connection()
                await self._connect_and_loop(endpoint, ticket)
                # _connect_and_loop returns on clean disconnect — reset backoff.
                backoff = self._backoff_base
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — stream is best-effort
                log.warning(
                    "dingtalk.stream.error",
                    f"client_id={self._client_id[:8]}… "
                    f"{type(exc).__name__}: {exc}; reconnect in {backoff:.1f}s",
                )

            if self._stopped:
                break

            self._emit_status("reconnecting")
            # Jitter is a fraction of the base interval so tiny test backoffs
            # don't get drowned out by a fixed 1s random.
            await asyncio.sleep(backoff + random.random() * self._backoff_base)
            backoff = min(backoff * 2, self._backoff_max)

        self._emit_status("stopped")

    async def _open_connection(self) -> tuple[str, str]:
        """POST ``/gateway/connections/open`` to mint a fresh ticket."""
        payload = {
            "clientId": self._client_id,
            "clientSecret": self._client_secret,
            "subscriptions": self._subscriptions,
            "ua": self._ua,
            "localIp": _local_ip(),
        }
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.post(OPEN_CONNECTION_URL, json=payload)
            resp.raise_for_status()
            body = resp.json()
        finally:
            if self._http_client is None:
                await client.aclose()

        endpoint = str(body.get("endpoint") or "")
        ticket = str(body.get("ticket") or "")
        if not endpoint or not ticket:
            raise RuntimeError(
                f"dingtalk open_connection response missing endpoint/ticket: {body!r}"
            )
        return endpoint, ticket

    async def _connect_and_loop(self, endpoint: str, ticket: str) -> None:
        url = f"{endpoint}?ticket={quote(ticket, safe='')}"
        log.info(
            "dingtalk.stream.connecting",
            f"client_id={self._client_id[:8]}… endpoint={endpoint}",
        )
        async with self._ws_connect(url, ping_interval=PING_INTERVAL_SECONDS) as ws:
            self._emit_status("connected")
            log.info("dingtalk.stream.connected", f"client_id={self._client_id[:8]}…")
            try:
                async for raw in ws:
                    await self._handle_raw(ws, raw)
            except ConnectionClosed:
                log.info("dingtalk.stream.closed", f"client_id={self._client_id[:8]}…")

    async def _handle_raw(self, ws: Any, raw: Any) -> None:
        """Parse, ACK, then dispatch one inbound frame.

        ACK happens *before* dispatch so a slow handler can't make the server
        retransmit. Dispatch errors are caught and logged — they must not
        crash the recv loop.
        """
        try:
            payload = json.loads(raw)
            frame = parse_inbound(payload)
        except Exception as exc:  # noqa: BLE001 — malformed input is best-effort
            log.warning("dingtalk.stream.parse_error", f"{type(exc).__name__}: {exc}")
            return

        try:
            ack = make_ack(frame)
            await ws.send(ack.model_dump_json())
        except Exception as exc:  # noqa: BLE001
            log.warning("dingtalk.stream.ack_error", f"{type(exc).__name__}: {exc}")
            # If we can't ACK, dispatching is pointless — the server will retry.
            return

        await self._dispatch(frame)

    async def _dispatch(self, frame: _BaseFrame) -> None:
        try:
            if isinstance(frame, CallbackFrame):
                await self._on_callback(frame)
            elif isinstance(frame, EventFrame):
                if self._on_event is not None:
                    await self._on_event(frame)
            elif isinstance(frame, SystemFrame):
                if self._on_system is not None:
                    await self._on_system(frame)
                if frame.headers.topic == "disconnect":
                    # Server is kicking us; treat it like a closed connection.
                    # ConnectionClosed is normally raised by websockets itself
                    # when this happens; nothing extra to do here besides log.
                    log.info("dingtalk.stream.kicked", "server requested disconnect")
        except Exception as exc:  # noqa: BLE001 — handler bugs shouldn't crash the loop
            log.error(
                "dingtalk.stream.dispatch_error",
                f"topic={frame.headers.topic} type={frame.type} "
                f"{type(exc).__name__}: {exc}",
            )

    def _emit_status(self, state: str) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(state)
        except Exception:  # noqa: BLE001
            pass
