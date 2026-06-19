"""Tests for the TodoStore and the `todo` tool wired through ToolRegistry."""

from __future__ import annotations

import asyncio
import json

import pytest

from nano_openclaw.todo import TodoStore, VALID_STATUSES
from nano_openclaw.core.tools import ToolExecutionContext, ToolRegistry, build_core_registry


def _dispatch_sync(registry: ToolRegistry, *args, **kwargs) -> dict:
    """Run the (async) dispatch on a fresh event loop.

    We don't monkey-patch ``ToolRegistry.dispatch`` here because
    ``tests/test_tools.py`` already does that for itself, and stacking two
    sync shims on the same method causes the inner one to receive a
    coroutine it never awaits. Pytest collects both files into one process,
    so we route through a helper that works regardless of whether dispatch
    has been patched or not.
    """
    result = ToolRegistry.dispatch(registry, *args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _todo_context(store: TodoStore) -> ToolExecutionContext:
    return ToolExecutionContext(todo_store=store)


# ──────────────────────────────────────────────────────────────────────────
# TodoStore unit tests
# ──────────────────────────────────────────────────────────────────────────


def test_write_replace_mode():
    """Non-merge mode 整体替换为新列表。"""
    store = TodoStore()
    store.write([{"id": "1", "content": "first", "status": "pending"}])
    result = store.write(
        [
            {"id": "2", "content": "second", "status": "in_progress"},
            {"id": "3", "content": "third", "status": "pending"},
        ]
    )
    assert len(result) == 2
    assert [item["id"] for item in result] == ["2", "3"]
    # Old item is gone.
    assert all(item["id"] != "1" for item in store.read())


def test_write_merge_mode():
    """Merge 模式按 id update + append；老项目保留。"""
    store = TodoStore()
    store.write(
        [
            {"id": "1", "content": "first", "status": "pending"},
            {"id": "2", "content": "second", "status": "pending"},
        ]
    )
    store.write(
        [
            # Update id=1 status only — content should be preserved.
            {"id": "1", "status": "completed"},
            # New item appended.
            {"id": "3", "content": "third", "status": "in_progress"},
        ],
        merge=True,
    )
    items = store.read()
    assert len(items) == 3
    by_id = {item["id"]: item for item in items}
    assert by_id["1"]["status"] == "completed"
    assert by_id["1"]["content"] == "first"  # 未被覆盖
    assert by_id["2"]["status"] == "pending"
    assert by_id["3"]["status"] == "in_progress"


def test_read_empty():
    """空 store read 返回空列表。"""
    store = TodoStore()
    assert store.read() == []
    assert store.has_items() is False


def test_invalid_status_defaults_to_pending():
    """非法 status 字符串降级到 pending（_validate 保护 schema）。"""
    store = TodoStore()
    items = store.write(
        [{"id": "1", "content": "hi", "status": "definitely-not-valid"}]
    )
    assert items[0]["status"] == "pending"

    # Also: missing content/id get sane defaults
    items = store.write([{"id": "", "content": "", "status": "pending"}])
    assert items[0]["id"] == "?"
    assert items[0]["content"] == "(no description)"


def test_dedupe_by_id():
    """同一次 write 中重复 id 只保留最后一个；位置取最后一次的位置。"""
    store = TodoStore()
    result = store.write(
        [
            {"id": "1", "content": "first ver", "status": "pending"},
            {"id": "2", "content": "other", "status": "pending"},
            {"id": "1", "content": "latest ver", "status": "in_progress"},
        ]
    )
    # Result should have 2 items; id=1 keeps the latest content/status
    # AND appears at the position of its last occurrence (index 1 after dedup
    # which means after id=2). Mirrors hermes behavior.
    assert result == [
        {"id": "2", "content": "other", "status": "pending"},
        {"id": "1", "content": "latest ver", "status": "in_progress"},
    ]


def test_format_for_injection_filters_completed():
    """format_for_injection 过滤 completed/cancelled。"""
    store = TodoStore()
    store.write(
        [
            {"id": "1", "content": "Done task", "status": "completed"},
            {"id": "2", "content": "Cancelled task", "status": "cancelled"},
            {"id": "3", "content": "Active task", "status": "in_progress"},
            {"id": "4", "content": "Queued task", "status": "pending"},
        ]
    )
    text = store.format_for_injection()
    assert text is not None
    # Completed/cancelled don't appear:
    assert "Done task" not in text
    assert "Cancelled task" not in text
    assert "[x]" not in text
    assert "[~]" not in text
    # Active ones appear with their markers:
    assert "Active task" in text
    assert "Queued task" in text
    assert "[>]" in text
    assert "[ ]" in text
    # Preamble line is present so the model knows what this is.
    assert "context compression" in text.lower()


def test_format_for_injection_empty_returns_none():
    """空列表 / 全 completed → None（无需注入）。"""
    assert TodoStore().format_for_injection() is None

    store = TodoStore()
    store.write(
        [
            {"id": "1", "content": "a", "status": "completed"},
            {"id": "2", "content": "b", "status": "cancelled"},
        ]
    )
    assert store.format_for_injection() is None


def test_round_trip_json():
    """to_json → from_json 等价。"""
    store = TodoStore()
    store.write(
        [
            {"id": "1", "content": "Plan", "status": "in_progress"},
            {"id": "2", "content": "Execute", "status": "pending"},
        ]
    )
    snapshot = store.to_json()
    restored = TodoStore.from_json(snapshot)
    assert restored.read() == store.read()

    # Tolerance for invalid persisted shape
    assert TodoStore.from_json(None).read() == []
    assert TodoStore.from_json("not-a-list").read() == []


# ──────────────────────────────────────────────────────────────────────────
# ToolRegistry dispatch integration
# ──────────────────────────────────────────────────────────────────────────


def _todo_payload(result: dict) -> dict:
    """Pull the JSON payload out of a tool_result block."""
    return json.loads(result["content"][0]["text"])


def test_tool_dispatch_read():
    """dispatch 不传 todos → read 当前列表。"""
    registry = build_core_registry()
    store = TodoStore()
    store.write([{"id": "1", "content": "Plan", "status": "pending"}])

    out = _dispatch_sync(registry, "id-r", "todo", {}, context=_todo_context(store))
    assert out.get("is_error") is None
    payload = _todo_payload(out)
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["pending"] == 1
    assert payload["todos"][0]["content"] == "Plan"


def test_tool_dispatch_write():
    """dispatch 传 todos → 写入并返回完整列表 + summary 计数。"""
    registry = build_core_registry()
    store = TodoStore()

    out = _dispatch_sync(
        registry,
        "id-w",
        "todo",
        {
            "todos": [
                {"id": "a", "content": "First", "status": "in_progress"},
                {"id": "b", "content": "Second", "status": "pending"},
            ]
        },
        context=_todo_context(store),
    )
    assert out.get("is_error") is None
    payload = _todo_payload(out)
    assert payload["summary"]["in_progress"] == 1
    assert payload["summary"]["pending"] == 1
    assert len(payload["todos"]) == 2
    # And the bound store actually reflects the write:
    assert len(store.read()) == 2


def test_tool_dispatch_no_store_returns_error():
    """store 未绑定 → handler 返回 error 字符串（dispatch 永不抛异常）。"""
    registry = build_core_registry()
    # Note: no set_todo_store() call here.
    out = _dispatch_sync(registry,"id-x", "todo", {})
    # dispatch returns a tool_result; the handler emitted an error payload.
    assert out.get("is_error") is None  # handler didn't raise
    payload = _todo_payload(out)
    assert "error" in payload
    assert "not bound" in payload["error"].lower() or "todostore" in payload["error"].lower()


def test_valid_statuses_constant():
    """Sanity check on exported VALID_STATUSES contract."""
    assert VALID_STATUSES == {"pending", "in_progress", "completed", "cancelled"}


# ──────────────────────────────────────────────────────────────────────────
# Compact-time reinjection — shape of the reminder Message
# ──────────────────────────────────────────────────────────────────────────


def test_compact_reinjection_builds_user_message():
    """When loop reinjects after compact, the synthetic Message it appends
    must be a ``role="user"`` text block whose content is exactly the
    ``format_for_injection`` snapshot. Other layers downstream
    (transcript writer, /usage stats) rely on that shape.
    """
    from nano_openclaw.core.loop import Message

    store = TodoStore()
    store.write(
        [
            {"id": "1", "content": "Step one", "status": "in_progress"},
            {"id": "2", "content": "Step two", "status": "pending"},
            {"id": "3", "content": "Done step", "status": "completed"},
        ]
    )
    snapshot = store.format_for_injection()
    assert snapshot is not None

    # Mirror exactly what loop.py builds:
    reminder = Message(role="user", content=[{"type": "text", "text": snapshot}])
    assert reminder.role == "user"
    assert isinstance(reminder.content, list)
    assert reminder.content[0]["type"] == "text"
    assert "Step one" in reminder.content[0]["text"]
    assert "Step two" in reminder.content[0]["text"]
    # Completed item must NOT leak into reinjected text.
    assert "Done step" not in reminder.content[0]["text"]


def test_compact_reinjection_skipped_when_no_active():
    """All-completed (or empty) lists yield no reminder (loop checks
    ``snapshot is not None`` before appending)."""
    empty = TodoStore()
    assert empty.format_for_injection() is None

    finished = TodoStore()
    finished.write(
        [
            {"id": "1", "content": "Done", "status": "completed"},
            {"id": "2", "content": "Skip", "status": "cancelled"},
        ]
    )
    assert finished.format_for_injection() is None
