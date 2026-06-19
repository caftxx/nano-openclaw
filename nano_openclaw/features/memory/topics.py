"""Topic-file primitives for the auto-memory extractor.

Mirrors claude-code memdir.ts + memoryScan.ts:

- ``scan_topic_files`` reads ``memory/topics/*.md`` headers (YAML frontmatter
  ``description`` / ``type``) and returns them sorted newest-first.
- ``format_manifest`` renders the header list as one-line text the extractor
  subagent sees in its prompt (so it doesn't burn a turn on ``ls``).
- ``truncate_index`` applies the 200-line / 25 KB caps used by the system
  prompt injection (Phase 2). Appends a warning line that names which cap
  fired so the model knows the index it sees is incomplete.
- ``is_topic_write_path`` gates extractor writes to ``memory/topics/*.md``
  and ``memory/MEMORY.md`` only — daily files (``memory/YYYY-MM-DD.md``)
  remain the pre-compaction flush's exclusive territory.

Kept pure (no I/O outside ``scan_topic_files``) so the extractor logic in
``extractor.py`` can unit-test deterministic helpers without filesystem
fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

# ─── Constants (mirror claude-code memdir.ts + memoryTypes.ts) ───
TOPIC_DIR = "topics"
INDEX_FILE = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# Match opening YAML frontmatter block. Same shape as skills/loader.py
# ``FRONTMATTER_PATTERN`` so behaviour is consistent across modules.
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Only read this many bytes at the start of each topic file to parse the
# frontmatter — keeps ``scan_topic_files`` cheap even when topic bodies grow.
_FRONTMATTER_MAX_BYTES = 8 * 1024


@dataclass
class TopicHeader:
    """Header metadata for a single ``memory/topics/*.md`` file."""

    filename: str  # Relative to ``memory/topics/`` (e.g. ``user-prefs.md`` or ``nested/foo.md``)
    file_path: Path
    mtime_ms: int
    description: Optional[str]
    memory_type: Optional[str]  # One of ``MEMORY_TYPES`` or None when frontmatter is missing/invalid


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """Return the YAML frontmatter as a dict, or ``{}`` on miss/parse error."""
    match = _FRONTMATTER_PATTERN.match(raw)
    if not match:
        return {}
    try:
        result = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return result if isinstance(result, dict) else {}


def _coerce_memory_type(value: Any) -> Optional[str]:
    if isinstance(value, str) and value in MEMORY_TYPES:
        return value
    return None


def scan_topic_files(memory_dir: Path) -> list[TopicHeader]:
    """Scan ``memory_dir/topics/`` for ``.md`` files, parse frontmatter, sort newest-first.

    Returns ``[]`` when the directory does not exist or cannot be read.
    Files whose frontmatter is missing / unparseable are still included
    (``description=None`` / ``memory_type=None``) so the model still sees
    them in the manifest and can choose to read them.
    """
    topics_dir = memory_dir / TOPIC_DIR
    if not topics_dir.is_dir():
        return []

    headers: list[TopicHeader] = []
    for path in topics_dir.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        try:
            with path.open("rb") as f:
                raw_bytes = f.read(_FRONTMATTER_MAX_BYTES)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
        except OSError:
            continue

        frontmatter = _parse_frontmatter(raw_text)
        description = frontmatter.get("description")
        description_str = str(description).strip() if description else None
        if description_str == "":
            description_str = None
        memory_type = _coerce_memory_type(frontmatter.get("type"))

        headers.append(
            TopicHeader(
                filename=str(path.relative_to(topics_dir)),
                file_path=path,
                mtime_ms=int(stat.st_mtime * 1000),
                description=description_str,
                memory_type=memory_type,
            )
        )

    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers


def format_manifest(headers: list[TopicHeader]) -> str:
    """Render headers as ``- [type] filename (ISO-timestamp): description`` lines.

    Mirrors claude-code ``formatMemoryManifest`` (memoryScan.ts:84-94).
    Returns an empty string when ``headers`` is empty so callers can
    distinguish "no topics yet" from "topics present but no descriptions".
    """
    lines: list[str] = []
    for h in headers:
        tag = f"[{h.memory_type}] " if h.memory_type else ""
        # ``fromtimestamp`` + ``isoformat`` mirrors ``new Date(mtimeMs).toISOString()``
        # — UTC, millisecond precision, ``Z`` suffix.
        dt = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


def _format_file_size(n: int) -> str:
    """Render bytes as ``25.0KB`` style — matches claude-code ``formatFileSize`` output shape."""
    if n < 1024:
        return f"{n}B"
    kb = n / 1024.0
    if kb < 1024:
        return f"{kb:.1f}KB"
    mb = kb / 1024.0
    return f"{mb:.1f}MB"


def truncate_index(content: str) -> tuple[str, bool, bool]:
    """Apply the 200-line / 25 KB caps to MEMORY.md, append a warning when truncated.

    Mirrors claude-code ``truncateEntrypointContent`` (memdir.ts:57). Line
    cap fires first (natural boundary), then byte cap trims to the last
    newline before the limit so we never cut a line in half.

    Returns ``(content, was_line_truncated, was_byte_truncated)`` where the
    booleans report which cap (if any) tripped. The warning sentence
    distinguishes the three cases (lines-only / bytes-only / both) so the
    operator can fix the right thing.
    """
    trimmed = content.strip()
    if not trimmed:
        return "", False, False

    lines = trimmed.split("\n")
    line_count = len(lines)
    byte_count = len(trimmed.encode("utf-8"))

    was_line_truncated = line_count > MAX_INDEX_LINES
    # Check original byte count — long lines are the failure mode the byte
    # cap targets, so post-line-truncation size would understate the warning.
    was_byte_truncated = byte_count > MAX_INDEX_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return trimmed, False, False

    truncated = "\n".join(lines[:MAX_INDEX_LINES]) if was_line_truncated else trimmed
    truncated_bytes = truncated.encode("utf-8")
    if len(truncated_bytes) > MAX_INDEX_BYTES:
        # Find the last newline at or before MAX_INDEX_BYTES (byte offset).
        cut_at = truncated_bytes.rfind(b"\n", 0, MAX_INDEX_BYTES)
        if cut_at > 0:
            truncated = truncated_bytes[:cut_at].decode("utf-8", errors="ignore")
        else:
            truncated = truncated_bytes[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore")

    if was_byte_truncated and not was_line_truncated:
        reason = (
            f"{_format_file_size(byte_count)} (limit: {_format_file_size(MAX_INDEX_BYTES)}) "
            "— index entries are too long"
        )
    elif was_line_truncated and not was_byte_truncated:
        reason = f"{line_count} lines (limit: {MAX_INDEX_LINES})"
    else:
        reason = f"{line_count} lines and {_format_file_size(byte_count)}"

    warning = (
        f"\n\n> WARNING: {INDEX_FILE} is {reason}. Only part of it was loaded. "
        "Keep index entries to one line under ~200 chars; move detail into topic files."
    )
    return truncated + warning, was_line_truncated, was_byte_truncated


def is_topic_write_path(workspace: Path, rel_path: str) -> bool:
    """Return True iff ``rel_path`` is a legal extractor write target.

    Legal: ``memory/MEMORY.md`` (the index) or anything under ``memory/topics/``
    that ends with ``.md``. Everything else (daily files, arbitrary memory
    siblings, paths escaping the workspace) is rejected.

    Inputs:
        workspace: Absolute workspace dir (used to resolve traversal).
        rel_path: Path string the model passes to ``write_file`` — may be
            relative to workspace or absolute. Backslashes normalised to ``/``.
    """
    if not rel_path:
        return False

    candidate = Path(rel_path)
    if candidate.is_absolute():
        abs_path = candidate.resolve()
    else:
        abs_path = (workspace / candidate).resolve()

    workspace_resolved = workspace.resolve()
    try:
        rel = abs_path.relative_to(workspace_resolved)
    except ValueError:
        return False

    parts = rel.parts
    if not parts or parts[0] != "memory":
        return False

    # ``memory/MEMORY.md``
    if len(parts) == 2 and parts[1] == INDEX_FILE:
        return True

    # ``memory/topics/...*.md``
    if len(parts) >= 3 and parts[1] == TOPIC_DIR and parts[-1].endswith(".md"):
        return True

    return False
