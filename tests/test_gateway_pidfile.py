"""Tests for gateway/pidfile.py — read/write/clear + liveness probes.

Real ``gateway start`` / ``gateway stop`` integration is tested separately
(see test_gateway_lifecycle.py) since it spawns subprocesses; this file
covers the pure-state-management primitives.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from nano_openclaw.gateway.pidfile import (
    DaemonStatus,
    PidfileEntry,
    gateway_status,
    is_alive,
    pidfile_path,
    port_responds,
    read_pidfile,
    remove_pidfile,
    write_pidfile,
)


# ────────────────────────────────────────────────────────────────────────────
# Read / write / remove
# ────────────────────────────────────────────────────────────────────────────


def test_read_returns_none_when_no_pidfile(tmp_path: Path):
    assert read_pidfile(tmp_path) is None


def test_write_then_read_roundtrip(tmp_path: Path):
    write_pidfile(tmp_path, pid=12345, port=5000, host="127.0.0.1")
    entry = read_pidfile(tmp_path)
    assert entry is not None
    assert entry.pid == 12345
    assert entry.port == 5000
    assert entry.host == "127.0.0.1"


def test_write_overwrites_existing(tmp_path: Path):
    write_pidfile(tmp_path, pid=111, port=5000)
    write_pidfile(tmp_path, pid=222, port=5001, host="0.0.0.0")
    entry = read_pidfile(tmp_path)
    assert entry == PidfileEntry(pid=222, port=5001, host="0.0.0.0")


def test_remove_idempotent(tmp_path: Path):
    remove_pidfile(tmp_path)  # missing file
    write_pidfile(tmp_path, pid=1, port=5000)
    remove_pidfile(tmp_path)
    remove_pidfile(tmp_path)  # already gone
    assert read_pidfile(tmp_path) is None


def test_garbled_pidfile_returns_none(tmp_path: Path):
    pidfile_path(tmp_path).write_text("not a valid pidfile\n")
    assert read_pidfile(tmp_path) is None


def test_empty_pidfile_returns_none(tmp_path: Path):
    pidfile_path(tmp_path).write_text("")
    assert read_pidfile(tmp_path) is None


def test_legacy_two_field_pidfile_defaults_loopback(tmp_path: Path):
    """Earlier pidfile format had only pid + port without host."""
    pidfile_path(tmp_path).write_text("999 5000\n")
    entry = read_pidfile(tmp_path)
    assert entry is not None
    assert entry.pid == 999
    assert entry.port == 5000
    assert entry.host == "127.0.0.1"


# ────────────────────────────────────────────────────────────────────────────
# Liveness probes
# ────────────────────────────────────────────────────────────────────────────


def test_is_alive_true_for_self():
    assert is_alive(os.getpid()) is True


def test_is_alive_false_for_zero_or_negative():
    assert is_alive(0) is False
    assert is_alive(-1) is False


def test_is_alive_false_for_unlikely_pid():
    # Pick a PID that's almost certainly not in use. 2**31 - 1 is past most
    # systems' kernel.pid_max but still within ssize_t.
    assert is_alive(2**30) is False


def test_port_responds_open_port():
    """Bind a real listener on a random port and probe it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert port_responds("127.0.0.1", port) is True
    finally:
        sock.close()


def test_port_responds_closed_port():
    # Port 1 (privileged + nothing listens) — connect refused / unreachable.
    assert port_responds("127.0.0.1", 1, timeout=0.1) is False


# ────────────────────────────────────────────────────────────────────────────
# gateway_status — combined view
# ────────────────────────────────────────────────────────────────────────────


def test_status_no_pidfile_means_not_running(tmp_path: Path):
    status = gateway_status(tmp_path)
    assert status.running is False
    assert status.entry is None
    assert status.stale is False
    assert status.as_summary() == "not running"


def test_status_dead_pid_is_stale(tmp_path: Path):
    write_pidfile(tmp_path, pid=2**30, port=1, host="127.0.0.1")
    status = gateway_status(tmp_path)
    assert status.running is False
    assert status.stale is True
    assert status.pid_alive is False
    assert "stale" in status.as_summary()


def test_status_alive_pid_with_listening_port_means_running(tmp_path: Path):
    """Use the running test process's PID + a real listening socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        write_pidfile(tmp_path, pid=os.getpid(), port=port, host="127.0.0.1")
        status = gateway_status(tmp_path)
        assert status.running is True
        assert status.pid_alive is True
        assert status.port_open is True
        assert status.entry is not None
        assert "running" in status.as_summary()
    finally:
        sock.close()


def test_status_alive_pid_but_dead_port_is_stale(tmp_path: Path):
    """Self pid is alive but no listener on chosen port → stale."""
    write_pidfile(tmp_path, pid=os.getpid(), port=1, host="127.0.0.1")
    status = gateway_status(tmp_path)
    assert status.running is False
    assert status.stale is True
    assert status.pid_alive is True
    assert status.port_open is False
