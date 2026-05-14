from __future__ import annotations

from nano_openclaw.wechat.ilink import extract_text


def test_extract_text_includes_ref_msg_text():
    items = [
        {
            "type": 1,
            "ref_msg": {
                "message_item": {
                    "type": 1,
                    "text_item": {
                        "text": "小番茄睡着了 🤍\n\n```\n状态: asleep ✅\n```",
                    },
                },
            },
            "text_item": {"text": "啥意思"},
        },
    ]

    assert extract_text(items) == (
        "啥意思\n"
        "[引用消息]\n"
        "小番茄睡着了 🤍\n\n"
        "```\n状态: asleep ✅\n```\n"
        "[/引用消息]"
    )


def test_extract_text_keeps_current_text_first_for_slash_commands():
    items = [
        {
            "type": 1,
            "ref_msg": {
                "message_item": {
                    "type": 1,
                    "text_item": {"text": "previous"},
                },
            },
            "text_item": {"text": "/help"},
        },
    ]

    assert extract_text(items).startswith("/help")


def test_extract_text_includes_ref_msg_title():
    items = [
        {
            "type": 1,
            "ref_msg": {
                "title": "引用",
                "message_item": {
                    "type": 1,
                    "text_item": {"text": "previous"},
                },
            },
            "text_item": {"text": "current"},
        },
    ]

    assert extract_text(items) == (
        "current\n"
        "[引用消息]\n"
        "标题: 引用\n"
        "previous\n"
        "[/引用消息]"
    )


def test_extract_text_describes_non_text_ref_msg_items():
    items = [
        {
            "type": 1,
            "ref_msg": {
                "message_item": {
                    "type": 4,
                    "file_item": {"file_name": "report.pdf"},
                },
            },
            "text_item": {"text": "这个看下"},
        },
    ]

    assert extract_text(items) == (
        "这个看下\n"
        "[引用消息]\n"
        "[用户发送了文件：report.pdf]\n"
        "[/引用消息]"
    )
