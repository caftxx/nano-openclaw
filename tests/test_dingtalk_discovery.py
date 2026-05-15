"""``discover_persisted_account_ids_dingtalk`` enumerates state_dir creds files."""

from __future__ import annotations

from pathlib import Path

from nano_openclaw.dingtalk.login_cli import discover_persisted_account_ids_dingtalk


def test_returns_empty_when_state_dir_missing(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert discover_persisted_account_ids_dingtalk(missing) == []


def test_returns_empty_when_no_creds_files(tmp_path: Path):
    # Unrelated files must not be mistaken for creds.
    (tmp_path / "wechat-tokens.json").write_text("{}")
    (tmp_path / "random.txt").write_text("noise")
    assert discover_persisted_account_ids_dingtalk(tmp_path) == []


def test_returns_sorted_client_ids(tmp_path: Path):
    (tmp_path / "dingtalk-creds.ding-zzz.json").write_text("{}")
    (tmp_path / "dingtalk-creds.ding-aaa.json").write_text("{}")
    (tmp_path / "dingtalk-creds.ding-mmm.json").write_text("{}")
    # Co-existing wechat files must be ignored.
    (tmp_path / "wechat-tokens.json").write_text("{}")
    (tmp_path / "wechat-tokens.work.json").write_text("{}")

    ids = discover_persisted_account_ids_dingtalk(tmp_path)
    assert ids == ["ding-aaa", "ding-mmm", "ding-zzz"]


def test_ignores_files_without_client_id(tmp_path: Path):
    # Pathological: a bare ``dingtalk-creds..json`` should not yield an empty
    # client_id entry; the discovery logic guards against this.
    (tmp_path / "dingtalk-creds..json").write_text("{}")
    (tmp_path / "dingtalk-creds.real.json").write_text("{}")

    ids = discover_persisted_account_ids_dingtalk(tmp_path)
    assert ids == ["real"]
