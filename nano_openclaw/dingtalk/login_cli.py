"""``nano-openclaw dingtalk register`` — persist DingTalk AppKey credentials.

Unlike WeChat, DingTalk authenticates with a long-lived AppKey/AppSecret pair
issued by the DingTalk Open Platform, so there is no QR flow. Registration
is just persisting the credentials at
``state_dir/dingtalk-creds.{clientId}.json`` with mode 0600. The gateway
daemon scans these files at startup and spawns one ``DingtalkChannel`` per
file.

Storing per ``clientId`` (not a free-form account label) matches the TS
connector's token-cache bucketing — it's a natural identity that lets us
tell two co-resident accounts apart in logs without an extra alias.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from nano_openclaw.config import resolve_state_dir
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


CREDS_FILENAME_PREFIX = "dingtalk-creds."
CREDS_FILENAME_SUFFIX = ".json"


def _creds_file(state_dir: Path, client_id: str) -> Path:
    return state_dir / f"{CREDS_FILENAME_PREFIX}{client_id}{CREDS_FILENAME_SUFFIX}"


def _atomic_write_creds(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically with 0600 permissions.

    ``os.replace`` is atomic on the same filesystem so a concurrent reader
    never sees a partial write. We chmod the temp file before rename so the
    file is never momentarily world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def discover_persisted_account_ids_dingtalk(state_dir: Path) -> list[str]:
    """Return clientIds inferred from ``state_dir/dingtalk-creds.{id}.json`` files.

    Used by the daemon at startup — one creds file = one channel instance.
    Sorted for stable startup ordering in logs and tests.
    """
    if not state_dir.exists():
        return []
    ids: list[str] = []
    for path in sorted(state_dir.glob(f"{CREDS_FILENAME_PREFIX}*{CREDS_FILENAME_SUFFIX}")):
        name = path.name
        if not name.startswith(CREDS_FILENAME_PREFIX) or not name.endswith(CREDS_FILENAME_SUFFIX):
            continue
        client_id = name[len(CREDS_FILENAME_PREFIX) : -len(CREDS_FILENAME_SUFFIX)]
        if client_id:
            ids.append(client_id)
    return ids


def load_persisted_creds(state_dir: Path, client_id: str) -> dict[str, Any]:
    """Read the creds blob for ``client_id``. Returns ``{}`` on missing/bad files.

    The daemon calls this and a malformed file shouldn't kill the whole boot
    — just leave that one account in an error state.
    """
    path = _creds_file(state_dir, client_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning(
            "dingtalk.creds.read.error",
            f"failed to read {path}: {type(exc).__name__}: {exc}",
        )
        return {}


def run_dingtalk_register(
    *,
    client_id: str,
    client_secret: str,
    dm_policy: str = "open",
    group_policy: str = "open",
    require_mention: bool = True,
) -> int:
    """Persist credentials for ``client_id`` and return a CLI exit code.

    ``dm_policy`` / ``group_policy`` / ``require_mention`` are inlined into
    the creds file so they're available to ``DingtalkChannel.start()``
    without a separate config round-trip. Policy semantics:

    - ``dm_policy``: ``open`` (anyone), ``allowlist`` (per-user opt-in).
    - ``group_policy``: ``open`` (any group can use the bot, gated by
      ``require_mention``), ``allowlist`` (only configured groups),
      ``disabled``.
    - ``require_mention``: in group chats, only respond to messages that
      ``@`` the bot. Strongly recommended.
    """
    if not client_id or not client_secret:
        print("✗ --client-id and --client-secret are required")
        return 2
    if dm_policy not in ("open", "allowlist"):
        print(f"✗ unsupported dm-policy {dm_policy!r}; use 'open' or 'allowlist'")
        return 2
    if group_policy not in ("open", "allowlist", "disabled"):
        print(f"✗ unsupported group-policy {group_policy!r}")
        return 2

    state_dir = resolve_state_dir()
    payload: dict[str, Any] = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "dmPolicy": dm_policy,
        "groupPolicy": group_policy,
        "requireMention": require_mention,
        "groups": {},
        "allowFrom": [],
        "createdAtMs": int(time.time() * 1000),
    }
    path = _creds_file(state_dir, client_id)
    _atomic_write_creds(path, payload)

    print(f"✓ DingTalk credentials saved to {path}")
    print(f"  client_id     = {client_id}")
    print(f"  dm_policy     = {dm_policy}")
    print(f"  group_policy  = {group_policy}")
    print(f"  require_mention = {require_mention}")
    print()
    print("Restart the gateway daemon to pick up the new account:")
    print("  nano-openclaw gateway stop && nano-openclaw gateway start")
    return 0
