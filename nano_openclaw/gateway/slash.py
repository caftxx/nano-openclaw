"""Shared slash-command dispatcher used by both REPL flavors.

Both ``cli.repl`` (embedded mode) and ``gateway/ws_repl`` (remote mode)
delegate to ``handle_slash`` here so users see the *same* tables, panels,
and feedback regardless of which Backend is powering the session.

Every command goes through Backend RPCs — never directly through
``runtime.registry`` / ``cfg``. That means:

- Wire-only consumers (``WebSocketBackend``) get the full slash surface.
- Embedded mode reuses the same renderers and the same RPC payloads, so
  drift between modes is impossible by construction.

``state`` is a small mutable dict the caller threads through. It carries:

- ``session_key``: current session_key. Updated by ``/new``, ``/session X``.
- ``session_changed``: set to True whenever the slash mutates which session
  the next chat.send should target. The caller observes it and rebinds any
  local state (e.g., subagent spawn ctx) accordingly.

Returns True when the command was handled (don't pass to chat.send),
False otherwise.

Special: ``/quit`` raises ``QuitREPL`` for the caller to catch — keeping the
shared module out of stdin/exit business.
"""

from __future__ import annotations

import shlex
from datetime import datetime
from typing import Any

from rich import markup
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nano_openclaw.gateway.backend import Backend, BackendError, BusyError, NotFoundError


# ────────────────────────────────────────────────────────────────────────────
# Help text — single source of truth, shared by both REPL banners
# ────────────────────────────────────────────────────────────────────────────


HELP_TEXT = (
    "/quit  /clear  /new  /help  /context  /compact  /sessions [all|delete <id>]  "
    "/session [prefix|#]  /skills  /plugins  /hooks  /tools  "
    "/subagents [list|kill <id>|all]  /active-memory [status|on|off|mode|style]  "
    "/dreaming [status|on|off|run]  /health  /channels  /runtime"
)


class QuitREPL(Exception):
    """Sentinel raised by ``/quit`` for the outer REPL to catch."""


# ────────────────────────────────────────────────────────────────────────────
# Public dispatcher
# ────────────────────────────────────────────────────────────────────────────


async def handle_slash(
    cmd: str,
    backend: Backend,
    console: Console,
    state: dict[str, Any],
) -> bool:
    """Dispatch a single slash command. See module docstring for ``state`` shape."""
    cmd = cmd.strip()
    if not cmd.startswith("/"):
        return False

    if cmd in {"/quit", "/exit", "/q"}:
        raise QuitREPL()

    if cmd == "/help":
        console.print(f"[dim]commands: {HELP_TEXT} — anything else is sent to the agent[/]")
        return True

    parts = cmd.split()
    verb = parts[0].lower()
    args = parts[1:]

    handler = _HANDLERS.get(verb)
    if handler is None:
        return False
    try:
        await handler(backend, console, state, args, cmd)
    except BusyError as exc:
        console.print(f"[yellow]busy:[/] {markup.escape(str(exc))} (retry in {exc.retry_after_ms}ms)")
    except NotFoundError as exc:
        console.print(f"[red]not found:[/] {markup.escape(str(exc))}")
    except BackendError as exc:
        console.print(f"[red]error:[/] {markup.escape(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{verb}:[/] {type(exc).__name__}: {markup.escape(str(exc))}")
    return True


# ────────────────────────────────────────────────────────────────────────────
# Session lifecycle
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_clear(backend, console, state, args, cmd):
    session_key = state.get("session_key") or ""
    if not session_key:
        console.print("[dim](no active session)[/]")
        return
    await backend.sessions_reset(session_key, reason="reset")
    console.print("[dim](history cleared)[/]")
    state["session_changed"] = True


async def _cmd_new(backend, console, state, args, cmd):
    session_key = state.get("session_key") or "default"
    info = await backend.sessions_reset(session_key, reason="new")
    state["session_key"] = info.session_id
    state["session_changed"] = True
    console.print(f"[dim]new session: {info.session_id[:8]}…[/]")


async def _cmd_restart(backend, console, state, args, cmd):
    """Restart the daemon NOW. Drops in-flight turns; user message is
    already on disk so the session resumes after restart."""
    console.print("[yellow]restarting gateway…[/]")
    try:
        info = await backend.gateway_restart()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]restart failed:[/] {type(exc).__name__}: {exc}")
        return
    strategy = info.get("strategy", "exec")
    pid = info.get("pid")
    console.print(f"[dim]strategy={strategy} pid={pid} — connection will drop[/]")


async def _cmd_sessions(backend, console, state, args, cmd):
    """Render the saved-sessions Table and handle the ``delete`` sub-verb."""
    if args and args[0].lower() == "delete":
        if len(args) < 2:
            console.print("[dim]usage: /sessions delete <id_prefix>[/]")
            return
        target_prefix = args[1]
        result = await backend.sessions_list()
        matches = [s for s in result.sessions if s.session_id.startswith(target_prefix)]
        if not matches:
            console.print(f"[dim]no session matches '{markup.escape(target_prefix)}'[/]")
            return
        if len(matches) > 1:
            console.print(f"[dim]{len(matches)} matches — be more specific[/]")
            return
        target = matches[0]
        await backend.sessions_delete(target.session_id)
        console.print(f"[dim]deleted session {target.session_id[:8]}…[/]")
        if state.get("session_key") == target.session_id:
            state["session_key"] = ""
            state["session_changed"] = True
        return

    show_all = bool(args and args[0].lower() == "all")
    result = await backend.sessions_list()
    _render_sessions_table(console, result, current_session_key=state.get("session_key"), show_all=show_all)


async def _cmd_session(backend, console, state, args, cmd):
    """``/session`` (no args) prints current; ``/session <prefix|#>`` switches."""
    if not args:
        sk = state.get("session_key") or ""
        console.print(f"[dim]current session: {sk or '(none)'}[/]")
        return

    key = args[0]
    result = await backend.sessions_list()
    target = None
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(result.sessions):
            target = result.sessions[idx]
    else:
        matches = [s for s in result.sessions if s.session_id.startswith(key)]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            console.print(f"[dim]{len(matches)} sessions match — be more specific:[/]")
            for s in matches:
                console.print(
                    f"  [cyan]{s.session_id[:12]}…[/]  {markup.escape(s.title or '(untitled)')}  "
                    f"{s.message_count} msgs"
                )
            return

    if target is None:
        console.print(f"[red]no session matches[/] {markup.escape(key)}")
        return

    if target.session_id == state.get("session_key"):
        console.print(f"[dim]already on session {target.session_id[:8]}…[/]")
        return

    state["session_key"] = target.session_id
    state["session_changed"] = True
    console.print(
        f"[dim]switched to session {target.session_id[:8]}…  "
        f"({target.message_count} msgs)[/]"
    )


# ────────────────────────────────────────────────────────────────────────────
# Context / compact
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_context(backend, console, state, args, cmd):
    snap = await backend.runtime_get()
    session_key = state.get("session_key") or ""
    msg_count = 0
    if session_key:
        try:
            payload = await backend.chat_history(session_key)
            msg_count = len(payload.history)
        except (BackendError, NotFoundError):
            msg_count = 0

    body = (
        f"messages: [cyan]{msg_count}[/]   "
        f"budget: [cyan]{snap.context_budget:,}[/]   "
        f"threshold: [cyan]{snap.context_threshold:.0%}[/]   "
        f"recent_turns: [cyan]{snap.context_recent_turns}[/]\n"
        f"model: [cyan]{markup.escape(snap.model_id)}[/]   "
        f"thinking: [cyan]{snap.thinking_level}[/]"
    )
    console.print(Panel.fit(Text.from_markup(body), title="Context", border_style="cyan"))


async def _cmd_compact(backend, console, state, args, cmd):
    session_key = state.get("session_key") or ""
    if not session_key:
        console.print("[dim](no active session)[/]")
        return
    result = await backend.sessions_compact(session_key)
    if not result.success:
        console.print(f"[dim]/compact: {result.summary or 'nothing to compact'}[/]")
        return
    console.print(
        f"[dim]compacted: {result.tokens_before:,} → {result.tokens_after:,} tokens[/]"
    )


# ────────────────────────────────────────────────────────────────────────────
# Introspection: tools / skills / plugins / hooks
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_tools(backend, console, state, args, cmd):
    tools = await backend.tools_list()
    if not tools:
        console.print("[dim]no tools registered[/]")
        return
    table = Table(title="Tools", border_style="cyan")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Description", style="white", no_wrap=False)
    for t in sorted(tools, key=lambda x: x.get("name", "")):
        desc = (t.get("description") or "").splitlines()[0][:120] if t.get("description") else ""
        table.add_row(t.get("name", ""), markup.escape(desc))
    console.print(table)
    console.print(f"[dim]{len(tools)} tool{'s' if len(tools) != 1 else ''} registered[/]")


async def _cmd_skills(backend, console, state, args, cmd):
    skills = await backend.skills_list()
    if not skills:
        console.print("[dim]no skills found (or workspace not configured)[/]")
        return
    table = Table(title="Skills", border_style="cyan")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="dim")
    table.add_column("Status", style="green")
    table.add_column("In Prompt", style="yellow")
    table.add_column("Reason", style="dim")
    eligible_count = 0
    visible_count = 0
    for s in sorted(skills, key=lambda x: x.get("name", "")):
        is_eligible = bool(s.get("eligible"))
        in_prompt = bool(s.get("in_prompt"))
        if is_eligible:
            eligible_count += 1
        if in_prompt:
            visible_count += 1
        status = "[green]eligible[/]" if is_eligible else "[red]blocked[/]"
        if in_prompt:
            prompt_cell = "[green]yes[/]"
        elif is_eligible:
            prompt_cell = "[yellow]no (hidden)[/]"
        else:
            prompt_cell = "[dim]—[/]"
        reason = s.get("reason") or ("" if in_prompt else ("gating failed" if not is_eligible else ""))
        if len(reason) > 40:
            reason = reason[:40] + "…"
        table.add_row(
            s.get("name", ""),
            s.get("source", ""),
            status,
            prompt_cell,
            markup.escape(reason),
        )
    console.print(table)
    blocked_count = len(skills) - eligible_count
    console.print(
        f"[dim]{eligible_count} eligible, {visible_count} in prompt, {blocked_count} blocked[/]"
    )


async def _cmd_plugins(backend, console, state, args, cmd):
    plugins = await backend.plugins_list()
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
    for p in sorted(plugins, key=lambda x: x.get("id", "") or x.get("name", "")):
        tools = ", ".join(p.get("tools") or []) or "[dim]—[/]"
        hooks = ", ".join(p.get("hooks") or []) or "[dim]—[/]"
        table.add_row(
            markup.escape(p.get("id", "")),
            markup.escape(p.get("name", "")),
            markup.escape(p.get("source", "")),
            markup.escape(p.get("entry", "")),
            "[green]loaded[/]",
            markup.escape(tools) if (p.get("tools") or []) else tools,
            markup.escape(hooks) if (p.get("hooks") or []) else hooks,
        )
    console.print(table)
    console.print(f"[dim]{len(plugins)} loaded plugin{'s' if len(plugins) != 1 else ''}[/]")


async def _cmd_hooks(backend, console, state, args, cmd):
    hooks = await backend.hooks_list()
    if not hooks:
        console.print("[dim]no hooks registered[/]")
        return
    table = Table(title="Hooks", border_style="cyan")
    table.add_column("Event", style="cyan")
    table.add_column("Handlers", style="green", justify="right")
    table.add_column("Plugins", style="yellow", no_wrap=False, max_width=44)
    table.add_column("Priorities", style="dim", no_wrap=False, max_width=28)
    total = 0
    for event in sorted(hooks):
        info = hooks[event]
        if isinstance(info, int):
            # Backwards-compat: legacy hooks_list shape was {event: count}.
            count, plugins, priorities = info, [], []
        else:
            count = info.get("count", 0)
            plugins = info.get("plugins") or []
            priorities = info.get("priorities") or []
        total += count
        table.add_row(
            markup.escape(event),
            str(count),
            markup.escape(", ".join(plugins)) if plugins else "[dim]—[/]",
            markup.escape(", ".join(str(p) for p in priorities)) if priorities else "[dim]—[/]",
        )
    console.print(table)
    console.print(
        f"[dim]{total} hook handler{'s' if total != 1 else ''} "
        f"across {len(hooks)} event{'s' if len(hooks) != 1 else ''}[/]"
    )


# ────────────────────────────────────────────────────────────────────────────
# Subagents
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_subagents(backend, console, state, args, cmd):
    sub = (args[0].lower() if args else "list")
    if sub == "kill":
        if len(args) < 2:
            console.print("[dim]usage: /subagents kill <run_id>[/]")
            return
        await backend.subagents_kill(args[1])
        console.print(f"[dim]killed subagent {args[1][:10]}…[/]")
        return
    items = await backend.subagents_list()
    if not items:
        console.print("[dim]no active subagent runs[/]")
        return
    table = Table(title="Active Subagent Runs", border_style="magenta")
    table.add_column("Run ID", style="cyan", width=10)
    table.add_column("Task", style="white", width=40)
    table.add_column("Status", style="yellow", width=10)
    for s in items:
        task_preview = (s.label or s.task or "")[:40]
        if s.task and len(s.task) > 40 and not s.label:
            task_preview += "…"
        table.add_row(s.run_id[:10], markup.escape(task_preview), s.status)
    console.print(table)


# ────────────────────────────────────────────────────────────────────────────
# Daemon-introspection passthroughs
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_health(backend, console, state, args, cmd):
    h = await backend.health()
    console.print(
        f"runtime_ready={h.runtime_ready}  channels={h.channels_running}  "
        f"sessions={h.sessions_loaded}  in_flight={h.in_flight_turns}"
    )


async def _cmd_channels(backend, console, state, args, cmd):
    statuses = await backend.channels_status()
    if not statuses:
        console.print("[dim](no channels running)[/]")
        return
    for c in statuses:
        color = "green" if c.state == "running" else "yellow"
        line = f"[{color}]{c.channel_id}/{c.account_id}[/{color}] · {c.state}"
        if c.error:
            line += f" · [red]{markup.escape(c.error)}[/red]"
        console.print(line)


async def _cmd_runtime(backend, console, state, args, cmd):
    snap = await backend.runtime_get()
    console.print(
        f"agent={snap.agent_id}  model={markup.escape(snap.model_id)}  "
        f"thinking={snap.thinking_level}  workspace={markup.escape(snap.workspace_dir)}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Active Memory / Dreaming
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_active_memory(backend, console, state, args, cmd):
    sub = args[0].lower() if args else "status"
    if sub == "status":
        cfg = await backend.active_memory_get()
        if not cfg.get("configured"):
            console.print(Panel.fit("Active Memory: [dim]not configured[/]", border_style="cyan"))
            return
        s = "enabled" if cfg["enabled"] else "disabled"
        color = "green" if cfg["enabled"] else "red"
        body = (
            f"State: [{color}]{s}[/]\n"
            f"Query Mode: [cyan]{cfg.get('query_mode')}[/]\n"
            f"Prompt Style: [cyan]{cfg.get('prompt_style')}[/]\n"
            f"Timeout: {cfg.get('timeout_ms')}ms\n"
            f"User Turns: {cfg.get('recent_user_turns')} / "
            f"Assistant Turns: {cfg.get('recent_assistant_turns')}"
        )
        console.print(Panel.fit(Text.from_markup(body), title="Active Memory", border_style="cyan"))
        return
    if sub in ("on", "off"):
        await backend.active_memory_set(enabled=(sub == "on"))
        console.print(f"[dim]Active Memory: {sub}[/]")
        return
    if sub == "mode":
        if len(args) < 2:
            console.print("[dim]usage: /active-memory mode <message|recent|full>[/]")
            return
        cfg = await backend.active_memory_set(query_mode=args[1].lower())
        console.print(f"[dim]Query mode: {cfg.get('query_mode')}[/]")
        return
    if sub == "style":
        if len(args) < 2:
            console.print("[dim]usage: /active-memory style <balanced|strict|...>[/]")
            return
        cfg = await backend.active_memory_set(prompt_style=args[1].lower())
        console.print(f"[dim]Prompt style: {cfg.get('prompt_style')}[/]")
        return
    console.print(
        "[dim]usage: /active-memory [status|on|off|mode <m>|style <s>][/]"
    )


async def _cmd_dreaming(backend, console, state, args, cmd):
    sub = args[0].lower() if args else "status"
    if sub == "status":
        cfg = await backend.dreaming_get()
        if not cfg.get("configured"):
            console.print(Panel.fit("Dreaming: [dim]not configured[/]", border_style="magenta"))
            return
        s = "enabled" if cfg["enabled"] else "disabled"
        color = "green" if cfg["enabled"] else "red"
        status_block = cfg.get("status") or {}
        last_run = status_block.get("last_run_at") or "never" if isinstance(status_block, dict) else "never"
        due_text = " [yellow](due)[/]" if isinstance(status_block, dict) and status_block.get("due") else ""
        tracked = status_block.get("total_tracked", 0) if isinstance(status_block, dict) else 0
        active = status_block.get("active_candidates", 0) if isinstance(status_block, dict) else 0
        promoted = status_block.get("promoted_total", 0) if isinstance(status_block, dict) else 0
        body = (
            f"State: [{color}]{s}[/]\n"
            f"Frequency: [cyan]{cfg.get('frequency')}[/]\n"
            f"Last Run: [cyan]{last_run}[/]{due_text}\n"
            f"Tracked: {tracked} entries | Active: {active} | Promoted: {promoted}"
        )
        console.print(Panel.fit(Text.from_markup(body), title="Dreaming", border_style="magenta"))
        return
    if sub in ("on", "off"):
        await backend.dreaming_set(enabled=(sub == "on"))
        console.print(f"[dim]Dreaming: {sub}[/]")
        return
    if sub == "run":
        console.print("[dim]Running dreaming sweep…[/]")
        result = await backend.dreaming_run()
        console.print(
            f"[dim]done in {result.get('elapsed_ms', 0)}ms · "
            f"candidates={result.get('candidates', 0)} · "
            f"promoted={len(result.get('promoted', []))}[/]"
        )
        for entry in result.get("promoted", []):
            console.print(
                f"  [green]↑[/] {entry.get('path')}:{entry.get('start_line')}  "
                f"score={entry.get('score', 0):.2f}"
            )
        return
    console.print("[dim]usage: /dreaming [status|on|off|run][/]")


# ────────────────────────────────────────────────────────────────────────────
# Renderers reused by the embedded REPL banner
# ────────────────────────────────────────────────────────────────────────────


def _render_sessions_table(
    console: Console,
    result: Any,
    *,
    current_session_key: str | None = None,
    show_all: bool = False,
) -> None:
    sessions = list(result.sessions)
    if not sessions:
        console.print("[dim](no sessions)[/]")
        return
    page_size = 12
    visible = sessions if show_all else sessions[:page_size]
    table = Table(title="Saved Sessions", border_style="cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Session ID", style="cyan")
    table.add_column("Description", style="white", no_wrap=False, max_width=62)
    table.add_column("Messages", justify="right")
    table.add_column("Last Active", style="dim")
    # The marker means "the session THIS REPL would chat into next" — there
    # is exactly one. If the caller provided a local session_key, that's
    # authoritative; otherwise fall back to the daemon's notion of "most
    # recent" (sessions.list sets ``current`` from store.lastSessionId, which
    # other clients may have changed).
    for idx, s in enumerate(visible, start=1):
        last_active = (
            datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
            if s.updated_at else "—"
        )
        if current_session_key:
            is_current = s.session_id == current_session_key
        else:
            is_current = s.current
        marker = " ← current" if is_current else ""
        snippet = s.preview or s.title or ""
        table.add_row(
            str(idx),
            s.session_id[:8] + "…" + marker,
            markup.escape(snippet) if snippet else "[dim](empty)[/]",
            str(s.message_count),
            last_active,
        )
    console.print(table)
    if not show_all and len(sessions) > page_size:
        hidden = len(sessions) - page_size
        console.print(
            f"[dim]showing {page_size} of {len(sessions)} — /sessions all to see {hidden} more[/]"
        )
    console.print("[dim]tip: /session #  or  /session <id-prefix>  to switch[/]")


# ────────────────────────────────────────────────────────────────────────────
# Dispatch table
# ────────────────────────────────────────────────────────────────────────────


_HANDLERS = {
    "/clear": _cmd_clear,
    "/new": _cmd_new,
    "/sessions": _cmd_sessions,
    "/session": _cmd_session,
    "/context": _cmd_context,
    "/compact": _cmd_compact,
    "/tools": _cmd_tools,
    "/skills": _cmd_skills,
    "/plugins": _cmd_plugins,
    "/hooks": _cmd_hooks,
    "/subagents": _cmd_subagents,
    "/health": _cmd_health,
    "/channels": _cmd_channels,
    "/runtime": _cmd_runtime,
    "/active-memory": _cmd_active_memory,
    "/dreaming": _cmd_dreaming,
    "/restart": _cmd_restart,
}
