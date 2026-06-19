"""Tests for ``memory/MEMORY.md`` (auto-memory topics index) injection.

Distinct from the workspace-root ``MEMORY.md`` covered by ``test_workspace.py``
— this index lives at ``<workspace>/memory/MEMORY.md`` and is maintained by
the Phase 1 auto-memory extractor. Phase 2 (these tests) only covers loading
+ system-prompt injection; extractor behaviour belongs to
``test_memory_extractor.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nano_openclaw.memory.topics import MAX_INDEX_BYTES, MAX_INDEX_LINES
from nano_openclaw.core.prompt import build_system_prompt
from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.workspace import load_workspace_memory_index


# ─── load_workspace_memory_index ───


def test_returns_none_when_memory_dir_missing(tmp_path: Path) -> None:
    """No ``memory/`` subdir → returns None silently."""
    assert load_workspace_memory_index(tmp_path) is None


def test_returns_none_when_index_file_missing(tmp_path: Path) -> None:
    """``memory/`` exists but ``MEMORY.md`` absent → None."""
    (tmp_path / "memory").mkdir()
    assert load_workspace_memory_index(tmp_path) is None


def test_returns_content_when_small(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    body = "- [user prefs](topics/user.md) — uses pnpm\n- [project](topics/proj.md) — nano-openclaw"
    (tmp_path / "memory" / "MEMORY.md").write_text(body, encoding="utf-8")

    loaded = load_workspace_memory_index(tmp_path)
    assert loaded is not None
    assert "user prefs" in loaded
    assert "WARNING" not in loaded


def test_truncates_when_line_cap_exceeded(tmp_path: Path) -> None:
    """>200 lines → truncated + warning sentence appears."""
    (tmp_path / "memory").mkdir()
    lines = [f"- [t{i}](topics/t{i}.md) — hook {i}" for i in range(MAX_INDEX_LINES + 50)]
    (tmp_path / "memory" / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")

    loaded = load_workspace_memory_index(tmp_path)
    assert loaded is not None
    assert "WARNING" in loaded
    # First MAX_INDEX_LINES are kept, surplus dropped
    assert "- [t0](topics/t0.md)" in loaded
    surplus_line = f"- [t{MAX_INDEX_LINES + 10}](topics/t{MAX_INDEX_LINES + 10}.md)"
    assert surplus_line not in loaded


def test_truncates_when_byte_cap_exceeded(tmp_path: Path) -> None:
    """Few lines but >25 KB → byte cap fires, warning mentions size."""
    (tmp_path / "memory").mkdir()
    huge = "x" * (MAX_INDEX_BYTES + 5_000)
    (tmp_path / "memory" / "MEMORY.md").write_text(huge, encoding="utf-8")

    loaded = load_workspace_memory_index(tmp_path)
    assert loaded is not None
    assert "WARNING" in loaded
    # Total bytes shrink to (cap + warning sentence) — i.e. an order of
    # magnitude under the raw file size — but not strictly ≤ cap because
    # the warning text is appended after truncation. Soft bound: at most
    # cap + 1 KB of warning + small padding.
    assert len(loaded.encode("utf-8")) <= MAX_INDEX_BYTES + 1_024


# ─── build_system_prompt integration ───


def test_main_prompt_includes_auto_memory_index_when_provided() -> None:
    registry = ToolRegistry()
    prompt = build_system_prompt(
        registry,
        auto_memory_index="- [foo](topics/foo.md) — bar",
    )
    assert "[Auto memory index (memory/MEMORY.md)]" in prompt
    assert "- [foo](topics/foo.md) — bar" in prompt


def test_main_prompt_omits_section_when_index_none() -> None:
    registry = ToolRegistry()
    prompt = build_system_prompt(registry, auto_memory_index=None)
    assert "[Auto memory index" not in prompt


def test_subagent_prompt_does_not_include_auto_memory_index() -> None:
    """Subagents build their own prompt via ``SubagentRunner._build_subagent_system_prompt``
    which never calls ``build_system_prompt`` — so the index is naturally absent.
    Locks the invariant: any future refactor that routes subagents through
    ``build_system_prompt`` must explicitly leave ``auto_memory_index=None``
    (the default) to preserve this isolation.
    """
    from nano_openclaw.subagent.runner import SubagentRunner

    runner = SubagentRunner.__new__(SubagentRunner)  # bypass __init__: only need the helper
    prompt = runner._build_subagent_system_prompt("do a thing")
    assert "[Auto memory index" not in prompt
    assert "memory/MEMORY.md" not in prompt
