"""Thin TUI REPL for **remote** mode (``tui --connect ws://...``).

This is a deliberately simpler loop than the embedded ``cli.repl`` — the
daemon owns history / approvals / cron / dreaming, so the client's only
job is: take user input, fire ``chat.send``, and render incoming PushFrame
payloads as they arrive.

Slash commands are delegated to :mod:`gateway.slash` so the surface +
rendering match embedded mode exactly. The streaming-event renderer
mirrors ``cli._make_event_handler`` — same Rich Live tree for tool calls
and subagents, same status-line widgets for skills / images / memory.
Translates push payloads (dicts) back into the typed slot/state shape
the cli helpers expect.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rich import markup
from rich.console import Console
from rich.live import Live

from nano_openclaw.gateway.backend import Backend, BusyError, PushEvent
from nano_openclaw.gateway.slash import HELP_TEXT, QuitREPL, handle_slash

# Reuse the embedded mode's pure renderer helpers verbatim — both modes
# render identical Rich widgets, only the event source differs.
from nano_openclaw.cli import (
    _build_status_tree,
    _build_subagent_tree,
    _build_tool_tree,
    _extract_tool_preview,
    _format_subagent_status,
    _render_compaction,
    _truncate_one_line,
)


# ────────────────────────────────────────────────────────────────────────────
# Streaming render of PushFrame payloads
# ────────────────────────────────────────────────────────────────────────────


class _PayloadRenderer:
    """Stateful printer for one chat turn's PushFrame stream.

    Mirrors ``cli._make_event_handler``'s state machine:

    - Buffers ``text.delta`` / ``thinking.delta`` inline (with leading
      newline before the first chunk so it renders below the user prompt).
    - Tracks each tool call as a slot (id → {name, args_buf, done, ...}),
      drives a single Rich Live tree that updates as args stream + status
      flips on result. Closes the Live when all slots are done.
    - Subagent spawns / progress / completions feed a parallel
      ``subagent_progress`` map → second Rich Live tree.
    - Skills / images / Active Memory recall use the static status-tree
      widget (one print + done).
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._text_in_flight = False
        self._thinking_in_flight = False
        # Tool batch state — keyed by tool_use_id
        self._tool_slots: dict[str, dict[str, Any]] = {}
        self._tool_name_counts: dict[str, int] = {}
        self._rendered_tool_results: set[str] = set()
        self._tool_live: Live | None = None
        self._tool_live_start: float | None = None
        # Subagent state — keyed by run_id
        self._subagent_progress: dict[str, dict[str, Any]] = {}
        self._subagent_live: Live | None = None

    # ─── Live lifecycle helpers ───

    def _reset_tool_batch(self) -> None:
        if self._tool_live is not None:
            self._tool_live.stop()
            self._tool_live = None
        self._tool_live_start = None
        self._tool_slots.clear()
        self._tool_name_counts.clear()
        self._rendered_tool_results.clear()

    def _update_tool_live(self) -> None:
        if self._tool_live is not None and self._tool_live_start is not None:
            self._tool_live.update(_build_tool_tree(self._tool_slots, self._tool_live_start))

    def _start_or_update_tool_live(self) -> None:
        if self._tool_live is None:
            self._tool_live_start = time.monotonic()
            self._tool_live = Live(
                _build_tool_tree(self._tool_slots, self._tool_live_start),
                console=self.console,
                refresh_per_second=8,
                transient=False,
            )
            self._tool_live.start()
        else:
            self._update_tool_live()

    def _stop_tool_live_if_done(self) -> None:
        if self._tool_slots and all(slot["done"] for slot in self._tool_slots.values()):
            self._update_tool_live()
            if self._tool_live is not None:
                self._tool_live.stop()
                self._tool_live = None
            self._tool_live_start = None

    def _update_subagent_live(self) -> None:
        if self._subagent_live is not None:
            self._subagent_live.update(_build_subagent_tree(self._subagent_progress))

    def _stop_subagent_live_if_done(self) -> None:
        if self._subagent_progress and all(p["done"] for p in self._subagent_progress.values()):
            self._update_subagent_live()
            if self._subagent_live is not None:
                self._subagent_live.stop()
                self._subagent_live = None
            self._subagent_progress.clear()

    def _end_inline_text(self) -> None:
        if self._text_in_flight:
            self.console.print()
            self._text_in_flight = False
        if self._thinking_in_flight:
            self.console.print()
            self._thinking_in_flight = False

    # ─── Public entry: render one PushEvent ───

    def render(self, evt: PushEvent) -> bool:
        """Return True when this event marks the end of the current turn
        (``turn.done``, ``turn.cancelled``, ``turn.error``).
        """
        if evt.event != "agent.event":
            # ``gap`` means the daemon's bounded queue dropped events — the
            # user should know to call ``chat.history`` if they need the
            # full record. Other non-agent events (session.changed /
            # channel.changed / approval.*) are internal bookkeeping and
            # should not pollute the chat stream.
            if evt.event == "gap":
                self._end_inline_text()
                dropped = evt.payload.get("dropped") or 0
                self.console.print(f"[yellow](gap: {dropped} events dropped)[/]")
            return False

        kind = str(evt.payload.get("type") or "")
        payload = evt.payload

        # Internal lifecycle markers — silent so the user doesn't see
        # bookkeeping noise alongside their actual reply.
        if kind == "turn.started":
            return False

        # ─── Inline text / thinking ───

        if kind == "text.delta":
            text = str(payload.get("text") or "")
            if not self._text_in_flight:
                self.console.print()
                self._text_in_flight = True
            self.console.print(markup.escape(text), end="", soft_wrap=True, highlight=False)
            self.console.file.flush()
            return False

        if kind == "thinking.delta":
            text = str(payload.get("text") or "")
            if not self._thinking_in_flight:
                self.console.print()
                self._thinking_in_flight = True
            self.console.print(markup.escape(text), end="", soft_wrap=True, style="dim", highlight=False)
            self.console.file.flush()
            return False

        if kind == "thinking.done":
            if self._thinking_in_flight:
                self.console.print()
                self._thinking_in_flight = False
            return False

        # ─── Tool call live tree ───

        if kind == "tool.start":
            if self._text_in_flight:
                self.console.print()
                self._text_in_flight = False
            tool_id = str(payload.get("tool_use_id") or "")
            name = str(payload.get("name") or "?")
            # If the previous batch finished, start a fresh tree so the
            # header counter resets.
            if self._tool_slots and all(slot["done"] for slot in self._tool_slots.values()):
                self._reset_tool_batch()
            if tool_id and tool_id not in self._tool_slots:
                count = self._tool_name_counts.get(name, 0) + 1
                self._tool_name_counts[name] = count
                display_name = name if count == 1 else f"{name} #{count}"
                self._tool_slots[tool_id] = {
                    "name": name,
                    "display_name": display_name,
                    "args_buf": "",
                    "done": False,
                    "is_error": False,
                    "result_preview": None,
                }
                # sessions_spawn lifecycle is tracked through the subagent
                # tree; don't pull it into the tool batch.
                if name != "sessions_spawn":
                    self._start_or_update_tool_live()
            return False

        if kind == "tool.delta":
            tool_id = str(payload.get("tool_use_id") or "")
            slot = self._tool_slots.get(tool_id)
            if slot is not None:
                slot["args_buf"] += str(payload.get("partial_json") or "")
                if slot.get("name") != "sessions_spawn":
                    self._update_tool_live()
            return False

        if kind == "tool.end":
            self._update_tool_live()
            return False

        if kind == "tool.result":
            tool_id = str(payload.get("tool_use_id") or "")
            if tool_id in self._rendered_tool_results:
                return False
            self._rendered_tool_results.add(tool_id)
            slot = self._tool_slots.get(tool_id)
            result = payload.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            if slot is not None:
                slot["done"] = True
                slot["is_error"] = bool(result.get("is_error"))
                slot["result_preview"] = _extract_tool_preview(result)
                self._update_tool_live()
                self._stop_tool_live_if_done()
            return False

        if kind == "message.end":
            if self._text_in_flight:
                self.console.print()
                self._text_in_flight = False
            return False

        if kind in {"turn.done", "turn.cancelled", "turn.error"}:
            self._end_inline_text()
            # Make sure any half-open Live trees flush before we move on.
            self._stop_tool_live_if_done()
            self._stop_subagent_live_if_done()
            if kind == "turn.cancelled":
                self.console.print("[dim](turn cancelled)[/]")
            elif kind == "turn.error":
                msg = str(payload.get("message") or "")
                self.console.print(f"[red]error:[/] {markup.escape(msg)}")
            return True

        if kind == "compaction":
            self._end_inline_text()
            _render_compaction(self.console, summary=str(payload.get("summary") or ""))
            return False

        # ─── Status-tree widgets (one-shot; static) ───

        if kind == "image.status":
            status = str(payload.get("status") or "")
            ref = str(payload.get("ref") or "")
            refs = payload.get("refs") or []
            items: list[tuple[str, str | None]]
            if status in {"described", "attached"} and isinstance(refs, list) and refs:
                items = [(str(r), status) for r in refs]
            elif status == "error":
                items = [(ref, f"[red]error[/] {markup.escape(str(payload.get('error') or ''))}")]
            elif status == "skipped":
                items = [(ref, f"[yellow]skipped[/] {markup.escape(str(payload.get('reason') or ''))}")]
            else:
                items = [(ref, status)]
            self._end_inline_text()
            self.console.print(_build_status_tree("Image", items))
            return False

        if kind == "attachment.status":
            status = str(payload.get("status") or "")
            ref = str(payload.get("ref") or "")
            refs = payload.get("refs") or []
            if status == "attached" and isinstance(refs, list) and refs:
                items = [(str(r), "attached") for r in refs]
            elif status == "error":
                items = [(ref, f"[red]error[/] {markup.escape(str(payload.get('error') or ''))}")]
            else:
                items = [(ref, status)]
            self._end_inline_text()
            self.console.print(_build_status_tree("Attachment", items))
            return False

        if kind == "skill.invoked":
            self._end_inline_text()
            self.console.print(_build_status_tree(
                "Skill",
                [(str(payload.get("skill_name") or ""), str(payload.get("skill_path") or ""))],
            ))
            return False

        if kind == "active_memory":
            self._end_inline_text()
            result = payload.get("result") or {}
            if isinstance(result, dict) and result.get("context"):
                cached = ", cached" if result.get("cached") else ""
                self.console.print(_build_status_tree(
                    "Active Memory",
                    [("recall", f"{result.get('elapsed_ms', 0)}ms{cached}")],
                ))
            return False

        # ─── Subagent live tree ───

        if kind == "subagent.status":
            status = str(payload.get("status") or "")
            run_id = str(payload.get("run_id") or "")
            if status == "spawned":
                # Tool batch hands off to the subagent tree.
                if self._tool_live is not None:
                    self._reset_tool_batch()
                task = str(payload.get("task") or "")
                explicit_label = payload.get("label")
                label = explicit_label or (task[:50] + ("..." if len(task) > 50 else ""))
                self._subagent_progress[run_id] = {
                    "label": label,
                    "tool_uses": 0,
                    "tokens": 0,
                    "activity": "starting...",
                    "done": False,
                }
                if self._subagent_live is None:
                    self._subagent_live = Live(
                        _build_subagent_tree(self._subagent_progress),
                        console=self.console,
                        refresh_per_second=8,
                        transient=False,
                    )
                    self._subagent_live.start()
                else:
                    self._update_subagent_live()
                return False

            if status == "progress":
                info = self._subagent_progress.setdefault(run_id, {
                    "label": str(payload.get("label") or ""),
                    "tool_uses": 0,
                    "tokens": 0,
                    "activity": "starting...",
                    "done": False,
                })
                info.update({
                    "label": str(payload.get("label") or info.get("label", "")),
                    "tool_uses": int(payload.get("tool_uses") or 0),
                    "tokens": int(payload.get("input_tokens") or 0) + int(payload.get("output_tokens") or 0),
                    "activity": str(payload.get("current_activity") or info.get("activity", "")),
                })
                self._update_subagent_live()
                return False

            if status == "killed":
                info = self._subagent_progress.setdefault(run_id, {
                    "label": str(payload.get("task") or ""),
                    "tool_uses": 0,
                    "tokens": 0,
                    "activity": "killed",
                    "done": False,
                })
                info["done"] = True
                info["status"] = "killed"
                self._update_subagent_live()
                self._stop_subagent_live_if_done()
                return False

            # Terminal status from SubagentAnnounced (completed / error / timeout).
            info = self._subagent_progress.setdefault(run_id, {
                "label": str(payload.get("task") or "")[:50],
                "tool_uses": 0,
                "tokens": 0,
                "activity": "done",
                "done": False,
            })
            info["done"] = True
            info["status"] = status or "done"
            elapsed = payload.get("elapsed_ms")
            if elapsed:
                info["elapsed_ms"] = elapsed
            preview = payload.get("result_text")
            if isinstance(preview, str) and preview.strip():
                info["result_preview"] = _truncate_one_line(preview, 120)
            err = payload.get("error_message")
            if isinstance(err, str) and err:
                info["error_message"] = err
            self._update_subagent_live()
            self._stop_subagent_live_if_done()
            return False

        if kind == "subagent.event":
            # Nested deltas inside a subagent — ignore at this layer; the
            # subagent tree's progress events already convey activity.
            return False

        # Unknown event kind — print a compact diagnostic so we don't lose it.
        self._end_inline_text()
        self.console.print(f"[dim]· {kind}[/dim]")
        return False


# ────────────────────────────────────────────────────────────────────────────
# REPL main loop
# ────────────────────────────────────────────────────────────────────────────


async def ws_repl(
    backend: Backend,
    *,
    session_key: str = "",
    console: Console | None = None,
) -> None:
    """Thin remote-mode REPL.

    ``session_key`` empty → adopt the daemon's most-recent session. The
    daemon picks a fresh one if nothing's saved yet.
    """
    console = console or Console()

    snap = await backend.runtime_get()
    console.print(
        f"[green]connected[/green] · agent={snap.agent_id} model={snap.model_id} · "
        f"type /help for commands"
    )

    if not session_key:
        sessions = await backend.sessions_list()
        if sessions.last_session_id:
            session_key = sessions.last_session_id

    state: dict[str, Any] = {"session_key": session_key, "session_changed": False}

    try:
        while True:
            from nano_openclaw.cli import _repl_input  # reuse prompt_toolkit input
            try:
                user_input = (await _repl_input(console)).strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return

            if not user_input:
                continue

            if user_input.startswith("/"):
                try:
                    handled = await handle_slash(user_input, backend, console, state)
                except QuitREPL:
                    console.print("[dim]bye.[/]")
                    return
                if handled:
                    # Pull the (possibly updated) session_key back so the
                    # next chat.send routes to the right session.
                    session_key = state.get("session_key") or session_key
                    state["session_changed"] = False
                    continue
                console.print(f"[dim]unknown command: {markup.escape(user_input)}[/]")
                continue

            # ── Send + stream ──
            sub = backend.subscribe(session_key=session_key or None)
            renderer = _PayloadRenderer(console)
            try:
                turn_id = await backend.chat_send(session_key=session_key, text=user_input)
            except BusyError as exc:
                console.print(f"[yellow]busy:[/] {exc} (retry in {exc.retry_after_ms}ms)")
                continue
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]error:[/] {type(exc).__name__}: {markup.escape(str(exc))}")
                continue

            try:
                async for evt in sub:
                    payload_turn = evt.payload.get("turn_id")
                    payload_session = evt.payload.get("session_key") or evt.payload.get("session_id")
                    # Only render events tied to *this* turn — multiple WS
                    # clients sharing a daemon means other sessions' deltas
                    # could otherwise spill into this TUI.
                    if payload_turn and payload_turn != turn_id:
                        continue
                    if not session_key and payload_session:
                        session_key = str(payload_session)
                        state["session_key"] = session_key
                    done = renderer.render(evt)
                    if done:
                        break
            except (asyncio.CancelledError, KeyboardInterrupt):
                try:
                    await backend.chat_abort(turn_id=turn_id)
                except Exception:  # noqa: BLE001
                    pass
                console.print("\n[dim](aborted)[/]")
                continue
    finally:
        try:
            await backend.aclose()
        except Exception:  # noqa: BLE001
            pass
