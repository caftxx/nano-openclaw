"""Pure-Python tests for the tool registry. No LLM call required."""

from __future__ import annotations

from pathlib import Path

import pytest

from nano_openclaw.tools import build_default_registry


@pytest.fixture
def registry():
    return build_default_registry()


def test_read_write_roundtrip(tmp_path, registry):
    target = tmp_path / "hello.txt"
    write = registry.dispatch(
        "id-w", "write_file", {"path": str(target), "content": "你好 nano"}
    )
    assert write.get("is_error") is None
    assert "wrote" in write["content"][0]["text"]

    read = registry.dispatch("id-r", "read_file", {"path": str(target)})
    assert read.get("is_error") is None
    assert read["content"][0]["text"] == "你好 nano"


def test_list_dir_marks_directories(tmp_path, registry):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "sub").mkdir()

    out = registry.dispatch("id-l", "list_dir", {"path": str(tmp_path)})
    text = out["content"][0]["text"]
    lines = text.splitlines()

    assert "a.txt" in lines
    assert "b.txt" in lines
    assert "sub/" in lines


def test_dispatch_unknown_tool_returns_error(registry):
    out = registry.dispatch("id-x", "does_not_exist", {})
    assert out["is_error"] is True
    assert "unknown tool" in out["content"][0]["text"]
    assert out["tool_use_id"] == "id-x"


def test_dispatch_handler_exception_becomes_error(registry):
    out = registry.dispatch("id-e", "read_file", {"path": "/no/such/path/__nope__"})
    assert out["is_error"] is True
    text = out["content"][0]["text"]
    assert "FileNotFoundError" in text or "Error" in text


def test_bash_captures_exit_code(registry):
    out = registry.dispatch("id-b", "bash", {"command": "exit 7"})
    assert out.get("is_error") is None
    assert "exit=7" in out["content"][0]["text"]


def test_schemas_have_required_anthropic_fields(registry):
    schemas = registry.schemas()
    assert {s["name"] for s in schemas} == {"read_file", "write_file", "list_dir", "bash", "session_status"}
    for s in schemas:
        assert "description" in s and isinstance(s["description"], str)
        assert s["input_schema"]["type"] == "object"


def test_session_status_without_context(registry):
    out = registry.dispatch("id-s", "session_status", {})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Clock:" in text


def test_session_status_with_context(registry):
    registry.set_session_status_context(
        model="anthropic/claude-sonnet-4",
        session_id="test-123",
        context_budget=100000,
        current_tokens=12500,
        compaction_count=1,
        message_count=15,
    )
    out = registry.dispatch("id-s", "session_status", {})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Clock:" in text
    assert "Model: anthropic/claude-sonnet-4" in text
    assert "Session: test-123" in text
    assert "Context:" in text and "tokens" in text
    assert "12.5k" in text
    assert "Compactions: 1" in text
    assert "Messages: 15" in text
