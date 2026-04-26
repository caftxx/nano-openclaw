"""Tests for provider routing and OpenAI message/tool format conversion.

No LLM calls required — only the pure translation helpers and the routing
layer are exercised here.
"""

from __future__ import annotations

import json

import pytest

from nano_openclaw._provider_openai import _to_openai_messages, _to_openai_tools
from nano_openclaw.loop import LoopConfig
from nano_openclaw.provider import stream_response


# ---------------------------------------------------------------------------
# _to_openai_messages
# ---------------------------------------------------------------------------


def test_user_text_message():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    result = _to_openai_messages(msgs)
    assert result == [{"role": "user", "content": "hello"}]


def test_assistant_text_only():
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}]
    result = _to_openai_messages(msgs)
    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "hi there"
    assert "tool_calls" not in result[0]


def test_assistant_with_tool_use():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "read_file",
                    "input": {"path": "foo.py"},
                },
            ],
        }
    ]
    result = _to_openai_messages(msgs)
    assert len(result) == 1
    msg = result[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "let me check"
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "tu_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "foo.py"}


def test_assistant_tool_use_no_text():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_2", "name": "bash", "input": {"command": "ls"}}
            ],
        }
    ]
    result = _to_openai_messages(msgs)
    assert result[0]["content"] is None  # empty text → None
    assert len(result[0]["tool_calls"]) == 1


def test_user_tool_results_become_separate_tool_messages():
    """One Anthropic user message with N tool_results → N OpenAI 'tool' messages."""
    msgs = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": [{"type": "text", "text": "file content"}],
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_2",
                    "content": [{"type": "text", "text": "exit=0\n"}],
                },
            ],
        }
    ]
    result = _to_openai_messages(msgs)
    assert len(result) == 2
    assert result[0] == {"role": "tool", "tool_call_id": "tu_1", "content": "file content"}
    assert result[1] == {"role": "tool", "tool_call_id": "tu_2", "content": "exit=0\n"}


def test_user_text_and_tool_results_in_same_message():
    """User text block + tool_result blocks in one Anthropic message → text msg + tool msgs."""
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "also note:"},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_3",
                    "content": [{"type": "text", "text": "ok"}],
                },
            ],
        }
    ]
    result = _to_openai_messages(msgs)
    assert any(m["role"] == "user" for m in result)
    assert any(m["role"] == "tool" for m in result)


def test_multi_turn_conversation():
    """Full user → assistant → tool_result round-trip preserves order."""
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "list_dir", "input": {"path": "."}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": [{"type": "text", "text": "a.py\nb.py"}],
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "There are 2 files."}]},
    ]
    result = _to_openai_messages(msgs)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "tool", "assistant"]


# ---------------------------------------------------------------------------
# _to_openai_tools
# ---------------------------------------------------------------------------


def test_tool_schema_conversion():
    anthropic_tools = [
        {
            "name": "read_file",
            "description": "Read a file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    result = _to_openai_tools(anthropic_tools)
    assert len(result) == 1
    t = result[0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "read_file"
    assert t["function"]["description"] == "Read a file."
    assert t["function"]["parameters"] == anthropic_tools[0]["input_schema"]


def test_multiple_tools_conversion():
    from nano_openclaw.tools import build_default_registry

    schemas = build_default_registry().schemas()  # Anthropic format
    openai_tools = _to_openai_tools(schemas)
    assert len(openai_tools) == len(schemas)
    for t in openai_tools:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "parameters" in t["function"]


# ---------------------------------------------------------------------------
# provider routing
# ---------------------------------------------------------------------------


def test_unsupported_api_raises():
    with pytest.raises(ValueError, match="unsupported api"):
        # Pass a dummy client — the ValueError should fire before any I/O.
        list(stream_response(api="bogus", client=None, model="x", system="s",
                             messages=[], tools=[]))


# ---------------------------------------------------------------------------
# LoopConfig defaults
# ---------------------------------------------------------------------------


def test_loop_config_default_api():
    cfg = LoopConfig()
    assert cfg.api == "anthropic"


def test_loop_config_openai_api():
    cfg = LoopConfig(model="gpt-4o", api="openai")
    assert cfg.api == "openai"
    assert cfg.model == "gpt-4o"
