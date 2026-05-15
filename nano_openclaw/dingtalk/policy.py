"""Engagement policy: should this DingTalk message trigger the agent?

The TS connector's full feature set includes ``pairing`` DM mode and
per-group overrides; PR2 ships the common subset:

- DM (1:1): ``open`` (anyone) or ``allowlist`` (sender's staffId must be in
  ``allow_from``). ``pairing`` is rejected here as unsupported so callers
  fail loudly rather than silently treating it as ``open``.
- Group chat: ``open`` / ``allowlist`` / ``disabled``. ``require_mention``
  gates all group responses on whether the bot was @-ed.

Order: the explicit policy decision wins; nothing here knows about admins
or business hours. Keeping it narrow makes the truth table testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nano_openclaw.dingtalk.extract import ExtractedMessage


@dataclass
class DingtalkPolicy:
    """Per-account engagement rules loaded from the creds file."""

    dm_policy: str = "open"
    group_policy: str = "open"
    require_mention: bool = True
    allow_from: list[str] = field(default_factory=list)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_creds(cls, creds: dict[str, Any]) -> "DingtalkPolicy":
        return cls(
            dm_policy=str(creds.get("dmPolicy") or "open"),
            group_policy=str(creds.get("groupPolicy") or "open"),
            require_mention=bool(creds.get("requireMention", True)),
            allow_from=list(creds.get("allowFrom") or []),
            groups=dict(creds.get("groups") or {}),
        )


def should_respond(msg: ExtractedMessage, policy: DingtalkPolicy) -> bool:
    """Return True when the bot should engage with ``msg``.

    Empty-text inbound messages don't trigger by themselves — sticker /
    media-only payloads fall through here and get answered by PR4's media
    handler once attachments land.
    """
    if msg.is_group:
        if policy.group_policy == "disabled":
            return False
        if policy.require_mention and not msg.at_self:
            return False
        if policy.group_policy == "allowlist":
            if msg.conversation_id not in policy.groups:
                return False
        elif policy.group_policy != "open":
            return False
        return True

    # DM path.
    if policy.dm_policy == "open":
        return True
    if policy.dm_policy == "allowlist":
        return msg.sender_staff_id in policy.allow_from
    # Unknown / pairing → treat as deny so we don't engage unexpectedly.
    return False
