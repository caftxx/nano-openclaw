"""``nano-openclaw wechat login`` — interactive QR login.

Runs the iLink QR login state machine, prints the QR code to the terminal,
and persists the resulting bot token at ``state_dir/wechat-tokens.{account}.json``.

The daemon's ``WechatChannel`` reads that file at startup (preferred over the
config's ``ilink_token``), so a successful login plus daemon restart is all
that's needed to bring an account online from scratch.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

from nano_openclaw.config import load_config, resolve_state_dir
from nano_openclaw.logger import get_logger
from nano_openclaw.wechat.ilink import (
    LoginCallbacks,
    LoginResult,
    login_with_qr,
    print_qrcode,
)

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


def _token_file(state_dir: Path, account_id: str) -> Path:
    """Return ``state_dir/wechat-tokens.{account}.json`` (no suffix for default).

    Mirrors the suffix convention used by ``notify-queue`` and
    ``wechat-sessions`` so the storage layout stays uniform per-account.
    """
    suffix = "" if account_id == "default" else f".{account_id}"
    return state_dir / f"wechat-tokens{suffix}.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def discover_persisted_account_ids(state_dir: Path) -> list[str]:
    """Return account ids inferred from ``state_dir/wechat-tokens*.json`` files.

    Inverse of :func:`_token_file`'s naming convention:
        ``wechat-tokens.json``        → ``"default"``
        ``wechat-tokens.{id}.json``   → ``id``

    Used by the daemon at startup so that running ``nano-openclaw wechat login``
    is sufficient to bring an account online — the user doesn't have to also
    duplicate the account id under ``wechat.accounts`` in the config.
    """
    if not state_dir.exists():
        return []
    ids: list[str] = []
    for path in sorted(state_dir.glob("wechat-tokens*.json")):
        # Drop trailing .json by hand: ``Path.stem`` only strips one suffix,
        # but multi-dot filenames like ``wechat-tokens.dj.json`` need just the
        # final ``.json`` removed.
        name = path.name
        if not name.endswith(".json"):
            continue
        base = name[: -len(".json")]
        if base == "wechat-tokens":
            ids.append("default")
        elif base.startswith("wechat-tokens."):
            ids.append(base[len("wechat-tokens."):])
    return ids


def load_persisted_token(state_dir: Path, account_id: str) -> tuple[str, str]:
    """Read the persisted token for ``account_id``.

    Returns ``(token, base_url)`` — both empty when the file is absent or
    unreadable. ``base_url`` reflects whatever the iLink server returned at
    login time and may differ from the configured one (the server can route
    bots to a sharded instance).
    """
    path = _token_file(state_dir, account_id)
    if not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning(
            "wechat.login.persisted.read.error",
            f"failed to read {path}: {type(exc).__name__}: {exc}",
        )
        return "", ""
    return str(data.get("token") or ""), str(data.get("base_url") or "")


async def run_wechat_login(
    *,
    config_path: str | None = None,
    account_id: str = "default",
) -> int:
    """Run the QR login flow and persist the resulting token. Returns exit code."""
    cfg, _warnings = load_config(config_path)
    state_dir = resolve_state_dir()

    # Pick the configured base_url for this account if any; otherwise default.
    # The server may overwrite this in the LoginResult so we treat it only as
    # the bootstrap address for fetching the QR code.
    base_url = DEFAULT_BASE_URL
    accounts = list(cfg.wechat.accounts) if cfg.wechat else []
    matched = next((a for a in accounts if a.id == account_id), None)
    if matched and matched.ilink_base_url:
        base_url = matched.ilink_base_url

    print(f"使用 iLink 地址: {base_url}", flush=True)
    print(f"账号 ID:         {account_id}", flush=True)
    print(f"Token 写入位置:  {_token_file(state_dir, account_id)}", flush=True)
    print(file=sys.stderr)

    def _on_qrcode(content: str) -> None:
        print("请用微信扫描下面的二维码登录:", flush=True)
        print_qrcode(content)
        print(file=sys.stderr, flush=True)

    callbacks = LoginCallbacks(
        on_qrcode=_on_qrcode,
        on_scanned=lambda: print("✓ 已扫码,请在手机上确认...", flush=True),
        on_expired=lambda n, mx: print(f"二维码已过期,刷新中 ({n}/{mx})", flush=True),
    )

    async with httpx.AsyncClient() as client:
        result: LoginResult = await login_with_qr(client, base_url, callbacks)

    if not result.connected:
        print(f"✗ 登录失败: {result.message}", file=sys.stderr, flush=True)
        return 1

    payload = {
        "token": result.bot_token,
        "base_url": result.base_url or base_url,
        "bot_id": result.bot_id,
        "user_id": result.user_id,
        "login_at": int(time.time()),
    }
    token_path = _token_file(state_dir, account_id)
    _atomic_write_json(token_path, payload)

    print()
    print(f"✓ 登录成功!")
    print(f"  bot_id={result.bot_id}")
    print(f"  user_id={result.user_id}")
    print(f"  token 已写入 {token_path}")
    print()
    print("重启 daemon 让新 token 生效:")
    print("  nano-openclaw gateway stop && nano-openclaw gateway start")
    return 0
