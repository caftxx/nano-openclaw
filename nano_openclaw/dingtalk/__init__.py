"""DingTalk Stream protocol + business helpers.

Layered, lowest first:

- :mod:`nano_openclaw.dingtalk.frames`        — Pydantic frame models
  (``EventFrame`` / ``CallbackFrame`` / ``SystemFrame`` / ``AckFrame``).
- :mod:`nano_openclaw.dingtalk.token`         — :class:`DingtalkTokenManager`,
  per-``clientId`` cached ``access_token`` with TTL-aware refresh.
- :mod:`nano_openclaw.dingtalk.stream_client` — :class:`DingtalkStreamClient`:
  ``open_connection`` → WebSocket → frame dispatch → ACK → backoff reconnect.
- :mod:`nano_openclaw.dingtalk.login_cli`     — ``register`` CLI / persistence
  / per-``clientId`` account discovery.

Higher-level pieces (message extraction, policy, sender, AI Card, bot loop)
live in PR2+ and are not exported here yet.
"""

from nano_openclaw.dingtalk.frames import (
    AckFrame,
    CallbackFrame,
    EventFrame,
    FrameHeaders,
    SystemFrame,
)
from nano_openclaw.dingtalk.login_cli import (
    discover_persisted_account_ids_dingtalk,
    load_persisted_creds,
    run_dingtalk_register,
)
from nano_openclaw.dingtalk.stream_client import DingtalkStreamClient
from nano_openclaw.dingtalk.token import DingtalkTokenManager

__all__ = [
    "AckFrame",
    "CallbackFrame",
    "DingtalkStreamClient",
    "DingtalkTokenManager",
    "EventFrame",
    "FrameHeaders",
    "SystemFrame",
    "discover_persisted_account_ids_dingtalk",
    "load_persisted_creds",
    "run_dingtalk_register",
]
