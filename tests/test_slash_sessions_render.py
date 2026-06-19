"""Regression tests for the ``/sessions`` Table renderer's "current" marker.

Bug: when multiple signals indicated "current" (the local TUI's session_key
AND the server's last_session_id), the OR-based logic marked both rows.
The Table now shows the marker on exactly one row — whichever the local
REPL would dispatch the next chat into.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from nano_openclaw.services.backend import SessionInfo, SessionList
from nano_openclaw.services.slash import _render_sessions_table


def _session(sid: str, *, current: bool = False, msg_count: int = 1) -> SessionInfo:
    return SessionInfo(
        session_id=sid,
        title=sid[:8],
        preview="",
        created_at=0,
        updated_at=0,
        model="test",
        message_count=msg_count,
        compaction_count=0,
        current=current,
    )


def _render(result: SessionList, current_session_key: str | None = None) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=200, no_color=True)
    _render_sessions_table(console, result, current_session_key=current_session_key, show_all=True)
    return buf.getvalue()


def test_only_one_row_marked_when_local_and_server_disagree():
    """The classic bug shape: local session_key=A, server says current=B —
    only A should be marked (it's where the user's next chat goes).
    """
    sessions = SessionList(
        sessions=[
            _session("aaaaaaaa", current=False),  # local current
            _session("bbbbbbbb", current=True),   # server "lastSessionId"
            _session("cccccccc", current=False),
        ],
        last_session_id="bbbbbbbb",
    )
    out = _render(sessions, current_session_key="aaaaaaaa")
    # Exactly one '← current' marker total
    assert out.count("← current") == 1
    # And it's on the local session
    assert "aaaaaaaa" in out
    line_with_marker = next(line for line in out.splitlines() if "← current" in line)
    assert "aaaaaaaa" in line_with_marker
    assert "bbbbbbbb" not in line_with_marker


def test_marker_falls_back_to_server_current_when_no_local_key():
    """If the caller doesn't carry a local session_key (e.g., before first
    interaction), the renderer marks whatever the server says is current.
    """
    sessions = SessionList(
        sessions=[
            _session("aaaaaaaa", current=False),
            _session("bbbbbbbb", current=True),
        ],
        last_session_id="bbbbbbbb",
    )
    out = _render(sessions, current_session_key=None)
    assert out.count("← current") == 1
    line = next(line for line in out.splitlines() if "← current" in line)
    assert "bbbbbbbb" in line


def test_marker_skipped_when_local_key_unknown():
    """Local session_key points at something not in the visible list (e.g.,
    a fresh session not yet persisted): no row gets the marker — better
    than mis-tagging an unrelated session.
    """
    sessions = SessionList(
        sessions=[
            _session("aaaaaaaa", current=False),
            _session("bbbbbbbb", current=True),
        ],
        last_session_id="bbbbbbbb",
    )
    out = _render(sessions, current_session_key="ffffffff")
    assert out.count("← current") == 0


def test_no_double_marker_when_local_matches_server():
    """Same session is both local + server-current: still ONE marker."""
    sessions = SessionList(
        sessions=[_session("aaaaaaaa", current=True)],
        last_session_id="aaaaaaaa",
    )
    out = _render(sessions, current_session_key="aaaaaaaa")
    assert out.count("← current") == 1
