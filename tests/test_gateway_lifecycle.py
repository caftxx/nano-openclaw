"""End-to-end lifecycle test for ``nano-openclaw gateway start/status/stop``.

Spawns the real CLI as a subprocess, hits a free port, and asserts the
external observables: ``gateway status`` output, HTTP reachability, and
clean shutdown via ``gateway stop``. Marked slow because it spends ~3-5s
waiting on the daemon to come up and tear down.

Hermetic: uses tmp_path as state_dir and injects a fake ANTHROPIC_API_KEY
so the test doesn't depend on the developer's ~/.nano-openclaw or shell env.
The daemon never calls the model during start/status/stop, so a fake key is
fine — resolve_api_key only validates that *some* key exists.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _free_port() -> int:
    """Grab a free TCP port (then release it). Race-prone but cheap."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _hermetic_env(state_dir: Path) -> dict[str, str]:
    """Build a subprocess env that isolates the daemon from the user's machine.

    - NANO_OPENCLAW_STATE_DIR pins state into tmp_path
    - NANO_OPENCLAW_CONFIG_PATH points at a minimal config so search-path
      fallbacks (cwd/workspace/, ~/.nano-openclaw/) can't leak in
    - ANTHROPIC_API_KEY is a fake placeholder — daemon startup only
      validates that a key exists, never calls the model
    """
    env = os.environ.copy()
    env["NANO_OPENCLAW_STATE_DIR"] = str(state_dir)
    env["NANO_OPENCLAW_CONFIG_PATH"] = str(state_dir / "nano-openclaw.json5")
    env["ANTHROPIC_API_KEY"] = "test-fake-key-not-used"
    return env


def _run(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=env,
    )


def test_gateway_lifecycle_start_status_stop(tmp_path: Path):
    # Minimal config — empty `{}` would also work since all fields have
    # defaults, but specifying the model explicitly makes the test resilient
    # to default-model changes.
    (tmp_path / "nano-openclaw.json5").write_text(
        '{ agents: { defaults: { model: "anthropic/claude-sonnet-4-5-20250929" } } }',
        encoding="utf-8",
    )
    env = _hermetic_env(tmp_path)
    port = _free_port()

    # Status before start
    pre = _run([sys.executable, "-m", "nano_openclaw", "gateway", "status"], env=env, cwd=tmp_path)
    assert "not running" in pre.stdout or "not running" in pre.stderr

    started = False
    try:
        # Start — pin --host so the test doesn't pick up whatever the user's
        # config.gateway.host is (could be 0.0.0.0 etc).
        start = _run([
            sys.executable, "-m", "nano_openclaw", "gateway", "start",
            "--host", "127.0.0.1", "--port", str(port),
        ], env=env, cwd=tmp_path)
        assert start.returncode == 0, f"start failed: {start.stdout}{start.stderr}"
        assert "started" in start.stdout, start.stdout
        started = True

        # Give the daemon a moment to fully settle
        time.sleep(0.5)

        # Status while running
        running = _run([sys.executable, "-m", "nano_openclaw", "gateway", "status"], env=env, cwd=tmp_path)
        assert running.returncode == 0
        assert f"127.0.0.1:{port}" in running.stdout
        assert "running" in running.stdout

        # HTTP probe — WebUI index should be reachable
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2.0)
            assert resp.status == 200
        except urllib.error.URLError as exc:
            pytest.fail(f"webui index not reachable: {exc}")

    finally:
        if started:
            _run([sys.executable, "-m", "nano_openclaw", "gateway", "stop"], env=env, cwd=tmp_path)
            time.sleep(0.5)

    # After stop, status should report not running
    post = _run([sys.executable, "-m", "nano_openclaw", "gateway", "status"], env=env, cwd=tmp_path)
    assert "not running" in post.stdout or "stale" in post.stdout
