"""Slash-command renderers — three implementations of one Protocol.

Why this exists: ``gateway/slash.py`` used to hardcode Rich Console output,
which only suited the TUI. WebUI and WeChat each grew their own slash
dispatcher with bespoke Markdown / plain-text rendering, drifting apart over
time. This module gives ``handle_slash`` a single render abstraction so the
same ``_cmd_*`` body can produce TUI Rich, WebUI Markdown, and WeChat / LLM
plain text — three frontends, one source of truth.

Three renderers:

- ``RichRenderer`` — TUI. Wraps an existing ``rich.console.Console`` and
  forwards directly. ``collect()`` returns ``""`` because the Console has
  already drawn to the terminal.
- ``MarkdownRenderer`` — WebUI. Writes GitHub-Flavored Markdown to an
  internal buffer (tables → ``| h | ... |`` rows, panels → blockquotes,
  ``success`` → ``✅`` etc.). ``collect()`` returns the buffer.
- ``PlainRenderer`` — WeChat / LLM tool. Reuses Rich's own table
  rendering but routes the output through a no-color StringIO Console, so
  every existing Rich-styled command degrades to readable ASCII without
  hand-writing a fallback layout.

All renderers accept Rich markup in input strings; non-Rich renderers strip
markup via ``Text.from_markup(s).plain`` so callers don't need to know which
mode they're feeding.
"""

from __future__ import annotations

import io
from typing import Protocol

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _strip_markup(s: str) -> str:
    """Render a Rich markup string down to plain text. Empty / None tolerated."""
    if not s:
        return ""
    try:
        return Text.from_markup(s).plain
    except Exception:  # noqa: BLE001
        return s


# ────────────────────────────────────────────────────────────────────────────
# Protocol
# ────────────────────────────────────────────────────────────────────────────


class SlashRenderer(Protocol):
    """Minimum render surface for slash command handlers.

    Methods accept Rich markup; renderers that don't support markup degrade
    gracefully. ``collect()`` returns the accumulated output for renderers
    that buffer (Markdown / Plain); the Rich renderer returns ``""`` because
    output goes straight to its Console.
    """

    def text(self, s: str) -> None: ...
    def dim(self, s: str) -> None: ...
    def success(self, s: str) -> None: ...
    def warning(self, s: str) -> None: ...
    def error(self, s: str) -> None: ...
    def heading(self, title: str) -> None: ...
    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        *,
        title: str | None = None,
        current_row_idx: int | None = None,
    ) -> None: ...
    def panel(
        self,
        body: str,
        *,
        title: str | None = None,
        style: str = "info",
    ) -> None: ...
    def collect(self) -> str: ...


# ────────────────────────────────────────────────────────────────────────────
# Rich (TUI)
# ────────────────────────────────────────────────────────────────────────────


class RichRenderer:
    """Renderer for the embedded TUI; forwards everything to a ``rich.Console``."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def text(self, s: str) -> None:
        self.console.print(s)

    def dim(self, s: str) -> None:
        self.console.print(f"[dim]{s}[/]")

    def success(self, s: str) -> None:
        self.console.print(f"[green]{s}[/]")

    def warning(self, s: str) -> None:
        self.console.print(f"[yellow]{s}[/]")

    def error(self, s: str) -> None:
        self.console.print(f"[red]{s}[/]")

    def heading(self, title: str) -> None:
        self.console.print(f"[bold cyan]{title}[/]")

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        *,
        title: str | None = None,
        current_row_idx: int | None = None,
    ) -> None:
        table = Table(title=title, border_style="cyan") if title else Table(border_style="cyan")
        for h in headers:
            table.add_column(h)
        for idx, row in enumerate(rows):
            cells = list(row)
            if current_row_idx is not None and idx == current_row_idx:
                cells = [f"[bold green]{c}[/]" for c in cells]
            table.add_row(*cells)
        self.console.print(table)

    def panel(
        self,
        body: str,
        *,
        title: str | None = None,
        style: str = "info",
    ) -> None:
        border = {"info": "cyan", "success": "green", "warning": "yellow", "error": "red"}.get(style, "cyan")
        self.console.print(Panel.fit(Text.from_markup(body), title=title, border_style=border))

    def collect(self) -> str:
        return ""


# ────────────────────────────────────────────────────────────────────────────
# Markdown (WebUI)
# ────────────────────────────────────────────────────────────────────────────


class MarkdownRenderer:
    """Renderer for the WebUI; emits GitHub-Flavored Markdown to a buffer."""

    def __init__(self) -> None:
        self._buf: list[str] = []

    def _push(self, s: str) -> None:
        self._buf.append(s)

    def text(self, s: str) -> None:
        self._push(_strip_markup(s))

    def dim(self, s: str) -> None:
        # Markdown has no native "dim", but italics carry the right semantics
        # (secondary information).
        plain = _strip_markup(s).strip()
        if plain:
            self._push(f"_{plain}_")
        else:
            self._push("")

    def success(self, s: str) -> None:
        self._push(f"✅ {_strip_markup(s)}")

    def warning(self, s: str) -> None:
        self._push(f"⚠️ {_strip_markup(s)}")

    def error(self, s: str) -> None:
        self._push(f"❌ {_strip_markup(s)}")

    def heading(self, title: str) -> None:
        self._push(f"## {_strip_markup(title)}")

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        *,
        title: str | None = None,
        current_row_idx: int | None = None,
    ) -> None:
        if title:
            self.heading(title)
        if not headers:
            return
        head = "| " + " | ".join(_strip_markup(h) for h in headers) + " |"
        sep = "| " + " | ".join("---" for _ in headers) + " |"
        lines = [head, sep]
        for idx, row in enumerate(rows):
            cells = [_strip_markup(c) for c in row]
            if current_row_idx is not None and idx == current_row_idx:
                cells = [f"**{c}**" for c in cells]
                if cells:
                    cells[-1] += " ← current"
            lines.append("| " + " | ".join(cells) + " |")
        self._push("\n".join(lines))

    def panel(
        self,
        body: str,
        *,
        title: str | None = None,
        style: str = "info",
    ) -> None:
        if title:
            self._push(f"### {_strip_markup(title)}")
        plain_body = _strip_markup(body)
        # Render as a blockquote so it reads as a callout.
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in plain_body.splitlines())
        self._push(quoted or "> (empty)")

    def collect(self) -> str:
        return "\n\n".join(part for part in self._buf if part != "")


# ────────────────────────────────────────────────────────────────────────────
# Plain (WeChat / LLM tool)
# ────────────────────────────────────────────────────────────────────────────


class PlainRenderer:
    """Plain-text renderer with optional emoji prefixes.

    Tables are rendered through Rich's Table layout into an in-memory
    no-color Console — that gives readable ASCII output without
    re-implementing column alignment by hand. Other primitives are simple
    string concatenations.

    ``max_chars``: WeChat practical message limit; if the buffered output
    exceeds it on ``collect()``, the result is truncated with a marker so a
    very long ``/skills`` listing doesn't blow up the wechat send.
    """

    def __init__(self, *, emoji: bool = True, max_chars: int = 0, width: int = 100) -> None:
        self._buf: list[str] = []
        self._emoji = emoji
        self._max_chars = max_chars
        self._width = width

    def _push(self, s: str) -> None:
        self._buf.append(s)

    def _render_via_rich(self, renderable) -> str:
        sio = io.StringIO()
        console = Console(
            file=sio,
            no_color=True,
            force_terminal=False,
            width=self._width,
            highlight=False,
        )
        console.print(renderable)
        return sio.getvalue().rstrip()

    def text(self, s: str) -> None:
        self._push(_strip_markup(s))

    def dim(self, s: str) -> None:
        self._push(_strip_markup(s))

    def success(self, s: str) -> None:
        prefix = "✅ " if self._emoji else "[ok] "
        self._push(prefix + _strip_markup(s))

    def warning(self, s: str) -> None:
        prefix = "⚠️ " if self._emoji else "[!] "
        self._push(prefix + _strip_markup(s))

    def error(self, s: str) -> None:
        prefix = "❌ " if self._emoji else "[x] "
        self._push(prefix + _strip_markup(s))

    def heading(self, title: str) -> None:
        prefix = "📋 " if self._emoji else ""
        self._push(f"{prefix}{_strip_markup(title)}")

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        *,
        title: str | None = None,
        current_row_idx: int | None = None,
    ) -> None:
        # WeChat (and similar plain-text channels) collapse consecutive
        # whitespace, which destroys ASCII column alignment from Rich's
        # Table renderer. Emit one line per row instead — separator chars
        # survive whitespace collapse so the structure stays readable on
        # wechat / dingtalk / sms.
        lines: list[str] = []
        if title:
            lines.append(_strip_markup(title))
        sep = " — "
        for idx, row in enumerate(rows):
            cells = [_strip_markup(c) for c in row]
            if not cells:
                continue
            mark = "* " if (current_row_idx is not None and idx == current_row_idx) else "- "
            if len(cells) == 1:
                lines.append(f"{mark}{cells[0]}")
            else:
                # First cell is the "label" (e.g. command name, model ref);
                # the rest collapse into a value joined by " | ".
                head = cells[0]
                tail = " | ".join(c for c in cells[1:] if c)
                lines.append(f"{mark}{head}{sep}{tail}" if tail else f"{mark}{head}")
        self._push("\n".join(lines))

    def panel(
        self,
        body: str,
        *,
        title: str | None = None,
        style: str = "info",
    ) -> None:
        # Multi-line bodies need a leading non-whitespace marker per line
        # because WeChat collapses \n in some chat surfaces. Without the
        # marker a panel like /model's "Current Model" comes through as one
        # mangled paragraph. We also collapse runs of internal whitespace
        # (the original ``model_ref:    foo`` padding is decorative for TTY
        # alignment — meaningless once wrapped to a single space).
        title_emoji = "📋 " if self._emoji else ""
        bullet = "🔹 " if self._emoji else "- "
        if title:
            self._push(f"{title_emoji}{_strip_markup(title)}")
        plain = _strip_markup(body)
        lines: list[str] = []
        for raw in plain.splitlines():
            stripped = " ".join(raw.split())
            if not stripped:
                continue
            if stripped.startswith(("- ", "* ", "🔹", "•")):
                lines.append(stripped)
            else:
                lines.append(f"{bullet}{stripped}")
        self._push("\n".join(lines) if lines else plain)

    def collect(self) -> str:
        out = "\n".join(part for part in self._buf if part != "")
        if self._max_chars and len(out) > self._max_chars:
            out = out[: self._max_chars] + "\n… (truncated)"
        return out
