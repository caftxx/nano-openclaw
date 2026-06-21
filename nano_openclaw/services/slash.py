"""Shared slash-command dispatcher used by every frontend.

TUI (adapters/cli/repl.py / adapters/cli/ws_repl.py), WebUI (adapters/webui/server.py) and WeChat
(channels/wechat) all delegate to ``handle_slash`` here, so users see
identical command behavior regardless of which frontend is talking to the
Backend. Each handler talks to the ``Backend`` Protocol — never to
``runtime.registry`` / ``cfg`` directly — and emits output through a
``SlashRenderer``. That gives us:

- One source of truth for slash semantics (no drift between frontends).
- Three rendering modes (Rich for TUI, Markdown for WebUI, Plain for
  WeChat / LLM tool) without three handler bodies.
- Wire-only consumers (``WebSocketBackend``) get the full slash surface.

``state`` is a small mutable dict the caller threads through:

- ``session_key``: current session_key. Updated by ``/new``, ``/session X``.
- ``session_changed``: set to True whenever the slash mutates which session
  the next chat.send should target. The caller observes it and rebinds any
  local state (e.g., subagent spawn ctx) accordingly.

Returns True when the command was handled, False otherwise.

Special: ``/quit`` raises ``QuitREPL`` for the caller to catch — keeping the
shared module out of stdin/exit business.
"""

from __future__ import annotations

from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from rich import markup
from rich.console import Console

from nano_openclaw.core.runtime_options import THINKING_LEVELS
from nano_openclaw.services.backend import Backend, BackendError, BusyError, NotFoundError
from nano_openclaw.services.slash_renderer import (
    MarkdownRenderer,
    PlainRenderer,
    RichRenderer,
    SlashRenderer,
)


# ────────────────────────────────────────────────────────────────────────────
# Help catalogue — single source of truth, shared by every banner + ``/help``
# ────────────────────────────────────────────────────────────────────────────


class HelpEntry(NamedTuple):
    """One slash command in the user-facing help.

    ``args`` is the bare arg-hint body (no surrounding brackets). The Rich
    one-liner wraps it in escaped ``\\[...]``; the structured table wraps it
    in ``(...)``. Renderers can't share bracket style — Rich treats ``[`` as
    markup whereas the others render it literally."""

    command: str
    args: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()


def _rich_token(entry: HelpEntry) -> str:
    """One token of the Rich one-liner: ``/cmd`` or ``/cmd \\[args]``."""
    return f"{entry.command} \\[{entry.args}]" if entry.args else entry.command


def _table_row(entry: HelpEntry) -> list[str]:
    """One row of the structured table: command (+ aliases + args) and desc."""
    name = " · ".join((entry.command, *entry.aliases))
    if entry.args:
        name = f"{name} ({entry.args})"
    return [name, entry.description]


class QuitREPL(Exception):
    """Sentinel raised by ``/quit`` for the outer REPL to catch."""


SlashHandler = Callable[[Backend, SlashRenderer, dict[str, Any], list[str], str], Awaitable[None]]


class SlashRegistry:
    """Small command registry used by the slash dispatcher."""

    def __init__(self) -> None:
        self._handlers: dict[str, SlashHandler] = {}
        self._entries: dict[str, HelpEntry] = {}

    def register(
        self,
        command: str,
        handler: SlashHandler,
        description: str = "",
        args: str = "",
        aliases: tuple[str, ...] = (),
    ) -> None:
        if not command.startswith("/"):
            raise ValueError("slash command must start with '/'")
        self._handlers[command] = handler
        self._entries[command] = HelpEntry(command, args, description, aliases)

    def handlers(self) -> dict[str, SlashHandler]:
        return dict(self._handlers)

    def entries(self) -> tuple[HelpEntry, ...]:
        return tuple(self._entries[command] for command in sorted(self._entries))


# ────────────────────────────────────────────────────────────────────────────
# Renderer adaptation
# ────────────────────────────────────────────────────────────────────────────


def _as_renderer(target: SlashRenderer | Console) -> SlashRenderer:
    """Allow callers to pass either the TUI's Rich Console or a SlashRenderer.

    Console is wrapped on the fly so local CLI, WebUI, and channel renderers
    share the same command handlers.
    """
    if isinstance(target, Console):
        return RichRenderer(target)
    return target


# ────────────────────────────────────────────────────────────────────────────
# Public dispatcher
# ────────────────────────────────────────────────────────────────────────────


async def handle_slash(
    cmd: str,
    backend: Backend,
    target: SlashRenderer | Console,
    state: dict[str, Any],
) -> bool:
    """Dispatch a single slash command through the shared registry."""
    cmd = cmd.strip()
    if not cmd.startswith("/"):
        return False

    if cmd in {"/quit", "/exit", "/q"}:
        raise QuitREPL()

    renderer = _as_renderer(target)

    if cmd == "/help":
        # The TUI banner is happy with a single ``dim`` line; non-Rich
        # renderers (Markdown / Plain) read better with a structured table
        # so a WebUI user sees clickable command names instead of one long
        # CSV, and a WeChat user sees one command per line. Only emit the
        # one-line CSV in Rich mode — for the others the table below is
        # already a complete listing.
        if isinstance(renderer, RichRenderer):
            renderer.dim(f"commands: {HELP_TEXT} — anything else is sent to the agent")
        else:
            renderer.table(
                ["Command", "Description"], HELP_TABLE_ROWS, title="Commands"
            )
        return True

    parts = cmd.split()
    verb = parts[0].lower()
    args = parts[1:]

    handler = _HANDLERS.get(verb)
    if handler is None:
        handler = _plugin_slash_handlers(backend).get(verb)
    if handler is None:
        return False
    try:
        await handler(backend, renderer, state, args, cmd)
    except BusyError as exc:
        renderer.warning(f"busy: {exc} (retry in {exc.retry_after_ms}ms)")
    except NotFoundError as exc:
        renderer.error(f"not found: {exc}")
    except BackendError as exc:
        renderer.error(f"error: {exc}")
    except Exception as exc:  # noqa: BLE001
        renderer.error(f"{verb}: {type(exc).__name__}: {exc}")
    return True


def _plugin_slash_handlers(backend: Backend) -> dict[str, SlashHandler]:
    runtime = getattr(backend, "runtime", None)
    hook_registry = getattr(runtime, "hook_registry", None)
    if hook_registry is None or not hasattr(hook_registry, "slash_handlers"):
        return {}
    return hook_registry.slash_handlers()


# ────────────────────────────────────────────────────────────────────────────
# Session lifecycle
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_clear(backend, renderer: SlashRenderer, state, args, cmd):
    session_key = state.get("session_key") or ""
    if not session_key:
        renderer.dim("(no active session)")
        return
    await backend.sessions_reset(session_key, reason="reset")
    renderer.dim("(history cleared)")
    state["session_changed"] = True


async def _cmd_new(backend, renderer: SlashRenderer, state, args, cmd):
    session_key = state.get("session_key") or "default"
    info = await backend.sessions_reset(session_key, reason="new")
    state["session_key"] = info.session_id
    state["session_changed"] = True
    renderer.dim(f"new session: {info.session_id[:8]}…")


async def _cmd_restart(backend, renderer: SlashRenderer, state, args, cmd):
    """Restart the daemon NOW. Drops in-flight turns; user message is
    already on disk so the session resumes after restart."""
    renderer.warning("restarting gateway…")
    try:
        info = await backend.gateway_restart()
    except Exception as exc:  # noqa: BLE001
        renderer.error(f"restart failed: {type(exc).__name__}: {exc}")
        return
    strategy = info.get("strategy", "exec")
    pid = info.get("pid")
    renderer.dim(f"strategy={strategy} pid={pid} — connection will drop")


async def _cmd_sessions(backend, renderer: SlashRenderer, state, args, cmd):
    """Render the saved-sessions Table and handle the ``delete`` sub-verb."""
    if args and args[0].lower() == "delete":
        if len(args) < 2:
            renderer.dim("usage: /sessions delete <id_prefix>")
            return
        target_prefix = args[1]
        result = await backend.sessions_list()
        matches = [s for s in result.sessions if s.session_id.startswith(target_prefix)]
        if not matches:
            renderer.dim(f"no session matches '{target_prefix}'")
            return
        if len(matches) > 1:
            renderer.dim(f"{len(matches)} matches — be more specific")
            return
        target = matches[0]
        await backend.sessions_delete(target.session_id)
        renderer.dim(f"deleted session {target.session_id[:8]}…")
        if state.get("session_key") == target.session_id:
            state["session_key"] = ""
            state["session_changed"] = True
        return

    show_all = bool(args and args[0].lower() == "all")
    result = await backend.sessions_list()
    _render_sessions_table(renderer, result, current_session_key=state.get("session_key"), show_all=show_all)


async def _cmd_session(backend, renderer: SlashRenderer, state, args, cmd):
    """``/session`` (no args) prints current; ``/session <prefix|#>`` switches."""
    if not args:
        sk = state.get("session_key") or ""
        renderer.dim(f"current session: {sk or '(none)'}")
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
            renderer.dim(f"{len(matches)} sessions match — be more specific:")
            for s in matches:
                renderer.dim(
                    f"  {s.session_id[:12]}…  {s.title or '(untitled)'}  {s.message_count} msgs"
                )
            return

    if target is None:
        renderer.error(f"no session matches {key}")
        return

    if target.session_id == state.get("session_key"):
        renderer.dim(f"already on session {target.session_id[:8]}…")
        return

    state["session_key"] = target.session_id
    state["session_changed"] = True
    renderer.dim(
        f"switched to session {target.session_id[:8]}…  ({target.message_count} msgs)"
    )


# ────────────────────────────────────────────────────────────────────────────
# Context / compact
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_context(backend, renderer: SlashRenderer, state, args, cmd):
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
    renderer.panel(body, title="Context", style="info")


async def _cmd_compact(backend, renderer: SlashRenderer, state, args, cmd):
    session_key = state.get("session_key") or ""
    if not session_key:
        renderer.dim("(no active session)")
        return
    result = await backend.sessions_compact(session_key)
    if not result.success:
        renderer.dim(f"/compact: {result.summary or 'nothing to compact'}")
        return
    renderer.dim(
        f"compacted: {result.tokens_before:,} → {result.tokens_after:,} tokens"
    )


async def _cmd_usage(backend, renderer: SlashRenderer, state, args, cmd):
    """Render this session's token, cache, and compaction counters.

    "% of budget" is computed off ``last_prompt_tokens`` (= input +
    cache_read + cache_creation, the total prompt the model saw last
    turn). Same signal ``compact_if_needed`` watches for its trigger,
    so the number visible here predicts when compaction will fire.

    Note: ``ctx`` rather than ``in`` to make clear that this is the
    full prompt size including cached portions, not Anthropic's
    ``input_tokens`` (which is only the billable, non-cached slice).
    """
    session_key = state.get("session_key") or ""
    if not session_key:
        renderer.dim("(no active session)")
        return
    report = await backend.sessions_usage(session_key)

    pct = (
        report.last_prompt_tokens / report.context_budget * 100
        if report.context_budget else 0.0
    )
    cache_hit_pct = (
        f"{report.cache_hit_ratio * 100:.0f}%"
        if report.cache_hit_ratio is not None
        else "—"
    )
    cache_status = (
        f"on ({markup.escape(report.cache_ttl)} TTL)"
        if report.cache_ttl else "off"
    )
    body_lines = [
        f"last prompt: [cyan]{report.last_prompt_tokens:,}[/] ctx   "
        f"[cyan]{report.last_output_tokens:,}[/] out   "
        f"([cyan]{pct:.1f}%[/] of [cyan]{report.context_budget:,}[/] budget)",
        f"cumulative:  [cyan]{report.total_prompt_tokens:,}[/] ctx   "
        f"[cyan]{report.total_output_tokens:,}[/] out   "
        f"({report.turns_recorded} turn{'s' if report.turns_recorded != 1 else ''})",
        f"cache:       {cache_status}   "
        f"hit ratio [cyan]{cache_hit_pct}[/]   "
        f"read [cyan]{report.total_cache_read_tokens:,}[/]   "
        f"creation [cyan]{report.total_cache_creation_tokens:,}[/]",
        f"compactions: [cyan]{report.compactions_fired}[/] this session",
    ]
    renderer.panel("\n".join(body_lines), title="Usage", style="info")


async def _cmd_todos(backend, renderer: SlashRenderer, state, args, cmd):
    """Show the current TODO list for the active session.

    Status markers mirror what the model sees post-compact:
    ``[ ]`` pending / ``[>]`` in_progress / ``[x]`` completed / ``[~]`` cancelled.
    """
    session_key = state.get("session_key") or ""
    if not session_key:
        renderer.dim("(no active session)")
        return
    try:
        items = await backend.get_todos(session_key)
    except Exception as exc:  # noqa: BLE001 — surface to user
        renderer.dim(f"(failed to fetch todos: {type(exc).__name__}: {exc})")
        return

    if not items:
        renderer.dim("(no todos in this session)")
        return

    markers = {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
        "cancelled": "[~]",
    }
    rows = []
    for item in items:
        status = str(item.get("status", "pending"))
        marker = markers.get(status, "[?]")
        rows.append([
            marker,
            str(item.get("id", "")),
            str(item.get("content", "")),
            status,
        ])
    renderer.table(["", "ID", "Content", "Status"], rows, title="Todos")
    pending = sum(1 for i in items if i.get("status") == "pending")
    in_progress = sum(1 for i in items if i.get("status") == "in_progress")
    completed = sum(1 for i in items if i.get("status") == "completed")
    cancelled = sum(1 for i in items if i.get("status") == "cancelled")
    renderer.dim(
        f"{len(items)} total · {in_progress} in_progress · {pending} pending · "
        f"{completed} completed · {cancelled} cancelled"
    )


# ────────────────────────────────────────────────────────────────────────────
# Introspection: tools / skills / plugins / hooks
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_tools(backend, renderer: SlashRenderer, state, args, cmd):
    tools = await backend.tools_list()
    if not tools:
        renderer.dim("no tools registered")
        return
    rows = []
    for t in sorted(tools, key=lambda x: x.get("name", "")):
        desc = (t.get("description") or "").splitlines()[0][:120] if t.get("description") else ""
        rows.append([t.get("name", ""), desc])
    renderer.table(["Name", "Description"], rows, title="Tools")
    renderer.dim(f"{len(tools)} tool{'s' if len(tools) != 1 else ''} registered")


async def _cmd_skills(backend, renderer: SlashRenderer, state, args, cmd):
    skills = await backend.skills_list()
    if not skills:
        renderer.dim("no skills found (or workspace not configured)")
        return
    rows = []
    eligible_count = 0
    visible_count = 0
    for s in sorted(skills, key=lambda x: x.get("name", "")):
        is_eligible = bool(s.get("eligible"))
        in_prompt = bool(s.get("in_prompt"))
        if is_eligible:
            eligible_count += 1
        if in_prompt:
            visible_count += 1
        status = "eligible" if is_eligible else "blocked"
        if in_prompt:
            prompt_cell = "yes"
        elif is_eligible:
            prompt_cell = "no (hidden)"
        else:
            prompt_cell = "—"
        reason = s.get("reason") or ("" if in_prompt else ("gating failed" if not is_eligible else ""))
        if len(reason) > 40:
            reason = reason[:40] + "…"
        rows.append([
            s.get("name", ""),
            s.get("source", ""),
            status,
            prompt_cell,
            reason,
        ])
    renderer.table(["Name", "Source", "Status", "In Prompt", "Reason"], rows, title="Skills")
    blocked_count = len(skills) - eligible_count
    renderer.dim(
        f"{eligible_count} eligible, {visible_count} in prompt, {blocked_count} blocked"
    )


async def _cmd_plugins(backend, renderer: SlashRenderer, state, args, cmd):
    plugins = await backend.plugins_list()
    if not plugins:
        renderer.dim("no plugins loaded")
        return
    rows = []
    for p in sorted(plugins, key=lambda x: x.get("id", "") or x.get("name", "")):
        tools = ", ".join(p.get("tools") or []) or "—"
        hooks = ", ".join(p.get("hooks") or []) or "—"
        slash = ", ".join(p.get("slash") or []) or "—"
        channels = ", ".join(p.get("channels") or []) or "—"
        features = ", ".join(p.get("features") or []) or "—"
        rows.append([
            p.get("id", ""),
            p.get("name", ""),
            p.get("source", ""),
            p.get("entry", ""),
            "loaded",
            tools,
            hooks,
            slash,
            channels,
            features,
        ])
    renderer.table(
        ["ID", "Name", "Source", "Entry", "Status", "Tools", "Hooks", "Slash", "Channels", "Features"],
        rows,
        title="Plugins",
    )
    renderer.dim(f"{len(plugins)} loaded plugin{'s' if len(plugins) != 1 else ''}")


async def _cmd_hooks(backend, renderer: SlashRenderer, state, args, cmd):
    hooks = await backend.hooks_list()
    if not hooks:
        renderer.dim("no hooks registered")
        return
    rows = []
    total = 0
    for event in sorted(hooks):
        info = hooks[event]
        if isinstance(info, int):
            count, plugins, priorities = info, [], []
        else:
            count = info.get("count", 0)
            plugins = info.get("plugins") or []
            priorities = info.get("priorities") or []
        total += count
        rows.append([
            event,
            str(count),
            ", ".join(plugins) if plugins else "—",
            ", ".join(str(p) for p in priorities) if priorities else "—",
        ])
    renderer.table(["Event", "Handlers", "Plugins", "Priorities"], rows, title="Hooks")
    renderer.dim(
        f"{total} hook handler{'s' if total != 1 else ''} "
        f"across {len(hooks)} event{'s' if len(hooks) != 1 else ''}"
    )


async def _cmd_mcp(backend, renderer: SlashRenderer, state, args, cmd):
    status = await backend.mcp_status()
    if not status.get("configured"):
        renderer.panel("No MCP servers configured.", title="MCP", style="info")
        return

    rows = []
    for server in status.get("servers") or []:
        error = str(server.get("error") or "")
        if len(error) > 180:
            error = error[:177].rstrip() + "..."
        rows.append([
            str(server.get("name") or ""),
            str(server.get("transport") or "unknown"),
            str(server.get("status") or "unknown"),
            str(server.get("tools") or 0),
            error or "—",
        ])
    renderer.table(["Server", "Transport", "Status", "Tools", "Reason"], rows, title="MCP")
    summary = (
        f"{status.get('connected', 0)} connected · "
        f"{status.get('failed', 0)} failed · "
        f"{status.get('starting', 0)} starting · "
        f"{status.get('total_tools', 0)} tool(s)"
    )
    load_error = str(status.get("load_error") or "")
    if load_error:
        summary += f" · load error: {load_error}"
    renderer.dim(summary)


# ────────────────────────────────────────────────────────────────────────────
# Subagents
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_subagents(backend, renderer: SlashRenderer, state, args, cmd):
    sub = (args[0].lower() if args else "list")
    if sub == "kill":
        if len(args) < 2:
            renderer.dim("usage: /subagents kill <run_id>")
            return
        await backend.subagents_kill(args[1])
        renderer.dim(f"killed subagent {args[1][:10]}…")
        return
    items = await backend.subagents_list()
    if not items:
        renderer.dim("no active subagent runs")
        return
    rows = []
    for s in items:
        task_preview = (s.label or s.task or "")[:40]
        if s.task and len(s.task) > 40 and not s.label:
            task_preview += "…"
        rows.append([s.run_id[:10], task_preview, s.status])
    renderer.table(["Run ID", "Task", "Status"], rows, title="Active Subagent Runs")


# ────────────────────────────────────────────────────────────────────────────
# Daemon-introspection passthroughs
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_health(backend, renderer: SlashRenderer, state, args, cmd):
    h = await backend.health()
    renderer.text(
        f"runtime_ready={h.runtime_ready}  channels={h.channels_running}  "
        f"sessions={h.sessions_loaded}  in_flight={h.in_flight_turns}"
    )


async def _cmd_channels(backend, renderer: SlashRenderer, state, args, cmd):
    statuses = await backend.channels_status()
    if not statuses:
        renderer.dim("(no channels running)")
        return
    for c in statuses:
        line = f"{c.channel_id}/{c.account_id} · {c.state}"
        if c.error:
            line += f" · {c.error}"
        if c.state == "running":
            renderer.success(line)
        elif c.error:
            renderer.error(line)
        else:
            renderer.warning(line)


async def _cmd_runtime(backend, renderer: SlashRenderer, state, args, cmd):
    snap = await backend.runtime_get()
    renderer.text(
        f"agent={snap.agent_id}  model={snap.model_id}  "
        f"thinking={snap.thinking_level}  workspace={snap.workspace_dir}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Models — list / set
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_models(backend, renderer: SlashRenderer, state, args, cmd):
    """List every model declared under ``config.models.providers``.

    The current model is highlighted via ``current_row_idx`` so renderers can
    decorate it differently (Rich → bold green; Markdown → bold + ← current;
    Plain → leading ``*``).
    """
    choices = await backend.models_list()
    if not choices:
        renderer.dim("no models configured")
        return
    snap = await backend.runtime_get()
    current_ref = snap.model_ref
    rows = []
    current_idx: int | None = None
    for i, m in enumerate(choices):
        if m.is_default or m.ref == current_ref:
            current_idx = i
        inputs = ", ".join(m.input) if m.input else "text"
        ctx = f"{m.context_window:,}" if m.context_window else "—"
        rows.append([
            m.ref,
            m.name or m.id,
            inputs,
            "yes" if m.reasoning else "no",
            ctx,
        ])
    renderer.table(
        ["Ref", "Name", "Inputs", "Reasoning", "Context"],
        rows,
        title="Models",
        current_row_idx=current_idx,
    )
    renderer.dim(f"{len(choices)} model{'s' if len(choices) != 1 else ''} · /model <ref> to switch")


async def _cmd_model(backend, renderer: SlashRenderer, state, args, cmd):
    """``/model`` (no args) shows the active runtime; ``/model <ref|id>`` switches.

    On switch, ``RuntimeUpdateGuard.writer()`` enforces "no active turn" via
    ``BusyError`` — surfaced as the standard ``warning(busy: ...)`` line. Other
    errors (unknown ref, build failure) come back as ``renderer.error``.
    """
    if not args:
        snap = await backend.runtime_get()
        body = (
            f"model_ref:    {markup.escape(snap.model_ref)}\n"
            f"model_id:     {markup.escape(snap.model_id)}\n"
            f"image_model:  {markup.escape(snap.image_model_ref or '—')}\n"
            f"thinking:     {snap.thinking_level}\n"
            f"context:      {snap.context_budget:,} budget · {snap.context_window:,} window"
        )
        renderer.panel(body, title="Current Model", style="info")
        return

    query = " ".join(args).strip()
    # Fetch current snapshot first so we can detect the no-op case before
    # resolving — avoids spamming the catalog parse on a no-op.
    snap_before = await backend.runtime_get()
    try:
        target = _resolve_model_choice(await backend.models_list(), query)
    except KeyError:
        renderer.error(f"unknown model: {query}. /models to list available.")
        return
    except ValueError as exc:
        renderer.error(str(exc))
        return

    target_ref = target.ref
    target_name = target.name or target.id or target_ref

    if target_ref == snap_before.model_ref:
        renderer.dim(f"model already set to {target_name} ({target_ref})")
        return

    old_ref = snap_before.model_ref
    new_snap = await backend.runtime_update(model_ref=target_ref)
    renderer.success(f"model set to {target_name} ({new_snap.model_ref}); previous {old_ref}")


def _resolve_model_choice(choices, query: str):
    query = (query or "").strip()
    if not query:
        raise KeyError("empty model query")

    for choice in choices:
        if choice.ref == query:
            return choice

    if "/" in query:
        raise KeyError(f"unknown model: {query}")

    matches = [choice for choice in choices if choice.id == query]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        refs = ", ".join(choice.ref for choice in matches)
        raise ValueError(f"ambiguous: {query} - try one of {refs}")
    raise KeyError(f"unknown model: {query}")


# ────────────────────────────────────────────────────────────────────────────
# Thinking level
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_thinking(backend, renderer: SlashRenderer, state, args, cmd):
    """``/thinking`` (no args) shows the active level; ``/thinking <level>``
    sets it. Goes through ``backend.runtime_update`` so the change is
    coordinated by ``RuntimeUpdateGuard.writer()`` — no in-flight turn
    races a half-applied ``thinking_level`` field.
    """
    if not args:
        snap = await backend.runtime_get()
        renderer.text(
            f"thinking: {snap.thinking_level}  (allowed: {' | '.join(THINKING_LEVELS)})"
        )
        return

    target = args[0].lower()
    if target not in THINKING_LEVELS:
        renderer.error(
            f"unknown thinking level: {target}. allowed: {' | '.join(THINKING_LEVELS)}"
        )
        return

    snap_before = await backend.runtime_get()
    if snap_before.thinking_level == target:
        renderer.dim(f"thinking already set to {target}")
        return
    new_snap = await backend.runtime_update(thinking_level=target)
    renderer.success(
        f"thinking: {snap_before.thinking_level} → {new_snap.thinking_level}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Active Memory / Dreaming
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_active_memory(backend, renderer: SlashRenderer, state, args, cmd):
    sub = args[0].lower() if args else "status"
    if sub == "status":
        cfg = await backend.active_memory_get()
        if not cfg.get("configured"):
            renderer.panel("Active Memory: not configured", title="Active Memory", style="info")
            return
        s = "enabled" if cfg["enabled"] else "disabled"
        body = (
            f"State: {s}\n"
            f"Query Mode: {cfg.get('query_mode')}\n"
            f"Prompt Style: {cfg.get('prompt_style')}\n"
            f"Timeout: {cfg.get('timeout_ms')}ms\n"
            f"User Turns: {cfg.get('recent_user_turns')} / "
            f"Assistant Turns: {cfg.get('recent_assistant_turns')}"
        )
        renderer.panel(body, title="Active Memory", style="info")
        return
    if sub in ("on", "off"):
        await backend.active_memory_set(enabled=(sub == "on"))
        renderer.dim(f"Active Memory: {sub}")
        return
    if sub == "mode":
        if len(args) < 2:
            renderer.dim("usage: /active-memory mode <message|recent|full>")
            return
        cfg = await backend.active_memory_set(query_mode=args[1].lower())
        renderer.dim(f"Query mode: {cfg.get('query_mode')}")
        return
    if sub == "style":
        if len(args) < 2:
            renderer.dim("usage: /active-memory style <balanced|strict|...>")
            return
        cfg = await backend.active_memory_set(prompt_style=args[1].lower())
        renderer.dim(f"Prompt style: {cfg.get('prompt_style')}")
        return
    renderer.dim("usage: /active-memory [status|on|off|mode <m>|style <s>]")


async def _cmd_dreaming(backend, renderer: SlashRenderer, state, args, cmd):
    sub = args[0].lower() if args else "status"
    if sub == "status":
        cfg = await backend.dreaming_get()
        if not cfg.get("configured"):
            renderer.panel("Dreaming: not configured", title="Dreaming", style="info")
            return
        s = "enabled" if cfg["enabled"] else "disabled"
        status_block = cfg.get("status") or {}
        last_run = status_block.get("last_run_at") or "never" if isinstance(status_block, dict) else "never"
        due_text = " (due)" if isinstance(status_block, dict) and status_block.get("due") else ""
        tracked = status_block.get("total_tracked", 0) if isinstance(status_block, dict) else 0
        active = status_block.get("active_candidates", 0) if isinstance(status_block, dict) else 0
        promoted = status_block.get("promoted_total", 0) if isinstance(status_block, dict) else 0
        body = (
            f"State: {s}\n"
            f"Frequency: {cfg.get('frequency')}\n"
            f"Last Run: {last_run}{due_text}\n"
            f"Tracked: {tracked} entries | Active: {active} | Promoted: {promoted}"
        )
        renderer.panel(body, title="Dreaming", style="info")
        return
    if sub in ("on", "off"):
        await backend.dreaming_set(enabled=(sub == "on"))
        renderer.dim(f"Dreaming: {sub}")
        return
    if sub == "run":
        renderer.dim("Running dreaming sweep…")
        result = await backend.dreaming_run()
        renderer.dim(
            f"done in {result.get('elapsed_ms', 0)}ms · "
            f"candidates={result.get('candidates', 0)} · "
            f"promoted={len(result.get('promoted', []))}"
        )
        for entry in result.get("promoted", []):
            renderer.dim(
                f"  ↑ {entry.get('path')}:{entry.get('start_line')}  "
                f"score={entry.get('score', 0):.2f}"
            )
        return
    renderer.dim("usage: /dreaming [status|on|off|run]")


async def _cmd_review_fork(backend, renderer: SlashRenderer, state, args, cmd):
    """Inspect / toggle / force-trigger the Background Review Fork plugin.

    All operations go through the Backend protocol so embedded + remote modes
    behave identically.
    """
    sub = args[0].lower() if args else "status"

    def _render_status(s: dict[str, Any]) -> None:
        if not s.get("configured"):
            renderer.panel(
                "Review Fork: plugin not loaded.\n"
                "Add nano-review-fork to plugins (default builtin) and restart.",
                title="Review Fork",
                style="info",
            )
            return
        body = (
            f"State: {'enabled' if s.get('enabled') else 'disabled'}\n"
            f"Trigger every: {s.get('trigger_n')} end_turn(s)  ·  Cooldown: {s.get('cooldown_s')}s  ·  Timeout: {s.get('timeout_s')}s\n"
            f"Aux Model: {s.get('model_aux') or '(follow parent)'}\n"
            f"Counter: {s.get('turn_counter')}  ·  Total Runs: {s.get('total_runs')}  ·  Skipped: {s.get('total_skipped')}\n"
            f"Cooldown Remaining: {(s.get('cooldown_remaining_s') or 0):.1f}s\n"
            f"Active Run: {s.get('active_run_id') or '(none)'}\n"
            f"Last Skip Reason: {s.get('last_skip_reason') or '(n/a)'}"
        )
        renderer.panel(body, title="Review Fork", style="info")

    if sub == "status":
        try:
            s = await backend.review_fork_get()
        except BackendError as exc:
            renderer.dim(f"review_fork.get failed: {exc}")
            return
        _render_status(s)
        return
    if sub in ("on", "off"):
        try:
            s = await backend.review_fork_set(enabled=(sub == "on"))
        except (BackendError, NotFoundError) as exc:
            renderer.dim(f"review_fork.set failed: {exc}")
            return
        _render_status(s)
        return
    if sub == "run":
        try:
            result = await backend.review_fork_run(session_key=state.get("session_key") or None)
        except (BackendError, NotFoundError) as exc:
            renderer.dim(f"review_fork.run failed: {exc}")
            return
        if result.get("skipped"):
            renderer.dim(f"review fork skipped: {result.get('reason') or 'unknown'}")
        else:
            renderer.panel(
                f"Spawned review fork.\nrun_id: {result.get('run_id')}",
                title="Review Fork",
                style="info",
            )
        return
    renderer.dim("usage: /review-fork [status|on|off|run]")


async def _cmd_curator(backend, renderer: SlashRenderer, state, args, cmd):
    """Inspect / toggle / run Curator Lite."""
    sub = args[0].lower() if args else "status"

    def _fmt_local(iso: str | None) -> str:
        """Render a UTC ISO-8601 timestamp from telemetry as local time.
        Falls back to the raw string if parsing fails — telemetry rows may
        carry partial / legacy formats we don't want to swallow."""
        if not iso:
            return "never"
        try:
            return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return iso

    def _render_status(s: dict[str, Any]) -> None:
        if not s.get("configured", True):
            renderer.panel("Curator: not configured", title="Curator", style="info")
            return
        counts = s.get("counts") or {}
        body = (
            f"State: {'enabled' if s.get('enabled') else 'disabled'}"
            f"{' (paused)' if s.get('paused') else ''}\n"
            f"Skills: {s.get('total', 0)} total · "
            f"{counts.get('active', 0)} active · {counts.get('stale', 0)} stale · "
            f"{counts.get('archived', 0)} archived\n"
            f"Rules: stale after {s.get('stale_after_days')}d · "
            f"archive after {s.get('archive_after_days')}d\n"
            f"Runs: {s.get('run_count', 0)} · Last: {_fmt_local(s.get('last_run_at'))}\n"
            f"Summary: {s.get('last_run_summary') or '(none)'}\n"
            f"Report: {s.get('last_report_path') or '(none)'}"
        )
        renderer.panel(body, title="Curator", style="info")
        least = s.get("least_recent") or []
        if least:
            rows = []
            for row in least[:8]:
                rows.append([
                    row.get("name", ""),
                    row.get("state", ""),
                    str(row.get("activity_count", 0)),
                    _fmt_local(row.get("last_activity_at")),
                ])
            renderer.table(["Skill", "State", "Activity", "Last Activity"], rows, title="Least Recent Skills")

    if sub == "status":
        _render_status(await backend.curator_get())
        return
    if sub in ("on", "off"):
        _render_status(await backend.curator_set(enabled=(sub == "on")))
        return
    if sub in ("pause", "resume"):
        _render_status(await backend.curator_set(paused=(sub == "pause")))
        return
    if sub in ("run", "dry-run"):
        result = await backend.curator_run(dry_run=(sub == "dry-run"))
        if result.get("skipped"):
            renderer.dim(f"curator skipped: {result.get('reason')}")
            return
        counts = result.get("counts") or {}
        renderer.panel(
            f"checked={counts.get('checked', 0)}  "
            f"stale={counts.get('marked_stale', 0)}  "
            f"archived={counts.get('archived', 0)}  "
            f"reactivated={counts.get('reactivated', 0)}\n"
            f"report: {result.get('report_path')}",
            title="Curator",
            style="info",
        )
        return
    renderer.dim("usage: /curator [status|on|off|pause|resume|run|dry-run]")


async def _cmd_checkpoint(backend, renderer: SlashRenderer, state, args, cmd):
    sub = args[0].lower() if args else "list"
    if sub == "list":
        result = await backend.checkpoint_list()
        checkpoints = result.get("checkpoints") or []
        if not checkpoints:
            renderer.dim("(no checkpoints)")
            return
        rows = [
            [cp.get("id", "")[:18], cp.get("created_at", ""), cp.get("reason", "")]
            for cp in checkpoints[:20]
        ]
        renderer.table(["ID", "Created", "Reason"], rows, title="Checkpoints")
        return
    if sub == "create":
        reason = " ".join(args[1:]).strip() or "manual"
        result = await backend.checkpoint_create(reason=reason)
        cp = result.get("checkpoint") or {}
        renderer.dim(f"checkpoint created: {cp.get('id')}")
        return
    if sub == "restore":
        if len(args) < 2:
            renderer.dim("usage: /checkpoint restore <id_prefix>")
            return
        result = await backend.checkpoint_restore(args[1])
        cp = result.get("restored") or {}
        renderer.warning(f"restored checkpoint: {cp.get('id')}")
        return
    renderer.dim("usage: /checkpoint [list|create [reason]|restore <id_prefix>]")


# ────────────────────────────────────────────────────────────────────────────
# Renderers reused by the embedded REPL banner
# ────────────────────────────────────────────────────────────────────────────


def _render_sessions_table(
    target: SlashRenderer | Console,
    result: Any,
    *,
    current_session_key: str | None = None,
    show_all: bool = False,
) -> None:
    """Render the sessions list with a single ``← current`` marker.

    ``target`` may be either a Rich ``Console`` (CLI adapters) or a
    ``SlashRenderer``. Both paths emit the same marker logic, so the existing
    snapshot tests remain valid.
    """
    renderer = _as_renderer(target)
    sessions = list(result.sessions)
    if not sessions:
        renderer.dim("(no sessions)")
        return
    page_size = 12
    visible = sessions if show_all else sessions[:page_size]
    rows = []
    current_idx: int | None = None
    # Determine which row gets the "current" decoration. If the local caller
    # supplied a session_key that's authoritative (it's the session the next
    # chat.send will target). Otherwise fall back to the daemon's notion of
    # "most recent" via the per-session ``current`` flag.
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
        if is_current:
            current_idx = idx - 1
        snippet = s.preview or s.title or ""
        rows.append([
            str(idx),
            s.session_id[:8] + "…" + marker,
            snippet if snippet else "(empty)",
            str(s.message_count),
            last_active,
        ])
    renderer.table(
        ["#", "Session ID", "Description", "Messages", "Last Active"],
        rows,
        title="Saved Sessions",
        current_row_idx=current_idx,
    )
    if not show_all and len(sessions) > page_size:
        hidden = len(sessions) - page_size
        renderer.dim(
            f"showing {page_size} of {len(sessions)} — /sessions all to see {hidden} more"
        )
    renderer.dim("tip: /session #  or  /session <id-prefix>  to switch")


# ────────────────────────────────────────────────────────────────────────────
# Renderer-mode helper — frontends call this when they hold a Backend already
# but don't want to thread their own renderer construction.
# ────────────────────────────────────────────────────────────────────────────


def renderer_for(mode: str, *, console: Console | None = None, **kwargs) -> SlashRenderer:
    """Pick a renderer by frontend name. ``mode`` is one of:
    ``"rich"`` / ``"tui"`` (requires ``console``), ``"markdown"`` /
    ``"webui"``, ``"plain"`` / ``"wechat"`` (kwargs forwarded to PlainRenderer).
    """
    if mode in ("rich", "tui"):
        if console is None:
            raise ValueError("rich renderer requires a Console")
        return RichRenderer(console)
    if mode in ("markdown", "md", "webui"):
        return MarkdownRenderer()
    if mode in ("plain", "wechat", "tool"):
        return PlainRenderer(**kwargs)
    raise ValueError(f"unknown renderer mode: {mode}")


# ────────────────────────────────────────────────────────────────────────────
# Built-in registration
# ────────────────────────────────────────────────────────────────────────────


async def _cmd_quit(_backend, _renderer: SlashRenderer, _state, _args, _cmd):
    raise QuitREPL()


async def _cmd_help(_backend, _renderer: SlashRenderer, _state, _args, _cmd):
    return None


def _register_service_slash(registry: SlashRegistry) -> None:
    registry.register("/channels", _cmd_channels, "Running channels")
    registry.register("/clear", _cmd_clear, "Clear current session history")
    registry.register("/compact", _cmd_compact, "Summarize / compact history")
    registry.register("/context", _cmd_context, "Context-window budget snapshot")
    registry.register("/health", _cmd_health, "Daemon health snapshot")
    registry.register("/help", _cmd_help, "Show this list")
    registry.register("/hooks", _cmd_hooks, "Registered hook handlers")
    registry.register("/mcp", _cmd_mcp, "MCP server status")
    registry.register("/new", _cmd_new, "Start a new session")
    registry.register("/plugins", _cmd_plugins, "Loaded plugins")
    registry.register("/quit", _cmd_quit, "Quit (TUI only)", aliases=("/exit", "/q"))
    registry.register("/session", _cmd_session, "Show or switch active session", "prefix|#")
    registry.register("/sessions", _cmd_sessions, "List or delete saved sessions", "all|delete <id>")
    registry.register("/todos", _cmd_todos, "Show current TODO list for this session")
    registry.register("/tools", _cmd_tools, "Registered tools")
    registry.register("/usage", _cmd_usage, "Per-session token + cache + compaction stats")


def _build_registry() -> SlashRegistry:
    registry = SlashRegistry()
    _register_service_slash(registry)

    from nano_openclaw.features.checkpoint.slash import register_slash as register_checkpoint_slash
    from nano_openclaw.features.memory.slash import register_slash as register_memory_slash
    from nano_openclaw.features.runtime.slash import register_slash as register_runtime_slash
    from nano_openclaw.features.skills.slash import register_slash as register_skills_slash
    from nano_openclaw.features.subagents.slash import register_slash as register_subagents_slash

    register_memory_slash(registry)
    register_skills_slash(registry)
    register_subagents_slash(registry)
    register_checkpoint_slash(registry)
    register_runtime_slash(registry)
    return registry


_REGISTRY = _build_registry()
_HANDLERS = _REGISTRY.handlers()
HELP_ENTRIES = _REGISTRY.entries()
HELP_TEXT = "  ".join(_rich_token(e) for e in HELP_ENTRIES)
HELP_TABLE_ROWS: list[list[str]] = [_table_row(e) for e in HELP_ENTRIES]


def register_slash_command(
    command: str,
    handler: SlashHandler,
    description: str = "",
    args: str = "",
) -> None:
    """Register a slash command from a feature or plugin."""
    _REGISTRY.register(command, handler, description, args)
    _HANDLERS[command] = handler
