"""Tool registry and built-in tools for nano-openclaw.

Mirrors `src/agents/tools/common.ts` (Tool interface) and
`src/agents/pi-embedded-subscribe.handlers.tools.ts` (dispatch).

Contract: ``ToolRegistry.dispatch`` ALWAYS returns a properly shaped
``tool_result`` content block. It NEVER raises. Failures are encoded as
``is_error=True`` so the model sees them and can react, just like
OpenClaw's ``isError: true`` convention.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TYPE_CHECKING

from rich.console import Console

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalDecision
from nano_openclaw.logger import get_logger

log = get_logger(__name__)

if TYPE_CHECKING:
    from nano_openclaw.todo import TodoStore

ToolHandler = Callable[..., "str | list[dict[str, Any]]"]
WorkspaceWriteHook = Callable[[str, "ToolExecutionContext"], None]
SkillUsageRecorder = Callable[[str, Any, "ToolExecutionContext"], None]
SkillInstaller = Callable[..., Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: ToolHandler
    terminal: bool = False


@dataclass(frozen=True)
class ToolExecutionContext:
    """Per-dispatch runtime state that should not live on the registry."""

    session_status_context: dict[str, Any] = field(default_factory=dict)
    eligible_skills: dict[str, Any] = field(default_factory=dict)
    workspace_dir: str | None = None
    state_dir: str | None = None
    allow_global_pip: bool = False
    spawn_tool_context: Optional[Any] = None
    todo_store: Optional["TodoStore"] = None


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)
    _session_status_context: dict[str, Any] = field(default_factory=dict)
    _eligible_skills: dict[str, Any] = field(default_factory=dict)
    approval_manager: Optional[ApprovalManager] = field(default=None)
    console: Optional[Console] = field(default=None)
    _workspace_dir: str | None = field(default=None)
    _state_dir: str | None = field(default=None)
    _allow_global_pip: bool = field(default=False)
    _spawn_tool_context: Optional[Any] = field(default=None)  # SpawnToolContext
    _hook_registry: Optional[Any] = field(default=None)
    approval_live_stopper: Optional[Callable[[], None]] = field(default=None)
    approval_handler: Optional[
        Callable[[Any, Any | None], "ApprovalDecision | Awaitable[ApprovalDecision]"]
    ] = field(default=None)
    before_workspace_write: Optional[WorkspaceWriteHook] = field(default=None)
    skill_usage_recorder: Optional[SkillUsageRecorder] = field(default=None)
    skill_installer: Optional[SkillInstaller] = field(default=None)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def set_session_status_context(self, **kwargs: Any) -> None:
        self._session_status_context = kwargs

    def set_eligible_skills(self, skills: dict[str, Any]) -> None:
        """Set eligible skills for Skill tool invocation."""
        self._eligible_skills = skills

    def set_workspace_dir(self, workspace_dir: str | Path) -> None:
        self._workspace_dir = str(workspace_dir)

    def set_state_dir(self, state_dir: str | Path) -> None:
        self._state_dir = str(state_dir)

    def set_allow_global_pip(self, allow: bool) -> None:
        self._allow_global_pip = bool(allow)

    def set_spawn_tool_context(self, context: Any) -> None:
        self._spawn_tool_context = context

    def set_hook_registry(self, hook_registry: Any) -> None:
        self._hook_registry = hook_registry

    def set_before_workspace_write(self, hook: WorkspaceWriteHook | None) -> None:
        self.before_workspace_write = hook

    def set_skill_usage_recorder(self, recorder: SkillUsageRecorder | None) -> None:
        self.skill_usage_recorder = recorder

    def set_skill_installer(self, installer: SkillInstaller | None) -> None:
        self.skill_installer = installer

    def hook_registry(self) -> Any | None:
        return self._hook_registry

    def clone(
        self,
        *,
        exclude: set[str] | None = None,
        console: Console | None | object = ...,
        approval_handler: Callable[[Any, Any | None], "ApprovalDecision | Awaitable[ApprovalDecision]"] | None | object = ...,
    ) -> "ToolRegistry":
        exclude = exclude or set()
        cloned = ToolRegistry(
            _tools={name: tool for name, tool in self._tools.items() if name not in exclude},
            approval_manager=self.approval_manager,
            console=self.console if console is ... else console,
            _workspace_dir=self._workspace_dir,
            _state_dir=self._state_dir,
            _allow_global_pip=self._allow_global_pip,
            _spawn_tool_context=self._spawn_tool_context,
            _hook_registry=self._hook_registry,
            approval_live_stopper=self.approval_live_stopper,
            approval_handler=self.approval_handler if approval_handler is ... else approval_handler,
            before_workspace_write=self.before_workspace_write,
            skill_usage_recorder=self.skill_usage_recorder,
            skill_installer=self.skill_installer,
        )
        cloned.set_session_status_context(**self._session_status_context)
        cloned.set_eligible_skills(dict(self._eligible_skills))
        return cloned

    def execution_context(self, **overrides: Any) -> ToolExecutionContext:
        """Build a per-dispatch context from registry-level defaults."""
        values = {
            "session_status_context": dict(self._session_status_context),
            "eligible_skills": dict(self._eligible_skills),
            "workspace_dir": self._workspace_dir,
            "state_dir": self._state_dir,
            "allow_global_pip": self._allow_global_pip,
            "spawn_tool_context": self._spawn_tool_context,
        }
        values.update(overrides)
        return ToolExecutionContext(**values)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Schemas in the shape Anthropic Messages API expects."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    async def dispatch(
        self,
        tool_use_id: str,
        name: str,
        args: dict[str, Any],
        cancellation_token: Any | None = None,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Dispatch tool with approval check if manager is set."""
        tool = self._tools.get(name)
        if tool is None:
            return _error_result(tool_use_id, f"unknown tool: {name!r}")

        ctx = context or ToolExecutionContext(
            session_status_context=self._session_status_context,
            eligible_skills=self._eligible_skills,
            workspace_dir=self._workspace_dir,
            state_dir=self._state_dir,
            allow_global_pip=self._allow_global_pip,
            spawn_tool_context=self._spawn_tool_context,
        )

        pip_install_protected = (
            name == "bash"
            and _is_python_package_install_command(str(args.get("command") or ""))
            and not ctx.allow_global_pip
        )

        # Check approval if manager is configured (sync, fast)
        if self.approval_manager and not pip_install_protected:
            eval_result = self.approval_manager.check_request(name, args)

            if eval_result.requires_approval:
                request = self.approval_manager.create_request(name, args)

                if self.approval_handler is None and self.console is None:
                    result = _error_result(
                        tool_use_id,
                        f"approval denied for {name}: non-interactive background execution cannot request approval ({request.reason})",
                    )
                    result["_denied"] = True
                    return result

                if callable(self.approval_live_stopper):
                    self.approval_live_stopper()

                ui = None
                if self.approval_handler is not None:
                    raw_decision = self.approval_handler(request, cancellation_token)
                    decision = await raw_decision if asyncio.iscoroutine(raw_decision) else raw_decision
                else:
                    if self.console is None:
                        result = _error_result(
                            tool_use_id,
                            f"approval denied for {name}: non-interactive background execution cannot request approval ({request.reason})",
                        )
                        result["_denied"] = True
                        return result

                    from nano_openclaw.approvals.ui import ApprovalUI
                    ui = ApprovalUI(self.console)
                    ui.render_request(request)
                    decision = ui.prompt_decision(
                        request,
                        cancellation_token=cancellation_token,
                    )

                self.approval_manager.record_decision(request.request_id, decision)

                if decision == ApprovalDecision.DENY:
                    if ui is not None:
                        ui.render_denied(request)
                    result = _error_result(
                        tool_use_id,
                        f"approval denied for {name}: {request.reason}",
                    )
                    result["_denied"] = True
                    return result

                if ui is not None:
                    ui.render_allowed(request, decision)

        if self._hook_registry:
            hook_payload = await self._hook_registry.run("before_tool_call", {
                "tool_name": name,
                "tool_args": args,
                "tool_use_id": tool_use_id,
            })
            if hook_payload.get("deny"):
                return _error_result(tool_use_id, hook_payload.get("reason", "denied by hook"))
            args = hook_payload.get("tool_args", args)

        # Execute tool — async-native tools are awaited directly; sync tools run
        # in a thread pool to avoid blocking the event loop.
        try:
            if name == "skill":
                skill_name = str(args.get("skill") or "")
                skill = ctx.eligible_skills.get(skill_name) if ctx.eligible_skills else None
                if skill is not None and self.skill_usage_recorder is not None:
                    try:
                        self.skill_usage_recorder(skill_name, skill, ctx)
                    except Exception:
                        pass
                raw = tool.run(args, eligible_skills=ctx.eligible_skills)
            elif name == "session_status":
                raw = tool.run(args, **ctx.session_status_context)
            elif name in ("read_file", "write_file", "list_dir", "apply_patch"):
                if name in ("write_file", "apply_patch") and self.before_workspace_write is not None:
                    try:
                        self.before_workspace_write(name, ctx)
                    except Exception:
                        pass
                raw = tool.run(args, workspace_dir=ctx.workspace_dir)
            elif name == "bash":
                raw = tool.run(
                    args,
                    workspace_dir=ctx.workspace_dir,
                    state_dir=ctx.state_dir,
                    allow_global_pip=ctx.allow_global_pip,
                )
            elif name == "skill_install":
                raw = tool.run(
                    args,
                    workspace_dir=ctx.workspace_dir,
                    state_dir=ctx.state_dir,
                    installer=self.skill_installer,
                )
            elif name in ("memory_get", "memory_search"):
                raw = tool.run(args, workspace_dir=ctx.workspace_dir)
            elif name in ("sessions_spawn", "subagents"):
                if ctx.spawn_tool_context is None:
                    return _error_result(tool_use_id, f"tool {name!r} requires spawn context (not configured)")
                raw = tool.run(args, context=ctx.spawn_tool_context)
            elif name == "todo":
                raw = tool.run(args, todo_store=ctx.todo_store)
            else:
                raw = tool.run(args)

            output = await raw if asyncio.iscoroutine(raw) else raw
        except Exception as exc:  # noqa: BLE001 — exceptions become tool_results
            log.warning("tools.dispatch.error", f"Tool {name} failed: {exc}")
            output: str | list[dict[str, Any]] = f"{type(exc).__name__}: {exc}"
            if self._hook_registry:
                hook_payload = await self._hook_registry.run("after_tool_call", {
                    "tool_name": name,
                    "tool_args": args,
                    "result": output,
                    "error": True,
                })
                output = hook_payload.get("result", output)
            if isinstance(output, list):
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": output,
                }
            return _error_result(tool_use_id, output)

        if self._hook_registry:
            hook_payload = await self._hook_registry.run("after_tool_call", {
                "tool_name": name,
                "tool_args": args,
                "result": output,
                "error": False,
            })
            output = hook_payload.get("result", output)

        content: list[dict[str, Any]] = (
            output if isinstance(output, list) else [{"type": "text", "text": output or "(no output)"}]
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }


def _error_result(tool_use_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": True,
        "content": [{"type": "text", "text": message}],
    }


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------

_READ_MAX_BYTES = 200_000

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_OTHER_MEDIA_EXTS = frozenset({
    ".bmp", ".tiff", ".tif", ".heic", ".heif", ".svg", ".ico",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".wav", ".ogg", ".flac", ".aac",
    ".pdf",
})


def _resolve_path(path_arg: str, workspace_dir: str | None) -> Path:
    """Resolve path relative to workspace_dir (mirrors openclaw pi-tools.host-edit.ts:25-29).
    
    Priority:
    1. Absolute path → use directly
    2. Relative path → resolve against workspace_dir
    3. No workspace_dir → resolve against cwd (fallback)
    """
    p = Path(path_arg)
    if p.is_absolute():
        return p
    if workspace_dir:
        return Path(workspace_dir) / p
    return p


def _read_file(args: dict[str, Any], workspace_dir: str | None = None) -> "str | list[dict[str, Any]]":
    path = _resolve_path(args["path"], workspace_dir)
    suffix = path.suffix.lower()

    if suffix in _IMAGE_EXTS:
        # Return the image as a content block so the model can actually see it,
        # rather than a stub that triggers a pointless retry.
        from nano_openclaw.core.images import load_image, to_anthropic_image_block
        try:
            b64, mime = load_image(str(path))
        except Exception as exc:
            log.warning("tools.read_file.image.error", f"Failed to load image {path}: {exc}")
            return f"[image load error: {path}: {exc}]"
        return [
            to_anthropic_image_block(b64, mime),
            {"type": "text", "text": f"Image: {path} ({path.stat().st_size:,} bytes)"},
        ]

    if suffix in _OTHER_MEDIA_EXTS:
        size = path.stat().st_size
        return f"[media file: {path} ({size:,} bytes)] Binary content not shown."

    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) > _READ_MAX_BYTES:
        return data[:_READ_MAX_BYTES] + f"\n[truncated at {_READ_MAX_BYTES} bytes]"
    return data


def _write_file(args: dict[str, Any], workspace_dir: str | None = None) -> str:
    path = _resolve_path(args["path"], workspace_dir)
    content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path}"


def _list_dir(args: dict[str, Any], workspace_dir: str | None = None) -> str:
    path = _resolve_path(args.get("path") or ".", workspace_dir)
    entries = sorted(
        f"{p.name}/" if p.is_dir() else p.name
        for p in path.iterdir()
    )
    return "\n".join(entries) if entries else "(empty)"


def _apply_patch(args: dict[str, Any], workspace_dir: str | None = None) -> str:
    """Apply a V4A-format patch."""
    try:
        from nano_openclaw.core.patch_parser import apply_v4a_patch
    except Exception as exc:  # pragma: no cover — defensive import guard
        raise RuntimeError(f"Patch failed: import error: {exc}") from exc

    patch = args.get("patch", "")
    if not patch:
        raise ValueError("patch is empty")

    try:
        result = apply_v4a_patch(patch, workspace_dir)
    except Exception as exc:  # noqa: BLE001 — normalize unexpected failures
        raise RuntimeError(f"Patch failed: {type(exc).__name__}: {exc}") from exc

    if not result.success:
        raise RuntimeError(f"Patch failed:\n{result.error or '(no error message)'}")

    summary_lines: list[str] = []
    if result.files_created:
        summary_lines.append(f"Created: {', '.join(result.files_created)}")
    if result.files_modified:
        summary_lines.append(f"Modified: {', '.join(result.files_modified)}")
    if result.files_deleted:
        summary_lines.append(f"Deleted: {', '.join(result.files_deleted)}")
    summary = "\n".join(summary_lines) or "Patch applied (no changes)."
    if result.diff:
        return summary + "\n\n" + result.diff
    return summary


_PIP_INSTALL_PATTERNS = (
    re.compile(r"(?i)(^|[;&|]\s*)pip(?:\d+(?:\.\d+)?)?\s+install(?:\s|$)"),
    re.compile(r"(?i)(^|[;&|]\s*)(?:python|python\d+(?:\.\d+)?|py)\s+-m\s+pip\s+install(?:\s|$)"),
)


def _is_python_package_install_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _PIP_INSTALL_PATTERNS)


def _pip_isolation_notice(state_dir: str | None) -> str:
    root = Path(state_dir) / "tools" / "python" / "skills" if state_dir else "{stateDir}/tools/python/skills"
    return (
        "\n[nano-openclaw] Bare pip installs are protected with PIP_REQUIRE_VIRTUALENV=true "
        "so skill dependencies do not install into the global Python environment. "
        "Declare the dependency in metadata.openclaw.install with kind: uv and run the "
        f"skill_install tool; isolated environments live under {root}.\n"
    )


async def _bash(
    args: dict[str, Any],
    workspace_dir: str | None = None,
    state_dir: str | None = None,
    allow_global_pip: bool = False,
) -> str:
    command = args["command"]
    timeout = int(args.get("timeout") or 30)
    workdir = args.get("workdir")
    cwd = workdir if workdir else (workspace_dir if workspace_dir else None)
    env = None
    pip_protected = _is_python_package_install_command(command) and not allow_global_pip
    if pip_protected:
        env = dict(os.environ)
        env["PIP_REQUIRE_VIRTUALENV"] = "true"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("tools.bash.timeout", f"Command timed out after {timeout}s: {command}")
            proc.kill()
            await proc.communicate()
            notice = _pip_isolation_notice(state_dir) if pip_protected else ""
            return f"exit=1\n--- stderr ---\nCommand timed out after {timeout}s\n{notice}"
        returncode = proc.returncode
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
    except NotImplementedError:
        # SelectorEventLoop on Windows doesn't support create_subprocess_shell.
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    command, shell=True, capture_output=True, cwd=cwd, env=env, timeout=timeout,
                ),
            )
        except subprocess.TimeoutExpired:
            log.warning("tools.bash.timeout", f"Command timed out after {timeout}s: {command}")
            notice = _pip_isolation_notice(state_dir) if pip_protected else ""
            return f"exit=1\n--- stderr ---\nCommand timed out after {timeout}s\n{notice}"
        returncode = result.returncode
        stdout = result.stdout.decode(errors="replace")
        stderr = result.stderr.decode(errors="replace")
    if pip_protected:
        stderr += _pip_isolation_notice(state_dir)
    return (
        f"exit={returncode}\n"
        f"--- stdout ---\n{stdout}"
        f"--- stderr ---\n{stderr}"
    )


def _session_status(
    args: dict[str, Any],
    *,
    model: str = "",
    session_id: str = "",
    context_budget: int = 0,
    context_window: int = 0,
    current_tokens: int = 0,
    compaction_count: int = 0,
    message_count: int = 0,
) -> str:
    lines: list[str] = []

    if model:
        lines.append(f"Model: {model}")

    if session_id:
        lines.append(f"Session: {session_id}")

    if context_budget > 0:
        def format_tokens(n: int) -> str:
            if n >= 1000:
                return f"{n / 1000:.1f}k"
            return str(n)
        used = format_tokens(current_tokens)
        budget = format_tokens(context_budget)
        ctx_line = f"Context: {used}/{budget} tokens"
        if context_window > 0 and context_window != context_budget:
            ctx_line += f" (window: {format_tokens(context_window)})"
        if compaction_count > 0:
            ctx_line += f" · Compactions: {compaction_count}"
        lines.append(ctx_line)

    if message_count > 0:
        lines.append(f"Messages: {message_count}")

    return "\n".join(lines)


def _invoke_skill(
    args: dict[str, Any],
    eligible_skills: dict[str, Any] | None = None,
) -> "str | list[dict[str, Any]]":
    """Invoke a skill by name, returning its content.

    Mirrors openclaw's Skill tool behavior:
    - LLM calls this tool to activate a skill
    - Returns the skill's SKILL.md content
    """
    skill_name = args.get("skill")
    if not skill_name:
        raise ValueError("skill name required")

    if not eligible_skills or skill_name not in eligible_skills:
        raise ValueError(f"skill '{skill_name}' not found or not eligible")

    skill = eligible_skills[skill_name]

    def with_skill_location(content: str) -> str:
        return "\n".join([
            f"[Skill invoked: {skill.name}]",
            "",
            f"Skill file location: {skill.filePath}",
            f"Skill directory: {skill.baseDir}",
            "Resolve any relative paths mentioned by the skill against the skill directory.",
            "",
            "Skill instructions:",
            content,
        ])

    # Return skill content
    if skill.content:
        return with_skill_location(skill.content)

    # Load content from file if not already loaded
    skill_path = Path(skill.filePath)
    if not skill_path.exists():
        raise FileNotFoundError(f"skill file not found: {skill.filePath}")
    return with_skill_location(skill_path.read_text(encoding="utf-8"))


async def _skill_install(
    args: dict[str, Any],
    workspace_dir: str | None = None,
    state_dir: str | None = None,
    installer: SkillInstaller | None = None,
) -> str:
    skill_name = str(args.get("skill") or "").strip()
    install_id = str(args.get("installId") or args.get("install_id") or "").strip()
    timeout = int(args.get("timeout") or 300)
    if not skill_name:
        raise ValueError("skill name required")
    if not install_id:
        raise ValueError("installId required")
    if not workspace_dir:
        raise ValueError("workspace_dir not configured")
    if not state_dir:
        from nano_openclaw.config import resolve_state_dir
        state_dir = str(resolve_state_dir())
    if installer is None:
        raise RuntimeError("skill installer not configured")

    return await installer(
        workspace_dir=str(workspace_dir),
        state_dir=str(state_dir),
        skill_name=skill_name,
        install_id=install_id,
        timeout=timeout,
    )


def _todo_handler(
    args: dict[str, Any],
    todo_store: "TodoStore | None" = None,
) -> str:
    """Single entry point for the `todo` tool.

    - 传 ``todos`` 参数 → 写入（可选 ``merge=true`` 按 id 增量）
    - 省略 ``todos`` → 读当前列表
    - store 未绑定 → 返回错误字符串（保持 dispatch 永不抛异常的不变量）
    """
    import json

    if todo_store is None:
        return json.dumps(
            {"error": "TodoStore not bound for this session"},
            ensure_ascii=False,
        )

    try:
        todos_arg = args.get("todos")
        merge = bool(args.get("merge", False))
        if todos_arg is not None:
            items = todo_store.write(todos_arg, merge=merge)
        else:
            items = todo_store.read()

        pending = sum(1 for i in items if i["status"] == "pending")
        in_progress = sum(1 for i in items if i["status"] == "in_progress")
        completed = sum(1 for i in items if i["status"] == "completed")
        cancelled = sum(1 for i in items if i["status"] == "cancelled")

        return json.dumps(
            {
                "todos": items,
                "summary": {
                    "total": len(items),
                    "pending": pending,
                    "in_progress": in_progress,
                    "completed": completed,
                    "cancelled": cancelled,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001 — keep dispatch contract
        return json.dumps(
            {"error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


def _current_time(args: dict[str, Any]) -> str:
    import json
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()

    def fmt(dt: datetime) -> dict[str, str]:
        return {
            "iso": dt.isoformat(timespec="seconds"),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "weekday": dt.strftime("%A"),
        }

    return json.dumps(
        {
            "local": {
                **fmt(now_local),
                "timezone": now_local.tzname() or "local",
                "utc_offset": now_local.strftime("%z"),
            },
            "utc": fmt(now_utc),
            "unix_ms": int(now_utc.timestamp() * 1000),
        },
        ensure_ascii=False,
        indent=2,
    )


def _build_core_tools() -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="current_time",
            description="Get the current time with explicit weekday, local timezone offset, UTC, ISO 8601, and Unix epoch. Call this before any reasoning that depends on today's date or day-of-week — do not infer the weekday from a date string.",
            input_schema={
                "type": "object",
                "properties": {},
            },
            run=_current_time,
        ),
        Tool(
            name="read_file",
            description="Read a UTF-8 text file from disk and return its contents. Binary/media files (images, video, audio, PDF) return a metadata summary only — attach image paths directly in the user message to analyse them.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read."}},
                "required": ["path"],
            },
            run=_read_file,
        ),
        Tool(
            name="write_file",
            description=(
                "Create a new file, or fully replace an existing file's contents. "
                "Parent directories are auto-created. "
                "For SURGICAL edits to an existing file (changing a function, fixing a bug, "
                "adding a few lines), use apply_patch instead — it's cheaper in tokens and "
                "safer against accidental whole-file overwrite. Use write_file only when you genuinely need "
                "the whole file (brand-new file, generated output, template render)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Destination file path."},
                    "content": {"type": "string", "description": "UTF-8 text content."},
                },
                "required": ["path", "content"],
            },
            run=_write_file,
        ),
        Tool(
            name="list_dir",
            description="List entries in a directory. Directories are suffixed with '/'.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path. Defaults to '.'."}
                },
            },
            run=_list_dir,
        ),
        Tool(
            name="apply_patch",
            description=(
                "Apply a V4A-format patch to files in the workspace. "
                "Use this instead of write_file for surgical edits to existing files. "
                "Supports Add / Update / Delete / Move operations across multiple files in one call.\n\n"
                "Format:\n"
                "*** Begin Patch\n"
                "*** Update File: path/to/file.py\n"
                "@@ optional context hint @@\n"
                " context line (space prefix)\n"
                "-removed line\n"
                "+added line\n"
                "*** Add File: path/new.py\n"
                "+content\n"
                "*** Delete File: path/old.py\n"
                "*** Move File: a.py -> b.py\n"
                "*** End Patch\n\n"
                "Validation is two-phase: if any hunk fails to locate, NO files are modified."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "V4A patch text."},
                },
                "required": ["patch"],
            },
            run=_apply_patch,
        ),
        Tool(
            name="bash",
            description="Run a shell command via /bin/sh -c (or cmd on Windows). Returns exit code, stdout, and stderr. Defaults to workspace directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Default 30.",
                        "default": 30,
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory for the command. Defaults to workspace directory.",
                    },
                },
                "required": ["command"],
            },
            run=_bash,
        ),
        Tool(
            name="session_status",
            description="Show current session status: model, session ID, context usage (tokens/compactions), and message count.",
            input_schema={
                "type": "object",
                "properties": {},
            },
            run=_session_status,
        ),
Tool(
            name="skill",
            description="Invoke a skill by name to load its specialized instructions. Use when the task matches a skill's description from the available_skills list in the system prompt.",
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill name to invoke (must match a name from available_skills)."},
                    "args": {"type": "string", "description": "Optional arguments for the skill task."},
                },
                "required": ["skill"],
            },
            run=_invoke_skill,
        ),
        Tool(
            name="todo",
            description=(
                "Manage your task list for the current session. "
                "Use for complex tasks with 3+ steps or when the user provides multiple tasks. "
                "Call with no parameters to read the current list.\n\n"
                "Writing:\n"
                "- Provide 'todos' array to create/update items\n"
                "- merge=false (default): replace the entire list with a fresh plan\n"
                "- merge=true: update existing items by id, add any new ones\n\n"
                "Each item: {id: string, content: string, "
                "status: pending|in_progress|completed|cancelled}\n"
                "List order is priority. **Only ONE item in_progress at a time.**\n"
                "Mark items completed immediately when done. If something fails, "
                "cancel it and add a revised item.\n\n"
                "Always returns the full current list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Task items to write. Omit to read the current list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Unique item identifier",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Task description",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                        "cancelled",
                                    ],
                                    "description": "Current status",
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                    "merge": {
                        "type": "boolean",
                        "description": (
                            "true: update existing items by id, add new ones. "
                            "false (default): replace the entire list."
                        ),
                        "default": False,
                    },
                },
            },
            run=_todo_handler,
        ),
        Tool(
            name="skill_install",
            description="Install a skill dependency into an OpenClaw-managed isolated environment. Only Python/uv skill installers are supported in nano-openclaw.",
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill name."},
                    "installId": {"type": "string", "description": "Installer id from metadata.openclaw.install."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Default 300.",
                        "default": 300,
                    },
                },
                "required": ["skill", "installId"],
            },
            run=_skill_install,
        ),
    ]

    return tools


def build_core_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in _build_core_tools():
        registry.register(tool)
    return registry
