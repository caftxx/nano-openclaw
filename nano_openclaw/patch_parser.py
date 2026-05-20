"""V4A patch format parser and applier.

Ported from hermes-agent/tools/patch_parser.py — slimmed:

- No lint integration (nano-openclaw has no lint system).
- No ``format_no_match_hint`` diagnostics — short error strings only.
- No ``FileOperations`` protocol abstraction — workspace_dir + pathlib only.

V4A format::

    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     context line (space prefix)
    -removed line
    +added line
    *** Add File: path/to/new.py
    +new file content
    *** Delete File: path/to/old.py
    *** Move File: old/path.py -> new/path.py
    *** End Patch

Usage::

    from nano_openclaw.patch_parser import apply_v4a_patch
    result = apply_v4a_patch(patch_text, workspace_dir)
    if result.success:
        ...
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from nano_openclaw.fuzzy_match import fuzzy_find_and_replace


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class OperationType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    """A single line in a patch hunk."""

    prefix: str  # ' ', '-', or '+'
    content: str


@dataclass
class Hunk:
    """A group of changes within a file."""

    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    """A single operation in a V4A patch."""

    operation: OperationType
    file_path: str
    new_path: Optional[str] = None  # For move operations
    hunks: List[Hunk] = field(default_factory=list)
    content: Optional[str] = None  # For add file operations


@dataclass
class PatchResult:
    """Outcome of applying a V4A patch."""

    success: bool
    diff: str = ""
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_v4a_patch(
    patch_content: str,
) -> Tuple[List[PatchOperation], Optional[str]]:
    """Parse a V4A format patch.

    Args:
        patch_content: The patch text in V4A format.

    Returns:
        Tuple of ``(operations, error_message)``. On success the error is None;
        on failure ``operations`` is empty.
    """
    lines = patch_content.split("\n")
    operations: List[PatchOperation] = []

    # Find patch boundaries
    start_idx: Optional[int] = None
    end_idx: Optional[int] = None

    for i, line in enumerate(lines):
        if "*** Begin Patch" in line or "***Begin Patch" in line:
            start_idx = i
        elif "*** End Patch" in line or "***End Patch" in line:
            end_idx = i
            break

    if start_idx is None:
        start_idx = -1

    if end_idx is None:
        end_idx = len(lines)

    i = start_idx + 1
    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None

    while i < end_idx:
        line = lines[i]

        update_match = re.match(r"\*\*\*\s*Update\s+File:\s*(.+)", line)
        add_match = re.match(r"\*\*\*\s*Add\s+File:\s*(.+)", line)
        delete_match = re.match(r"\*\*\*\s*Delete\s+File:\s*(.+)", line)
        move_match = re.match(r"\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)", line)

        if update_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.UPDATE,
                file_path=update_match.group(1).strip(),
            )
            current_hunk = None

        elif add_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.ADD,
                file_path=add_match.group(1).strip(),
            )
            current_hunk = Hunk()

        elif delete_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.DELETE,
                file_path=delete_match.group(1).strip(),
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif move_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.MOVE,
                file_path=move_match.group(1).strip(),
                new_path=move_match.group(2).strip(),
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif line.startswith("@@"):
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)

                hint_match = re.match(r"@@\s*(.+?)\s*@@", line)
                hint = hint_match.group(1) if hint_match else None
                current_hunk = Hunk(context_hint=hint)

        elif current_op and line:
            if current_hunk is None:
                current_hunk = Hunk()

            if line.startswith("+"):
                current_hunk.lines.append(HunkLine("+", line[1:]))
            elif line.startswith("-"):
                current_hunk.lines.append(HunkLine("-", line[1:]))
            elif line.startswith(" "):
                current_hunk.lines.append(HunkLine(" ", line[1:]))
            elif line.startswith("\\"):
                # "\ No newline at end of file" marker — skip
                pass
            else:
                # Treat as context line (implicit space prefix)
                current_hunk.lines.append(HunkLine(" ", line))

        i += 1

    if current_op:
        if current_hunk and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)

    if not operations:
        return operations, None

    parse_errors: List[str] = []
    for op in operations:
        if not op.file_path:
            parse_errors.append("Operation with empty file path")
        if op.operation == OperationType.UPDATE and not op.hunks:
            parse_errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == OperationType.MOVE and not op.new_path:
            parse_errors.append(
                f"MOVE {op.file_path!r}: missing destination path (expected 'src -> dst')"
            )

    if parse_errors:
        return [], "Parse error: " + "; ".join(parse_errors)

    return operations, None


# ---------------------------------------------------------------------------
# Filesystem helpers (module-level, internal)
# ---------------------------------------------------------------------------


def _resolve_path(workspace_dir: Optional[str], rel_path: str) -> Path:
    """Resolve ``rel_path`` within the workspace.

    Absolute paths are accepted only when they still point inside the workspace.
    Relative paths may not escape via ``..``. The returned path may not exist yet
    (for Add/Move destinations), so resolution is non-strict.
    """
    workspace = Path(workspace_dir).resolve() if workspace_dir else Path.cwd().resolve()
    candidate = Path(rel_path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {rel_path}") from exc
    return resolved


def _read(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Read a file as UTF-8. Returns ``(content, error)``; one is always None."""
    try:
        if not path.exists():
            return None, "file not found"
        if not path.is_file():
            return None, "not a regular file"
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"unicode decode error: {exc}"
    except OSError as exc:
        return None, f"read failed: {exc}"


def _write(path: Path, content: str) -> Optional[str]:
    """Write ``content`` to ``path``. Returns an error message, or None on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return None
    except OSError as exc:
        return f"write failed: {exc}"


def _delete(path: Path) -> Optional[str]:
    """Delete a file. Returns an error message, or None on success."""
    try:
        if not path.exists():
            return "file not found"
        if not path.is_file():
            return "not a regular file"
        path.unlink()
        return None
    except OSError as exc:
        return f"delete failed: {exc}"


def _move(src: Path, dst: Path) -> Optional[str]:
    """Move a file. Returns an error message, or None on success."""
    try:
        if not src.exists():
            return "source not found"
        if dst.exists():
            return "destination already exists"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return None
    except OSError as exc:
        return f"move failed: {exc}"


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _count_occurrences(text: str, pattern: str) -> int:
    """Count non-overlapping occurrences of ``pattern`` in ``text``."""
    count = 0
    start = 0
    while True:
        pos = text.find(pattern, start)
        if pos == -1:
            break
        count += 1
        start = pos + 1
    return count


def _validate_operations(
    operations: List[PatchOperation],
    workspace_dir: Optional[str],
) -> List[str]:
    """Validate every operation without writing. Returns a list of error strings."""
    errors: List[str] = []
    write_targets: set[Path] = set()

    for op in operations:
        try:
            abs_path = _resolve_path(workspace_dir, op.file_path)
            dst_abs = (
                _resolve_path(workspace_dir, op.new_path)
                if op.operation == OperationType.MOVE and op.new_path
                else None
            )
        except ValueError as exc:
            errors.append(f"{op.file_path}: {exc}")
            continue

        if op.operation == OperationType.UPDATE:
            content, read_err = _read(abs_path)
            if read_err is not None:
                errors.append(f"{op.file_path}: {read_err}")
                continue

            simulated = content or ""
            for hunk in op.hunks:
                plus_count = sum(1 for l in hunk.lines if l.prefix == "+")
                minus_count = sum(1 for l in hunk.lines if l.prefix == "-")
                if plus_count == 0 and minus_count == 0 and hunk.lines:
                    # Every line was treated as context — model likely forgot
                    # the `-`/`+` prefixes. parse_v4a_patch silently treats
                    # unprefixed lines as context, so this fails as "identical"
                    # later, which is opaque. Catch it here with a clear hint.
                    raw = [l.content for l in hunk.lines]
                    extra = ""
                    if any(l.strip().startswith("=======") for l in raw):
                        extra = (
                            " — detected `=======` separator, which is git "
                            "merge-conflict format. V4A uses line-prefix `-`/`+`, "
                            "not separator blocks."
                        )
                    errors.append(
                        f"{op.file_path}: hunk has no `+` or `-` lines, so it "
                        f"adds and removes nothing. In V4A format each modified "
                        f"line must start with `-` (remove) or `+` (add); "
                        f"unprefixed lines are context only.{extra}"
                    )
                    continue

                search_lines = [
                    l.content for l in hunk.lines if l.prefix in (" ", "-")
                ]
                if not search_lines:
                    # Addition-only hunk: validate context hint uniqueness
                    if hunk.context_hint:
                        occurrences = _count_occurrences(simulated, hunk.context_hint)
                        if occurrences == 0:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' not found"
                            )
                        elif occurrences > 1:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' is ambiguous "
                                f"({occurrences} occurrences)"
                            )
                    continue

                search_pattern = "\n".join(search_lines)
                replace_lines = [
                    l.content for l in hunk.lines if l.prefix in (" ", "+")
                ]
                replacement = "\n".join(replace_lines)

                new_simulated, count, _strategy, match_error = fuzzy_find_and_replace(
                    simulated, search_pattern, replacement, replace_all=False
                )
                if count == 0:
                    label = (
                        f"'{hunk.context_hint}'" if hunk.context_hint else "(no hint)"
                    )
                    msg = f"{op.file_path}: hunk {label} not found" + (
                        f" — {match_error}" if match_error else ""
                    )
                    errors.append(msg)
                else:
                    simulated = new_simulated

        elif op.operation == OperationType.DELETE:
            _, read_err = _read(abs_path)
            if read_err is not None:
                errors.append(f"{op.file_path}: file not found for deletion")

        elif op.operation == OperationType.MOVE:
            if not op.new_path:
                errors.append(
                    f"{op.file_path}: MOVE operation missing destination path"
                )
                continue
            _, src_err = _read(abs_path)
            if src_err is not None:
                errors.append(f"{op.file_path}: source file not found for move")
            assert dst_abs is not None
            _, dst_err = _read(dst_abs)
            if dst_err is None:
                errors.append(
                    f"{op.new_path}: destination already exists — move would overwrite"
                )
            if dst_abs in write_targets:
                errors.append(f"{op.new_path}: duplicate write target in patch")
            write_targets.add(dst_abs)

        elif op.operation == OperationType.ADD:
            if abs_path.exists():
                errors.append(f"{op.file_path}: file already exists — add would overwrite")
            if abs_path in write_targets:
                errors.append(f"{op.file_path}: duplicate write target in patch")
            write_targets.add(abs_path)

    return errors


def apply_v4a_patch(
    patch_content: str,
    workspace_dir: Optional[str | Path] = None,
) -> PatchResult:
    """Parse and apply a V4A patch using two-phase validate-then-apply.

    Phase 1 validates every operation against current file contents without
    writing anything. If any validation error fires, the function returns
    immediately with ``success=False`` and **no** filesystem changes.

    Phase 2 applies every operation. A failure here (e.g. a race between
    validation and apply) leaves state potentially inconsistent — the error
    reflects that.
    """
    operations, parse_err = parse_v4a_patch(patch_content)
    if parse_err:
        return PatchResult(success=False, error=parse_err)

    if not operations:
        return PatchResult(success=False, error="Empty patch (no operations parsed)")

    ws = str(workspace_dir) if workspace_dir is not None else None

    # ---- Phase 1: validate ----
    validation_errors = _validate_operations(operations, ws)
    if validation_errors:
        return PatchResult(
            success=False,
            error="Patch validation failed (no files were modified):\n"
            + "\n".join(f"  • {e}" for e in validation_errors),
        )

    # ---- Phase 2: apply ----
    files_modified: List[str] = []
    files_created: List[str] = []
    files_deleted: List[str] = []
    all_diffs: List[str] = []
    errors: List[str] = []

    for op in operations:
        try:
            if op.operation == OperationType.ADD:
                ok, payload = _apply_add(op, ws)
                if ok:
                    files_created.append(op.file_path)
                    all_diffs.append(payload)
                else:
                    errors.append(f"Failed to add {op.file_path}: {payload}")

            elif op.operation == OperationType.DELETE:
                ok, payload = _apply_delete(op, ws)
                if ok:
                    files_deleted.append(op.file_path)
                    all_diffs.append(payload)
                else:
                    errors.append(f"Failed to delete {op.file_path}: {payload}")

            elif op.operation == OperationType.MOVE:
                ok, payload = _apply_move(op, ws)
                if ok:
                    files_modified.append(f"{op.file_path} -> {op.new_path}")
                    all_diffs.append(payload)
                else:
                    errors.append(f"Failed to move {op.file_path}: {payload}")

            elif op.operation == OperationType.UPDATE:
                ok, payload = _apply_update(op, ws)
                if ok:
                    files_modified.append(op.file_path)
                    all_diffs.append(payload)
                else:
                    errors.append(f"Failed to update {op.file_path}: {payload}")

        except Exception as exc:  # noqa: BLE001 — surface as error string
            errors.append(f"Error processing {op.file_path}: {exc}")

    combined_diff = "\n".join(all_diffs)

    if errors:
        return PatchResult(
            success=False,
            diff=combined_diff,
            files_modified=files_modified,
            files_created=files_created,
            files_deleted=files_deleted,
            error=(
                "Apply phase failed (state may be inconsistent — run `git diff` "
                "to assess):\n" + "\n".join(f"  • {e}" for e in errors)
            ),
        )

    return PatchResult(
        success=True,
        diff=combined_diff,
        files_modified=files_modified,
        files_created=files_created,
        files_deleted=files_deleted,
    )


def _apply_add(op: PatchOperation, workspace_dir: Optional[str]) -> Tuple[bool, str]:
    """Apply an Add File operation."""
    content_lines: List[str] = []
    for hunk in op.hunks:
        for line in hunk.lines:
            if line.prefix == "+":
                content_lines.append(line.content)

    content = "\n".join(content_lines)

    abs_path = _resolve_path(workspace_dir, op.file_path)
    err = _write(abs_path, content)
    if err is not None:
        return False, err

    diff = f"--- /dev/null\n+++ b/{op.file_path}\n"
    diff += "\n".join(f"+{line}" for line in content_lines)
    return True, diff


def _apply_delete(op: PatchOperation, workspace_dir: Optional[str]) -> Tuple[bool, str]:
    """Apply a Delete File operation."""
    abs_path = _resolve_path(workspace_dir, op.file_path)
    content, read_err = _read(abs_path)
    if read_err is not None:
        return False, f"Cannot delete {op.file_path}: {read_err}"

    err = _delete(abs_path)
    if err is not None:
        return False, err

    removed_lines = (content or "").splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            removed_lines,
            [],
            fromfile=f"a/{op.file_path}",
            tofile="/dev/null",
        )
    )
    return True, diff or f"# Deleted: {op.file_path}"


def _apply_move(op: PatchOperation, workspace_dir: Optional[str]) -> Tuple[bool, str]:
    """Apply a Move File operation."""
    src = _resolve_path(workspace_dir, op.file_path)
    dst = _resolve_path(workspace_dir, op.new_path or "")
    err = _move(src, dst)
    if err is not None:
        return False, err
    return True, f"# Moved: {op.file_path} -> {op.new_path}"


def _apply_update(op: PatchOperation, workspace_dir: Optional[str]) -> Tuple[bool, str]:
    """Apply an Update File operation (one or more hunks)."""
    abs_path = _resolve_path(workspace_dir, op.file_path)
    current_content, read_err = _read(abs_path)
    if read_err is not None:
        return False, f"Cannot read file: {read_err}"

    current_content = current_content or ""
    new_content = current_content

    for hunk in op.hunks:
        search_lines: List[str] = []
        replace_lines: List[str] = []

        for line in hunk.lines:
            if line.prefix == " ":
                search_lines.append(line.content)
                replace_lines.append(line.content)
            elif line.prefix == "-":
                search_lines.append(line.content)
            elif line.prefix == "+":
                replace_lines.append(line.content)

        if search_lines:
            search_pattern = "\n".join(search_lines)
            replacement = "\n".join(replace_lines)

            new_content, count, _strategy, error = fuzzy_find_and_replace(
                new_content, search_pattern, replacement, replace_all=False
            )

            if error and count == 0:
                # Retry within a window around the context hint, if any
                if hunk.context_hint:
                    hint_pos = new_content.find(hunk.context_hint)
                    if hint_pos != -1:
                        window_start = max(0, hint_pos - 500)
                        window_end = min(len(new_content), hint_pos + 2000)
                        window = new_content[window_start:window_end]

                        window_new, count, _strategy, error = fuzzy_find_and_replace(
                            window, search_pattern, replacement, replace_all=False
                        )

                        if count > 0:
                            new_content = (
                                new_content[:window_start]
                                + window_new
                                + new_content[window_end:]
                            )
                            error = None

                if error:
                    return False, f"Could not apply hunk: {error}"
        else:
            # Addition-only hunk: insert at context hint, or append
            insert_text = "\n".join(replace_lines)
            if hunk.context_hint:
                occurrences = _count_occurrences(new_content, hunk.context_hint)
                if occurrences == 0:
                    new_content = new_content.rstrip("\n") + "\n" + insert_text + "\n"
                elif occurrences > 1:
                    return False, (
                        f"Addition-only hunk: context hint '{hunk.context_hint}' "
                        f"is ambiguous ({occurrences} occurrences) — provide a "
                        "more unique hint"
                    )
                else:
                    hint_pos = new_content.find(hunk.context_hint)
                    eol = new_content.find("\n", hint_pos)
                    if eol != -1:
                        new_content = (
                            new_content[: eol + 1]
                            + insert_text
                            + "\n"
                            + new_content[eol + 1 :]
                        )
                    else:
                        new_content = new_content + "\n" + insert_text
            else:
                new_content = new_content.rstrip("\n") + "\n" + insert_text + "\n"

    write_err = _write(abs_path, new_content)
    if write_err is not None:
        return False, write_err

    diff_lines = difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{op.file_path}",
        tofile=f"b/{op.file_path}",
    )
    return True, "".join(diff_lines)
