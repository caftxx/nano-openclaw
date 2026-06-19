"""Tests for the microcompact tool whitelist in ``_prune_old_tool_results``.

The compaction pre-pass must NOT summarize / truncate tool_results or tool_use
inputs for tools whose payload carries load-bearing state (e.g. ``todo``,
``apply_patch``, ``skill``). Only tools in ``_COMPACTABLE_TOOL_NAMES`` get
microcompacted; everything else (including orphan tool_results whose tool_use
is missing) is preserved verbatim.

Pass 1 (md5 dedupe of identical tool_result text) is whitelist-agnostic:
identical-output dedupe is safe regardless of tool semantics.
"""

from __future__ import annotations

import json

from nano_openclaw.core.compact import (
    _COMPACTABLE_TOOL_NAMES,
    _DUPLICATE_TOOL_RESULT_TEXT,
    _PRUNE_INPUT_THRESHOLD_CHARS,
    _PRUNE_RESULT_THRESHOLD_CHARS,
    _prune_old_tool_results,
    _tool_result_text,
)
from nano_openclaw.core.loop import Message


# ---------------------------------------------------------------------------
# Test helpers (mirror tests/test_compact_prune.py shape)
# ---------------------------------------------------------------------------


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


def _tail(n: int) -> list[Message]:
    """Filler messages to push earlier turns out of the protected tail."""
    return [_text("assistant", f"tail {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# T1: bash compacted, todo preserved
# ---------------------------------------------------------------------------


def test_whitelisted_bash_compacted_but_todo_preserved():
    big_bash = "bash output line\n" * 50            # ~850 chars
    big_todo = "task list state " * 50              # ~800 chars
    history = [
        _text("user", "do stuff"),
        _assistant_tool_use("t-bash", "bash", {"command": "ls"}),
        _user_tool_result("t-bash", big_bash),
        _assistant_tool_use("t-todo", "todo", {"todos": []}),
        _user_tool_result("t-todo", big_todo),
        _text("assistant", "ok"),
        # Push the tool I/O outside the protected tail.
        *_tail(6),
    ]

    _prune_old_tool_results(history, protect_tail_count=6)

    # bash result was summarized to a 1-line ``[bash]`` placeholder.
    bash_result_text = _tool_result_text(history[2].content[0])
    assert bash_result_text.startswith("[bash]")
    assert "ls" in bash_result_text

    # todo result is preserved verbatim — it carries the live task list.
    todo_result_text = _tool_result_text(history[4].content[0])
    assert todo_result_text == big_todo


# ---------------------------------------------------------------------------
# T2: pass 2 + pass 3 both fire for whitelisted tool
# ---------------------------------------------------------------------------


def test_whitelisted_tool_input_and_result_both_compacted():
    big_blob = "z" * (_PRUNE_INPUT_THRESHOLD_CHARS * 2)
    big_output = "line\n" * 100  # ~500 chars, above result threshold
    history = [
        _text("user", "write a file"),
        _assistant_tool_use("t1", "write_file", {
            "path": "/tmp/out.txt",
            "content": big_blob,
        }),
        _user_tool_result("t1", big_output),
        *_tail(6),
    ]

    _prune_old_tool_results(history, protect_tail_count=6)

    # Pass 3: tool_use input shrunk while keeping JSON shape.
    tu = history[1].content[0]
    assert tu["input"]["path"] == "/tmp/out.txt"
    assert len(tu["input"]["content"]) < len(big_blob)
    # And the dict still round-trips through json.
    json.loads(json.dumps(tu["input"], ensure_ascii=False))

    # Pass 2: tool_result body summarized.
    summary = _tool_result_text(history[2].content[0])
    assert summary.startswith("[write_file]")
    assert "/tmp/out.txt" in summary


# ---------------------------------------------------------------------------
# T3: orphan tool_result (no matching tool_use) is preserved
# ---------------------------------------------------------------------------


def test_orphan_tool_result_preserved_when_tool_use_missing():
    big_output = "Q" * 500
    # Note: no assistant tool_use with id "orphan-id". The tool_result has
    # nothing to look up in call_id_to_tool — must stay untouched.
    history = [
        _text("user", "..."),
        _user_tool_result("orphan-id", big_output),
        *_tail(6),
    ]

    _prune_old_tool_results(history, protect_tail_count=6)

    assert _tool_result_text(history[1].content[0]) == big_output


def test_non_whitelisted_tool_use_input_not_truncated():
    """Pass 3 must skip tool_use blocks whose name is outside the whitelist —
    truncating a ``todo``/``apply_patch`` input would corrupt the call."""
    big_blob = "Z" * (_PRUNE_INPUT_THRESHOLD_CHARS * 2)
    todo_input = {
        "todos": [
            {"id": "1", "content": big_blob, "status": "pending"},
        ],
        "merge": False,
    }
    history = [
        _text("user", "plan it"),
        _assistant_tool_use("t1", "todo", todo_input),
        _user_tool_result("t1", "ok"),
        *_tail(6),
    ]

    _prune_old_tool_results(history, protect_tail_count=6)

    tu = history[1].content[0]
    # input dict preserved verbatim, including the big content field.
    assert tu["input"]["todos"][0]["content"] == big_blob


# ---------------------------------------------------------------------------
# T4: dedupe (pass 1) is whitelist-agnostic
# ---------------------------------------------------------------------------


def test_pass1_dedupe_applies_to_non_whitelisted_tools():
    """Two identical large ``todo`` outputs should still be deduped — the
    older one becomes a back-reference. Pass 1 doesn't care about the tool
    name; identical text is safe to collapse regardless of semantics."""
    duplicate_text = "TODO_STATE " * 30   # 330 chars, above prune threshold
    assert len(duplicate_text) > _PRUNE_RESULT_THRESHOLD_CHARS
    history = [
        _text("user", "plan"),
        _assistant_tool_use("t1", "todo", {"todos": []}),
        _user_tool_result("t1", duplicate_text),
        _text("assistant", "ok"),
        _text("user", "re-plan"),
        _assistant_tool_use("t2", "todo", {"todos": []}),
        _user_tool_result("t2", duplicate_text),  # identical to t1
        _text("assistant", "still ok"),
    ]

    _prune_old_tool_results(history, protect_tail_count=2)

    # Older duplicate (t1, idx 2) collapsed to back-reference.
    assert _tool_result_text(history[2].content[0]) == _DUPLICATE_TOOL_RESULT_TEXT
    # Newer (t2, idx 6) preserved.
    assert _tool_result_text(history[6].content[0]) == duplicate_text


# ---------------------------------------------------------------------------
# Sanity: the whitelist itself
# ---------------------------------------------------------------------------


def test_whitelist_contains_expected_io_tools_and_excludes_state_tools():
    # Read/write/search style tools — safe to summarize.
    for name in (
        "read_file", "write_file", "list_dir", "bash",
        "web_fetch", "web_search",
        "memory_get", "memory_search",
    ):
        assert name in _COMPACTABLE_TOOL_NAMES, name

    # State-bearing or side-effectful tools — must stay out.
    for name in (
        "todo", "apply_patch", "skill", "skill_install",
        "sessions_spawn", "subagents",
        "current_time", "session_status",
    ):
        assert name not in _COMPACTABLE_TOOL_NAMES, name
