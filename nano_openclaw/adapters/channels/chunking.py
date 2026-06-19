"""Length-aware outbound message chunking with markdown-fence protection.

Used by channel adapters (WeChat today; future Telegram/Discord/Slack) to split
LLM output into segments each <= ``limit`` characters. When a split point would
land inside a ``` / ~~~ fenced code block, the current segment is closed with a
matching fence and the next segment re-opens with the original opening line so
both sides render as valid markdown.

Splitting priority within the limit window: newline > any whitespace > hard cut.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

DEFAULT_TEXT_CHUNK_LIMIT = 4000

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_CLOSING_FENCE_TRAILING_RE = re.compile(r"^\s*$")


@dataclass(frozen=True)
class FenceSpan:
    start: int
    end: int
    open_line: str
    marker: str
    indent: str


def parse_fence_spans(text: str) -> list[FenceSpan]:
    """Single-pass line scan recording every fenced block.

    Nesting is not honored: once a fence opens with marker char/length M, only a
    line whose marker char matches and length >= M closes it. An unclosed fence
    at EOF is closed at the last character of ``text``.
    """

    spans: list[FenceSpan] = []
    pos = 0
    open_state: tuple[int, str, str, str] | None = None
    n = len(text)

    while pos < n:
        nl = text.find("\n", pos)
        line_end = n if nl == -1 else nl
        line = text[pos:line_end]
        match = _FENCE_RE.match(line)

        if match is not None:
            indent, marker, trailing = match.group(1), match.group(2), match.group(3)
            marker_char = marker[0]
            marker_len = len(marker)

            if open_state is None:
                open_state = (pos, line, marker_char + str(marker_len), indent)
            else:
                open_start, open_line, open_marker_key, _open_indent = open_state
                open_char = open_marker_key[0]
                open_len = int(open_marker_key[1:])
                is_closing = (
                    marker_char == open_char
                    and marker_len >= open_len
                    and _CLOSING_FENCE_TRAILING_RE.match(trailing) is not None
                )
                if is_closing:
                    spans.append(
                        FenceSpan(
                            start=open_start,
                            end=line_end - 1 if line_end > open_start else open_start,
                            open_line=open_line,
                            marker=open_char * open_len,
                            indent=_open_indent,
                        )
                    )
                    open_state = None

        pos = line_end + 1 if nl != -1 else n

    if open_state is not None:
        open_start, open_line, open_marker_key, open_indent = open_state
        open_char = open_marker_key[0]
        open_len = int(open_marker_key[1:])
        spans.append(
            FenceSpan(
                start=open_start,
                end=max(open_start, n - 1),
                open_line=open_line,
                marker=open_char * open_len,
                indent=open_indent,
            )
        )

    return spans


def find_fence_span_at(spans: list[FenceSpan], idx: int) -> FenceSpan | None:
    """Binary search over disjoint spans (sorted by ``start``)."""
    if not spans:
        return None
    starts = [s.start for s in spans]
    i = bisect_right(starts, idx) - 1
    if i < 0:
        return None
    span = spans[i]
    if span.start <= idx <= span.end:
        return span
    return None


def find_safe_break(
    text: str,
    start: int,
    end: int,
    spans: list[FenceSpan],
) -> int:
    """Return a break index in (start, end] preferring newlines outside fences,
    then any whitespace outside fences, else ``end`` (hard cut).
    """
    if end <= start:
        return end

    last_ws = -1
    for i in range(end - 1, start, -1):
        ch = text[i]
        if not ch.isspace():
            continue
        if find_fence_span_at(spans, i) is not None:
            continue
        if ch == "\n":
            return i + 1
        if last_ws == -1:
            last_ws = i

    if last_ws != -1:
        return last_ws + 1
    return end


def chunk_text(text: str, limit: int = DEFAULT_TEXT_CHUNK_LIMIT) -> list[str]:
    """Split ``text`` into segments each at most ``limit`` characters.

    Single-segment shortcut: if ``len(text) <= limit`` return ``[text]`` as-is.
    When a split point falls inside a fenced block, close the current segment
    with the fence marker and prepend the original opening line to the next
    segment so each segment is independently well-formed markdown.

    The original text is preserved across split boundaries. Fence close/reopen
    markers are the only synthetic content inserted.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    spans = parse_fence_spans(text)
    chunks: list[str] = []
    pos = 0
    pending_reopen: str | None = None

    while pos < len(text):
        # When the previous segment closed a fence, this segment must re-open
        # it; that re-open line plus its trailing "\n" eats into the budget.
        prefix = (pending_reopen + "\n") if pending_reopen else ""
        budget = limit - len(prefix)
        if budget <= 0:
            raise ValueError("limit too small to fit markdown fence reopen")

        remaining = len(text) - pos
        if remaining <= budget:
            chunks.append(prefix + text[pos:])
            break

        break_idx = find_safe_break(text, pos, pos + budget, spans)

        # Only the character immediately before the break determines whether we
        # cut inside a fence; ``break_idx`` itself may be exactly ``fence.end+1``
        # (a clean exit) and that should not trigger a reopen.
        fence = find_fence_span_at(spans, break_idx - 1)
        if fence is not None and break_idx - 1 <= fence.end:
            # Closing overhead ("\n" + marker) also has to fit inside the
            # limit, so retry the break search with a tightened budget.
            close_overhead = 1 + len(fence.marker)
            tight_budget = budget - close_overhead
            if tight_budget <= 0:
                raise ValueError("limit too small to fit markdown fence close")
            break_idx = find_safe_break(text, pos, pos + tight_budget, spans)
            segment = text[pos:break_idx]
            segment += "\n" + fence.marker
            pending_reopen = fence.open_line
        else:
            segment = text[pos:break_idx]
            pending_reopen = None

        chunks.append(prefix + segment)

        pos = break_idx

    return chunks
