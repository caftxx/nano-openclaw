"""Tests for ``nano_openclaw.memory.extractor`` — the stop-hook extractor.

Mocks out the subagent execution (``_execute_extraction``) so the suite
exercises the trigger / cooldown / mutual-exclusion / coalesce / cursor
state machine without touching a real LLM client.

Follows the project's ``asyncio.run`` test pattern (no pytest-asyncio
dependency — see ``tests/test_backend_embedded.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nano_openclaw.config.types import ExtractMemoriesConfig
from nano_openclaw.memory import extractor as ex_module
from nano_openclaw.memory.extractor import (
    ExtractorState,
    _has_topic_writes_since,
    _index_after_cursor,
    _last_snapshot_id,
    _states,
    clear_state,
    run_extractor,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Each test starts with an empty state map."""
    _states.clear()
    yield
    _states.clear()


def _make_payload(
    *,
    session_key: str = "sess-1",
    workspace: Path,
    messages: list[dict[str, Any]] | None = None,
    turn_source: str = "tui",
) -> dict[str, Any]:
    """Minimal payload shaped like loop.py's after_turn hook."""
    return {
        "session_key": session_key,
        "workspace_dir": str(workspace),
        "turn_source": turn_source,
        "messages_snapshot": messages
        or [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
        # _execute_extraction normally reads these — the mocked path doesn't.
        "loop_config": object(),
        "client": object(),
    }


def _enabled_cfg(**overrides: Any) -> ExtractMemoriesConfig:
    defaults = {
        "enabled": True,
        "triggerSources": ["tui", "webui", "wechat"],
        "maxTurns": 5,
        "cooldownTurns": 1,
    }
    defaults.update(overrides)
    return ExtractMemoriesConfig(**defaults)


async def _drain(session_key: str = "sess-1") -> None:
    """Await any in-flight extractor task (and its trailing chain) for the session."""
    state = _states.get(session_key)
    while state is not None and state.in_flight is not None:
        task = state.in_flight
        try:
            await task
        except BaseException:
            pass
        # ``_run_subagent``'s finally clears state.in_flight to None when there's
        # no pending payload, or replaces it with a fresh trailing task. Loop
        # while a new task keeps showing up.
        if state.in_flight is task:
            # Same task still on the slot — finally hasn't run (shouldn't happen
            # after await, but guard against tight races).
            state.in_flight = None
            break


# ─── trigger source filter ───


def test_cron_source_does_not_trigger(tmp_path: Path) -> None:
    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path, turn_source="cron")

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            mock.assert_not_called()

    asyncio.run(run())


def test_tui_source_triggers(tmp_path: Path) -> None:
    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path, turn_source="tui")

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await _drain()
            mock.assert_called_once()

    asyncio.run(run())


def test_disabled_cfg_does_not_trigger(tmp_path: Path) -> None:
    cfg = _enabled_cfg(enabled=False)
    payload = _make_payload(workspace=tmp_path)

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            mock.assert_not_called()

    asyncio.run(run())


def test_no_workspace_does_not_trigger(tmp_path: Path) -> None:
    """No workspace_dir → nothing to write to → skip silently."""
    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path)
    payload["workspace_dir"] = ""

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            mock.assert_not_called()

    asyncio.run(run())


# ─── cooldown ───


def test_cooldown_skips_early_turns(tmp_path: Path) -> None:
    cfg = _enabled_cfg(cooldownTurns=3)
    payload = _make_payload(workspace=tmp_path)

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            # Turn 1 + 2: counter advances, no run.
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            assert mock.call_count == 0
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            assert mock.call_count == 0

            # Turn 3: counter hits threshold, run fires.
            await run_extractor(payload, cfg)
            await _drain()
            assert mock.call_count == 1

    asyncio.run(run())


def test_empty_window_does_not_burn_cooldown(tmp_path: Path) -> None:
    """When there are no new messages since the cursor, the cooldown counter is rolled back."""
    cfg = _enabled_cfg(cooldownTurns=1)
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "x"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "y"}]},
    ]
    payload = _make_payload(workspace=tmp_path, messages=msgs)

    # Pre-seed cursor at the last message so the window is empty,
    # AND set turns_since_last_extract so the cooldown gate passes
    # (otherwise we'd be testing the cooldown path, not the window path).
    _states["sess-1"] = ExtractorState(
        last_extract_message_id=_last_snapshot_id(msgs),
        turns_since_last_extract=0,
    )

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            mock.assert_not_called()
            # turn counter was rolled back: incremented from 0→1, then -1 → 0.
            # So a follow-up turn (with real new messages) still has to wait one round.
            assert _states["sess-1"].turns_since_last_extract == 0

    asyncio.run(run())


# ─── mutual exclusion ───


def test_main_agent_topic_write_skips_extractor(tmp_path: Path) -> None:
    """Main agent wrote memory/topics/foo.md this turn → skip and advance cursor."""
    cfg = _enabled_cfg()
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "save my pref"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "saving"},
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "memory/topics/foo.md", "content": "x"},
                },
            ],
        },
    ]
    payload = _make_payload(workspace=tmp_path, messages=msgs)

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await asyncio.sleep(0)
            mock.assert_not_called()
            # Cursor advanced to last message id.
            assert _states["sess-1"].last_extract_message_id == _last_snapshot_id(msgs)
            # Cooldown reset.
            assert _states["sess-1"].turns_since_last_extract == 0

    asyncio.run(run())


def test_main_agent_daily_write_does_not_count_as_exclusion(tmp_path: Path) -> None:
    """Pre-compaction flush writing memory/2026-05-20.md must NOT block extractor."""
    cfg = _enabled_cfg()
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "memory/2026-05-20.md", "content": "daily log"},
                },
            ],
        },
    ]
    payload = _make_payload(workspace=tmp_path, messages=msgs)

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])) as mock:
            await run_extractor(payload, cfg)
            await _drain()
            mock.assert_called_once()

    asyncio.run(run())


def test_has_topic_writes_since_detects_index_writes(tmp_path: Path) -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "memory/MEMORY.md", "content": "..."},
                },
            ],
        },
    ]
    assert _has_topic_writes_since(msgs, tmp_path, 0) is True


def test_has_topic_writes_since_ignores_user_messages(tmp_path: Path) -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                # User-role tool_use shouldn't really happen but be defensive.
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "memory/MEMORY.md", "content": "..."},
                },
            ],
        },
    ]
    assert _has_topic_writes_since(msgs, tmp_path, 0) is False


# ─── coalesce + trailing ───


def test_in_flight_payload_is_coalesced(tmp_path: Path) -> None:
    """Second after_turn while a run is in flight stashes, not spawns a parallel task."""
    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path)

    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0
    seen_payloads: list[dict[str, Any]] = []

    async def slow_execute(payload_arg: dict[str, Any], _cfg: ExtractMemoriesConfig) -> None:
        nonlocal call_count
        call_count += 1
        seen_payloads.append(payload_arg)
        if call_count == 1:
            started.set()
            await release.wait()
        # Trailing call (#2) returns immediately.

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=slow_execute):
            await run_extractor(payload, cfg)
            await started.wait()

            # Fire a second after_turn — should stash, not spawn another task.
            payload2 = _make_payload(
                workspace=tmp_path,
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "second"}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
                ],
            )
            await run_extractor(payload2, cfg)
            state = _states["sess-1"]
            assert state.pending_payload is payload2
            assert call_count == 1  # second payload did NOT spawn a parallel run

            # Release the slow first run. The finally clause spawns the trailing
            # run from pending_payload, which then completes immediately.
            release.set()
            # Drain everything (first + trailing chain).
            await _drain()
            assert call_count == 2
            assert seen_payloads[1] is payload2  # trailing ran with the stashed payload
            assert state.pending_payload is None
            assert state.in_flight is None

    asyncio.run(run())


def test_cursor_only_advances_on_success(tmp_path: Path) -> None:
    cfg = _enabled_cfg()
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "u"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
    ]
    payload = _make_payload(workspace=tmp_path, messages=msgs)

    async def boom(_p: Any, _c: Any) -> None:
        raise RuntimeError("subagent crashed")

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=boom):
            await run_extractor(payload, cfg)
            await _drain()
            state = _states["sess-1"]
            # Cursor NOT advanced because the run errored.
            assert state.last_extract_message_id is None

    asyncio.run(run())


def test_cursor_advances_on_success(tmp_path: Path) -> None:
    cfg = _enabled_cfg()
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "u"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
    ]
    payload = _make_payload(workspace=tmp_path, messages=msgs)

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])):
            await run_extractor(payload, cfg)
            await _drain()
            state = _states["sess-1"]
            assert state.last_extract_message_id == _last_snapshot_id(msgs)

    asyncio.run(run())


# ─── on_event emission (Phase 2 UI plumbing) ───


def test_on_event_callback_receives_memory_extracted(tmp_path: Path) -> None:
    """When the extractor writes paths, it pushes a MemoryExtracted event
    through ``payload['on_event']`` so the TUI / WebUI can render it.
    """
    from nano_openclaw._stream_events import MemoryExtracted

    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path)
    received: list[Any] = []
    payload["on_event"] = received.append

    async def run() -> None:
        with patch.object(
            ex_module,
            "_execute_extraction",
            new=AsyncMock(return_value=["memory/topics/user.md", "memory/MEMORY.md"]),
        ):
            await run_extractor(payload, cfg)
            await _drain()

    asyncio.run(run())

    assert len(received) == 1
    event = received[0]
    assert isinstance(event, MemoryExtracted)
    assert event.written_paths == ["memory/topics/user.md", "memory/MEMORY.md"]
    assert event.topic_paths == ["memory/topics/user.md"]


def test_on_event_not_called_when_nothing_written(tmp_path: Path) -> None:
    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path)
    received: list[Any] = []
    payload["on_event"] = received.append

    async def run() -> None:
        with patch.object(ex_module, "_execute_extraction", new=AsyncMock(return_value=[])):
            await run_extractor(payload, cfg)
            await _drain()

    asyncio.run(run())
    assert received == []


def test_on_event_failure_does_not_break_extractor(tmp_path: Path) -> None:
    """A broken UI callback must not bubble up to the fire-and-forget task."""
    cfg = _enabled_cfg()
    payload = _make_payload(workspace=tmp_path)

    def explode(_event: Any) -> None:
        raise RuntimeError("renderer crashed")

    payload["on_event"] = explode

    async def run() -> None:
        with patch.object(
            ex_module,
            "_execute_extraction",
            new=AsyncMock(return_value=["memory/topics/x.md"]),
        ):
            # Must not raise — extractor swallows callback errors.
            await run_extractor(payload, cfg)
            await _drain()

    asyncio.run(run())


# ─── helper sanity ───


def test_index_after_cursor_falls_back_to_zero_when_missing() -> None:
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
    ]
    # Cursor that no longer exists in the snapshot → recover by replaying all.
    assert _index_after_cursor(msgs, "snap:99:x:deadbeef") == 0
    # None cursor → start at index 0.
    assert _index_after_cursor(msgs, None) == 0


def test_clear_state_removes_session() -> None:
    _states["sess-x"] = ExtractorState(turns_since_last_extract=5)
    clear_state("sess-x")
    assert "sess-x" not in _states
    # Idempotent.
    clear_state("sess-x")
