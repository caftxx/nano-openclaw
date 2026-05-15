"""``run_dingtalk_register`` writes a 0600 creds file in state_dir."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from nano_openclaw.dingtalk import login_cli


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(login_cli, "resolve_state_dir", lambda: tmp_path)
    return tmp_path


def test_writes_creds_file_with_expected_payload(state_dir: Path):
    rc = login_cli.run_dingtalk_register(
        client_id="ding-test",
        client_secret="shh",
        dm_policy="open",
        group_policy="allowlist",
        require_mention=False,
    )
    assert rc == 0

    path = state_dir / "dingtalk-creds.ding-test.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["clientId"] == "ding-test"
    assert data["clientSecret"] == "shh"
    assert data["dmPolicy"] == "open"
    assert data["groupPolicy"] == "allowlist"
    assert data["requireMention"] is False
    assert isinstance(data["createdAtMs"], int)


def test_creds_file_is_chmod_0600(state_dir: Path):
    login_cli.run_dingtalk_register(
        client_id="ding-perm",
        client_secret="x",
    )
    path = state_dir / "dingtalk-creds.ding-perm.json"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_rejects_missing_credentials(state_dir: Path, capsys: pytest.CaptureFixture[str]):
    rc = login_cli.run_dingtalk_register(client_id="", client_secret="x")
    assert rc != 0
    assert "required" in capsys.readouterr().out


def test_rejects_unknown_dm_policy(state_dir: Path, capsys: pytest.CaptureFixture[str]):
    rc = login_cli.run_dingtalk_register(
        client_id="ding-bad",
        client_secret="x",
        dm_policy="weird",  # type: ignore[arg-type]
    )
    assert rc != 0
    assert "dm-policy" in capsys.readouterr().out


def test_rejects_unknown_group_policy(state_dir: Path, capsys: pytest.CaptureFixture[str]):
    rc = login_cli.run_dingtalk_register(
        client_id="ding-bad",
        client_secret="x",
        group_policy="invalid",  # type: ignore[arg-type]
    )
    assert rc != 0
    assert "group-policy" in capsys.readouterr().out


def test_overwrites_existing_creds_file(state_dir: Path):
    login_cli.run_dingtalk_register(client_id="ding-x", client_secret="v1")
    login_cli.run_dingtalk_register(client_id="ding-x", client_secret="v2")
    path = state_dir / "dingtalk-creds.ding-x.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["clientSecret"] == "v2"


def test_load_persisted_creds_round_trip(state_dir: Path):
    login_cli.run_dingtalk_register(
        client_id="ding-rt",
        client_secret="rt-sec",
        dm_policy="allowlist",
    )
    loaded = login_cli.load_persisted_creds(state_dir, "ding-rt")
    assert loaded["clientId"] == "ding-rt"
    assert loaded["clientSecret"] == "rt-sec"
    assert loaded["dmPolicy"] == "allowlist"


def test_load_persisted_creds_returns_empty_on_missing_file(tmp_path: Path):
    assert login_cli.load_persisted_creds(tmp_path, "no-such") == {}


def test_load_persisted_creds_swallows_bad_json(state_dir: Path):
    path = state_dir / "dingtalk-creds.broken.json"
    path.write_text("{not valid json")
    assert login_cli.load_persisted_creds(state_dir, "broken") == {}
