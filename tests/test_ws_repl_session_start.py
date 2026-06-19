import asyncio
import io

from rich.console import Console

from nano_openclaw.adapters.cli import repl as cli_repl
from nano_openclaw.adapters.cli.ws_repl import ws_repl
from nano_openclaw.services.backend import RuntimeSnapshot, SessionInfo, SessionList


class _EofPrompt:
    async def prompt_async(self):
        raise EOFError


class _Backend:
    def __init__(self):
        self.sessions_list_calls = 0
        self.sessions_reset_calls = []
        self.closed = False

    async def runtime_get(self):
        return RuntimeSnapshot(
            agent_id="default",
            model_ref="custom/test",
            model_id="test",
            image_model_ref=None,
            thinking_level="minimal",
            workspace_dir="/tmp/ws",
            state_dir="/tmp/state",
        )

    async def sessions_list(self):
        self.sessions_list_calls += 1
        return SessionList(sessions=[], last_session_id="last-session")

    async def sessions_reset(self, session_key, *, reason="reset"):
        self.sessions_reset_calls.append((session_key, reason))
        return SessionInfo(
            session_id="new-session",
            title="new",
            preview="",
            created_at=0,
            updated_at=0,
            model="test",
            message_count=0,
            compaction_count=0,
            current=True,
        )

    async def aclose(self):
        self.closed = True


def _run_ws_repl(monkeypatch, backend, **kwargs):
    monkeypatch.setattr(cli_repl, "_get_pt_session", lambda: _EofPrompt())
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    asyncio.run(ws_repl(backend, console=console, **kwargs))


def test_ws_repl_defaults_to_new_session(monkeypatch):
    backend = _Backend()

    _run_ws_repl(monkeypatch, backend)

    assert backend.sessions_list_calls == 0
    assert backend.sessions_reset_calls == [("", "new")]
    assert backend.closed is True


def test_ws_repl_resume_uses_last_session(monkeypatch):
    backend = _Backend()

    _run_ws_repl(monkeypatch, backend, resume=True)

    assert backend.sessions_list_calls == 1
    assert backend.sessions_reset_calls == []
    assert backend.closed is True
