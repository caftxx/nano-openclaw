"""``extract_message`` decodes DingTalk callback payloads.

Coverage:
- ``text`` 单聊/群聊 basic fields
- ``richText`` multi-segment concatenation
- ``isReplyMsg`` nested quote chains up to MAX_QUOTE_DEPTH
- ``atUsers`` staffId extraction
- empty/missing fields don't crash
"""

from __future__ import annotations

from nano_openclaw.dingtalk.extract import MAX_QUOTE_DEPTH, extract_message


def test_text_dm():
    data = {
        "msgtype": "text",
        "text": {"content": "  hello bot  "},
        "msgId": "m-1",
        "senderStaffId": "user-1",
        "senderNick": "Alice",
        "conversationId": "c-dm-1",
        "conversationType": "1",
        "isInAtList": False,
        "sessionWebhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
        "sessionWebhookExpiredTime": 9_999_999_999_000,
    }
    msg = extract_message(data)
    assert msg.text == "hello bot"
    assert msg.sender_staff_id == "user-1"
    assert msg.sender_nick == "Alice"
    assert msg.conversation_id == "c-dm-1"
    assert msg.is_group is False
    assert msg.at_self is False
    assert msg.msg_id == "m-1"
    assert msg.session_webhook.startswith("https://oapi.")
    assert msg.msgtype == "text"


def test_text_group_with_at_self():
    data = {
        "msgtype": "text",
        "text": {"content": "@bot do thing"},
        "conversationType": "2",
        "conversationId": "c-group-1",
        "isInAtList": True,
        "atUsers": [
            {"dingtalkId": "ding-x", "staffId": "bot-staff"},
            {"dingtalkId": "ding-y", "staffId": "user-2"},
        ],
    }
    msg = extract_message(data)
    assert msg.is_group is True
    assert msg.at_self is True
    assert msg.at_user_staff_ids == ["bot-staff", "user-2"]


def test_rich_text_concatenates_text_segments_only():
    data = {
        "msgtype": "richText",
        "content": {
            "richText": [
                {"text": "line one"},
                {"downloadCode": "media-here"},  # image, ignored in PR2
                {"text": "line two"},
                {"text": ""},  # blank, filtered
            ],
        },
        "conversationType": "1",
        "conversationId": "c-1",
    }
    msg = extract_message(data)
    assert msg.text == "line one\nline two"
    assert msg.msgtype == "richText"


def test_reply_msg_prepends_quote():
    data = {
        "msgtype": "text",
        "text": {"content": "my reply"},
        "conversationType": "1",
        "conversationId": "c-1",
        "isReplyMsg": True,
        "repliedMsg": {
            "msgtype": "text",
            "text": {"content": "original question"},
            "senderNick": "Bob",
        },
    }
    msg = extract_message(data)
    assert msg.text == "> @Bob: original question\n\nmy reply"


def test_nested_reply_capped_at_max_depth():
    # Build a chain deeper than MAX_QUOTE_DEPTH to check the cap kicks in.
    def make_chain(depth: int) -> dict:
        msg = {
            "msgtype": "text",
            "text": {"content": f"msg-{depth}"},
            "senderNick": f"u{depth}",
        }
        if depth > 0:
            msg["isReplyMsg"] = True
            msg["repliedMsg"] = make_chain(depth - 1)
        return msg

    chain = make_chain(MAX_QUOTE_DEPTH + 2)
    chain["conversationType"] = "1"
    chain["conversationId"] = "c-1"
    msg = extract_message(chain)
    # Top-level text + (MAX_QUOTE_DEPTH) quoted levels. The deepest msg
    # (msg-0) should be absent because we hit the cap before reaching it.
    assert "msg-0" not in msg.text
    # Top-level present.
    assert f"msg-{MAX_QUOTE_DEPTH + 2}" in msg.text


def test_missing_fields_yield_safe_defaults():
    msg = extract_message({})
    assert msg.text == ""
    assert msg.sender_staff_id == ""
    assert msg.conversation_id == ""
    assert msg.is_group is False
    assert msg.at_self is False
    assert msg.msg_id == ""
    assert msg.session_webhook == ""
    assert msg.session_webhook_expire_ms == 0


def test_at_users_with_malformed_entries_skipped():
    data = {
        "msgtype": "text",
        "text": {"content": "x"},
        "conversationType": "1",
        "conversationId": "c-1",
        "atUsers": [
            {"dingtalkId": "abc"},  # no staffId — skipped
            "not-a-dict",
            {"staffId": "real-user"},
        ],
    }
    msg = extract_message(data)
    assert msg.at_user_staff_ids == ["real-user"]
