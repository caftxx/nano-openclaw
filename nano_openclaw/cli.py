"""Rich-based REPL and tool-call rendering.

Mirrors `src/cli/tui-cli.ts:8-63` -> `src/tui/tui.ts:1-52` (REPL shell)
and `src/tui/components/tool-execution.ts:55-137` (tool panels).
Production OpenClaw uses pi-tui — a custom React-like terminal lib.
nano uses ``rich``: simpler, less to learn, same visual idea.

Slash commands: ``/quit``, ``/clear``, ``/help``, ``/context``, ``/compact``. No multiline editor.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from anthropic import Anthropic
from rich import markup
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from nano_openclaw.compact import compact_if_needed, estimate_tokens
from nano_openclaw.loop import LoopConfig, Message, agent_loop
from nano_openclaw.provider import (
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseStart,
)
from nano_openclaw.tools import ToolRegistry

_PREVIEW_LINES = 12


def repl(registry: ToolRegistry, *, client: Anthropic, cfg: LoopConfig) -> None:
    """Interactive read-eval-print loop. Blocks until /quit or Ctrl-D."""
    console = Console()
    history: list[Message] = []

    _print_banner(console, cfg.model, registry)

    while True:
        try:
            user_input = console.input("[bold cyan]>>>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return

        if not user_input:
            continue
        if user_input in {"/quit", "/exit", "/q"}:
            console.print("[dim]bye.[/]")
            return
        if user_input == "/clear":
            history.clear()
            console.print("[dim](history cleared)[/]")
            continue
        if user_input == "/help":
            console.print(
                "[dim]commands: /quit, /clear, /help, /context, /compact — anything else is sent to the model[/]"
            )
            continue
        if user_input == "/context":
            _show_context(console, history, cfg)
            continue
        if user_input == "/compact":
            _manual_compact(console, history, cfg, client)
            continue

        on_event = _make_event_handler(console)
        try:
            agent_loop(
                user_input=user_input,
                history=history,
                registry=registry,
                on_event=on_event,
                client=client,
                cfg=cfg,
            )
        except Exception as exc:  # noqa: BLE001 — surface model/network errors to user
            console.print(f"\n[red]error:[/] {type(exc).__name__}: {markup.escape(str(exc))}")
            continue
        console.print()  # blank line between turns


def _print_banner(console: Console, model: str, registry: ToolRegistry) -> None:
    tools = ", ".join(registry.names()) or "(none)"
    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold]nano-openclaw[/]\n"
                f"model:  [cyan]{markup.escape(model)}[/]\n"
                f"tools:  {markup.escape(tools)}\n"
                f"commands: /quit  /clear  /help  /context  /compact"
            ),
            border_style="cyan",
        )
    )


def _manual_compact(
    console: Console,
    history: list[Message],
    cfg: LoopConfig,
    client: Any,
) -> None:
    """Manually trigger context compaction."""
    if len(history) < cfg.context_recent_turns * 2:
        console.print("[dim](not enough history to compact)[/]")
        return

    console.print("[dim]compacting context...[/]")

    try:
        _, summary = compact_if_needed(
            history,
            budget=1,  # Force compaction by setting very low budget
            client=client,
            model=cfg.model,
            api=cfg.api,
            threshold_ratio=1.0,  # Trigger immediately
            recent_turns=cfg.context_recent_turns,
        )

        if summary:
            _render_compaction(console, summary=summary)
            # Show updated context stats
            current_tokens = estimate_tokens(history)
            console.print(f"[dim]context reduced to {current_tokens:,} tokens ({len(history)} messages)[/]")
        else:
            console.print("[dim](compaction not needed — history too short)[/]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]error:[/] {type(exc).__name__}: {markup.escape(str(exc))}")


def _show_context(console: Console, history: list[Message], cfg: LoopConfig) -> None:
    """Display current context window usage."""
    current_tokens = estimate_tokens(history)
    budget = cfg.context_budget
    threshold = int(budget * cfg.context_threshold)
    usage_pct = (current_tokens / budget) * 100 if budget > 0 else 0

    # Color based on usage level
    if usage_pct < 50:
        color = "green"
    elif usage_pct < cfg.context_threshold * 100:
        color = "yellow"
    else:
        color = "red"

    console.print(
        Panel.fit(
            Text.from_markup(
                f"context usage: [{color}]{current_tokens:,}[/] / {budget:,} tokens\n"
                f"usage: [{color}]{usage_pct:.1f}%[/]\n"
                f"threshold: {threshold:,} tokens ({cfg.context_threshold * 100:.0f}%)\n"
                f"messages: {len(history)}"
            ),
            title="Context Status",
            border_style=color,
        )
    )


def _make_event_handler(console: Console) -> Callable[[Any], None]:
    """Return a per-turn callback that renders streaming events live.

    Strategy: print text deltas inline (no Live overlay), draw a clear
    header line when a tool_use begins, and render the completed tool
    call as a Panel after dispatch (via the ``("ToolResult", ...)`` tuple
    emitted by ``loop.agent_loop``).
    """
    state = {"text_in_flight": False}

    def handle(event: Any) -> None:
        if isinstance(event, TextDelta):
            if not state["text_in_flight"]:
                console.print()  # gap before assistant text
                state["text_in_flight"] = True
            console.print(event.text, end="", soft_wrap=True, highlight=False)

        elif isinstance(event, ToolUseStart):
            if state["text_in_flight"]:
                console.print()  # finish text line
                state["text_in_flight"] = False
            console.print(f"\n[bold yellow]>> {markup.escape(event.name)}[/]", end="")

        elif isinstance(event, ToolUseEnd):
            console.print()  # newline after tool_use header

        elif isinstance(event, MessageEnd):
            if state["text_in_flight"]:
                console.print()
                state["text_in_flight"] = False

        elif isinstance(event, tuple) and event and event[0] == "ToolResult":
            _, name, args, result = event
            _render_tool_result(console, name=name, args=args, result=result)

        elif isinstance(event, tuple) and event and event[0] == "Compaction":
            _, summary = event
            _render_compaction(console, summary=summary)

    return handle


def _render_compaction(console: Console, *, summary: str) -> None:
    """Render a compaction notification showing the conversation was summarized."""
    # Truncate summary for display
    lines = summary.splitlines() or [""]
    if len(lines) > _PREVIEW_LINES:
        escaped_content = markup.escape("\n".join(lines[:_PREVIEW_LINES]))
        body = escaped_content + f"\n[dim](... +{len(lines) - _PREVIEW_LINES} more lines)[/]"
    else:
        body = markup.escape("\n".join(lines))

    console.print(
        Panel(
            Text.from_markup(body),
            title=Text.from_markup("[yellow]Context Compacted[/]"),
            title_align="left",
            border_style="yellow",
        )
    )


def _render_tool_result(
    console: Console,
    *,
    name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    is_error = bool(result.get("is_error"))
    border = "red" if is_error else "green"
    text_block = result["content"][0]["text"] if result.get("content") else ""

    lines = text_block.splitlines() or [""]
    if len(lines) > _PREVIEW_LINES:
        # Escape the content, then add markup for the truncation notice
        escaped_content = markup.escape("\n".join(lines[:_PREVIEW_LINES]))
        body = escaped_content + f"\n[dim](... +{len(lines) - _PREVIEW_LINES} more lines)[/]"
    else:
        body = markup.escape("\n".join(lines))

    args_repr = _short_args(args)
    title = f"{markup.escape(name)}({markup.escape(args_repr)})"
    if is_error:
        title = f"{title} [red](error)[/]"

    console.print(
        Panel(
            Text.from_markup(body),
            title=Text.from_markup(title),
            title_align="left",
            border_style=border,
        )
    )


def _short_args(args: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(args, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        rendered = str(args)
    if len(rendered) > 80:
        rendered = rendered[:77] + "..."
    return rendered
