"""Tests for Stage 2 prune helpers + real-token wiring in compact.py.

Covers:
  - _summarize_tool_result generates tool-aware 1-liners
  - _truncate_tool_use_input keeps JSON valid while shrinking long strings
  - _strip_image_blocks_from_tool_result swaps image blocks for placeholder
  - _prune_old_tool_results dedupes, summarizes, strips images, truncates
  - compact_if_needed: post-prune below threshold short-circuits the LLM
  - compact_if_needed: last_prompt_tokens overrides character estimate
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from nano_openclaw.compact import (
    _DUPLICATE_TOOL_RESULT_TEXT,
    _IMAGE_REMOVED_PLACEHOLDER,
    _prune_old_tool_results,
    _strip_image_blocks_from_tool_result,
    _summarize_tool_result,
    _tool_result_text,
    _truncate_tool_use_input,
    compact_if_needed,
)
from nano_openclaw.loop import Message


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[{"type": "text", "text": text}])


def _assistant_tool_use(tool_id: str, name: str, input_dict: dict) -> Message:
    return Message(
        role="assistant",
        content=[
            {"type": "tool_use", "id": tool_id, "name": name, "input": input_dict}
        ],
    )


def _user_tool_result(tool_id: str, text: str) -> Message:
    return Message(
        role="user",
        content=[
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": [{"type": "text", "text": text}],
            }
        ],
    )


# ---------------------------------------------------------------------------
# _tool_result_text
# ---------------------------------------------------------------------------


def test_tool_result_text_extracts_from_block_list():
    block = {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": [
            {"type": "text", "text": "first line"},
            {"type": "text", "text": "second line"},
        ],
    }
    assert _tool_result_text(block) == "first line\nsecond line"


def test_tool_result_text_handles_legacy_string_content():
    block = {"type": "tool_result", "tool_use_id": "t1", "content": "raw string"}
    assert _tool_result_text(block) == "raw string"


def test_tool_result_text_returns_empty_for_unknown_shape():
    assert _tool_result_text({"type": "tool_result", "content": None}) == ""
    assert _tool_result_text({}) == ""


# ---------------------------------------------------------------------------
# _summarize_tool_result
# ---------------------------------------------------------------------------


def test_summarize_bash_includes_command_and_exit_code():
    out = _summarize_tool_result(
        "bash",
        {"command": "pytest tests/"},
        '{"stdout":"...","exit_code":0}\nmore\nlines',
    )
    assert "[bash]" in out
    assert "pytest tests/" in out
    assert "exit 0" in out


def test_summarize_read_file_mentions_path_and_offset():
    out = _summarize_tool_result(
        "read_file", {"path": "/tmp/x.py", "offset": 10}, "x" * 500
    )
    assert "[read_file]" in out
    assert "/tmp/x.py" in out
    assert "from line 10" in out


def test_summarize_unknown_tool_falls_back_to_generic():
    out = _summarize_tool_result(
        "weird_custom_tool", {"foo": "bar", "baz": "qux"}, "result"
    )
    assert "[weird_custom_tool]" in out
    assert "foo=bar" in out


# ---------------------------------------------------------------------------
# _truncate_tool_use_input
# ---------------------------------------------------------------------------


def test_truncate_input_shrinks_long_string_leaves():
    long_str = "x" * 800
    inp = {"path": "/tmp/file.txt", "content": long_str, "mode": "write"}
    out = _truncate_tool_use_input(inp, head_chars=100)
    # Short fields untouched
    assert out["path"] == "/tmp/file.txt"
    assert out["mode"] == "write"
    # Long field truncated
    assert out["content"].endswith("...[truncated]")
    assert len(out["content"]) == 100 + len("...[truncated]")


def test_truncate_input_recurses_into_nested_lists_and_dicts():
    inp = {
        "items": [
            {"text": "a" * 600},
            {"text": "short"},
        ],
        "nested": {"deep": {"k": "y" * 600}},
    }
    out = _truncate_tool_use_input(inp, head_chars=50)
    assert out["items"][0]["text"].endswith("...[truncated]")
    assert out["items"][1]["text"] == "short"
    assert out["nested"]["deep"]["k"].endswith("...[truncated]")


def test_truncate_input_output_is_valid_json():
    long_str = '"contains" \\backslashes\n and newlines' * 50
    inp = {"a": long_str, "b": [1, 2, 3], "c": {"nested": long_str}}
    out = _truncate_tool_use_input(inp, head_chars=100)
    # Must round-trip through json
    serialized = json.dumps(out, ensure_ascii=False)
    reparsed = json.loads(serialized)
    assert reparsed == out


def test_truncate_input_preserves_non_string_leaves():
    inp = {"count": 42, "active": True, "ratio": 3.14, "tags": None}
    out = _truncate_tool_use_input(inp)
    assert out == inp


# ---------------------------------------------------------------------------
# _strip_image_blocks_from_tool_result
# ---------------------------------------------------------------------------


def test_strip_image_blocks_swaps_images_for_placeholder():
    content = [
        {"type": "text", "text": "before"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "text", "text": "after"},
    ]
    new_content, had_image = _strip_image_blocks_from_tool_result(content)
    assert had_image is True
    assert len(new_content) == 3
    assert new_content[1]["type"] == "text"
    assert new_content[1]["text"] == _IMAGE_REMOVED_PLACEHOLDER


def test_strip_image_blocks_no_op_when_no_images():
    content = [{"type": "text", "text": "no images"}]
    new_content, had_image = _strip_image_blocks_from_tool_result(content)
    assert had_image is False
    # Content survives unchanged (we don't care about list identity)
    assert all(b.get("type") != "image" for b in new_content)
    assert new_content == content


# ---------------------------------------------------------------------------
# _prune_old_tool_results
# ---------------------------------------------------------------------------


def test_prune_replaces_large_old_tool_result_with_summary():
    big_output = "line\n" * 100  # 500 chars
    history = [
        _text("user", "run pytest"),
        _assistant_tool_use("t1", "bash", {"command": "pytest"}),
        _user_tool_result("t1", big_output),
        _text("assistant", "done"),
        # 6 more tail messages so the tool_result is outside protect_tail_count=6
        *[_text("assistant", f"tail msg {i}") for i in range(6)],
    ]
    n_pruned = _prune_old_tool_results(history, protect_tail_count=6)
    assert n_pruned >= 1
    # The tool_result's content is now a 1-line bash summary
    pruned_msg = history[2]
    text = _tool_result_text(pruned_msg.content[0])
    assert text.startswith("[bash]")
    assert "pytest" in text


def test_prune_skips_tool_results_inside_protected_tail():
    big_output = "x" * 500
    history = [
        _text("user", "ask"),
        _assistant_tool_use("t1", "read_file", {"path": "/foo.txt"}),
        _user_tool_result("t1", big_output),
    ]
    # protect_tail_count=6 covers the entire 3-msg history → no pruning
    n_pruned = _prune_old_tool_results(history, protect_tail_count=6)
    assert n_pruned == 0
    # Tool result text is unchanged
    assert _tool_result_text(history[2].content[0]) == big_output


def test_prune_dedupes_identical_tool_results_across_history():
    duplicate_text = "DUP " * 100  # >200 chars
    history = [
        _text("user", "first read"),
        _assistant_tool_use("t1", "read_file", {"path": "/a"}),
        _user_tool_result("t1", duplicate_text),
        _text("assistant", "ok"),
        _text("user", "read again"),
        _assistant_tool_use("t2", "read_file", {"path": "/a"}),
        _user_tool_result("t2", duplicate_text),  # SAME content
        _text("assistant", "still ok"),
    ]
    _prune_old_tool_results(history, protect_tail_count=2)
    # Older duplicate (t1) becomes the dedupe back-reference; the newer one
    # (t2) keeps its full content.
    older_text = _tool_result_text(history[2].content[0])
    newer_text = _tool_result_text(history[6].content[0])
    assert older_text == _DUPLICATE_TOOL_RESULT_TEXT
    assert newer_text == duplicate_text


def test_prune_strips_image_blocks_from_old_tool_results():
    history = [
        _text("user", "screenshot"),
        _assistant_tool_use("t1", "bash", {"command": "screencap"}),
        Message(role="user", content=[{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "X" * 1000}},
                {"type": "text", "text": "screenshot saved"},
            ],
        }]),
        _text("assistant", "ok"),
        # Push the screenshot outside the protected tail
        *[_text("assistant", f"tail {i}") for i in range(6)],
    ]
    _prune_old_tool_results(history, protect_tail_count=6)
    tr = history[2].content[0]
    assert isinstance(tr["content"], list)
    has_image = any(b.get("type") == "image" for b in tr["content"])
    assert not has_image
    # Replaced by placeholder text block
    assert any(
        b.get("type") == "text" and b.get("text") == _IMAGE_REMOVED_PLACEHOLDER
        for b in tr["content"]
    )


def test_prune_truncates_large_tool_use_input_in_old_assistant_msg():
    big_blob = "z" * 2000
    history = [
        _text("user", "write file"),
        _assistant_tool_use("t1", "write_file", {
            "path": "/tmp/out.txt", "content": big_blob,
        }),
        _user_tool_result("t1", "ok"),
        # Push assistant outside the protected tail
        *[_text("assistant", f"tail {i}") for i in range(6)],
    ]
    _prune_old_tool_results(history, protect_tail_count=6)
    assistant_msg = history[1]
    tu = assistant_msg.content[0]
    assert tu["input"]["path"] == "/tmp/out.txt"
    # Content was shrunk
    assert len(tu["input"]["content"]) < len(big_blob)
    # And the dict still serialises as valid JSON
    json.loads(json.dumps(tu["input"], ensure_ascii=False))


# ---------------------------------------------------------------------------
# Integration with compact_if_needed
# ---------------------------------------------------------------------------


def test_compact_if_needed_skips_llm_when_prune_alone_is_enough():
    """After pre-prune drops history under threshold, compact_if_needed
    should return without calling the summarization LLM."""
    big_text = "x" * 800  # ~200 tokens (way above prune threshold)
    history = [
        _text("user", "do it"),
        _assistant_tool_use("t1", "bash", {"command": "echo hi"}),
        _user_tool_result("t1", big_text),
        _text("assistant", "ok"),
        # Tail (≤ keep_count=4 with recent_turns=2)
        _text("user", "next"),
        _text("assistant", "sure"),
        _text("user", "and again"),
        _text("assistant", "ack"),
    ]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()  # should NOT be called

    # budget=80 → threshold=64. Pre-prune big_text was ~200 tokens; replaced
    # by ~50 char [bash] summary → estimate drops well below 64 tokens.
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=80,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
    ))

    # Compaction did NOT need the LLM — prune alone was enough
    assert summary is None
    mock_client.messages.create.assert_not_called()
    # The big tool_result was replaced by a bash summary
    pruned_text = _tool_result_text(result[2].content[0])
    assert pruned_text.startswith("[bash]")


def test_compact_if_needed_last_prompt_tokens_triggers_prune_pass():
    """When last_prompt_tokens > threshold but the char estimate is BELOW
    threshold, compact_if_needed still enters the trigger branch and runs
    the pre-prune pass. Pre-prune dedupe is observable in the returned
    history; LLM is skipped because post-prune drops back under threshold.
    """
    duplicate_text = "DUP " * 60  # 240 chars → above prune threshold (200)
    history = [
        _text("user", "first"),
        _assistant_tool_use("t1", "read_file", {"path": "/a"}),
        _user_tool_result("t1", duplicate_text),
        _text("assistant", "ok"),
        _text("user", "second"),
        _assistant_tool_use("t2", "read_file", {"path": "/a"}),
        _user_tool_result("t2", duplicate_text),  # identical to t1 → dedupe target
        _text("assistant", "still ok"),
    ]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()  # must NOT be called

    # budget=300 → threshold=240. char estimate of this history is well
    # under 240, so without last_prompt_tokens compaction would not fire at
    # all. last_prompt_tokens=300 forces the trigger; post-prune estimate
    # is also under threshold, so we return without invoking the LLM.
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=300,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        last_prompt_tokens=300,
    ))

    # No LLM call happened — the prune pass alone was enough.
    assert summary is None
    mock_client.messages.create.assert_not_called()
    # Older duplicate (idx 2) replaced by dedupe back-reference; newer
    # (idx 6) preserved.
    older_text = _tool_result_text(result[2].content[0])
    newer_text = _tool_result_text(result[6].content[0])
    assert older_text == _DUPLICATE_TOOL_RESULT_TEXT
    assert newer_text == duplicate_text


def test_compact_if_needed_falls_back_to_estimate_when_last_prompt_tokens_zero():
    """last_prompt_tokens=0 (no prior turn) triggers the estimate fallback."""
    history = [_text("user", "hi"), _text("assistant", "hello")]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()

    # budget=10000, threshold=8000; this tiny history is well under either
    # estimate or "real" tokens — so no compaction either way.
    result, summary = asyncio.run(compact_if_needed(
        history,
        budget=10000,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,
        last_prompt_tokens=0,  # explicit "no real tokens yet"
    ))

    assert summary is None
    mock_client.messages.create.assert_not_called()
