"""Stop-hook memory extractor.

Mirrors claude-code ``services/extractMemories/extractMemories.ts``. Triggered
by the ``after_turn`` plugin hook (wired in ``plugins/builtin/memory_plugin.py``);
each eligible main-agent turn fires-and-forgets a short-lived subagent that
reads the new messages and distills them into ``memory/topics/*.md`` topic
files plus a ``memory/MEMORY.md`` index entry.

Concurrency model is intentionally simple — there is exactly one
``ExtractorState`` per ``session_key``:

- ``last_extract_message_id``: cursor into ``messages_snapshot``. Each
  successful run advances it to the last message it saw, so the next run
  only considers messages added after.
- ``turns_since_last_extract``: cooldown counter; reset when a run starts.
- ``in_flight``: the asyncio.Task of the currently-running subagent.
- ``pending_payload``: a single stashed payload from any after_turn that
  fired while a previous run was still in flight. When the in-flight task
  ends, the stash is run as a "trailing" extraction.

Mutual exclusion: if the main agent itself wrote a topic / index file in
the same turn, we skip the extractor (and advance the cursor) so we don't
overwrite the human-explicit save with a model-distilled one. Daily files
(``memory/YYYY-MM-DD.md``) DO NOT count as topic writes — pre-compaction
flush owns daily, extractor owns topics.

Phase 1 deliberately omits claude-code's runForkedAgent prompt-cache
optimization (Anthropic-only) and the multi-feature-flag throttle stack:
single ``cooldownTurns`` knob is enough until we have real traffic data.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from nano_openclaw.config.types import ExtractMemoriesConfig
from nano_openclaw.logger import get_logger
from nano_openclaw.features.memory.extractor_prompts import (
    EXTRACTOR_SYSTEM_PROMPT,
    build_extractor_user_prompt,
)
from nano_openclaw.features.memory.topics import (
    INDEX_FILE,
    TOPIC_DIR,
    format_manifest,
    is_topic_write_path,
    scan_topic_files,
)

logger = get_logger(__name__)


# ─── State ───


@dataclass
class ExtractorState:
    """Per-session extractor bookkeeping. One instance per ``session_key``."""

    last_extract_message_id: Optional[str] = None
    turns_since_last_extract: int = 0
    in_flight: Optional[asyncio.Task[None]] = None
    # Payload from an after_turn that fired while ``in_flight`` was running.
    # Only the most recent is kept; intermediate ones are coalesced away —
    # the trailing run sees the latest history snapshot and any earlier
    # interesting messages are still part of it.
    pending_payload: Optional[dict[str, Any]] = None


# Module-level state map. Cleared per-session on ``session_end`` via
# ``clear_state``. Plain dict + asyncio (no threads) so no lock needed.
_states: dict[str, ExtractorState] = {}


def _get_state(session_key: str) -> ExtractorState:
    state = _states.get(session_key)
    if state is None:
        state = ExtractorState()
        _states[session_key] = state
    return state


def clear_state(session_key: str) -> None:
    """Drop the state entry for a session. Called from the session_end hook."""
    _states.pop(session_key, None)


# ─── Helpers ───


def _message_id(msg: Any) -> Optional[str]:
    """Best-effort extraction of an id from a snapshot message.

    ``messages_snapshot`` in the after_turn hook is a list of
    ``{"role": ..., "content": ...}`` dicts derived from in-memory
    ``Message`` objects, which currently have no stable id. We synthesize
    one from index+role+content-hash to make the cursor deterministic
    within a single session lifetime. Good enough — the cursor only needs
    to round-trip within this process; it never survives a restart.
    """
    return msg.get("_id") if isinstance(msg, dict) else None


def _synthesize_id(index: int, msg: dict[str, Any]) -> str:
    """Stable per-process id derived from position + role + content prefix."""
    role = msg.get("role", "?")
    content = msg.get("content")
    summary = ""
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            # Take a short fingerprint of whichever text field is present.
            for key in ("text", "content", "input", "id"):
                value = first.get(key)
                if isinstance(value, str):
                    summary = value[:32]
                    break
    return f"snap:{index}:{role}:{hash(summary) & 0xFFFFFFFF:08x}"


def _index_after_cursor(messages: list[dict[str, Any]], cursor: Optional[str]) -> int:
    """Return the index of the first message *after* the cursor.

    When ``cursor`` is None (first extraction) the whole snapshot counts as
    new. Otherwise we synthesize ids for each message and return the index
    immediately past the match. If the cursor is no longer present (e.g.
    a compaction rewrote earlier messages out of the snapshot) we fall back
    to 0 — better to over-include than to silently skip recent turns.
    """
    if cursor is None:
        return 0
    for i, msg in enumerate(messages):
        synthesized = _message_id(msg) or _synthesize_id(i, msg)
        if synthesized == cursor:
            return i + 1
    return 0


def _has_topic_writes_since(
    messages: list[dict[str, Any]],
    workspace: Path,
    since_index: int,
) -> bool:
    """Did the main agent write to a topic file or MEMORY.md after ``since_index``?

    Scans assistant messages for ``tool_use`` blocks targeting ``write_file``
    or ``apply_patch`` whose path lands inside the topic-writable area. Daily
    files (``memory/YYYY-MM-DD.md``) deliberately do NOT count — pre-compaction
    flush writes those and we still want the extractor to fire on the same
    turn so topic distillation continues.
    """
    for msg in messages[since_index:]:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_name = block.get("name")
            tool_input = block.get("input") or {}
            path = tool_input.get("path") if isinstance(tool_input, dict) else None
            if tool_name == "write_file" and isinstance(path, str):
                if is_topic_write_path(workspace, path):
                    return True
            # ``apply_patch`` doesn't carry a single path arg — its hunks
            # are inside the patch text. Phase 1 ignores it: extractor will
            # run, and worst case it just re-saves the same content. A
            # cheap regex over the patch body would over-trigger and a real
            # parse pulls in patch_parser as a dependency we don't need.
    return False


def _count_visible_messages_since(messages: list[dict[str, Any]], since_index: int) -> int:
    """User + assistant messages after the cursor — used in the extractor prompt opener."""
    if since_index >= len(messages):
        return 0
    return sum(
        1
        for m in messages[since_index:]
        if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
    )


def _last_snapshot_id(messages: list[dict[str, Any]]) -> Optional[str]:
    """Synthesize an id for the last message in the snapshot. Used to advance the cursor."""
    if not messages:
        return None
    last_index = len(messages) - 1
    last_msg = messages[last_index]
    if not isinstance(last_msg, dict):
        return None
    return _message_id(last_msg) or _synthesize_id(last_index, last_msg)


# ─── Public entry ───


async def run_extractor(payload: dict[str, Any], cfg: ExtractMemoriesConfig) -> None:
    """``after_turn`` hook entry point. Fire-and-forget. Never raises.

    Decision flow:
      1. ``cfg.enabled`` gate.
      2. ``cfg.triggerSources`` membership (default excludes cron / channel_auto).
      3. Cooldown — only run every ``cfg.cooldownTurns`` eligible turns.
      4. Mutual exclusion — skip + advance cursor when the main agent
         already wrote a topic file this turn.
      5. Coalesce — if a previous run is still in flight, stash this
         payload as the trailing run and return.
      6. Otherwise spawn the subagent task.

    Errors anywhere in this function are logged + swallowed; the main agent
    must never break because the extractor misbehaved.
    """
    if not cfg.enabled:
        return

    try:
        turn_source = payload.get("turn_source", "tui")
        if turn_source not in cfg.triggerSources:
            return

        session_key = str(payload.get("session_key") or "default")
        workspace_dir_raw = payload.get("workspace_dir") or ""
        if not workspace_dir_raw:
            # No workspace = no place to write topic files. Skip silently.
            return
        workspace = Path(workspace_dir_raw)

        messages = payload.get("messages_snapshot") or []
        if not isinstance(messages, list):
            return

        state = _get_state(session_key)
        state.turns_since_last_extract += 1

        if state.turns_since_last_extract < cfg.cooldownTurns:
            logger.debug(
                "memory.extractor.cooldown",
                f"session={session_key} skip ({state.turns_since_last_extract}/{cfg.cooldownTurns})",
            )
            return

        since_index = _index_after_cursor(messages, state.last_extract_message_id)
        new_message_count = _count_visible_messages_since(messages, since_index)
        if new_message_count == 0:
            # Nothing new to extract. Don't burn a cooldown slot on a no-op.
            state.turns_since_last_extract = max(0, state.turns_since_last_extract - 1)
            return

        # Mutual exclusion: the main agent's own writes win, skip the extractor
        # this turn and advance the cursor so the next extraction starts fresh.
        if _has_topic_writes_since(messages, workspace, since_index):
            logger.info(
                "memory.extractor.skip_main_wrote",
                f"session={session_key} main agent wrote topics this turn, skipping",
            )
            state.last_extract_message_id = _last_snapshot_id(messages)
            state.turns_since_last_extract = 0
            return

        # Coalesce: if a previous run is still going, stash and return.
        if state.in_flight is not None and not state.in_flight.done():
            state.pending_payload = payload
            logger.debug(
                "memory.extractor.coalesce",
                f"session={session_key} stashed pending payload (in-flight)",
            )
            return

        state.turns_since_last_extract = 0
        state.in_flight = asyncio.create_task(
            _run_subagent(payload, cfg, state),
            name=f"memory.extractor:{session_key}",
        )
    except Exception as exc:  # noqa: BLE001 — hook must never crash the loop
        logger.warning("memory.extractor.hook_error", f"{type(exc).__name__}: {exc}")


# ─── Subagent execution ───


async def _run_subagent(
    payload: dict[str, Any],
    cfg: ExtractMemoriesConfig,
    state: ExtractorState,
) -> None:
    """Run one extraction round, advance the cursor on success, drain pending."""
    from nano_openclaw.core._stream_events import MemoryExtracted

    session_key = str(payload.get("session_key") or "default")
    started_at = time.time()
    try:
        written_paths = await _execute_extraction(payload, cfg)
        # Advance cursor only on success. On failure we keep the old cursor
        # so the next eligible turn re-tries the same window.
        messages = payload.get("messages_snapshot") or []
        if isinstance(messages, list):
            state.last_extract_message_id = _last_snapshot_id(messages)
        duration_ms = int((time.time() - started_at) * 1000)
        if written_paths:
            topic_paths = [
                p for p in written_paths
                if "/" + TOPIC_DIR + "/" in p.replace("\\", "/")
                or p.replace("\\", "/").startswith(TOPIC_DIR + "/")
            ]
            event = MemoryExtracted(
                written_paths=list(written_paths),
                topic_paths=topic_paths,
                duration_ms=duration_ms,
            )
            # Phase 2: emit into the same per-turn event stream the UI is
            # subscribed to (TUI prints a one-liner; WebUI adds an activity
            # row). The callback is the unhooked ``original_on_event``
            # captured at after_turn fire time. Guard against absence /
            # exceptions so a broken UI never bubbles up to the
            # fire-and-forget subagent task.
            on_event_cb = payload.get("on_event")
            if callable(on_event_cb):
                try:
                    on_event_cb(event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "memory.extractor.emit_event_error",
                        f"session={session_key}: {type(exc).__name__}: {exc}",
                    )
            logger.info(
                "memory.extractor.saved_event",
                f"session={session_key} {event}",
            )
        else:
            logger.info(
                "memory.extractor.done",
                f"session={session_key} duration_ms={duration_ms} (nothing saved)",
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — log and continue
        logger.warning(
            "memory.extractor.error",
            f"session={session_key}: {type(exc).__name__}: {exc}",
        )
    finally:
        # Drain at most one trailing payload to avoid head-of-line buildup.
        trailing = state.pending_payload
        state.pending_payload = None
        state.in_flight = None
        if trailing is not None:
            # Fire-and-forget; the trailing run owns its own state slot.
            state.in_flight = asyncio.create_task(
                _run_subagent(trailing, cfg, state),
                name=f"memory.extractor.trailing:{session_key}",
            )


async def _execute_extraction(payload: dict[str, Any], cfg: ExtractMemoriesConfig) -> list[str]:
    """Build the extractor subagent and run one turn.

    Returns the list of paths the guarded ``write_file`` successfully wrote
    (empty if the subagent saved nothing or errored mid-run). Kept as a
    separate function so tests can patch it without monkey-patching
    ``_run_subagent`` (which owns the cursor + coalesce bookkeeping).
    """
    from dataclasses import replace as dc_replace
    from nano_openclaw.core.loop import AgentSession, CancellationToken, LoopConfig, Message
    from nano_openclaw.core.tools import Tool, ToolRegistry, _read_file, _list_dir, _write_file
    from nano_openclaw.todo import TodoStore

    workspace_dir_raw = payload.get("workspace_dir") or ""
    workspace = Path(workspace_dir_raw)
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / TOPIC_DIR).mkdir(parents=True, exist_ok=True)

    base_cfg = payload.get("loop_config")
    client = payload.get("client")
    messages_snapshot = payload.get("messages_snapshot") or []

    if base_cfg is None or client is None:
        logger.debug("memory.extractor.no_base_cfg", "missing loop_config or client; skipping")
        return []

    session_key = str(payload.get("session_key") or "default")
    state = _get_state(session_key)
    since_index = _index_after_cursor(messages_snapshot, state.last_extract_message_id)
    new_message_count = _count_visible_messages_since(messages_snapshot, since_index)

    headers = scan_topic_files(memory_dir)
    manifest = format_manifest(headers)
    user_prompt_body = build_extractor_user_prompt(
        new_message_count=new_message_count,
        manifest=manifest,
    )

    # Serialise the new conversation slice into the user prompt so the
    # extractor has the raw text to reason over. Simple text rendering —
    # keep it readable, skip tool_use blocks (the extractor doesn't need
    # to know what tools ran, only what was said).
    transcript_chunks: list[str] = []
    for msg in messages_snapshot[since_index:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
        if text_parts:
            transcript_chunks.append(f"### {role}\n{''.join(text_parts)}")
    transcript_text = "\n\n".join(transcript_chunks) or "(empty transcript window)"

    full_user_prompt = (
        f"{user_prompt_body}\n\n"
        f"## Conversation transcript (last {new_message_count} messages)\n\n"
        f"{transcript_text}\n"
    )

    # Build a minimal tool registry: read-only filesystem helpers + a
    # write_file wrapper that enforces ``is_topic_write_path``. This is the
    # extractor's hard guard — extractor cannot escape memory/topics/ or
    # memory/MEMORY.md regardless of what the prompt tells it to do.
    written_paths: list[str] = []

    def _guarded_write(args: dict[str, Any], workspace_dir: str | None = None) -> str:
        path_arg = str(args.get("path") or "")
        if not is_topic_write_path(workspace, path_arg):
            return (
                f"refused: extractor may only write to memory/{INDEX_FILE} or "
                f"memory/{TOPIC_DIR}/*.md (got {path_arg!r})"
            )
        result = _write_file(args, workspace_dir=workspace_dir)
        # Track successful writes so the caller can attribute them in the
        # MemoryExtracted event (emitted by the hook layer in step 5).
        try:
            from pathlib import Path as _Path
            written_paths.append(str(_Path(path_arg)))
        except Exception:  # pragma: no cover — defensive
            pass
        return result

    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file",
        description="Read a UTF-8 text file from disk.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        run=_read_file,
    ))
    registry.register(Tool(
        name="list_dir",
        description="List entries in a directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        run=_list_dir,
    ))
    registry.register(Tool(
        name="write_file",
        description=(
            f"Create or overwrite a memory file. Path MUST be either "
            f"memory/{INDEX_FILE} or under memory/{TOPIC_DIR}/. Other paths are refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        run=_guarded_write,
    ))
    registry.set_workspace_dir(workspace)

    # Build a stripped LoopConfig: no plugin hooks (no recursion), no
    # memory plugins, capped iterations, extractor system prompt override.
    extractor_cfg = dc_replace(
        base_cfg,
        max_iterations=cfg.maxTurns,
        active_memory_config=None,
        dreaming_config=None,
        extract_memories_config=None,
        hook_registry=None,
        system_prompt_override=EXTRACTOR_SYSTEM_PROMPT,
        # Use override model if configured; else inherit parent.
        model=cfg.model.split("/", 1)[1] if (cfg.model and "/" in cfg.model) else base_cfg.model,
        api=cfg.model.split("/", 1)[0] if (cfg.model and "/" in cfg.model) else base_cfg.api,
        # Tag the turn source so future hooks can recognize extractor runs.
        turn_source="extractor",
        session_key=f"extractor:{session_key}",
        workspace_dir=workspace,
    )

    history: list[Message] = [Message("user", [{"type": "text", "text": full_user_prompt}])]

    session = AgentSession(
        history=history,
        registry=registry,
        on_event=lambda _evt: None,
        client=client,
        cfg=extractor_cfg,
        cancellation_token=CancellationToken(),
        todo_store=TodoStore(),
    )

    try:
        await session.run_turn(full_user_prompt)
    except Exception as exc:  # noqa: BLE001 — surface as warning, don't raise
        logger.warning("memory.extractor.run_turn_failed", f"{type(exc).__name__}: {exc}")
        return list(written_paths)

    return list(written_paths)
