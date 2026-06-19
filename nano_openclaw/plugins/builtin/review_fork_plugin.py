"""Background Review Fork plugin.

Each end_turn (every N turns + cooldown), spawn a restricted background
sub-agent that reads the recent conversation and decides whether to distill
durable user preferences / lessons into MEMORY.md or an existing SKILL.md.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from nano_openclaw.logger import get_logger
from nano_openclaw.plugins.builtin.review_fork_prompt import REVIEW_PROMPT
from nano_openclaw.plugins.api import PluginApi
from nano_openclaw.core.tools import ToolRegistry

logger = get_logger(__name__)


REVIEW_FORK_ALLOWLIST = frozenset({
    "read_file",
    "list_dir",
    "memory_get",
    "memory_search",
    "write_file",
    "current_time",
})

REVIEW_FORK_LABEL = "review"


@dataclass
class ReviewForkConfig:
    enabled: bool = True
    trigger_n: int = 10
    cooldown_s: int = 60
    timeout_s: int = 90
    model_aux: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewForkConfig":
        if not d:
            return cls()
        return cls(
            enabled=bool(d.get("enabled", True)),
            trigger_n=int(d.get("trigger_n", d.get("triggerN", 10))),
            cooldown_s=int(d.get("cooldown_s", d.get("cooldownS", 60))),
            timeout_s=int(d.get("timeout_s", d.get("timeoutS", 90))),
            model_aux=d.get("model_aux") or d.get("modelAux"),
        )


def build_review_fork_registry(parent: ToolRegistry) -> ToolRegistry:
    """Build a tool registry restricted to the review-fork allowlist."""
    registry = ToolRegistry()
    for name, tool in parent._tools.items():
        if name in REVIEW_FORK_ALLOWLIST:
            registry.register(tool)
    registry.approval_manager = parent.approval_manager
    registry.console = None  # background — no interactive prompts
    if parent._workspace_dir:
        registry.set_workspace_dir(parent._workspace_dir)
    if parent._state_dir:
        registry.set_state_dir(parent._state_dir)
    registry.set_allow_global_pip(parent._allow_global_pip)
    return registry


def _serialize_recent_messages(
    messages: list[dict[str, Any]],
    *,
    max_messages: int = 10,
    max_chars: int = 8000,
) -> str:
    tail = messages[-max_messages:] if len(messages) > max_messages else list(messages)
    pieces: list[str] = []
    for m in tail:
        role = m.get("role", "?")
        content = m.get("content")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(str(block.get("text", "")))
                elif btype == "tool_use":
                    text_parts.append(f"[tool_use: {block.get('name', '?')}]")
                elif btype == "tool_result":
                    raw = block.get("content")
                    if isinstance(raw, str):
                        text_parts.append(f"[tool_result: {raw[:200]}]")
                    elif isinstance(raw, list):
                        for sub in raw:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                text_parts.append(f"[tool_result: {str(sub.get('text', ''))[:200]}]")
                elif btype == "thinking":
                    continue
        joined = " ".join(p for p in text_parts if p).strip()
        if joined:
            pieces.append(f"{role}: {joined}")
    blob = "\n\n".join(pieces)
    if len(blob) > max_chars:
        blob = blob[-max_chars:]
    return blob


def _format_skill_paths(parent_registry: ToolRegistry) -> str:
    skills = parent_registry._eligible_skills or {}
    if not skills:
        return "(no skills currently loaded)"
    lines: list[str] = []
    for name, skill in list(skills.items())[:30]:
        path = getattr(skill, "path", None) or getattr(skill, "skill_path", None) or ""
        lines.append(f"- {name}: {path}")
    return "\n".join(lines) if lines else "(no skills currently loaded)"


@dataclass
class ReviewForkState:
    cfg: ReviewForkConfig
    turn_counter: int = 0
    cooldown_until: float = 0.0
    active_run_id: Optional[str] = None
    last_run_at: float = 0.0
    total_runs: int = 0
    total_skipped: int = 0
    last_skip_reason: Optional[str] = None
    state_dir: Optional[Path] = None

    def _is_review_session(self, session_key: str) -> bool:
        if not session_key:
            return False
        return ":subagent:" in session_key

    async def maybe_fork(self, payload: dict[str, Any]) -> None:
        """`after_turn` hook entry — apply skip rules then optionally spawn."""
        if not self.cfg.enabled:
            return
        stop_reason = payload.get("stop_reason")
        if stop_reason != "end_turn":
            self.total_skipped += 1
            self.last_skip_reason = f"stop_reason={stop_reason!r}"
            return
        session_key = str(payload.get("session_key") or "")
        if self._is_review_session(session_key):
            self.total_skipped += 1
            self.last_skip_reason = "recursive (subagent session)"
            return
        if not payload.get("workspace_dir"):
            self.total_skipped += 1
            self.last_skip_reason = "no workspace_dir"
            return
        now = time.time()
        if now < self.cooldown_until:
            self.total_skipped += 1
            self.last_skip_reason = "cooldown"
            return
        self.turn_counter += 1
        if self.turn_counter % self.cfg.trigger_n != 0:
            self.total_skipped += 1
            self.last_skip_reason = (
                f"counter {self.turn_counter} not multiple of {self.cfg.trigger_n}"
            )
            return
        await self._do_fork(payload)

    async def force_fork(self, payload: dict[str, Any]) -> Optional[str]:
        """Bypass cooldown / counter / stop-reason / recursion checks."""
        return await self._do_fork(payload)

    async def _do_fork(self, payload: dict[str, Any]) -> Optional[str]:
        from nano_openclaw.features.subagents.runner import get_runner
        from nano_openclaw.features.subagents.types import SpawnParams, SubagentCleanupMode

        parent_registry = payload.get("tool_registry")
        client = payload.get("client")
        loop_config = payload.get("loop_config")
        session_key = str(payload.get("session_key") or "")
        session_dir = payload.get("session_dir") or ""
        workspace_dir = payload.get("workspace_dir") or ""
        if parent_registry is None or client is None or loop_config is None:
            self.total_skipped += 1
            self.last_skip_reason = "missing payload deps"
            return None
        if not session_key:
            self.total_skipped += 1
            self.last_skip_reason = "no session_key"
            return None

        runner = get_runner()
        if not runner.can_spawn(session_key):
            self.total_skipped += 1
            self.last_skip_reason = "runner concurrency cap reached"
            return None

        restricted = build_review_fork_registry(parent_registry)
        messages = payload.get("messages_snapshot") or []
        transcript_blob = _serialize_recent_messages(messages)
        skill_paths = _format_skill_paths(parent_registry)
        task_blob = REVIEW_PROMPT.format(
            workspace=workspace_dir or "(unknown)",
            skill_paths=skill_paths,
            transcript_blob=transcript_blob or "(empty)",
        )

        params = SpawnParams(
            task=task_blob,
            label=REVIEW_FORK_LABEL,
            model=self.cfg.model_aux,
            run_timeout_seconds=self.cfg.timeout_s,
            cleanup=SubagentCleanupMode.KEEP,
        )
        def _on_review_event(ev: Any) -> None:
            try:
                from nano_openclaw.core.loop import SubagentAnnounced
                if isinstance(ev, SubagentAnnounced):
                    self._append_result_log(
                        now=time.time(),
                        run_id=ev.run_id,
                        status=ev.status,
                        elapsed_ms=ev.elapsed_ms,
                        result_text=ev.result_text,
                        error_message=ev.error_message,
                    )
            except Exception:
                pass

        try:
            record = runner.spawn(
                params,
                requester_session_key=session_key,
                client=client,
                base_cfg=loop_config,
                session_dir=Path(session_dir) if session_dir else Path("."),
                workspace_dir=Path(workspace_dir),
                parent_registry=restricted,
                on_event=_on_review_event,
            )
        except Exception as exc:  # noqa: BLE001 - plugin must not break loop
            logger.warning(
                "review_fork.spawn_error",
                f"failed to spawn review fork: {exc}",
            )
            self.total_skipped += 1
            self.last_skip_reason = f"spawn error: {exc}"
            return None

        now = time.time()
        self.cooldown_until = now + self.cfg.cooldown_s
        self.active_run_id = record.run_id
        self.last_run_at = now
        self.total_runs += 1
        self._append_run_log(
            now=now,
            run_id=record.run_id,
            session_key=session_key,
            workspace_dir=workspace_dir,
            messages_count=len(messages),
        )
        logger.info(
            "review_fork.spawned",
            f"review fork spawned run_id={record.run_id} session={session_key}",
        )
        return record.run_id

    def _append_run_log(
        self,
        *,
        now: float,
        run_id: str,
        session_key: str,
        workspace_dir: str,
        messages_count: int,
    ) -> None:
        if self.state_dir is None:
            return
        try:
            log_path = self.state_dir / "review-fork.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": now,
                "run_id": run_id,
                "session_key": session_key,
                "workspace_dir": workspace_dir,
                "messages_count": messages_count,
                "trigger_n": self.cfg.trigger_n,
                "model_aux": self.cfg.model_aux,
            }
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - observability best-effort
            pass

    def _append_result_log(
        self,
        *,
        now: float,
        run_id: str,
        status: str,
        elapsed_ms: Optional[int],
        result_text: Optional[str],
        error_message: Optional[str],
    ) -> None:
        if self.state_dir is None:
            return
        parsed: Any = None
        raw = (result_text or "").strip()
        if raw and raw != "NOOP":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        try:
            log_path = self.state_dir / "review-fork-results.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": now,
                "run_id": run_id,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "noop": raw == "NOOP",
                "structured": parsed if isinstance(parsed, dict) else None,
                "summary": (parsed or {}).get("summary") if isinstance(parsed, dict) else raw[:240],
                "error_message": error_message,
            }
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        now = time.time()
        cooldown_remaining = max(0.0, self.cooldown_until - now)
        return {
            "enabled": self.cfg.enabled,
            "trigger_n": self.cfg.trigger_n,
            "cooldown_s": self.cfg.cooldown_s,
            "timeout_s": self.cfg.timeout_s,
            "model_aux": self.cfg.model_aux,
            "turn_counter": self.turn_counter,
            "total_runs": self.total_runs,
            "total_skipped": self.total_skipped,
            "active_run_id": self.active_run_id,
            "last_run_at": self.last_run_at,
            "cooldown_remaining_s": cooldown_remaining,
            "last_skip_reason": self.last_skip_reason,
        }


# Module-level singleton so the slash command can introspect state without
# threading it through the backend RPC layer.
_REVIEW_FORK_STATE: Optional[ReviewForkState] = None


def _set_state(state: ReviewForkState) -> None:
    global _REVIEW_FORK_STATE
    _REVIEW_FORK_STATE = state


def get_state() -> Optional[ReviewForkState]:
    return _REVIEW_FORK_STATE


def reset_state() -> None:
    """Reset the global state (for tests)."""
    global _REVIEW_FORK_STATE
    _REVIEW_FORK_STATE = None


class ReviewForkPlugin:
    id = "nano-review-fork"
    name = "Review Fork"

    def register(self, api: PluginApi) -> None:
        plugin_cfg = api.plugin_config or {}
        # Prefer top-level config (review_fork field) when plugin_config is empty.
        top_cfg = getattr(api.config, "review_fork", None)
        if not plugin_cfg and top_cfg is not None:
            try:
                plugin_cfg = top_cfg.model_dump()
            except Exception:
                plugin_cfg = {}
        cfg = ReviewForkConfig.from_dict(plugin_cfg)
        # Always register the hook + state so runtime `review_fork.set` can flip
        # enabled on/off without re-registering. `maybe_fork` early-returns when
        # cfg.enabled is False, so the cost is a single dict-deref per turn.
        state = ReviewForkState(cfg)
        # Wire the state_dir for observability sidecar (jsonl run log).
        state_dir = getattr(api.config, "state_dir", None)
        if state_dir:
            try:
                state.state_dir = Path(str(state_dir))
            except Exception:
                state.state_dir = None
        _set_state(state)
        api.register_hook("after_turn", state.maybe_fork)
