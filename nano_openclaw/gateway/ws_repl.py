"""Thin TUI REPL for **remote** mode (``tui --connect ws://...``).

This is a deliberately simpler loop than the embedded ``cli.repl`` — the
daemon owns history / approvals / cron / dreaming, so the client's only
job is: take user input, fire ``chat.send``, and render incoming PushFrame
payloads as they arrive.

Slash commands are delegated to :mod:`gateway.slash` so the surface +
rendering match embedded mode exactly. Users shouldn't be able to tell
which Backend is powering their session.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich import markup
from rich.console import Console

from nano_openclaw.gateway.backend import Backend, BusyError, PushEvent
from nano_openclaw.gateway.slash import HELP_TEXT, QuitREPL, handle_slash


# ────────────────────────────────────────────────────────────────────────────
# Streaming render of PushFrame payloads
# ────────────────────────────────────────────────────────────────────────────


class _PayloadRenderer:
    """Stateful printer for one chat turn's PushFrame stream.

    Maintains "is text in flight" so successive text.delta events flow
    without inserting newlines. Tool starts/ends print compact status lines.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._text_in_flight = False
        self._thinking_in_flight = False

    def render(self, evt: PushEvent) -> bool:
        """Return True when this event marks the end of the current turn
        (``turn.done``, ``turn.cancelled``, ``turn.error``).
        """
        if evt.event != "agent.event":
            self._end_inline_text()
            self.console.print(f"[dim]({evt.event})[/dim]")
            return False

        kind = str(evt.payload.get("type") or "")

        if kind == "text.delta":
            text = str(evt.payload.get("text") or "")
            if not self._text_in_flight:
                self.console.print()
                self._text_in_flight = True
            self.console.print(markup.escape(text), end="", soft_wrap=True, highlight=False)
            self.console.file.flush()
            return False

        if kind == "thinking.delta":
            text = str(evt.payload.get("text") or "")
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

        if kind == "tool.start":
            self._end_inline_text()
            name = str(evt.payload.get("name") or "?")
            self.console.print(f"[cyan]→ {name}[/]")
            return False

        if kind == "tool.result":
            name = str(evt.payload.get("name") or "?")
            result = evt.payload.get("result") or {}
            text = ""
            if isinstance(result, dict):
                content = result.get("content")
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                    text = "".join(p for p in parts if p)
                elif isinstance(content, str):
                    text = content
            text = text.strip()
            if text:
                preview = text[:240] + ("…" if len(text) > 240 else "")
                self.console.print(f"[dim]  {name}: {markup.escape(preview)}[/dim]")
            else:
                self.console.print(f"[dim]  {name}: (no output)[/dim]")
            return False

        if kind == "message.end":
            self._end_inline_text()
            return False

        if kind in {"turn.done", "turn.cancelled", "turn.error"}:
            self._end_inline_text()
            if kind == "turn.cancelled":
                self.console.print("[dim](turn cancelled)[/]")
            elif kind == "turn.error":
                msg = str(evt.payload.get("message") or "")
                self.console.print(f"[red]error:[/] {markup.escape(msg)}")
            return True

        if kind == "compaction":
            self._end_inline_text()
            self.console.print("[yellow](compacted)[/]")
            return False

        # Subagent / image / approval / unknown — print compact status.
        self._end_inline_text()
        self.console.print(f"[dim]· {kind}[/dim]")
        return False

    def _end_inline_text(self) -> None:
        if self._text_in_flight:
            self.console.print()
            self._text_in_flight = False
        if self._thinking_in_flight:
            self.console.print()
            self._thinking_in_flight = False


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
