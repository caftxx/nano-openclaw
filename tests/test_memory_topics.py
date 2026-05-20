"""Tests for ``nano_openclaw.memory.topics`` — scan / manifest / truncate / path guard.

Pure-function level: no extractor wiring, no subagent spawn. The extractor
module's behavioural tests live in ``tests/test_memory_extractor.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nano_openclaw.memory.topics import (
    INDEX_FILE,
    MAX_INDEX_BYTES,
    MAX_INDEX_LINES,
    TopicHeader,
    format_manifest,
    is_topic_write_path,
    scan_topic_files,
    truncate_index,
)


# ─── scan_topic_files ───


def test_scan_returns_empty_when_topics_dir_missing(tmp_path: Path) -> None:
    """No ``topics/`` subdir → empty list, no exception."""
    assert scan_topic_files(tmp_path) == []


def test_scan_parses_frontmatter_and_sorts_by_mtime(tmp_path: Path) -> None:
    topics = tmp_path / "topics"
    topics.mkdir()
    older = topics / "older.md"
    newer = topics / "newer.md"
    older.write_text(
        "---\ndescription: An older memory\ntype: user\n---\n\nbody\n",
        encoding="utf-8",
    )
    newer.write_text(
        "---\ndescription: A newer one\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )
    # Force mtimes so order is deterministic regardless of fs precision.
    older_mtime = time.time() - 10
    newer_mtime = time.time()
    os.utime(older, (older_mtime, older_mtime))
    os.utime(newer, (newer_mtime, newer_mtime))

    headers = scan_topic_files(tmp_path)
    assert [h.filename for h in headers] == ["newer.md", "older.md"]
    assert headers[0].description == "A newer one"
    assert headers[0].memory_type == "feedback"
    assert headers[1].memory_type == "user"


def test_scan_recurses_into_subdirs(tmp_path: Path) -> None:
    topics = tmp_path / "topics" / "sub"
    topics.mkdir(parents=True)
    nested = topics / "deep.md"
    nested.write_text("---\ndescription: Deep\ntype: project\n---\n", encoding="utf-8")
    headers = scan_topic_files(tmp_path)
    assert len(headers) == 1
    # Relative path within ``topics/`` keeps the subdir prefix.
    assert headers[0].filename in {"sub/deep.md", os.path.join("sub", "deep.md")}


def test_scan_tolerates_missing_or_invalid_frontmatter(tmp_path: Path) -> None:
    topics = tmp_path / "topics"
    topics.mkdir()
    (topics / "no_fm.md").write_text("just body, no frontmatter\n", encoding="utf-8")
    (topics / "bad_fm.md").write_text("---\n: : :\n---\nbody\n", encoding="utf-8")
    (topics / "no_desc.md").write_text("---\ntype: user\n---\nbody\n", encoding="utf-8")
    (topics / "bad_type.md").write_text(
        "---\ndescription: d\ntype: invented\n---\nbody\n", encoding="utf-8"
    )

    headers = {h.filename: h for h in scan_topic_files(tmp_path)}
    assert len(headers) == 4
    assert headers["no_fm.md"].description is None
    assert headers["no_fm.md"].memory_type is None
    assert headers["bad_fm.md"].description is None
    assert headers["no_desc.md"].description is None
    assert headers["no_desc.md"].memory_type == "user"
    # Unknown type tags are coerced to ``None`` so the manifest stays clean.
    assert headers["bad_type.md"].memory_type is None
    assert headers["bad_type.md"].description == "d"


# ─── format_manifest ───


def test_format_manifest_renders_tag_and_iso_timestamp() -> None:
    # 2024-06-15T12:34:56.789Z → mtime 1718454896.789
    header = TopicHeader(
        filename="user-prefs.md",
        file_path=Path("/tmp/x"),
        mtime_ms=1_718_454_896_789,
        description="Coding preferences",
        memory_type="user",
    )
    line = format_manifest([header])
    assert line == "- [user] user-prefs.md (2024-06-15T12:34:56.789Z): Coding preferences"


def test_format_manifest_skips_tag_when_type_missing() -> None:
    header = TopicHeader(
        filename="misc.md",
        file_path=Path("/tmp/x"),
        mtime_ms=0,
        description=None,
        memory_type=None,
    )
    line = format_manifest([header])
    # No leading "[type] ", no trailing ": description" when both absent.
    assert line == "- misc.md (1970-01-01T00:00:00.000Z)"


def test_format_manifest_empty() -> None:
    assert format_manifest([]) == ""


# ─── truncate_index ───


def test_truncate_returns_input_when_under_limits() -> None:
    content = "line 1\nline 2\nline 3\n"
    out, line_t, byte_t = truncate_index(content)
    # ``strip`` removes trailing newline so the round-trip is the stripped form.
    assert out == content.strip()
    assert line_t is False
    assert byte_t is False


def test_truncate_at_199_lines_no_truncation() -> None:
    content = "\n".join(f"l{i}" for i in range(199))
    out, line_t, byte_t = truncate_index(content)
    assert out == content
    assert (line_t, byte_t) == (False, False)


def test_truncate_at_exactly_200_lines_no_truncation() -> None:
    content = "\n".join(f"l{i}" for i in range(200))
    out, line_t, byte_t = truncate_index(content)
    assert out == content
    assert (line_t, byte_t) == (False, False)


def test_truncate_at_201_lines_fires_line_cap() -> None:
    content = "\n".join(f"l{i}" for i in range(201))
    out, line_t, byte_t = truncate_index(content)
    assert line_t is True
    assert byte_t is False
    # Body keeps exactly MAX_INDEX_LINES from the head.
    body = out.split("\n\n> WARNING:")[0]
    assert body.count("\n") == MAX_INDEX_LINES - 1
    assert "201 lines (limit: 200)" in out


def test_truncate_at_byte_cap_only_fires_byte_warning() -> None:
    # 5 lines, each ~6 KB → 30 KB total, well under the line cap.
    big_line = "x" * 6000
    content = "\n".join([big_line] * 5)
    out, line_t, byte_t = truncate_index(content)
    assert line_t is False
    assert byte_t is True
    assert "index entries are too long" in out
    body = out.split("\n\n> WARNING:")[0]
    assert len(body.encode("utf-8")) <= MAX_INDEX_BYTES


def test_truncate_when_both_caps_fire_emits_combined_warning() -> None:
    # 250 lines of 200 chars each → 50 KB, both caps exceeded.
    line = "x" * 200
    content = "\n".join([line] * 250)
    out, line_t, byte_t = truncate_index(content)
    assert line_t is True
    assert byte_t is True
    assert "250 lines and" in out
    # Final body fits both caps.
    body = out.split("\n\n> WARNING:")[0]
    assert body.count("\n") <= MAX_INDEX_LINES - 1
    assert len(body.encode("utf-8")) <= MAX_INDEX_BYTES


def test_truncate_empty_input() -> None:
    out, line_t, byte_t = truncate_index("")
    assert out == ""
    assert (line_t, byte_t) == (False, False)


# ─── is_topic_write_path ───


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.parametrize(
    "rel_path",
    [
        "memory/MEMORY.md",
        "memory/topics/user-prefs.md",
        "memory/topics/sub/deep.md",
        "memory/topics/a/b/c.md",
    ],
)
def test_legal_write_paths(workspace: Path, rel_path: str) -> None:
    assert is_topic_write_path(workspace, rel_path) is True


@pytest.mark.parametrize(
    "rel_path",
    [
        "",
        "memory/2026-05-20.md",  # daily file — pre-compaction flush territory
        "memory/notes.md",  # sibling of topics/, not the index
        "memory/topics/file.txt",  # wrong extension
        "memory/topics",  # the directory itself, not a file
        "other/MEMORY.md",  # outside memory/
        "../escape/memory/MEMORY.md",  # path traversal
        "MEMORY.md",  # root-level, missing memory/ prefix
    ],
)
def test_illegal_write_paths(workspace: Path, rel_path: str) -> None:
    assert is_topic_write_path(workspace, rel_path) is False


def test_absolute_paths_inside_workspace_are_accepted(workspace: Path) -> None:
    abs_path = str(workspace / "memory" / "MEMORY.md")
    assert is_topic_write_path(workspace, abs_path) is True


def test_absolute_paths_outside_workspace_rejected(workspace: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    elsewhere = tmp_path_factory.mktemp("other")
    abs_path = str(elsewhere / "memory" / "MEMORY.md")
    assert is_topic_write_path(workspace, abs_path) is False
