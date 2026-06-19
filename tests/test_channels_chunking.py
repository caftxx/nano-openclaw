"""Unit tests for ``channels.chunking.chunk_text``.

Each test exercises one branch of the algorithm in isolation; the function is
pure so no fixtures or mocks are required.
"""

from __future__ import annotations

from nano_openclaw.adapters.channels.chunking import (
    DEFAULT_TEXT_CHUNK_LIMIT,
    chunk_text,
    parse_fence_spans,
)


def test_short_text_returned_as_single_segment() -> None:
    assert chunk_text("hello", limit=100) == ["hello"]


def test_default_limit_short_text() -> None:
    text = "x" * (DEFAULT_TEXT_CHUNK_LIMIT - 1)
    assert chunk_text(text) == [text]


def test_long_whitespace_text_split_on_spaces() -> None:
    text = "a " * 2500  # 5000 chars, alternating "a "
    chunks = chunk_text(text, limit=DEFAULT_TEXT_CHUNK_LIMIT)
    assert len(chunks) >= 2
    for seg in chunks:
        assert len(seg) <= DEFAULT_TEXT_CHUNK_LIMIT
    assert "".join(chunks) == text


def test_code_fence_split_inserts_close_and_reopen() -> None:
    # Open the fence early so the only viable break inside the limit window
    # falls between two newlines that are *inside* the fence — that forces
    # the algorithm to close + reopen.
    prefix = "intro\n"
    code = "```python\n" + ("y = 1\n" * 1000) + "```\n"
    text = prefix + code
    chunks = chunk_text(text, limit=DEFAULT_TEXT_CHUNK_LIMIT)

    assert len(chunks) >= 2
    for seg in chunks:
        assert len(seg) <= DEFAULT_TEXT_CHUNK_LIMIT

    closed_at: int | None = None
    for i, seg in enumerate(chunks[:-1]):
        if seg.rstrip().endswith("```"):
            closed_at = i
            break
    assert closed_at is not None, "expected at least one fence-aware close"
    assert chunks[closed_at + 1].startswith("```python")
    assert _join_without_synthetic_fences(chunks, "```python", "```") == text


def test_code_fence_within_limit_left_intact() -> None:
    text = "intro\n```python\nx = 1\n```\noutro"
    assert chunk_text(text, limit=DEFAULT_TEXT_CHUNK_LIMIT) == [text]


def test_hard_cut_when_no_whitespace_available() -> None:
    text = "中" * 5000
    chunks = chunk_text(text, limit=DEFAULT_TEXT_CHUNK_LIMIT)
    assert len(chunks) >= 2
    for seg in chunks:
        assert len(seg) <= DEFAULT_TEXT_CHUNK_LIMIT
    assert "".join(chunks) == text


def test_newline_preferred_over_space_within_window() -> None:
    text = "a" * 3500 + " " * 100 + "\n" + "b" * 500
    chunks = chunk_text(text, limit=DEFAULT_TEXT_CHUNK_LIMIT)
    assert len(chunks) == 2
    # The first segment must end where the newline was — i.e. it must not
    # contain any "b" characters and must not contain the trailing spaces
    # that would only survive if the break point fell on the space.
    assert "b" not in chunks[0]
    # The break landed on the newline, and the original text remains intact.
    assert chunks[0].endswith("\n")
    assert "".join(chunks) == text


def test_code_fence_split_preserves_code_whitespace() -> None:
    text = "```python\n" + "".join(f"    x = {i}\n" for i in range(1000)) + "```"
    chunks = chunk_text(text, limit=120)

    for seg in chunks:
        assert len(seg) <= 120
    assert _join_without_synthetic_fences(chunks, "```python", "```") == text


def test_parse_fence_spans_detects_multiple_blocks() -> None:
    text = (
        "intro\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "middle\n"
        "~~~bash\n"
        "echo hi\n"
        "~~~\n"
        "tail\n"
    )
    spans = parse_fence_spans(text)
    assert len(spans) == 2
    assert spans[0].marker == "```"
    assert spans[0].open_line == "```python"
    assert spans[1].marker == "~~~"
    assert spans[1].open_line == "~~~bash"
    # Spans must be disjoint and ordered.
    assert spans[0].end < spans[1].start


def test_parse_fence_spans_closes_unterminated_fence_at_eof() -> None:
    text = "hi\n```python\nx = 1"
    spans = parse_fence_spans(text)
    assert len(spans) == 1
    span = spans[0]
    assert span.open_line == "```python"
    assert span.end == len(text) - 1
    # No exception, and chunk_text below the limit still returns intact.
    assert chunk_text(text, limit=DEFAULT_TEXT_CHUNK_LIMIT) == [text]


def test_parse_fence_spans_requires_closing_trailing_whitespace_only() -> None:
    text = "```markdown\n```not a close\nstill fenced\n```\nafter"
    spans = parse_fence_spans(text)

    assert len(spans) == 1
    assert spans[0].end == text.index("```\nafter") + 2


def _join_without_synthetic_fences(
    chunks: list[str],
    open_line: str,
    marker: str,
) -> str:
    restored: list[str] = []
    previous_closed_synthetically = False
    for i, chunk in enumerate(chunks):
        if previous_closed_synthetically and chunk.startswith(open_line + "\n"):
            chunk = chunk[len(open_line) + 1:]
        previous_closed_synthetically = (
            i < len(chunks) - 1 and chunk.endswith("\n" + marker)
        )
        if previous_closed_synthetically:
            chunk = chunk[: -(len(marker) + 1)]
        restored.append(chunk)
    return "".join(restored)
