"""Tests for the three SlashRenderer implementations.

These guard the rendering contract so handlers in ``gateway/slash.py`` keep
working when output goes through any of the frontends:

- RichRenderer  → TUI Console
- MarkdownRenderer → WebUI GFM
- PlainRenderer → WeChat / LLM tool
"""

from __future__ import annotations

import io

from rich.console import Console

from nano_openclaw.gateway.slash_renderer import (
    MarkdownRenderer,
    PlainRenderer,
    RichRenderer,
)


def test_rich_renderer_writes_to_console():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, no_color=True)
    r = RichRenderer(console)
    r.heading("Models")
    r.success("ok")
    r.error("nope")
    r.table(["A", "B"], [["1", "2"], ["3", "4"]], title="T", current_row_idx=0)
    out = buf.getvalue()
    assert "Models" in out
    assert "ok" in out
    assert "nope" in out
    assert "T" in out  # table title
    assert "A" in out and "B" in out
    # collect() returns "" for the Rich path — output already on the Console.
    assert r.collect() == ""


def test_markdown_renderer_emits_gfm_table_with_current_marker():
    r = MarkdownRenderer()
    r.heading("Models")
    r.table(
        ["Ref", "Name"],
        [["a/b", "Beta"], ["a/c", "Gamma"]],
        title="Models",
        current_row_idx=1,
    )
    r.success("model set to Gamma")
    out = r.collect()
    # GFM table separator pipes
    assert "| Ref | Name |" in out
    assert "| --- | --- |" in out
    # Non-current row plain
    assert "| a/b | Beta |" in out
    # Current row bolded with ← current suffix on last cell
    assert "**a/c**" in out and "← current" in out
    # Success rendered with check
    assert "✅" in out
    # Heading with ##
    assert "## Models" in out


def test_plain_renderer_table_uses_rich_layout_without_color():
    """PlainRenderer reuses Rich's Table laid out via a no-color StringIO
    Console — that gives ASCII alignment for free. Verify the current-row
    marker (``* ``) lands on the right row and color/markup is stripped."""
    r = PlainRenderer(emoji=False, width=60)
    r.table(
        ["Ref", "Name"],
        [["a/b", "Beta"], ["a/c", "Gamma"]],
        title="Models",
        current_row_idx=0,
    )
    r.success("done")
    out = r.collect()
    # Rich-rendered ASCII table: contains both refs and the title.
    assert "Models" in out
    assert "a/b" in out and "a/c" in out
    # ``* `` prefix marks the current row (first one in this test).
    lines = out.splitlines()
    star_line = next(line for line in lines if "* a/b" in line)
    assert "Beta" in star_line
    # Non-emoji success prefix
    assert "[ok] done" in out


def test_plain_renderer_truncates_long_output():
    """``max_chars`` clips the buffer so a giant /skills listing doesn't blow
    up a WeChat send."""
    r = PlainRenderer(emoji=False, max_chars=50)
    for i in range(50):
        r.text(f"line {i:03d}")
    out = r.collect()
    assert len(out) <= 50 + len("\n… (truncated)")
    assert out.endswith("(truncated)")


def test_markdown_renderer_strips_rich_markup_input():
    """Handlers may pass Rich markup (e.g. '[cyan]foo[/]') for visual hints;
    non-Rich renderers must strip cleanly without leaking square brackets.
    """
    r = MarkdownRenderer()
    r.text("[cyan]hello[/] world")
    out = r.collect()
    assert "hello world" in out
    assert "[cyan]" not in out
