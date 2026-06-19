"""Rich-based REPL and tool-call rendering.

Mirrors `src/cli/tui-cli.ts:8-63` -> `src/tui/tui.ts:1-52` (REPL shell)
and `src/tui/components/tool-execution.ts:55-137` (tool panels).
Production OpenClaw uses pi-tui — a custom React-like terminal lib.
nano uses ``rich``: simpler, less to learn, same visual idea.

Slash commands: ``/quit``, ``/clear`` (clear history, keep session), ``/new`` (new session + new ID), ``/help``, ``/context``, ``/compact``, ``/sessions`` (interactive picker; ``/sessions all`` for plain list), ``/session [prefix|#]``. No multiline editor.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from rich import markup
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nano_openclaw.core.compact import compact_if_needed, estimate_tokens
from nano_openclaw.gateway import slash
from nano_openclaw.logger import get_logger
from nano_openclaw.memory.active import ActiveMemoryConfig, QueryMode, PromptStyle
from nano_openclaw.core.loop import (
    ActiveMemoryRecall,
    Compaction,
    ImageAttached,
    ImageDescribe,
    ImageError,
    ImageSkip,
    LoopConfig,
    MaxIterationsReached,
    Message,
    CancellationToken,
    RetryAttempt,
    SkillInvoked,
    StopReasonWarning,
    SubagentSpawned,
    SubagentAnnounced,
    SubagentKilled,
    SubagentProgress,
    ToolResult,
    TurnCancelled,
    AgentSession,
    run_pre_compaction_memory_flush,
)
from nano_openclaw.core._stream_events import MemoryExtracted
from nano_openclaw.core.provider import (
    MessageEnd,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseDelta,
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
from nano_openclaw.core.tools import ToolRegistry

logger = get_logger(__name__)

_PREVIEW_LINES = 12
_MAX_HISTORY_PREVIEW_TURNS = 10  # turns shown when replaying history after session switch


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
    backend: Any = None,
) -> None:
    """Interactive read-eval-print loop. Runs until /quit or Ctrl-D.

    ``backend`` (Phase 1 of the gateway port) routes chat dispatch through
    an ``EmbeddedBackend`` when supplied. Slash commands and history
    bookkeeping continue to operate on the same in-memory ``history`` and
    ``transcript_writer`` — when ``backend`` is provided, those are bound to
    the backend's session entity so the data is shared, not duplicated.

    When ``backend`` is None, the legacy direct-AgentSession path runs
    unchanged. This keeps existing tests + ``__main__.py`` working until the
    CLI entry is migrated.
    """
    console = Console()

    # If a backend is supplied, bind history + transcript_writer + session_id to
    # the backend's session entity. Slash commands keep working unchanged because
    # they mutate `history` (a list reference) and call methods on
    # `transcript_writer`; both come straight from the backend's session, so
    # mutations are visible through the backend too.
    if backend is not None:
        try:
            backend_session = backend.manager.get_or_load(session_id or None)
        except KeyError:
            # session_id pointed at a transcript that doesn't exist (or was
            # mid-creation by a legacy path) — fall through to a fresh session.
            backend_session = backend.manager.create()
        session_id = backend_session.session_id
        history = backend_session.history
        transcript_writer = backend_session.writer
        if cfg.session_key != session_id:
            cfg.session_key = session_id
    else:
        history = list(initial_history) if initial_history else []
    from nano_openclaw.todo import TodoStore
    todo_store = backend_session.todo_store if backend is not None else TodoStore()

    _load_input_history(history)

    # Wire spawn context so sessions_spawn / subagents tools are callable and
    # lifecycle events (SubagentSpawned, SubagentAnnounced, SubagentKilled) reach
    # the console.  The stable handler lasts across turns; it receives only
    # lifecycle events (subagent internal events are suppressed in the runner).
    # _spawn_ctx is kept in scope so session switches can update requester_session_key.
    _spawn_ctx: Any = None
    if registry.get("sessions_spawn") is not None:
        from nano_openclaw.subagent.tools import SpawnToolContext
        _spawn_ctx = SpawnToolContext(
            requester_session_key=session_id or cfg.session_key or "main",
            session_dir=session_dir or Path("."),
            workspace_dir=cfg.workspace_dir or Path("."),
            client=client,
            base_cfg=cfg,
            on_event=_make_event_handler(console),
            parent_registry=registry,
        )
        registry.set_spawn_tool_context(_spawn_ctx)

    _print_banner(console, cfg.model, registry, session_id)

    while True:
        try:
            user_input = (await _get_pt_session().prompt_async()).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return

        if not user_input:
            continue

        # Drain completed subagent results as early as possible — before any
        # slash command handling — so results are persisted even when the user
        # runs /new, /session, /quit, etc. instead of normal input.
        if _spawn_ctx is not None:
            from nano_openclaw.subagent.runner import get_runner
            _pending = get_runner().drain_announcements(_spawn_ctx.requester_session_key)
            if _pending:
                history.extend(_pending)
                if transcript_writer:
                    for _msg in _pending:
                        transcript_writer.append_message(_msg)
                n = len(_pending)
                console.print(f"[dim]({n} subagent result{'s' if n > 1 else ''} added to context)[/]")

        # ── Slash command dispatch ────────────────────────────────────────
        # When ``backend`` is wired (the default tui invocation), delegate to
        # ``gateway.slash.handle_slash`` so embedded + remote modes share one
        # surface and one set of Rich renderers. When backend is None (legacy
        # direct invocation paths), fall through to the inline branches below.
        if backend is not None and user_input.startswith("/"):
            from nano_openclaw.gateway.slash import QuitREPL, handle_slash
            slash_state = {"session_key": session_id, "session_changed": False}
            try:
                handled = await handle_slash(user_input, backend, console, slash_state)
            except QuitREPL:
                console.print("[dim]bye.[/]")
                return
            if handled:
                if slash_state.get("session_changed"):
                    # Slash mutated which session future turns should target.
                    # Re-bind local references to the new session entity so
                    # legacy variables (history, transcript_writer, session_id)
                    # all point to the same place the backend now uses.
                    new_key = slash_state.get("session_key") or session_id
                    if new_key:
                        try:
                            new_sess = backend.manager.get_or_load(new_key)
                        except KeyError:
                            new_sess = backend.manager.create()
                            new_key = new_sess.session_id
                        history = new_sess.history
                        transcript_writer = new_sess.writer
                        session_id = new_sess.session_id
                        cfg.session_key = session_id
                        if _spawn_ctx is not None:
                            _spawn_ctx.requester_session_key = session_id
                        _load_input_history(history)
                continue
            # Unknown slash verbs may be user-invocable skills. Let the agent
            # loop parse /skill-name instead of stopping at the builtin slash
            # dispatcher.

        # ── Legacy inline slash dispatch (backend=None path) ──────────────
        if user_input in {"/quit", "/exit", "/q"}:
            console.print("[dim]bye.[/]")
            return
        if user_input == "/clear":
            history.clear()
            todo_store = TodoStore()
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
            todo_store = TodoStore()
            if store_path and session_dir:
                session_id = new_session_id()
                new_path = session_dir / f"{session_id}.jsonl"
                transcript_writer = TranscriptWriter(new_path)
                transcript_writer.start(model=cfg.model)
                cfg.session_key = session_id
                transcript_writer._on_first_write = (
                    lambda _sp=store_path, _sid=session_id, _tw=transcript_writer, _model=cfg.model:
                    _update_session_metadata(_sp, _sid, _tw, _model)
                )
                if _spawn_ctx is not None:
                    _spawn_ctx.requester_session_key = session_id
                console.print(f"[dim]new session: {session_id[:8]}…[/]")
            else:
                console.print("[dim](history cleared)[/]")
            continue
        if user_input == "/help":
            console.print(
                f"[dim]commands: {slash.HELP_TEXT} — anything else is sent to the model[/]"
            )
            continue
        if user_input == "/context":
            _show_context(console, history, cfg)
            continue
        if user_input == "/compact":
            await _manual_compact(console, history, cfg, client, registry, todo_store=todo_store)
            continue
        if user_input == "/skills":
            _list_skills(console, cfg)
            continue
        if user_input == "/plugins":
            _list_plugins(console, registry)
            continue
        if user_input == "/hooks":
            _list_hooks(console, registry)
            continue
        if user_input == "/tools":
            _list_tools(console, registry)
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
                        cfg.session_key = session_id
                        if _spawn_ctx is not None:
                            _spawn_ctx.requester_session_key = session_id
                        _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
                        _load_input_history(history)
                        _replay_history(console, history, session_id)
                else:
                    _list_sessions_cli(console, store_path, session_id, cfg.model, transcript_writer, session_dir)
            else:
                console.print("[dim](no session store configured)[/]")
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
                            cfg.session_key = session_id
                            if _spawn_ctx is not None:
                                _spawn_ctx.requester_session_key = session_id
                            _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)
                            _load_input_history(history)
                            _replay_history(console, history, session_id)
                else:
                    console.print("[dim](no session store configured)[/]")
            continue
        if user_input.startswith("/active-memory"):
            if not _memory_commands_available(registry):
                console.print("[dim](/active-memory unavailable; load the memory plugin)[/]")
                continue
            _handle_active_memory_command(console, user_input, cfg)
            continue
        if user_input.startswith("/dreaming"):
            if not _memory_commands_available(registry):
                console.print("[dim](/dreaming unavailable; load the memory plugin)[/]")
                continue
            await _handle_dreaming_command(console, user_input, cfg, client)
            continue
        if user_input.startswith("/subagents"):
            if not _subagents_command_available(registry):
                console.print("[dim](/subagents unavailable; load the subagent plugin)[/]")
                continue
            await _handle_subagents_command(console, user_input, cfg, client)
            continue

        on_event = _make_event_handler(console, registry=registry)
        try:
            with _escape_cancellation_token() as cancellation_token:
                if backend is not None:
                    # Phase 1: route turn through Backend. Identical observable
                    # behavior — events still fire synchronously into on_event,
                    # cancellation token is honored, history mutates in place.
                    try:
                        turn_id = await backend.chat_send(
                            session_key=session_id,
                            text=user_input,
                            on_local_event=on_event,
                            cancellation_token=cancellation_token,
                        )
                    except Exception as exc:  # BusyError or worse
                        from nano_openclaw.gateway.backend import BusyError
                        if isinstance(exc, BusyError):
                            console.print(f"\n[yellow]busy:[/] {exc} (retry in {exc.retry_after_ms}ms)")
                            continue
                        raise
                    await backend.await_turn(turn_id)
                else:
                    session = AgentSession(
                        history=history,
                        registry=registry,
                        on_event=on_event,
                        client=client,
                        cfg=cfg,
                        transcript_writer=transcript_writer,
                        cancellation_token=cancellation_token,
                        todo_store=todo_store,
                    )
                    await session.run_turn(user_input)
        except (TurnCancelled, KeyboardInterrupt):
            # SIGINT can still slip through the watcher on Windows (no raw mode)
            # and during POSIX setup/teardown windows where ISIG is briefly on,
            # so treat KeyboardInterrupt during a turn as a soft cancel rather
            # than letting it crash the whole REPL.
            console.print("\n[dim](turn cancelled)[/]")
            continue
        except Exception as exc:  # noqa: BLE001 — surface model/network errors to user
            console.print(f"\n[red]error:[/] {type(exc).__name__}: {markup.escape(str(exc))}")
            continue
        console.print()  # blank line between turns

        # Persist session metadata after each turn
        if transcript_writer and store_path and session_id:
            _update_session_metadata(store_path, session_id, transcript_writer, cfg.model)


def _memory_commands_available(registry: ToolRegistry) -> bool:
    return registry.get("memory_get") is not None and registry.get("memory_search") is not None


def _subagents_command_available(registry: ToolRegistry) -> bool:
    return registry.get("sessions_spawn") is not None or registry.get("subagents") is not None


def _print_banner(
    console: Console,
    model: str,
    registry: ToolRegistry,
    session_id: str = "",
) -> None:
    tools = ", ".join(registry.names()) or "(none)"
    session_line = f"session: {session_id[:8]}..." if session_id else ""
    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold]nano-openclaw[/]\n"
                f"model:  [cyan]{markup.escape(model)}[/]\n"
                f"tools:  {markup.escape(tools)}"
                + (f"\n{session_line}" if session_line else "")
                + f"\ncommands: {slash.HELP_TEXT}"
            ),
            border_style="cyan",
        )
    )


async def _manual_compact(
    console: Console,
    history: list[Message],
    cfg: LoopConfig,
    client: Any,
    registry: ToolRegistry,
    todo_store: Any | None = None,
) -> None:
    """Manually trigger context compaction."""
    if len(history) < cfg.context_recent_turns * 2:
        console.print("[dim](not enough history to compact)[/]")
        return

    console.print("[dim]compacting context...[/]")

    try:
        await run_pre_compaction_memory_flush(
            client=client,
            cfg=cfg,
            history=history,
            registry=registry,
            force=True,
        )

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
            from nano_openclaw.core.loop import append_active_todo_reminder

            append_active_todo_reminder(history, todo_store)
            _render_compaction(console, summary=summary)
            current_tokens = estimate_tokens(history)
            _render_status_tree(console, "Context", [("reduced", f"{current_tokens:,} tokens · {len(history)} messages")])
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

    window_line = (
        f"model window: {cfg.context_window:,} tokens\n" if cfg.context_window > 0 else ""
    )
    console.print(
        Panel.fit(
            Text.from_markup(
                f"context usage: [{color}]{current_tokens:,}[/] / {budget:,} tokens\n"
                f"usage: [{color}]{usage_pct:.1f}%[/]\n"
                f"threshold: {threshold:,} tokens ({cfg.context_threshold * 100:.0f}%)\n"
                f"{window_line}"
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


def _make_event_handler(console: Console, registry: ToolRegistry | None = None) -> Callable[[Any], None]:
    """Return a per-turn callback that renders streaming events live.

    Strategy: print assistant text deltas inline, render long-running
    tool/subagent activity as retained Live trees, and keep one-off status
    events in the same compact tree style.
    """
    state = {
        "text_in_flight": False,
        "thinking_in_flight": False,
        "tool_slots": {},
        "tool_name_counts": {},
        "rendered_tool_results": set(),
        "tool_live": None,
        "tool_live_start": None,
        "subagent_progress": {},
        "subagent_live": None,
    }

    def reset_tool_batch() -> None:
        live = state["tool_live"]
        if live is not None:
            live.stop()
            state["tool_live"] = None
        state["tool_live_start"] = None
        state["tool_slots"].clear()
        state["tool_name_counts"].clear()
        state["rendered_tool_results"].clear()

    if registry is not None:
        registry.approval_live_stopper = reset_tool_batch

    def update_tool_live() -> None:
        live = state["tool_live"]
        start_time = state["tool_live_start"]
        if live is not None and start_time is not None:
            live.update(_build_tool_tree(state["tool_slots"], start_time))

    def start_or_update_tool_live() -> None:
        if state["tool_live"] is None:
            state["tool_live_start"] = time.monotonic()
            state["tool_live"] = Live(
                _build_tool_tree(state["tool_slots"], state["tool_live_start"]),
                console=console,
                refresh_per_second=8,
                transient=False,
            )
            state["tool_live"].start()
        else:
            update_tool_live()

    def stop_tool_live_if_done() -> None:
        tool_slots = state["tool_slots"]
        if tool_slots and all(slot["done"] for slot in tool_slots.values()):
            update_tool_live()
            live = state["tool_live"]
            if live is not None:
                live.stop()
                state["tool_live"] = None
            state["tool_live_start"] = None

    def update_subagent_live() -> None:
        live = state["subagent_live"]
        if live is not None:
            live.update(_build_subagent_tree(state["subagent_progress"]))

    def stop_subagent_live_if_done() -> None:
        progress = state["subagent_progress"]
        if progress and all(info["done"] for info in progress.values()):
            update_subagent_live()
            live = state["subagent_live"]
            if live is not None:
                live.stop()
                state["subagent_live"] = None
            progress.clear()

    def handle(event: Any) -> None:
        event_type = type(event).__name__
        logger.debug("event.received", "", event_type=event_type)
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
            tool_slots = state["tool_slots"]
            if tool_slots and all(slot["done"] for slot in tool_slots.values()):
                reset_tool_batch()
                tool_slots = state["tool_slots"]
            if event.id not in tool_slots:
                count = state["tool_name_counts"].get(event.name, 0) + 1
                state["tool_name_counts"][event.name] = count
                display_name = event.name if count == 1 else f"{event.name} #{count}"
                tool_slots[event.id] = {
                    "name": event.name,
                    "display_name": display_name,
                    "args_buf": "",
                    "done": False,
                    "is_error": False,
                    "result_preview": None,
                }
                if event.name == "sessions_spawn":
                    if state["tool_live"] is not None:
                        reset_tool_batch()
                        tool_slots = state["tool_slots"]
                        tool_slots[event.id] = {
                            "name": event.name,
                            "display_name": display_name,
                            "args_buf": "",
                            "done": False,
                            "is_error": False,
                            "result_preview": None,
                        }
                else:
                    start_or_update_tool_live()

        elif isinstance(event, ToolUseDelta):
            slot = state["tool_slots"].get(event.id)
            if slot is not None:
                slot["args_buf"] += event.partial_json
                if slot.get("name") != "sessions_spawn":
                    update_tool_live()

        elif isinstance(event, ToolUseEnd):
            update_tool_live()

        elif isinstance(event, MessageEnd):
            if state["text_in_flight"]:
                console.print()
                state["text_in_flight"] = False

        elif isinstance(event, ToolResult):
            if event.tool_use_id in state["rendered_tool_results"]:
                return
            state["rendered_tool_results"].add(event.tool_use_id)
            slot = state["tool_slots"].get(event.tool_use_id)
            if slot is not None:
                slot["done"] = True
                slot["is_error"] = bool(event.result.get("is_error"))
                slot["result_preview"] = _extract_tool_preview(event.result)
                update_tool_live()
                stop_tool_live_if_done()

        elif isinstance(event, Compaction):
            _render_compaction(console, summary=event.summary)

        elif isinstance(event, ImageDescribe):
            _render_status_tree(console, "Image", [(event.ref, "describing")])

        elif isinstance(event, ImageAttached):
            # "described" = Media Understanding path; "attached" = Native Vision path
            mode = "described" if event.via_model else "attached"
            _render_status_tree(console, "Image", [(ref, mode) for ref in event.refs])

        elif isinstance(event, ImageError):
            _render_status_tree(console, "Image", [(event.ref, f"[red]error[/] {markup.escape(event.error)}")])

        elif isinstance(event, ImageSkip):
            _render_status_tree(console, "Image", [(event.ref, f"[yellow]skipped[/] {markup.escape(event.reason)}")])

        elif isinstance(event, SkillInvoked):
            _render_status_tree(console, "Skill", [(event.skill_name, event.skill_path)])

        elif isinstance(event, ActiveMemoryRecall):
            result = event.result
            if result.context:
                cached_str = ", cached" if result.cached else ""
                _render_status_tree(console, "Active Memory", [("recall", f"{result.elapsed_ms}ms{cached_str}")])

        elif isinstance(event, MemoryExtracted):
            # Only surface when the extractor actually wrote something —
            # otherwise the "Saved 0 memories" line is just noise.
            if event.written_paths:
                count = len(event.topic_paths) if event.topic_paths else len(event.written_paths)
                # Show topic filenames (without the memory/topics/ prefix) since
                # those are the new content; the MEMORY.md index update is
                # implied by any save. Cap at 3 names to keep the line tidy.
                shown_paths = event.topic_paths or event.written_paths
                names = []
                for p in shown_paths[:3]:
                    norm = p.replace("\\", "/")
                    short = norm.rsplit("/", 1)[-1] if "/" in norm else norm
                    names.append(short)
                if len(shown_paths) > 3:
                    names.append(f"+{len(shown_paths) - 3} more")
                detail = f"{count} saved · {', '.join(names)}" if names else f"{count} saved"
                _render_status_tree(console, "Memory", [("extracted", f"{markup.escape(detail)} ({event.duration_ms}ms)")])

        elif isinstance(event, SubagentSpawned):
            if state["tool_live"] is not None:
                reset_tool_batch()
            label = event.label or event.task[:50]
            if len(event.task) > 50 and not event.label:
                label += "..."
            state["subagent_progress"][event.run_id] = {
                "label": label,
                "tool_uses": 0,
                "tokens": 0,
                "activity": "starting...",
                "done": False,
            }
            if state["subagent_live"] is None:
                state["subagent_live"] = Live(
                    _build_subagent_tree(state["subagent_progress"]),
                    console=console,
                    refresh_per_second=8,
                    transient=False,
                )
                state["subagent_live"].start()
            else:
                update_subagent_live()

        elif isinstance(event, SubagentProgress):
            info = state["subagent_progress"].setdefault(event.run_id, {
                "label": event.label,
                "tool_uses": 0,
                "tokens": 0,
                "activity": "starting...",
                "done": False,
            })
            info.update({
                "label": event.label,
                "tool_uses": event.tool_uses,
                "tokens": event.input_tokens + event.output_tokens,
                "activity": event.current_activity,
            })
            update_subagent_live()

        elif isinstance(event, SubagentAnnounced):
            info = state["subagent_progress"].get(event.run_id)
            if info is not None:
                info["done"] = True
                info["status"] = event.status
                info["activity"] = event.status
                info["elapsed_ms"] = event.elapsed_ms
                info["result_preview"] = _truncate_one_line(event.result_text or "", 120) if event.result_text else None
                info["error_message"] = event.error_message
                stop_subagent_live_if_done()
            else:
                _render_subagent_summary(console, event)

        elif isinstance(event, SubagentKilled):
            info = state["subagent_progress"].get(event.run_id)
            if info is not None:
                info["done"] = True
                info["status"] = "killed"
                info["activity"] = "Killed"
                stop_subagent_live_if_done()
            else:
                _render_status_tree(console, "Subagent", [(event.task[:40], f"[yellow]killed[/] {event.run_id}")])

        elif isinstance(event, MaxIterationsReached):
            console.print(
                f"[yellow]⚠ 已达到最大迭代次数 ({event.max_iterations})，正在请求最终结论…[/]"
            )

        elif isinstance(event, StopReasonWarning):
            console.print(
                f"[yellow]⚠ 第 {event.iteration} 轮输出被截断 (max_tokens)，压缩上下文后重试…[/]"
            )

        elif isinstance(event, RetryAttempt):
            _render_status_tree(console, "Retry", [
                (f"attempt {event.attempt}/{event.max_attempts}", markup.escape(event.error[:80]))
            ])

    return handle


def _build_tool_tree(tool_slots: dict[str, dict[str, Any]], start_time: float) -> Group:
    total = len(tool_slots)
    done = sum(1 for slot in tool_slots.values() if slot["done"])
    elapsed = time.monotonic() - start_time

    noun = "tool call" if total == 1 else "tool calls"
    if total and done == total:
        header = f"● {total} {noun} done ({elapsed:.1f}s)"
    else:
        header = f"● {total} {noun}..."

    lines = [Text.from_markup(f"[bold]{markup.escape(header)}[/]")]
    items = list(tool_slots.items())
    for i, (_, slot) in enumerate(items):
        is_last = i == len(items) - 1
        branch = "└" if is_last else "├"
        args_preview = _short_tool_args(slot["args_buf"])
        name_str = f"{slot['display_name']}({args_preview})"
        if slot["done"] and slot.get("is_error"):
            if slot["result_preview"]:
                status = f"[red]✗[/] {markup.escape(slot['result_preview'])}"
            else:
                status = "[red]✗[/]"
        elif slot["done"] and slot["result_preview"]:
            status = f"[green]✓[/] {markup.escape(slot['result_preview'])}"
        elif slot["done"]:
            status = "[green]✓[/]"
        else:
            status = "[dim]running...[/]"
        line = Text(f"   {branch} {name_str} · ")
        line.append(Text.from_markup(status))
        lines.append(line)
    return Group(*lines)


def _render_status_tree(console: Console, header: str, items: list[tuple[str, str | None]]) -> None:
    console.print(_build_status_tree(header, items))


def _build_status_tree(header: str, items: list[tuple[str, str | None]]) -> Group:
    lines = [Text.from_markup(f"[bold]● {markup.escape(header)}[/]")]
    for i, (label, status) in enumerate(items):
        is_last = i == len(items) - 1
        branch = "└" if is_last else "├"
        line = Text.from_markup(f"   {branch} {markup.escape(label)}")
        if status:
            line.append(Text.from_markup(f" · {status}"))
        lines.append(line)
    return Group(*lines)


def _render_subagent_summary(console: Console, event: SubagentAnnounced) -> None:
    label = event.task[:50] + ("..." if len(event.task) > 50 else "")
    status = _format_subagent_status({
        "status": event.status,
        "elapsed_ms": event.elapsed_ms,
        "result_preview": _truncate_one_line(event.result_text or "", 120) if event.result_text else None,
        "error_message": event.error_message,
    })
    _render_status_tree(console, "Subagent", [(label, status), ("run", f"[dim]{event.run_id}[/]")])


def _short_tool_args(args_buf: str, limit: int = 40) -> str:
    text = " ".join(args_buf.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _extract_tool_preview(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)

    if result.get("is_error"):
        for key in ("error", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_one_line(value, 80)
        text = "\n".join(part for part in text_parts if part.strip())
        if text:
            return _truncate_one_line(text, 80)
        return None

    text = "\n".join(part for part in text_parts if part.strip())
    if not text:
        return None
    line_count = len(text.splitlines())
    return f"{line_count} line{'s' if line_count != 1 else ''}"


def _truncate_one_line(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) > limit:
        return compact[:limit].rstrip() + "..."
    return compact


def _build_subagent_tree(progress: dict[str, dict[str, Any]]) -> Group:
    running = sum(1 for p in progress.values() if not p["done"])
    total = len(progress)
    if running:
        header = f"● Running {running} agent{'s' if running != 1 else ''}..."
    else:
        header = f"● {total} agent{'s' if total != 1 else ''} done"
    lines = [Text.from_markup(f"[bold]{markup.escape(header)}[/]")]
    items = list(progress.values())
    for i, info in enumerate(items):
        is_last = i == len(items) - 1
        branch = "└" if is_last else "├"
        tokens = info["tokens"]
        tok_str = f" · {tokens / 1000:.1f}k tokens" if tokens else ""
        lines.append(Text.from_markup(
            f"   {branch} {markup.escape(info['label'])} · {info['tool_uses']} tool uses{tok_str}"
        ))
        indent = "  " if is_last else "│ "
        activity = _format_subagent_status(info) if info["done"] else markup.escape(info.get("activity", ""))
        lines.append(Text.from_markup(f"   {indent}⎿  {activity}"))
    return Group(*lines)


def _format_subagent_status(info: dict[str, Any]) -> str:
    status = str(info.get("status") or info.get("activity") or "done")
    icon = {
        "completed": "[green]✓[/]",
        "done": "[green]✓[/]",
        "error": "[red]✗[/]",
        "timeout": "[yellow]⏱[/]",
        "killed": "[yellow]✗[/]",
        "Killed": "[yellow]✗[/]",
    }.get(status, "[green]✓[/]")
    elapsed_ms = info.get("elapsed_ms")
    elapsed = f" · {elapsed_ms / 1000:.1f}s" if elapsed_ms else ""
    error = info.get("error_message")
    if error:
        return f"{icon} {markup.escape(status)}{elapsed} · [red]{markup.escape(str(error))}[/]"
    result = info.get("result_preview")
    result_text = f" · {markup.escape(str(result))}" if result else ""
    return f"{icon} {markup.escape(status)}{elapsed}{result_text}"


def _render_compaction(console: Console, *, summary: str) -> None:
    """Render a compaction notification showing the conversation was summarized."""
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


def _load_input_history(messages: list[Message]) -> None:
    """Populate prompt_toolkit history from session's user messages."""
    global _pt_history, _pt_session
    # get_strings() returns a reversed copy, not the internal list; create a
    # fresh instance instead so the old session's history is fully replaced.
    # PromptSession binds history at construction — drop the cached session so
    # the next _get_pt_session() call rebuilds against the new history.
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
    _pt_session = None


@contextmanager
def _escape_cancellation_token() -> Iterator[CancellationToken]:
    """Listen for Esc during a turn and flip a cancellation token when pressed.

    POSIX additionally captures Ctrl+C as a soft cancel: the watcher holds
    raw mode for the lifetime of an active read window, so ISIG is off and
    SIGINT can't fire — translating the ``\\x03`` byte into a token flip is
    the only path that gets Ctrl+C noticed. Windows does NOT enter raw mode
    here (msvcrt path has no termios), so SIGINT still raises
    KeyboardInterrupt naturally; we deliberately don't intercept ``\\x03``
    there. Callers must catch KeyboardInterrupt around the turn for the
    Windows / pre-raw-mode windows.
    """
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
        # Hold raw_mode for the whole read window so ISIG stays off the entire
        # turn (preventing SIGINT races at the per-iteration boundary) and we
        # don't tcsetattr thousands of times per second. Release it only when
        # the pause-handshake fires so approval prompts can read normally.
        try:
            while not stop_event.is_set() and not token.is_cancelled:
                if _wait_if_input_paused():
                    continue
                with input_handle.raw_mode():
                    while not stop_event.is_set() and not token.is_cancelled:
                        if token._input_pause_requested.is_set():
                            break
                        keys_read = False
                        for kp in input_handle.read_keys():
                            keys_read = True
                            if kp.key in (Keys.Escape, Keys.ControlC):
                                token.cancel()
                                return
                        if not keys_read:
                            time.sleep(0.01)
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

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import has_completions as _has_completions
from prompt_toolkit.history import InMemoryHistory as _InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings as _KeyBindings
from prompt_toolkit.styles import Style as _PTStyle
_pt_history: _InMemoryHistory = _InMemoryHistory()
_pt_session: PromptSession | None = None


# Completion menu palette — ansi-named colors so the look adapts to whatever
# theme the user's terminal is set to (default bg, etc), instead of the
# library's hard-coded white box. Selected row mirrors the cyan accent that
# the banner / sessions table / help table already use.
_PT_STYLE = _PTStyle.from_dict({
    "completion-menu": "bg:default",
    "completion-menu.completion": "bg:default fg:default",
    "completion-menu.completion.current": "bg:ansicyan fg:ansiblack bold",
    "completion-menu.meta.completion": "bg:default fg:ansibrightblack",
    "completion-menu.meta.completion.current": "bg:ansicyan fg:ansiblack",
    "scrollbar.background": "bg:default",
    "scrollbar.button": "bg:ansibrightblack",
})


def _build_pt_keybindings() -> _KeyBindings:
    """Custom keybindings layered on top of prompt_toolkit defaults.

    Esc on a visible completion menu cancels it immediately. Without this the
    default emacs-mode Esc-as-meta-prefix logic waits ~500ms for a follow-up
    key before treating Esc as a standalone press — feels laggy. The
    ``has_completions`` filter keeps the meta-prefix behavior intact when no
    menu is showing.
    """
    kb = _KeyBindings()

    @kb.add("escape", filter=_has_completions, eager=True)
    def _(event):
        event.current_buffer.cancel_completion()

    return kb


# Snapshot stdin termios at import time and restore it on process exit.
# prompt_toolkit usually cleans up after itself, but some exit paths
# (notably WordCompleter + async prompt followed by a slash-driven exit)
# leak a missing ECHO flag, leaving the parent shell with no input echo.
# atexit gives us a no-cost belt-and-braces restore on any exit path.
_initial_termios: Any = None
if sys.platform != "win32":
    try:
        import termios as _termios

        if sys.stdin.isatty():
            _initial_termios = _termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        _initial_termios = None


def _restore_terminal_state() -> None:
    if _initial_termios is None:
        return
    try:
        import termios as _termios

        _termios.tcsetattr(sys.stdin.fileno(), _termios.TCSADRAIN, _initial_termios)
    except Exception:
        pass


if _initial_termios is not None:
    import atexit as _atexit

    _atexit.register(_restore_terminal_state)


def _slash_completer() -> WordCompleter:
    """Build a slash completer from the real ``HELP_ENTRIES`` catalogue.

    Imported lazily to avoid a top-of-file cycle through ``gateway.slash``.
    """
    from nano_openclaw.gateway.slash import HELP_ENTRIES
    words: list[str] = []
    for entry in HELP_ENTRIES:
        words.append(entry.command)
        words.extend(entry.aliases)
    # WORD=True splits on whitespace (not alphanumeric boundary), so the
    # leading "/" is part of the current token — otherwise typing "/ski<Tab>"
    # would look up "ski" against words that all start with "/" and miss.
    return WordCompleter(sorted(set(words)), ignore_case=True, WORD=True)


def _get_pt_session() -> PromptSession:
    """Lazily build (and rebuild after history reset) the shared PromptSession.

    PromptSession binds the ``history=`` object at construction, so swapping
    ``_pt_history`` to a new instance (see ``_load_input_history``) requires
    discarding the old session — the next call recreates one bound to the
    fresh history.
    """
    global _pt_session
    if _pt_session is None:
        _pt_session = PromptSession(
            ">>> ",
            history=_pt_history,
            completer=_slash_completer(),
            style=_PT_STYLE,
            key_bindings=_build_pt_keybindings(),
        )
    return _pt_session


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


def _list_plugins(console: Console, registry: ToolRegistry) -> None:
    """Display loaded plugins and their registered capabilities."""
    hook_registry = registry.hook_registry()
    if hook_registry is None:
        console.print("[dim]no plugins loaded[/]")
        return

    plugins = hook_registry.plugins()
    if not plugins:
        console.print("[dim]no plugins loaded[/]")
        return

    table = Table(title="Plugins", border_style="cyan")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Source", style="dim")
    table.add_column("Entry", style="dim", no_wrap=False, max_width=28)
    table.add_column("Status", style="green")
    table.add_column("Tools", style="yellow", no_wrap=False, max_width=36)
    table.add_column("Hooks", style="dim", no_wrap=False, max_width=36)

    for plugin in sorted(plugins, key=lambda p: p.id):
        tools = ", ".join(plugin.tools) if plugin.tools else "[dim]—[/]"
        hooks = ", ".join(plugin.hooks) if plugin.hooks else "[dim]—[/]"
        table.add_row(
            markup.escape(plugin.id),
            markup.escape(plugin.name),
            markup.escape(plugin.source),
            markup.escape(plugin.entry),
            "[green]loaded[/]",
            markup.escape(tools) if plugin.tools else tools,
            markup.escape(hooks) if plugin.hooks else hooks,
        )

    console.print(table)
    console.print(f"[dim]{len(plugins)} loaded plugin{'s' if len(plugins) != 1 else ''}[/]")


def _list_hooks(console: Console, registry: ToolRegistry) -> None:
    """Display registered plugin hooks grouped by hook event."""
    hook_registry = registry.hook_registry()
    if hook_registry is None:
        console.print("[dim]no hooks registered[/]")
        return

    hooks_by_event = hook_registry.hooks_by_event()
    if not hooks_by_event:
        console.print("[dim]no hooks registered[/]")
        return

    table = Table(title="Hooks", border_style="cyan")
    table.add_column("Event", style="cyan")
    table.add_column("Handlers", style="green", justify="right")
    table.add_column("Plugins", style="yellow", no_wrap=False, max_width=44)
    table.add_column("Priorities", style="dim", no_wrap=False, max_width=28)

    for event in sorted(hooks_by_event):
        hooks = hooks_by_event[event]
        plugins = ", ".join(f"{hook.plugin_name} ({hook.plugin_id})" for hook in hooks)
        priorities = ", ".join(str(hook.priority) for hook in hooks)
        table.add_row(
            markup.escape(event),
            str(len(hooks)),
            markup.escape(plugins),
            markup.escape(priorities),
        )

    console.print(table)
    total = sum(len(hooks) for hooks in hooks_by_event.values())
    console.print(f"[dim]{total} hook handler{'s' if total != 1 else ''} across {len(hooks_by_event)} event{'s' if len(hooks_by_event) != 1 else ''}[/]")


def _list_tools(console: Console, registry: ToolRegistry) -> None:
    """Display all registered tools with name and description."""
    names = sorted(registry.names())
    if not names:
        console.print("[dim]no tools registered[/]")
        return

    table = Table(title="Tools", border_style="cyan")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Description", style="white", no_wrap=False)

    for name in names:
        tool = registry.get(name)
        desc = tool.description.split("\n")[0][:120] if tool else ""
        table.add_row(markup.escape(name), markup.escape(desc))

    console.print(table)
    console.print(f"[dim]{len(names)} tool{'s' if len(names) != 1 else ''} registered[/]")


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

    parts = user_input.strip().split()

    # Lazily init dreaming config on LoopConfig
    if cfg.dreaming_config is None:
        cfg.dreaming_config = DreamingConfig(enabled=True)

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


async def _handle_subagents_command(
    console: Console,
    user_input: str,
    cfg: LoopConfig,
    client: Any,
) -> None:
    """Handle /subagents command for listing and controlling subagent runs."""
    from nano_openclaw.subagent import get_runner, SubagentStatus

    parts = user_input.strip().split()
    runner = get_runner()

    if len(parts) == 1 or parts[1].lower() == "list":
        runs = runner.registry.list_active()
        if not runs:
            console.print("[dim]No active subagent runs[/]")
            return

        table = Table(title="Active Subagent Runs", border_style="magenta")
        table.add_column("Run ID", style="cyan", width=10)
        table.add_column("Task", style="white", width=40)
        table.add_column("Status", style="yellow", width=10)
        table.add_column("Elapsed", style="dim", width=12)

        for run in runs:
            elapsed = ""
            if run.elapsed_ms:
                elapsed_sec = run.elapsed_ms / 1000
                if elapsed_sec < 60:
                    elapsed = f"{elapsed_sec:.1f}s"
                else:
                    elapsed = f"{int(elapsed_sec / 60)}m {int(elapsed_sec % 60)}s"

            task_preview = run.label or run.task[:40]
            if len(run.task) > 40 and not run.label:
                task_preview += "..."

            status_icon = {
                SubagentStatus.PENDING: "⏳",
                SubagentStatus.RUNNING: "🔄",
                SubagentStatus.COMPLETED: "✓",
                SubagentStatus.ERROR: "✗",
                SubagentStatus.TIMEOUT: "⏱",
                SubagentStatus.KILLED: "💀",
            }.get(run.status, "?")

            table.add_row(
                run.run_id,
                markup.escape(task_preview),
                f"{status_icon} {run.status.value}",
                elapsed,
            )

        console.print(table)
        console.print("[dim]Use /subagents kill <run_id> to stop a run[/]")
        return

    if parts[1].lower() == "all":
        runs = runner.registry.list_all()
        if not runs:
            console.print("[dim]No subagent runs (including terminated)[/]")
            return

        table = Table(title="All Subagent Runs", border_style="magenta")
        table.add_column("Run ID", style="cyan", width=10)
        table.add_column("Task", style="white", width=40)
        table.add_column("Status", style="yellow", width=12)
        table.add_column("Elapsed", style="dim", width=12)
        table.add_column("Ended", style="dim", width=16)

        for run in runs:
            elapsed = ""
            if run.elapsed_ms:
                elapsed_sec = run.elapsed_ms / 1000
                if elapsed_sec < 60:
                    elapsed = f"{elapsed_sec:.1f}s"
                else:
                    elapsed = f"{int(elapsed_sec / 60)}m {int(elapsed_sec % 60)}s"

            task_preview = run.label or run.task[:40]
            if len(run.task) > 40 and not run.label:
                task_preview += "..."

            ended = ""
            if run.ended_at:
                ended = run.ended_at.strftime("%H:%M:%S")

            status_icon = {
                SubagentStatus.PENDING: "⏳",
                SubagentStatus.RUNNING: "🔄",
                SubagentStatus.COMPLETED: "✓",
                SubagentStatus.ERROR: "✗",
                SubagentStatus.TIMEOUT: "⏱",
                SubagentStatus.KILLED: "💀",
            }.get(run.status, "?")

            table.add_row(
                run.run_id,
                markup.escape(task_preview),
                f"{status_icon} {run.status.value}",
                elapsed,
                ended,
            )

        console.print(table)
        return

    if parts[1].lower() == "kill":
        if len(parts) < 3:
            console.print("[dim]Usage: /subagents kill <run_id|all>[/]")
            return

        target = parts[2].lower()

        if target == "all":
            killed = await runner.kill_all()
            if killed:
                console.print(f"[dim]Killed {len(killed)} subagent run(s): {', '.join(killed)}[/]")
            else:
                console.print("[dim]No active subagent runs to kill[/]")
            return

        success = await runner.kill(target)
        if success:
            console.print(f"[dim]Killed subagent run: {target}[/]")
        else:
            console.print(f"[dim]Run {target} not found or already terminated[/]")
        return

    console.print(
        "[dim]Usage:\n"
        "  /subagents - List active runs\n"
        "  /subagents list - List active runs\n"
        "  /subagents all - List all runs (including terminated)\n"
        "  /subagents kill <run_id> - Kill a specific run\n"
        "  /subagents kill all - Kill all active runs[/]"
    )
