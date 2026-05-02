"""Tool registry and built-in tools for nano-openclaw.

Mirrors `src/agents/tools/common.ts` (Tool interface) and
`src/agents/pi-embedded-subscribe.handlers.tools.ts` (dispatch).

Contract: ``ToolRegistry.dispatch`` ALWAYS returns a properly shaped
``tool_result`` content block. It NEVER raises. Failures are encoded as
``is_error=True`` so the model sees them and can react, just like
OpenClaw's ``isError: true`` convention.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from rich.console import Console

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalDecision

if TYPE_CHECKING:
    from nano_openclaw.skills.types import Skill

ToolHandler = Callable[..., "str | list[dict[str, Any]]"]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: ToolHandler


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)
    _session_status_context: dict[str, Any] = field(default_factory=dict)
    _eligible_skills: dict[str, "Skill"] = field(default_factory=dict)
    approval_manager: Optional[ApprovalManager] = field(default=None)
    console: Optional[Console] = field(default=None)
    _workspace_dir: str | None = field(default=None)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def set_session_status_context(self, **kwargs: Any) -> None:
        self._session_status_context = kwargs

    def set_eligible_skills(self, skills: dict[str, "Skill"]) -> None:
        """Set eligible skills for Skill tool invocation."""
        self._eligible_skills = skills

    def set_workspace_dir(self, workspace_dir: str | Path) -> None:
        self._workspace_dir = str(workspace_dir)

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

    def dispatch(self, tool_use_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool with approval check if manager is set."""
        tool = self._tools.get(name)
        if tool is None:
            return _error_result(tool_use_id, f"unknown tool: {name!r}")
        
        # Check approval if manager is configured
        if self.approval_manager:
            eval_result = self.approval_manager.check_request(name, args)
            
            if eval_result.requires_approval:
                # Need approval - create request and prompt user
                request = self.approval_manager.create_request(name, args)
                
                if self.console:
                    from nano_openclaw.approvals.ui import ApprovalUI
                    ui = ApprovalUI(self.console)
                    ui.render_request(request)
                    decision = ui.prompt_decision(request)
                    
                    self.approval_manager.record_decision(request.request_id, decision)
                    
                    if decision == ApprovalDecision.DENY:
                        ui.render_denied(request)
                        return _error_result(
                            tool_use_id,
                            f"approval denied for {name}: {request.reason}"
                        )
                    
                    ui.render_allowed(request, decision)
        
        # Execute tool
        try:
            if name == "Skill":
                output = tool.run(args, eligible_skills=self._eligible_skills)
            elif name == "session_status":
                output = tool.run(args, **self._session_status_context)
            elif name in ("read_file", "write_file", "list_dir", "bash"):
                output = tool.run(args, workspace_dir=self._workspace_dir)
            else:
                output = tool.run(args)
        except Exception as exc:  # noqa: BLE001 — exceptions become tool_results
            return _error_result(tool_use_id, f"{type(exc).__name__}: {exc}")
        
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
        from nano_openclaw.images import load_image, to_anthropic_image_block
        try:
            b64, mime = load_image(str(path))
        except Exception as exc:
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


def _bash(args: dict[str, Any], workspace_dir: str | None = None) -> str:
    command = args["command"]
    timeout = int(args.get("timeout") or 30)
    workdir = args.get("workdir")
    cwd = workdir if workdir else (workspace_dir if workspace_dir else None)
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    return (
        f"exit={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}"
        f"--- stderr ---\n{result.stderr}"
    )


def _session_status(
    args: dict[str, Any],
    *,
    model: str = "",
    session_id: str = "",
    context_budget: int = 0,
    current_tokens: int = 0,
    compaction_count: int = 0,
    message_count: int = 0,
) -> str:
    from datetime import datetime
    now = datetime.now()
    weekday = now.strftime("%A")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    timezone = datetime.now().astimezone().tzname() or "local"

    lines = [f"Clock: {weekday}, {date_str} {time_str} ({timezone})"]

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
        lines.append(f"Context: {used}/{budget} tokens")
        if compaction_count > 0:
            lines[-1] += f" · Compactions: {compaction_count}"

    if message_count > 0:
        lines.append(f"Messages: {message_count}")

    return "\n".join(lines)


def _invoke_skill(
    args: dict[str, Any],
    eligible_skills: dict[str, "Skill"] | None = None,
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

    # Return skill content
    if skill.content:
        return skill.content

    # Load content from file if not already loaded
    skill_path = Path(skill.filePath)
    if not skill_path.exists():
        raise FileNotFoundError(f"skill file not found: {skill.filePath}")
    return skill_path.read_text(encoding="utf-8")


BUILTIN_TOOLS: list[Tool] = [
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
        description="Write text to a file, creating parent directories. Overwrites existing files.",
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
        description="Show current session status: date/time, model, session ID, context usage (tokens/compactions), and message count. Use for current time or session state.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        run=_session_status,
    ),
    Tool(
        name="Skill",
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
]


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    return registry
