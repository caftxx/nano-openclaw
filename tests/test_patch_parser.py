"""Tests for V4A patch parser & applier.

Covers the 11 cases enumerated in the plan:

- add / update single hunk / update multi hunk
- whitespace drift / indentation drift
- delete / move (incl. destination collision)
- validation rollback
- addition-only hunk with context hint
- patch without begin/end markers
- multi-file patch in a single block
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nano_openclaw.patch_parser import (
    OperationType,
    apply_v4a_patch,
    parse_v4a_patch,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_add_file(tmp_path):
    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Add File: hello.py
        +print("hello")
        +print("world")
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert result.files_created == ["hello.py"]
    created = (tmp_path / "hello.py").read_text(encoding="utf-8")
    assert created == 'print("hello")\nprint("world")'


def test_add_file_refuses_to_overwrite_existing_file(tmp_path):
    target = tmp_path / "hello.py"
    _write(target, "original\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Add File: hello.py
        +replacement
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)

    assert not result.success
    assert result.error is not None
    assert "already exists" in result.error
    assert target.read_text(encoding="utf-8") == "original\n"


def test_paths_must_stay_inside_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    if outside.exists():
        outside.unlink()

    patch = textwrap.dedent(
        f"""\
        *** Begin Patch
        *** Add File: ../{outside.name}
        +outside
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)

    assert not result.success
    assert result.error is not None
    assert "escapes workspace" in result.error
    assert not outside.exists()


def test_multi_operation_write_targets_must_not_collide(tmp_path):
    src = tmp_path / "src.py"
    _write(src, "moved\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Move File: src.py -> dst.py
        *** Add File: dst.py
        +added
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)

    assert not result.success
    assert result.error is not None
    assert "duplicate write target" in result.error
    assert src.read_text(encoding="utf-8") == "moved\n"
    assert not (tmp_path / "dst.py").exists()


def test_update_single_hunk(tmp_path):
    target = tmp_path / "mod.py"
    _write(target, "def foo():\n    return 1\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: mod.py
        @@ foo @@
         def foo():
        -    return 1
        +    return 2
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert result.files_modified == ["mod.py"]
    assert target.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


def test_update_multiple_hunks_same_file(tmp_path):
    target = tmp_path / "multi.py"
    _write(
        target,
        textwrap.dedent(
            """\
            def alpha():
                return 1

            def beta():
                return 2

            def gamma():
                return 3
            """
        ),
    )

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: multi.py
        @@ alpha @@
         def alpha():
        -    return 1
        +    return 10
        @@ gamma @@
         def gamma():
        -    return 3
        +    return 30
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    out = target.read_text(encoding="utf-8")
    assert "return 10" in out
    assert "return 30" in out
    # Middle function untouched
    assert "def beta():\n    return 2" in out


def test_update_with_whitespace_drift(tmp_path):
    target = tmp_path / "ws.py"
    # Real file has tabs / extra trailing spaces.
    _write(target, "def foo():    \n    return 1\n")

    # Patch context line trimmed (no trailing spaces). Should match via
    # line_trimmed strategy.
    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: ws.py
         def foo():
        -    return 1
        +    return 99
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert "return 99" in target.read_text(encoding="utf-8")


def test_update_with_indentation_drift(tmp_path):
    target = tmp_path / "indent.py"
    # Real file: 4-space indent.
    _write(
        target,
        textwrap.dedent(
            """\
            class C:
                def foo(self):
                    return 1
            """
        ),
    )

    # Patch with zero indent for context — indentation_flexible should match.
    patch = (
        "*** Begin Patch\n"
        "*** Update File: indent.py\n"
        "def foo(self):\n"
        "-    return 1\n"
        "+    return 7\n"
        "*** End Patch\n"
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert "return 7" in target.read_text(encoding="utf-8")


def test_delete_file(tmp_path):
    target = tmp_path / "gone.py"
    _write(target, "x = 1\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Delete File: gone.py
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert result.files_deleted == ["gone.py"]
    assert not target.exists()


def test_move_file_success_and_collision(tmp_path):
    src = tmp_path / "src.py"
    _write(src, "moved = True\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Move File: src.py -> dst.py
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert not src.exists()
    assert (tmp_path / "dst.py").read_text(encoding="utf-8") == "moved = True\n"

    # Now collision: prepare both paths again, attempt move — must fail.
    _write(tmp_path / "src2.py", "a\n")
    _write(tmp_path / "dst2.py", "b\n")
    patch2 = textwrap.dedent(
        """\
        *** Begin Patch
        *** Move File: src2.py -> dst2.py
        *** End Patch
        """
    )
    result2 = apply_v4a_patch(patch2, tmp_path)
    assert not result2.success
    assert result2.error is not None
    assert "destination already exists" in result2.error
    # Validation phase: no filesystem mutation expected.
    assert (tmp_path / "src2.py").read_text(encoding="utf-8") == "a\n"
    assert (tmp_path / "dst2.py").read_text(encoding="utf-8") == "b\n"


def test_validate_failure_no_writes(tmp_path):
    # File A: exists and the hunk for it WOULD match.
    a = tmp_path / "a.py"
    _write(a, "x = 1\n")
    # File B: also exists, but the patch's hunk references content that's
    # not there — validation should fail and roll back the whole patch.
    b = tmp_path / "b.py"
    _write(b, "y = 2\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: a.py
        -x = 1
        +x = 99
        *** Update File: b.py
        -NONEXISTENT
        +REPLACEMENT
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert not result.success
    assert result.error is not None
    assert "validation failed" in result.error.lower()
    # CRITICAL: neither file was modified.
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "y = 2\n"


def test_hunk_without_plus_or_minus_prefixes_emits_clear_error(tmp_path):
    target = tmp_path / "MEMORY.md"
    _write(target, "**性格特点：** foo\n\n---\n\n## 关于家庭\n")
    # Model forgot the `-`/`+` prefixes — every line was treated as context,
    # so the hunk adds and removes nothing. Without the diagnostic, this
    # surfaces as the opaque "identical" error.
    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: MEMORY.md
         **性格特点：** foo
         **编程偏好：** Python
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert not result.success
    assert "no `+` or `-` lines" in (result.error or "")
    assert "V4A" in (result.error or "")


def test_merge_conflict_separator_is_called_out(tmp_path):
    target = tmp_path / "MEMORY.md"
    _write(target, "foo\nbar\n")
    # Model used git merge-conflict style with `=======`.
    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: MEMORY.md
         foo
         bar
        =======
         foo
         baz
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert not result.success
    assert "=======" in (result.error or "")
    assert "merge-conflict" in (result.error or "")


def test_addition_only_hunk_with_context_hint(tmp_path):
    target = tmp_path / "additive.py"
    _write(
        target,
        textwrap.dedent(
            """\
            # MARKER
            existing = True
            """
        ),
    )

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: additive.py
        @@ # MARKER @@
        +inserted = 42
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    content = target.read_text(encoding="utf-8")
    # Inserted line lands after the marker, before subsequent existing line.
    lines = content.splitlines()
    assert "# MARKER" in lines
    marker_idx = lines.index("# MARKER")
    assert lines[marker_idx + 1] == "inserted = 42"
    assert "existing = True" in content


def test_missing_begin_end_markers(tmp_path):
    target = tmp_path / "nomarkers.py"
    _write(target, "value = 0\n")

    # No *** Begin Patch / *** End Patch wrappers; parser still accepts.
    patch = (
        "*** Update File: nomarkers.py\n"
        "-value = 0\n"
        "+value = 42\n"
    )
    operations, err = parse_v4a_patch(patch)
    assert err is None, err
    assert len(operations) == 1
    assert operations[0].operation == OperationType.UPDATE

    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert target.read_text(encoding="utf-8") == "value = 42\n"


def test_multi_file_patch(tmp_path):
    a = tmp_path / "a.py"
    _write(a, "alpha = 1\n")
    c = tmp_path / "c.py"
    _write(c, "gamma = 3\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: a.py
        -alpha = 1
        +alpha = 11
        *** Add File: b.py
        +beta = 2
        *** Delete File: c.py
        *** End Patch
        """
    )
    result = apply_v4a_patch(patch, tmp_path)
    assert result.success, result.error
    assert result.files_modified == ["a.py"]
    assert result.files_created == ["b.py"]
    assert result.files_deleted == ["c.py"]
    assert a.read_text(encoding="utf-8") == "alpha = 11\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "beta = 2"
    assert not c.exists()


def test_tools_registry_dispatches_apply_patch(tmp_path):
    """Integration sanity: ToolRegistry routes apply_patch to the handler."""
    import asyncio
    import inspect

    from nano_openclaw.tools import build_core_registry

    registry = build_core_registry()
    registry.set_workspace_dir(tmp_path)

    target = tmp_path / "via_tool.py"
    _write(target, "answer = 0\n")

    patch = textwrap.dedent(
        """\
        *** Begin Patch
        *** Update File: via_tool.py
        -answer = 0
        +answer = 42
        *** End Patch
        """
    )
    # ``test_tools.py`` may monkey-patch dispatch to a sync wrapper at import time.
    # Handle both cases so this file works in isolation and as part of the suite.
    call = registry.dispatch("id-1", "apply_patch", {"patch": patch})
    result = asyncio.run(call) if inspect.iscoroutine(call) else call
    assert result.get("is_error") is None, result
    text = result["content"][0]["text"]
    assert "Modified: via_tool.py" in text
    assert target.read_text(encoding="utf-8") == "answer = 42\n"
