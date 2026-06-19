"""Pure helpers for loading the workspace memory index."""

from __future__ import annotations

INDEX_FILE = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000


def _format_file_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    kb = n / 1024.0
    if kb < 1024:
        return f"{kb:.1f}KB"
    mb = kb / 1024.0
    return f"{mb:.1f}MB"


def truncate_index(content: str) -> tuple[str, bool, bool]:
    """Apply the 200-line / 25 KB caps to MEMORY.md."""
    trimmed = content.strip()
    if not trimmed:
        return "", False, False

    lines = trimmed.split("\n")
    line_count = len(lines)
    byte_count = len(trimmed.encode("utf-8"))

    was_line_truncated = line_count > MAX_INDEX_LINES
    was_byte_truncated = byte_count > MAX_INDEX_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return trimmed, False, False

    truncated = "\n".join(lines[:MAX_INDEX_LINES]) if was_line_truncated else trimmed
    truncated_bytes = truncated.encode("utf-8")
    if len(truncated_bytes) > MAX_INDEX_BYTES:
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
