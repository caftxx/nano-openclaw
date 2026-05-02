"""Rich-based REPL and tool-call rendering.

Mirrors `src/cli/tui-cli.ts:8-63` -> `src/tui/tui.ts:1-52` (REPL shell)
and `src/tui/components/tool-execution.ts:55-137` (tool panels).
Production OpenClaw uses pi-tui — a custom React-like terminal lib.
nano uses ``rich``: simpler, less to learn, same visual idea.

Slash commands: ``/quit``, ``/clear`` (clear history, keep session), ``/new`` (new session + new ID), ``/help``, ``/context``, ``/compact``, ``/sessions``, ``/save``, ``/session [prefix]``. No multiline editor.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from anthropic import Anthropic
from rich import markup
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nano_openclaw.compact import compact_if_needed, estimate_tokens
from nano_openclaw.loop import (
    Compaction,
    ImageAttached,
    ImageDescribe,
    ImageError,
    ImageSkip,
    LoopConfig,
    Message,
    SkillInvoked,
    ToolResult,
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
_COMMANDS_HELP = "/quit  /clear  /new  /help  /context  /compact  /sessions  /save  /session [prefix]  /skills"


def repl(
    registry: ToolRegistry,
    *,
    client: Anthropic,
    cfg: LoopConfig,
    session_dir: Path | None = None,
    transcript_writer: TranscriptWriter | None = None,
    session_id: str = "",
    store_path: Path | None = None,
    initial_history: list[Message] | None = None,
) -> None:
    """Interactive read-eval-print loop. Blocks until /quit or Ctrl-D."""
    console = Console()
    history: list[Message] = list(initial_history) if initial_history else []

    _print_banner(console, cfg.model, registry, session_id)

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
            _manual_compact(console, history, cfg, client)
            continue
        if user_input == "/skills":
            _list_skills(console, cfg)
            continue
        if user_input == "/sessions":
            if store_path:
                _list_sessions_cli(console, store_path, session_id, cfg.model, transcript_writer)
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
                prefix = parts[1].strip()
                if store_path and session_dir:
                    result = _load_session_by_prefix(console, store_path, session_dir, prefix)
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
                            console.print(f"[dim]switched to session {session_id[:8]}… ({len(history)} messages)[/]")
                else:
                    console.print("[dim](no session store configured)[/]")
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
                transcript_writer=transcript_writer,
            )
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


def _list_sessions_cli(
    console: Console,
    store_path: Path,
    current_session_id: str | None = None,
    current_model: str = "",
    transcript_writer: TranscriptWriter | None = None,
) -> None:
    """Display available sessions in a table."""
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

    table = Table(title="Saved Sessions", border_style="cyan")
    table.add_column("Session ID", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Messages", justify="right")
    table.add_column("Compactions", justify="right")
    table.add_column("Last Active", style="dim")

    for s in sessions:
        last_active = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M:%S")
        is_current = (current_session_id and s.session_id == current_session_id) or (
            not current_session_id and s.session_id == store.get("lastSessionId")
        )
        marker = " ← current" if is_current else ""
        table.add_row(
            s.session_id[:8] + "…" + marker,
            s.model or "(unknown)",
            str(s.message_count),
            str(s.compaction_count),
            last_active,
        )

    console.print(table)


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
