"""``should_respond`` truth table: dm + group × open/allowlist × @-mention."""

from __future__ import annotations

import pytest

from nano_openclaw.dingtalk.extract import ExtractedMessage
from nano_openclaw.dingtalk.policy import DingtalkPolicy, should_respond


def _make(*, group: bool = False, at_self: bool = False, sender: str = "alice", conv: str = "c-1") -> ExtractedMessage:
    return ExtractedMessage(
        text="hi",
        sender_staff_id=sender,
        sender_nick="A",
        conversation_id=conv,
        is_group=group,
        at_self=at_self,
        msg_id="m-1",
        session_webhook="https://w/",
        session_webhook_expire_ms=0,
        msgtype="text",
    )


# ── DM ─────────────────────────────────────────────────────────────────────


def test_dm_open_lets_everyone_through():
    assert should_respond(_make(), DingtalkPolicy(dm_policy="open"))


def test_dm_allowlist_admits_listed_sender():
    policy = DingtalkPolicy(dm_policy="allowlist", allow_from=["alice"])
    assert should_respond(_make(sender="alice"), policy)
    assert not should_respond(_make(sender="bob"), policy)


def test_dm_unknown_policy_denies():
    policy = DingtalkPolicy(dm_policy="pairing")
    assert not should_respond(_make(), policy)


# ── Group ──────────────────────────────────────────────────────────────────


def test_group_disabled_blocks_all():
    policy = DingtalkPolicy(group_policy="disabled", require_mention=False)
    assert not should_respond(_make(group=True, at_self=True), policy)


def test_group_open_requires_mention_when_required():
    policy = DingtalkPolicy(group_policy="open", require_mention=True)
    assert should_respond(_make(group=True, at_self=True), policy)
    assert not should_respond(_make(group=True, at_self=False), policy)


def test_group_open_without_mention_requirement_lets_any_message_through():
    policy = DingtalkPolicy(group_policy="open", require_mention=False)
    assert should_respond(_make(group=True, at_self=False), policy)


def test_group_allowlist_consults_groups_dict():
    policy = DingtalkPolicy(
        group_policy="allowlist",
        require_mention=False,
        groups={"c-allowed": {}},
    )
    assert should_respond(_make(group=True, conv="c-allowed"), policy)
    assert not should_respond(_make(group=True, conv="c-other"), policy)


def test_group_allowlist_still_honors_require_mention():
    policy = DingtalkPolicy(
        group_policy="allowlist",
        require_mention=True,
        groups={"c-allowed": {}},
    )
    assert not should_respond(_make(group=True, conv="c-allowed", at_self=False), policy)
    assert should_respond(_make(group=True, conv="c-allowed", at_self=True), policy)


def test_policy_from_creds_round_trip():
    p = DingtalkPolicy.from_creds({
        "dmPolicy": "allowlist",
        "groupPolicy": "open",
        "requireMention": False,
        "allowFrom": ["a", "b"],
        "groups": {"g1": {}, "g2": {"foo": 1}},
    })
    assert p.dm_policy == "allowlist"
    assert p.group_policy == "open"
    assert p.require_mention is False
    assert p.allow_from == ["a", "b"]
    assert set(p.groups.keys()) == {"g1", "g2"}


def test_policy_from_creds_defaults_safely():
    p = DingtalkPolicy.from_creds({})
    assert p.dm_policy == "open"
    assert p.group_policy == "open"
    assert p.require_mention is True
    assert p.allow_from == []
    assert p.groups == {}
