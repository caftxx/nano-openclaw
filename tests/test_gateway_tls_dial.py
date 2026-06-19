"""Regression tests for dialing a TLS-enabled gateway over wss.

When the daemon serves HTTPS (``tls_cert`` + ``tls_key`` set), its ``/rpc``
endpoint only speaks ``wss``. Three call sites used to hardcode plaintext
``ws://`` and would hit "InvalidMessage / did not receive a valid HTTP
response" (or, for ``gateway status``, a bogus "rpc probe: timed out") against
such a daemon:

- ``__main__._daemon_connect_url`` — TUI auto-detect of a local daemon.
- ``gateway.cli._probe_gateway_rpc`` — the ``gateway status`` RPC probe.
- ``gateway.backend_websocket.WebSocketBackend.aopen`` — the actual dial.

Plus a smoke guard that ``gateway.server`` imports ``lan_ip`` (a missing
import crashed ``gateway run`` with ``NameError``).
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import pytest

from nano_openclaw.__main__ import _daemon_connect_url
from nano_openclaw.gateway.pidfile import PidfileEntry


# ────────────────────────────────────────────────────────────────────────────
# TUI auto-detect URL builder (__main__._daemon_connect_url)
# ────────────────────────────────────────────────────────────────────────────


def _entry(host: str, scheme: str, port: int = 5000) -> PidfileEntry:
    return PidfileEntry(pid=123, port=port, host=host, scheme=scheme)


def test_connect_url_https_uses_wss():
    assert _daemon_connect_url(_entry("127.0.0.1", "https")) == "wss://127.0.0.1:5000/rpc"


def test_connect_url_http_uses_ws():
    assert _daemon_connect_url(_entry("127.0.0.1", "http")) == "ws://127.0.0.1:5000/rpc"


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_connect_url_wildcard_host_loops_back(wildcard: str):
    # A wildcard bind host isn't dialable — must collapse to loopback.
    assert _daemon_connect_url(_entry(wildcard, "https")) == "wss://127.0.0.1:5000/rpc"
    assert _daemon_connect_url(_entry(wildcard, "http")) == "ws://127.0.0.1:5000/rpc"


def test_connect_url_concrete_host_preserved():
    assert (
        _daemon_connect_url(_entry("192.168.1.9", "https", port=8080))
        == "wss://192.168.1.9:8080/rpc"
    )


# ────────────────────────────────────────────────────────────────────────────
# gateway status RPC probe (cli._probe_gateway_rpc)
# ────────────────────────────────────────────────────────────────────────────


class _FakeProbeWS:
    """Answers the probe's health / runtime.get / channels.status calls."""

    async def send(self, _payload: str) -> None:
        return None

    async def recv(self) -> str:
        return '{"ok": true, "payload": {}}'


class _FakeProbeConn:
    """Async context manager standing in for ``websockets.connect(...)``."""

    def __init__(self, recorder: dict[str, Any]):
        self._recorder = recorder

    async def __aenter__(self) -> _FakeProbeWS:
        return _FakeProbeWS()

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _patch_probe_connect(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import websockets

    recorder: dict[str, Any] = {}

    def fake_connect(url: str, **kwargs: Any) -> _FakeProbeConn:
        recorder["url"] = url
        recorder["kwargs"] = kwargs
        return _FakeProbeConn(recorder)

    monkeypatch.setattr(websockets, "connect", fake_connect)
    return recorder


def test_probe_https_dials_wss_with_ssl(monkeypatch: pytest.MonkeyPatch):
    from nano_openclaw.gateway.cli import _probe_gateway_rpc

    recorder = _patch_probe_connect(monkeypatch)
    result = _probe_gateway_rpc("0.0.0.0", 5000, scheme="https", timeout=1.0)

    assert result is not None  # probe succeeded over the (fake) wss transport
    assert recorder["url"] == "wss://127.0.0.1:5000/rpc"
    assert isinstance(recorder["kwargs"].get("ssl"), ssl.SSLContext)
    # Self-signed certs in LAN/phone setups — verification must be off.
    assert recorder["kwargs"]["ssl"].verify_mode == ssl.CERT_NONE


def test_probe_http_dials_ws_without_ssl(monkeypatch: pytest.MonkeyPatch):
    from nano_openclaw.gateway.cli import _probe_gateway_rpc

    recorder = _patch_probe_connect(monkeypatch)
    result = _probe_gateway_rpc("127.0.0.1", 5000, scheme="http", timeout=1.0)

    assert result is not None
    assert recorder["url"] == "ws://127.0.0.1:5000/rpc"
    assert recorder["kwargs"].get("ssl") is None


# ────────────────────────────────────────────────────────────────────────────
# WebSocketBackend.aopen TLS dial (backend_websocket)
# ────────────────────────────────────────────────────────────────────────────


class _FakeBackendWS:
    """Async-iterable stand-in; the receive loop parks on __anext__."""

    def __init__(self) -> None:
        self._gate = asyncio.Event()  # never set → loop blocks until cancelled

    def __aiter__(self) -> "_FakeBackendWS":
        return self

    async def __anext__(self) -> str:
        await self._gate.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        return None


def _patch_backend_connect(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import websockets

    recorder: dict[str, Any] = {}

    async def fake_connect(url: str, **kwargs: Any) -> _FakeBackendWS:
        recorder["url"] = url
        recorder["kwargs"] = kwargs
        return _FakeBackendWS()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    return recorder


def test_backend_wss_passes_insecure_ssl_context(monkeypatch: pytest.MonkeyPatch):
    from nano_openclaw.api.backend_websocket import WebSocketBackend

    recorder = _patch_backend_connect(monkeypatch)

    async def run() -> None:
        backend = WebSocketBackend("wss://127.0.0.1:5000/rpc")
        await backend.aopen()
        try:
            assert recorder["url"] == "wss://127.0.0.1:5000/rpc"
            ctx = recorder["kwargs"].get("ssl")
            assert isinstance(ctx, ssl.SSLContext)
            assert ctx.verify_mode == ssl.CERT_NONE
            assert ctx.check_hostname is False
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_backend_ws_omits_ssl(monkeypatch: pytest.MonkeyPatch):
    from nano_openclaw.api.backend_websocket import WebSocketBackend

    recorder = _patch_backend_connect(monkeypatch)

    async def run() -> None:
        backend = WebSocketBackend("ws://127.0.0.1:5000/rpc")
        await backend.aopen()
        try:
            assert recorder["url"] == "ws://127.0.0.1:5000/rpc"
            # Plaintext dial — no ssl kwarg handed to websockets.connect at all.
            assert "ssl" not in recorder["kwargs"]
        finally:
            await backend.aclose()

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# server.py lan_ip import (gateway run NameError regression)
# ────────────────────────────────────────────────────────────────────────────


def test_server_imports_lan_ip():
    import nano_openclaw.gateway.server as server
    from nano_openclaw.gateway.pidfile import lan_ip

    # The wildcard-bind LAN URL advert calls lan_ip(); a missing import made
    # `gateway run` crash with NameError before binding.
    assert server.lan_ip is lan_ip
