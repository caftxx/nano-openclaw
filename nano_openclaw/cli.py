"""Rich-based REPL and tool-call rendering.

Mirrors `src/cli/tui-cli.ts:8-63` -> `src/tui/tui.ts:1-52` (REPL shell)
and `src/tui/components/tool-execution.ts:55-137` (tool panels).
Production OpenClaw uses pi-tui — a custom React-like terminal lib.
nano uses ``rich``: simpler, less to learn, same visual idea.

Slash commands: ``/quit``, ``/clear`` (clear history, keep session), ``/new`` (new session + new ID), ``/help``, ``/context``, ``/compact``, ``/sessions`` (interactive picker; ``/sessions all`` for plain list), ``/save``, ``/session [prefix|#]``. No multiline editor.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from rich import markup
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nano_openclaw.compact import compact_if_needed, estimate_tokens
from nano_openclaw.memory.active import ActiveMemoryConfig, QueryMode, PromptStyle
from nano_openclaw.loop import (
    ActiveMemoryRecall,
    Compaction,
    ImageAttached,
    ImageDescribe,
    ImageError,
    ImageSkip,
    LoopConfig,
    Message,
    CancellationToken,
    SkillInvoked,
    ToolResult,
    TurnCancelled,
    agent_loop,
)
from nano_openclaw.provider import (
    MessageEnd,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseStart,
)
from nano_openclaw.session import (
    TranscriptWriter,
    TranscriptReader,
    SessionInfo,
    load_session_store,
    save_session_store,
    get_last_session,
    update_session,
    list_sessions,
    new_session_id,
)
from nano_openclaw.skills import (
    filter_eligible_skills,
    filter_visible_skills,
    get_or_load_skills,
)
from nano_openclaw.tools import ToolRegistry

_PREVIEW_LINES = 12
_MAX_HISTORY_PREVIEW_TURNS = 10  # turns shown when replaying history after session switch
_COMMANDS_HELP = "/quit  /clear  /new  /help  /context  /compact  /sessions \\[all]  /save  /session \\[prefix|#]  /skills  /active-memory \\[status|on|off|mode|style]  /dreaming \\[status|on|off|run]  — /sessions launches interactive picker"


async def repl(
    registry: ToolRegistry,
    *,
    client: Any,
    cfg: LoopConfig,
    session_dir: Path | None = None,
    transcript_writer: TranscriptWriter | None = None,
    session_id: str = "",
    store_path: Path | None = None,
    initial_history: list[Message] | None = None,
) -> None:
    """Interactive read-eval-print loop. Runs until /quit or Ctrl-D."""
    console = Console()
    history: list[Message] = list(initial_history) if initial_history else []
    _load_input_history(history)

    _print_banner(console, cfg.model, registry, session_id)

    while True:
        try:
            user_input = (await _repl_input(console)).strip()
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
            if transcript_writer:
                transcript_writer.clear()
            if transcript_writer and store_path and session_id:
                _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
            console.print("[dim](history cleared)[/]")
            continue
        if user_input == "/new":
            if transcript_writer and store_path and session_id:
                _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
            history.clear()
            if store_path and session_dir:
                session_id = new_session_id()
                new_path = session_dir / f"{session_id}.jsonl"
                transcript_writer = TranscriptWriter(new_path)
                transcript_writer.start(model=cfg.model)
                store = load_session_store(store_path)
                update_session(store, session_id, model=cfg.model, message_count=0, compaction_count=0)
                save_session_store(store_path, store)
                console.print(f"[dim]new session: {session_id[:8]}…[/]")
            else:
                console.print("[dim](history cleared)[/]")
            continue
        if user_input == "/help":
            console.print(
                f"[dim]commands: {_COMMANDS_HELP} — anything else is sent to the model[/]"
            )
            continue
        if user_input == "/context":
            _show_context(console, history, cfg)
            continue
        if user_input == "/compact":
            await _manual_compact(console, history, cfg, client)
            continue
        if user_input == "/skills":
            _list_skills(console, cfg)
            continue
        if user_input.startswith("/sessions"):
            if store_path:
                if user_input.strip() == "/sessions all":
                    _list_sessions_cli(console, store_path, session_id, cfg.model, transcript_writer, session_dir, show_all=True)
                elif session_dir:
                    target_id = _interactive_session_picker(console, store_path, session_dir, session_id)
                    if target_id and target_id != session_id:
                        transcript_path = session_dir / f"{target_id}.jsonl"
                        reader = TranscriptReader(transcript_path)
                        new_history, _, msg_count, comp_count, last_msg_id = reader.load_history()
                        new_writer = TranscriptWriter.resume(transcript_path, target_id, msg_count, comp_count, last_msg_id)
                        if transcript_writer and session_id:
                            _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
                        history.clear()
                        history.extend(new_history)
                        transcript_writer = new_writer
                        session_id = target_id
                        _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
                        _load_input_history(history)
                        _replay_history(console, history, session_id)
                else:
                    _list_sessions_cli(console, store_path, session_id, cfg.model, transcript_writer, session_dir)
            else:
                console.print("[dim](no session store configured)[/]")
            continue
        if user_input == "/save":
            if transcript_writer and store_path and session_id:
                _save_session_now(console, store_path, transcript_writer, session_id, cfg.model)
            else:
                console.print("[dim](no active session to save)[/]")
            continue
        if user_input.startswith("/session"):
            parts = user_input.split(None, 1)
            if len(parts) == 1:
                if session_id:
                    console.print(f"[dim]current session: {session_id}[/]")
                else:
                    console.print("[dim](no active session)[/]")
            else:
                key = parts[1].strip()
                if store_path and session_dir:
                    if key.isdigit():
                        result = _load_session_by_index(console, store_path, session_dir, int(key))
                    else:
                        result = _load_session_by_prefix(console, store_path, session_dir, key)
                    if result:
                        new_history, new_writer, new_sid = result
                        if new_sid == session_id:
                            console.print(f"[dim]already on session {new_sid[:8]}…[/]")
                        else:
                            if transcript_writer and store_path and session_id:
                                _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
                            history.clear()
                            history.extend(new_history)
                            transcript_writer = new_writer
                            session_id = new_sid
                            _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
                            _load_input_history(history)
                            _replay_history(console, history, session_id)
                else:
                    console.print("[dim](no session store configured)[/]")
            continue
        if user_input.startswith("/active-memory"):
            _handle_active_memory_command(console, user_input, cfg)
            continue
        if user_input.startswith("/dreaming"):
            await _handle_dreaming_command(console, user_input, cfg, client)
            continue

        on_event = _make_event_handler(console)
        try:
            with _escape_cancellation_token() as cancellation_token:
                await agent_loop(
                    user_input=user_input,
                    history=history,
                    registry=registry,
                    on_event=on_event,
                    client=client,
                    cfg=cfg,
                    transcript_writer=transcript_writer,
                    cancellation_token=cancellation_token,
                )
        except TurnCancelled:
            console.print("\n[dim](turn cancelled)[/]")
            continue
        except Exception as exc:  # noqa: BLE001 — surface model/network errors to user
            console.print(f"\n[red]error:[/] {type(exc).__name__}: {markup.escape(str(exc))}")
            continue
        console.print()  # blank line between turns

        # Persist session metadata after each turn
        if transcript_writer and store_path and session_id:
            _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)


def _print_banner(console: Console, model: str, registry: ToolRegistry, session_id: str = "") -> None:
    tools = ", ".join(registry.names()) or "(none)"
    session_line = f"session: {session_id[:8]}…" if session_id else ""
    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold]nano-openclaw[/]\n"
                f"model:  [cyan]{markup.escape(model)}[/]\n"
                f"tools:  {markup.escape(tools)}"
                + (f"\n{session_line}" if session_line else "")
                + f"\ncommands: {_COMMANDS_HELP}"
            ),
            border_style="cyan",
        )
    )


async def _manual_compact(
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
        _, summary = await compact_if_needed(
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


def _replay_history(console: Console, history: list[Message], session_id: str) -> None:
    """Print a compact recap of conversation history after switching sessions."""
    if not history:
        return

    # Group into (user_msg, asst_msg) turn pairs
    turns: list[tuple[Message | None, Message | None]] = []
    i = 0
    while i < len(history):
        if history[i].role == "user":
            asst = history[i + 1] if i + 1 < len(history) and history[i + 1].role == "assistant" else None
            turns.append((history[i], asst))
            i += 2 if asst else 1
        else:
            turns.append((None, history[i]))
            i += 1

    total_turns = len(turns)
    skip = max(0, total_turns - _MAX_HISTORY_PREVIEW_TURNS)

    console.rule(
        Text.from_markup(f"[dim cyan]session [cyan]{session_id[:8]}…[/cyan]  {len(history)} messages[/]"),
        style="dim cyan",
    )

    if skip:
        console.print(f"[dim]  … {skip} earlier turn{'s' if skip > 1 else ''} not shown …[/]")

    for user_msg, asst_msg in turns[skip:]:
        if user_msg:
            text = " ".join(
                b.get("text", "").strip()
                for b in user_msg.content
                if b.get("type") == "text"
            ).strip()
            preview = markup.escape(text[:140]) + ("[dim]…[/]" if len(text) > 140 else "")
            console.print(Text.from_markup(f" [bold cyan]You[/] [dim]›[/] {preview}"))

        if asst_msg:
            text = " ".join(
                b.get("text", "").strip()
                for b in asst_msg.content
                if b.get("type") == "text"
            ).strip()
            tools = [b.get("name", "?") for b in asst_msg.content if b.get("type") == "tool_use"]
            parts: list[str] = []
            if text:
                parts.append(markup.escape(text[:200]) + ("[dim]…[/]" if len(text) > 200 else ""))
            if tools:
                parts.append(f"[dim](used {markup.escape(', '.join(tools))})[/]")
            body = "  ".join(parts) if parts else "[dim](no text)[/]"
            console.print(Text.from_markup(f"  [bold] AI[/] [dim]›[/] {body}"))

    console.rule(style="dim cyan")


def _make_event_handler(console: Console) -> Callable[[Any], None]:
    """Return a per-turn callback that renders streaming events live.

    Strategy: print text deltas inline (no Live overlay), draw a clear
    header line when a tool_use begins, and render the completed tool
    call as a Panel after dispatch (via the ``("ToolResult", ...)`` tuple
    emitted by ``loop.agent_loop``).
    """
    state = {"text_in_flight": False, "thinking_in_flight": False}

    def handle(event: Any) -> None:
        if isinstance(event, ThinkingDelta):
            if not state["thinking_in_flight"]:
                console.print()
                state["thinking_in_flight"] = True
            console.print(markup.escape(event.text), end="", soft_wrap=True, style="dim", highlight=False)
            console.file.flush()

        elif isinstance(event, ThinkingBlockComplete):
            if state["thinking_in_flight"]:
                console.print()
                state["thinking_in_flight"] = False

        elif isinstance(event, TextDelta):
            if not state["text_in_flight"]:
                console.print()  # gap before assistant text
                state["text_in_flight"] = True
            console.print(markup.escape(event.text), end="", soft_wrap=True, highlight=False)
            console.file.flush()

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

        elif isinstance(event, ToolResult):
            _render_tool_result(console, name=event.name, args=event.args, result=event.result)

        elif isinstance(event, Compaction):
            _render_compaction(console, summary=event.summary)

        elif isinstance(event, ImageDescribe):
            console.print(f"[dim]describing: {markup.escape(event.ref)}[/]")

        elif isinstance(event, ImageAttached):
            # "described" = Media Understanding path; "attached" = Native Vision path
            mode = "described" if event.via_model else "attached"
            for ref in event.refs:
                console.print(f"[dim]{mode}: {markup.escape(ref)}[/]")

        elif isinstance(event, ImageError):
            console.print(f"[red]image error:[/] {markup.escape(event.ref)}: {markup.escape(event.error)}")

        elif isinstance(event, ImageSkip):
            console.print(f"[yellow]image skipped:[/] {markup.escape(event.ref)}: {markup.escape(event.reason)}")

        elif isinstance(event, SkillInvoked):
            console.print(f"[cyan]skill invoked:[/] {markup.escape(event.skill_name)} ({markup.escape(event.skill_path)})")

        elif isinstance(event, ActiveMemoryRecall):
            result = event.result
            if result.context:
                cached_str = ", cached" if result.cached else ""
                console.print(f"[dim]Active Memory: {result.elapsed_ms}ms{cached_str}[/]")

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
    content_blocks: list[dict[str, Any]] = result.get("content") or []

    # Collect text blocks only; image blocks are not displayable as text.
    image_count = sum(1 for b in content_blocks if b.get("type") == "image")
    text_block = " ".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    if image_count and not text_block:
        text_block = f"[{image_count} image{'s' if image_count > 1 else ''} returned]"
    elif image_count:
        text_block = f"[{image_count} image{'s' if image_count > 1 else ''} + text] " + text_block

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


def _update_session_metadata(
    store_path: Path,
    session_id: str,
    transcript_writer: TranscriptWriter,
    model: str,
) -> None:
    """Update sessions.json with current session stats."""
    store = load_session_store(store_path)
    update_session(
        store,
        session_id,
        model=model,
        message_count=transcript_writer.message_count,
        compaction_count=transcript_writer.compaction_count,
    )
    save_session_store(store_path, store)


def _save_session_now(
    console: Console,
    store_path: Path,
    transcript_writer: TranscriptWriter,
    session_id: str,
    model: str,
) -> None:
    """Force-save session metadata to sessions.json."""
    _update_session_metadata(store_path, session_id, transcript_writer, model)
    console.print(f"[dim]session {session_id[:8]}… saved[/]")


def _load_input_history(messages: list[Message]) -> None:
    """Populate prompt_toolkit history from session's user messages."""
    global _pt_history
    # get_strings() returns a reversed copy, not the internal list; create a
    # fresh instance instead so the old session's history is fully replaced.
    texts: list[str] = []
    for msg in messages:
        if msg.role != "user":
            continue
        text = " ".join(
            b.get("text", "").strip()
            for b in msg.content
            if b.get("type") == "text"
        ).strip()
        if text:
            texts.append(text)
    _pt_history = _InMemoryHistory(history_strings=texts)


async def _repl_input(_console: Console) -> str:
    """Input prompt with full readline editing and history via prompt_toolkit."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _pt_prompt(">>> ", history=_pt_history))


@contextmanager
def _escape_cancellation_token() -> Iterator[CancellationToken]:
    """Listen for Esc during a turn and flip a cancellation token when pressed."""
    token = CancellationToken()

    stop_event = threading.Event()

    def _wait_if_input_paused() -> bool:
        if not token._input_pause_requested.is_set():
            return False
        token._input_pause_ack.set()
        while (
            token._input_pause_requested.is_set()
            and not stop_event.is_set()
            and not token.is_cancelled
        ):
            time.sleep(0.01)
        token._input_pause_ack.clear()
        return True

    if sys.platform == "win32":
        import msvcrt

        def watch_for_escape() -> None:
            while not stop_event.is_set() and not token.is_cancelled:
                if _wait_if_input_paused():
                    continue
                if msvcrt.kbhit():
                    if msvcrt.getwch() == "\x1b":
                        token.cancel()
                        return
                else:
                    time.sleep(0.01)

        watcher = threading.Thread(target=watch_for_escape, name="nano-openclaw-esc-watch", daemon=True)
        watcher.start()
        try:
            yield token
        finally:
            stop_event.set()
            watcher.join(timeout=0.2)
        return

    try:
        from prompt_toolkit.input import create_input
        from prompt_toolkit.keys import Keys
    except Exception:
        yield token
        return

    input_handle = create_input()

    def watch_for_escape() -> None:
        try:
            with input_handle.raw_mode():
                while not stop_event.is_set() and not token.is_cancelled:
                    if _wait_if_input_paused():
                        continue
                    for kp in input_handle.read_keys():
                        if kp.key == Keys.Escape:
                            token.cancel()
                            return
                        if stop_event.is_set() or token.is_cancelled:
                            return
                        if token._input_pause_requested.is_set():
                            break
        except Exception:
            return

    watcher = threading.Thread(target=watch_for_escape, name="nano-openclaw-esc-watch", daemon=True)
    watcher.start()
    try:
        yield token
    finally:
        stop_event.set()
        close = getattr(input_handle, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        watcher.join(timeout=0.2)




def _render_sessions_page(
    sessions: list[SessionInfo],
    snippets: dict[str, str],
    current_session_id: str | None,
    store_last_id: str | None,
    selected: int,
    page: int,
    page_size: int,
) -> Table:
    total = len(sessions)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = page * page_size
    page_sessions = sessions[start : start + page_size]

    page_info = f"page {page + 1}/{total_pages}  " if total_pages > 1 else ""
    table = Table(
        title=f"Sessions  {page_info}[dim]↑↓ select  ←→ page  Enter switch  q cancel[/]",
        border_style="cyan",
        highlight=False,
    )
    table.add_column("#", justify="right", width=4, style="dim")
    table.add_column("Session ID", width=12)
    table.add_column("Description", no_wrap=False, max_width=58)
    table.add_column("Msgs", justify="right", width=5)
    table.add_column("Last Active", width=16)

    for i, s in enumerate(page_sessions):
        abs_idx = start + i
        is_current = (current_session_id and s.session_id == current_session_id) or (
            not current_session_id and s.session_id == store_last_id
        )
        marker = " ←" if is_current else ""
        last_active = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
        snippet = snippets.get(s.session_id, "") or "(empty)"
        row_style = "bold reverse" if abs_idx == selected else ""
        table.add_row(
            str(abs_idx + 1),
            s.session_id[:8] + "…" + marker,
            snippet,
            str(s.message_count),
            last_active,
            style=row_style,
        )

    return table


def _interactive_session_picker(
    console: Console,
    store_path: Path,
    session_dir: Path | None,
    current_session_id: str | None,
) -> str | None:
    """Arrow-key session picker. Returns session_id of selected session, or None if cancelled."""
    import time
    from prompt_toolkit.input import create_input
    from prompt_toolkit.keys import Keys
    from rich.live import Live

    store = load_session_store(store_path)
    sessions = list_sessions(store)

    saved_ids = {s.session_id for s in sessions}
    if current_session_id and current_session_id not in saved_ids:
        sessions.insert(0, SessionInfo(
            session_id=current_session_id,
            created_at=time.time(),
            updated_at=time.time(),
            model="",
            message_count=0,
            compaction_count=0,
        ))

    if not sessions:
        console.print("[dim](no saved sessions)[/]")
        return None

    total = len(sessions)
    page_size = _SESSIONS_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    store_last_id = store.get("lastSessionId")

    snippets: dict[str, str] = {}
    if session_dir:
        for s in sessions:
            snippets[s.session_id] = _get_session_snippet(session_dir, s.session_id)

    selected = 0
    if current_session_id:
        for i, s in enumerate(sessions):
            if s.session_id == current_session_id:
                selected = i
                break
    page = selected // page_size

    inp = create_input()
    with inp.raw_mode(), Live(console=console, auto_refresh=False) as live:
        def refresh() -> None:
            live.update(_render_sessions_page(
                sessions, snippets, current_session_id,
                store_last_id, selected, page, page_size,
            ))
            live.refresh()

        refresh()
        while True:
            for kp in inp.read_keys():
                key = kp.key
                if key == Keys.Up:
                    if selected > 0:
                        selected -= 1
                        page = selected // page_size
                elif key == Keys.Down:
                    if selected < total - 1:
                        selected += 1
                        page = selected // page_size
                elif key == Keys.Left:
                    if page > 0:
                        page -= 1
                        selected = page * page_size
                elif key == Keys.Right:
                    if page < total_pages - 1:
                        page += 1
                        selected = page * page_size
                elif key in (Keys.ControlM, Keys.ControlJ):  # Enter
                    return sessions[selected].session_id
                elif key in ("q", "Q", Keys.Escape, Keys.ControlC):
                    return None
            refresh()


def _get_session_snippet(session_dir: Path, session_id: str, max_chars: int = 60) -> str:
    """Return the first user text from a session transcript, truncated."""
    path = session_dir / f"{session_id}.jsonl"
    if not path.exists():
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "message" and entry.get("role") == "user":
                    for block in entry.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                return text[:max_chars] + ("…" if len(text) > max_chars else "")
    except OSError:
        pass
    return ""


_SESSIONS_PAGE_SIZE = 20

from prompt_toolkit import prompt as _pt_prompt
from prompt_toolkit.history import InMemoryHistory as _InMemoryHistory
_pt_history: _InMemoryHistory = _InMemoryHistory()


def _list_sessions_cli(
    console: Console,
    store_path: Path,
    current_session_id: str | None = None,
    current_model: str = "",
    transcript_writer: TranscriptWriter | None = None,
    session_dir: Path | None = None,
    show_all: bool = False,
) -> None:
    """Display available sessions in a numbered table with descriptions."""
    import time

    store = load_session_store(store_path)
    sessions = list_sessions(store)

    saved_ids = {s.session_id for s in sessions}
    if current_session_id and current_session_id not in saved_ids:
        sessions.insert(0, SessionInfo(
            session_id=current_session_id,
            created_at=time.time(),
            updated_at=time.time(),
            model=current_model,
            message_count=transcript_writer.message_count if transcript_writer else 0,
            compaction_count=transcript_writer.compaction_count if transcript_writer else 0,
        ))

    if not sessions:
        console.print("[dim](no saved sessions)[/]")
        return

    total = len(sessions)
    visible = sessions if show_all else sessions[:_SESSIONS_PAGE_SIZE]

    table = Table(title="Saved Sessions", border_style="cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Session ID", style="cyan")
    table.add_column("Description", style="white", no_wrap=False, max_width=62)
    table.add_column("Messages", justify="right")
    table.add_column("Last Active", style="dim")

    for idx, s in enumerate(visible, start=1):
        last_active = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
        is_current = (current_session_id and s.session_id == current_session_id) or (
            not current_session_id and s.session_id == store.get("lastSessionId")
        )
        marker = " ← current" if is_current else ""
        snippet = _get_session_snippet(session_dir, s.session_id) if session_dir else ""
        table.add_row(
            str(idx),
            s.session_id[:8] + "…" + marker,
            snippet or "[dim](empty)[/]",
            str(s.message_count),
            last_active,
        )

    console.print(table)
    if not show_all and total > _SESSIONS_PAGE_SIZE:
        hidden = total - _SESSIONS_PAGE_SIZE
        console.print(f"[dim]showing {_SESSIONS_PAGE_SIZE} of {total} — /sessions all to see {hidden} more[/]")
    console.print("[dim]tip: /session # to switch by number[/]")


def _load_session_by_prefix(
    console: Console,
    store_path: Path,
    session_dir: Path,
    prefix: str,
) -> tuple[list[Message], TranscriptWriter, str] | None:
    """Find a session by ID prefix, load its transcript, return (history, writer, session_id)."""
    store = load_session_store(store_path)
    sessions = list_sessions(store)
    matches = [s for s in sessions if s.session_id.startswith(prefix)]

    if not matches:
        console.print(f"[dim]no session matching '{markup.escape(prefix)}'[/]")
        return None
    if len(matches) > 1:
        console.print(f"[dim]{len(matches)} sessions match — be more specific:[/]")
        for s in matches:
            last_active = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M:%S")
            console.print(f"  [cyan]{s.session_id[:12]}…[/]  {s.model or '(unknown)'}  {s.message_count} msgs  {last_active}")
        return None

    target = matches[0]
    transcript_path = session_dir / f"{target.session_id}.jsonl"
    reader = TranscriptReader(transcript_path)
    loaded_history, _, msg_count, comp_count, last_msg_id = reader.load_history()
    writer = TranscriptWriter.resume(transcript_path, target.session_id, msg_count, comp_count, last_msg_id)
    return loaded_history, writer, target.session_id


def _load_session_by_index(
    console: Console,
    store_path: Path,
    session_dir: Path,
    n: int,
) -> tuple[list[Message], TranscriptWriter, str] | None:
    """Load the nth session (1-based) from the sorted sessions list."""
    store = load_session_store(store_path)
    sessions = list_sessions(store)

    if n < 1 or n > len(sessions):
        console.print(f"[dim]no session #{n} — run /sessions to see available sessions[/]")
        return None

    target = sessions[n - 1]
    transcript_path = session_dir / f"{target.session_id}.jsonl"
    reader = TranscriptReader(transcript_path)
    loaded_history, _, msg_count, comp_count, last_msg_id = reader.load_history()
    writer = TranscriptWriter.resume(transcript_path, target.session_id, msg_count, comp_count, last_msg_id)
    return loaded_history, writer, target.session_id


def _list_skills(console: Console, cfg: LoopConfig) -> None:
    """Display available skills with eligibility status."""
    if not cfg.workspace_dir:
        console.print("[dim](no workspace configured — skills unavailable)[/]")
        return
    
    # Load all skills
    try:
        all_entries = get_or_load_skills(
            cfg.workspace_dir,
            cfg.session_key,
            extra_dirs=cfg.extra_skill_dirs,
            max_bytes=cfg.max_skill_file_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]error loading skills:[/] {type(exc).__name__}: {markup.escape(str(exc))}")
        return
    
    if not all_entries:
        console.print("[dim]no skills found[/]")
        return

    # Apply gating with skill filter (mutates entries in-place)
    eligible = filter_eligible_skills(all_entries, skill_filter=cfg.skill_filter)
    visible = filter_visible_skills(eligible)
    
    # Build table
    table = Table(title="Skills", border_style="cyan")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="dim")
    table.add_column("Status", style="green")
    table.add_column("In Prompt", style="yellow")
    table.add_column("Reason", style="dim")
    
    # Sort by name
    sorted_entries = sorted(all_entries, key=lambda e: e.skill.name)
    
    for entry in sorted_entries:
        skill = entry.skill
        
        # Status
        if entry.eligible:
            status = "[green]eligible[/]"
        else:
            status = "[red]blocked[/]"
        
        # In prompt
        if skill in visible:
            in_prompt = "[green]yes[/]"
        elif entry.eligible:
            in_prompt = "[yellow]no (hidden)[/]"
        else:
            in_prompt = "[dim]—[/]"
        
        # Reason
        reason = entry.eligibilityReason or ""
        if skill in visible:
            reason = ""  # Clear reason for visible skills
        elif not entry.eligible and not reason:
            reason = "gating failed"
        
        table.add_row(
            skill.name,
            skill.source,
            status,
            in_prompt,
            markup.escape(reason[:40] + "..." if len(reason) > 40 else reason),
        )
    
    console.print(table)
    
    # Summary
    eligible_count = len(eligible)
    visible_count = len(visible)
    blocked_count = len(all_entries) - eligible_count
    
    console.print(
        f"[dim]{eligible_count} eligible, {visible_count} in prompt, {blocked_count} blocked[/]"
    )
    
    # Skill filter info
    if cfg.skill_filter:
        console.print(f"[dim]skill filter: {', '.join(cfg.skill_filter)}[/]")
    else:
        console.print("[dim]skill filter: unrestricted[/]")


def _handle_active_memory_command(console: Console, user_input: str, cfg: LoopConfig) -> None:
    """Handle /active-memory command for toggling and configuring Active Memory.

    Usage:
        /active-memory           - Show current status
        /active-memory on        - Enable Active Memory
        /active-memory off       - Disable Active Memory
        /active-memory mode <mode> - Set query mode (message/recent/full)
        /active-memory style <style> - Set prompt style
    """
    parts = user_input.strip().split()

    # Initialize config if not exists (default to disabled)
    if cfg.active_memory_config is None:
        cfg.active_memory_config = ActiveMemoryConfig(enabled=False)

    config = cfg.active_memory_config

    if len(parts) == 1:
        # Just /active-memory - show status
        status_text = "enabled" if config.enabled else "disabled"
        console.print(
            Panel.fit(
                Text.from_markup(
                    f"[bold]Active Memory Status[/]\n"
                    f"State: [{('green' if config.enabled else 'red')}]{status_text}[/]\n"
                    f"Query Mode: [cyan]{config.query_mode.value}[/]\n"
                    f"Prompt Style: [cyan]{config.prompt_style.value}[/]\n"
                    f"Timeout: {config.timeout_ms}ms\n"
                    f"User Turns: {config.recent_user_turns} / "
                    f"Assistant Turns: {config.recent_assistant_turns}"
                ),
                border_style="cyan",
            )
        )
        return

    cmd = parts[1].lower()

    if cmd == "on":
        config.enabled = True
        console.print("[dim]Active Memory: enabled[/]")
        return

    if cmd == "off":
        config.enabled = False
        console.print("[dim]Active Memory: disabled[/]")
        return

    if cmd == "status":
        status_text = "enabled" if config.enabled else "disabled"
        console.print(
            f"[dim]Active Memory: {status_text}, "
            f"mode={config.query_mode.value}, "
            f"style={config.prompt_style.value}[/]"
        )
        return

    if cmd == "mode" and len(parts) > 2:
        try:
            mode = QueryMode(parts[2].lower())
            config.query_mode = mode
            console.print(f"[dim]Query mode set to: {mode.value}[/]")
        except ValueError:
            valid_modes = ", ".join(m.value for m in QueryMode)
            console.print(f"[red]Invalid mode. Options: {valid_modes}[/]")
        return

    if cmd == "style" and len(parts) > 2:
        try:
            style = PromptStyle(parts[2].lower())
            config.prompt_style = style
            console.print(f"[dim]Prompt style set to: {style.value}[/]")
        except ValueError:
            valid_styles = ", ".join(s.value for s in PromptStyle)
            console.print(f"[red]Invalid style. Options: {valid_styles}[/]")
        return

    # Unknown command - show help
    console.print(
        "[dim]Usage:\n"
        "  /active-memory - Show status\n"
        "  /active-memory on - Enable\n"
        "  /active-memory off - Disable\n"
        "  /active-memory mode <message|recent|full>\n"
        "  /active-memory style <balanced|strict|contextual|recall-heavy|precision-heavy|preference-only>[/]"
    )


async def _handle_dreaming_command(
    console: Console, user_input: str, cfg: "LoopConfig", client: Any
) -> None:
    """Handle /dreaming command for toggling and running Dreaming.

    Usage:
        /dreaming            - Show current status
        /dreaming on         - Enable Dreaming
        /dreaming off        - Disable Dreaming
        /dreaming run        - Run a dreaming sweep now (blocking)
        /dreaming status     - Show detailed candidate list
    """
    from nano_openclaw.memory.dreaming import (
        DreamingConfig,
        get_dreaming_status,
        run_dreaming,
    )
    from rich.panel import Panel
    from rich.text import Text

    parts = user_input.strip().split()

    # Lazily init dreaming config on LoopConfig
    if cfg.dreaming_config is None:
        cfg.dreaming_config = DreamingConfig(enabled=False)

    dc = cfg.dreaming_config
    workspace_dir = str(cfg.workspace_dir) if cfg.workspace_dir else None

    if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() == "status"):
        detailed = len(parts) > 1 and parts[1].lower() == "status"
        if not workspace_dir:
            console.print("[dim]Dreaming: no workspace directory configured[/]")
            return

        st = get_dreaming_status(workspace_dir, dc)
        state_color = "green" if st["enabled"] else "red"
        state_text = "enabled" if st["enabled"] else "disabled"
        last_run = st["last_run_at"] or "never"
        due_text = " [yellow](due)[/]" if st["due"] else ""

        lines = [
            f"[bold]Dreaming Status[/]",
            f"State: [{state_color}]{state_text}[/]",
            f"Frequency: [cyan]{st['frequency']}[/]",
            f"Last Run: [cyan]{last_run}[/]{due_text}",
            f"Tracked: {st['total_tracked']} entries | Active: {st['active_candidates']} | Promoted: {st['promoted_total']}",
        ]

        if detailed and st["top_candidates"]:
            lines.append("")
            lines.append("[bold]Top candidates:[/]")
            for c in st["top_candidates"]:
                lines.append(
                    f"  [cyan]{c['path']}:{c['start_line']}[/] "
                    f"score={c['score']:.2f} recalls={c['recall_count']} "
                    f"queries={c['unique_queries']}"
                )

        console.print(Panel.fit(Text.from_markup("\n".join(lines)), border_style="magenta"))
        return

    cmd = parts[1].lower()

    if cmd == "on":
        dc.enabled = True
        console.print("[dim]Dreaming: enabled[/]")
        return

    if cmd == "off":
        dc.enabled = False
        console.print("[dim]Dreaming: disabled[/]")
        return

    if cmd == "run":
        if not workspace_dir:
            console.print("[dim]Dreaming: no workspace directory configured[/]")
            return
        console.print("[dim]Running dreaming sweep...[/]")
        try:
            result = await run_dreaming(workspace_dir, dc, cfg.model, api_client=client)
            console.print(
                f"[dim]Dreaming complete in {result.elapsed_ms}ms — "
                f"candidates: {len(result.candidates)}, promoted: {len(result.promoted)}[/]"
            )
            for entry, score, content in result.promoted:
                preview = content[:60].replace("\n", " ")
                console.print(
                    f"  [green]↑[/] {entry.path}:{entry.start_line} "
                    f"(score={score:.2f}) {preview}..."
                )
        except Exception as exc:
            console.print(f"[red]Dreaming error:[/] {exc}")
        return

    console.print(
        "[dim]Usage:\n"
        "  /dreaming - Show status\n"
        "  /dreaming on - Enable\n"
        "  /dreaming off - Disable\n"
        "  /dreaming run - Run sweep now\n"
        "  /dreaming status - Detailed candidate list[/]"
    )
