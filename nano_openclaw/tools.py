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

if TYPE_CHECKING:
    from nano_openclaw.config.types import ToolsConfig
    from nano_openclaw.plugins.registry import HookRegistry
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
    _state_dir: str | None = field(default=None)
    _allow_global_pip: bool = field(default=False)
    _spawn_tool_context: Optional[Any] = field(default=None)  # SpawnToolContext
    _hook_registry: Optional["HookRegistry"] = field(default=None)
    approval_live_stopper: Optional[Callable[[], None]] = field(default=None)
    approval_handler: Optional[
        Callable[[Any, Any | None], "ApprovalDecision | Awaitable[ApprovalDecision]"]
    ] = field(default=None)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def set_session_status_context(self, **kwargs: Any) -> None:
        self._session_status_context = kwargs

    def set_eligible_skills(self, skills: dict[str, "Skill"]) -> None:
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

    def set_hook_registry(self, hook_registry: "HookRegistry") -> None:
        self._hook_registry = hook_registry

    def hook_registry(self) -> "HookRegistry | None":
        return self._hook_registry

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
    ) -> dict[str, Any]:
        """Dispatch tool with approval check if manager is set."""
        tool = self._tools.get(name)
        if tool is None:
            return _error_result(tool_use_id, f"unknown tool: {name!r}")

        pip_install_protected = (
            name == "bash"
            and _is_python_package_install_command(str(args.get("command") or ""))
            and not self._allow_global_pip
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
                raw = tool.run(args, eligible_skills=self._eligible_skills)
            elif name == "session_status":
                raw = tool.run(args, **self._session_status_context)
            elif name in ("read_file", "write_file", "list_dir"):
                raw = tool.run(args, workspace_dir=self._workspace_dir)
            elif name == "bash":
                raw = tool.run(
                    args,
                    workspace_dir=self._workspace_dir,
                    state_dir=self._state_dir,
                    allow_global_pip=self._allow_global_pip,
                )
            elif name == "skill_install":
                raw = tool.run(
                    args,
                    workspace_dir=self._workspace_dir,
                    state_dir=self._state_dir,
                )
            elif name in ("memory_get", "memory_search"):
                raw = tool.run(args, workspace_dir=self._workspace_dir)
            elif name in ("sessions_spawn", "subagents"):
                if self._spawn_tool_context is None:
                    return _error_result(tool_use_id, f"tool {name!r} requires spawn context (not configured)")
                raw = tool.run(args, context=self._spawn_tool_context)
            else:
                raw = tool.run(args)

            output = await raw if asyncio.iscoroutine(raw) else raw
        except Exception as exc:  # noqa: BLE001 — exceptions become tool_results
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
        proc.kill()
        await proc.communicate()
        return f"exit=1\n--- stderr ---\nCommand timed out after {timeout}s\n"
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if pip_protected:
        stderr += _pip_isolation_notice(state_dir)
    return (
        f"exit={proc.returncode}\n"
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

    from nano_openclaw.skills.install import install_skill

    result = await install_skill(
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        skill_name=skill_name,
        install_id=install_id,
        timeout=timeout,
    )
    parts = [f"ok={str(result.ok).lower()}", f"message={result.message}"]
    if result.ok:
        from nano_openclaw.skills.install import resolve_skill_python_env
        env_info = resolve_skill_python_env(state_dir, skill_name)
        parts.append(f"python={env_info.python_executable}")
        parts.append(f"venv={env_info.venv_dir}")
    if result.code is not None:
        parts.append(f"code={result.code}")
    if result.stdout:
        parts.append(f"--- stdout ---\n{result.stdout}")
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr}")
    return "\n".join(parts)


def web_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from nano_openclaw.web_search import web_search as _web_search

    return _web_search(*args, **kwargs)


async def web_fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from nano_openclaw.web_fetch import web_fetch as _web_fetch

    return await _web_fetch(*args, **kwargs)


def _build_core_tools() -> list[Tool]:
    tools: list[Tool] = [
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


def build_memory_tools(memory_search_config: Any | None = None) -> list[Tool]:
    from nano_openclaw.memory.tools import memory_get, memory_search

    return [
        Tool(
            name="memory_get",
            description="Read a specific memory file (MEMORY.md or memory/*.md). Use to retrieve exact content by path.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace (e.g., MEMORY.md or memory/2026-05-02.md)"},
                    "from": {"type": "integer", "description": "Starting line number (1-indexed)"},
                    "lines": {"type": "integer", "description": "Number of lines to read"},
                },
                "required": ["path"],
            },
            run=lambda args, workspace_dir=None: memory_get(args, workspace_dir),
        ),
        Tool(
            name="memory_search",
            description="Search memory files (MEMORY.md + memory/*.md) for keywords. Use before answering questions about prior work or decisions.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "maxResults": {"type": "integer", "description": "Max results (default 10)"},
                    "minScore": {"type": "number", "description": "Min match score 0-1 (default 0.1)"},
                },
                "required": ["query"],
            },
            run=lambda args, workspace_dir=None: memory_search(
                args,
                workspace_dir,
                config=memory_search_config,
            ),
        ),
    ]


def build_web_tools(tools_config: "ToolsConfig | None" = None) -> list[Tool]:
    web_config = tools_config.web if tools_config else None
    search_config = web_config.search if web_config else None
    fetch_config = web_config.fetch if web_config else None
    tools: list[Tool] = []

    if search_config is None or search_config.enabled:
        default_max_results = search_config.maxResults if search_config else 10
        default_region = search_config.region if search_config else "wt-wt"
        tools.append(
            Tool(
                name="web_search",
                description="Search the web using DuckDuckGo. Returns titles, URLs, and snippets. Use before web_fetch to find relevant pages.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "maxResults": {
                            "type": "integer",
                            "description": f"Max results (default {default_max_results})",
                            "default": default_max_results,
                        },
                    },
                    "required": ["query"],
                },
                run=lambda args: web_search(
                    args["query"],
                    max_results=args.get("maxResults", default_max_results),
                    region=default_region,
                ).get("text", "[no results]"),
            )
        )

    if fetch_config is None or fetch_config.enabled:
        default_extract_mode = fetch_config.extractMode if fetch_config else "markdown"
        default_max_chars = fetch_config.maxChars if fetch_config else 20_000
        default_max_redirects = fetch_config.maxRedirects if fetch_config else 3
        default_timeout_seconds = fetch_config.timeoutSeconds if fetch_config else 30

        async def _run_web_fetch(
            args: dict[str, Any],
            _em: str = default_extract_mode,
            _mc: int = default_max_chars,
            _mr: int = default_max_redirects,
            _ts: int = default_timeout_seconds,
        ) -> str:
            result = await web_fetch(
                args["url"],
                extract_mode=args.get("extractMode", _em),
                max_chars=args.get("maxChars", _mc),
                max_redirects=_mr,
                timeout_seconds=_ts,
            )
            return result.get("text", "[fetch failed]")

        tools.append(
            Tool(
                name="web_fetch",
                description="Fetch and extract readable content from a URL (HTML→markdown/text). Use after web_search to read specific pages.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "HTTP/HTTPS URL"},
                        "extractMode": {
                            "type": "string",
                            "enum": ["markdown", "text"],
                            "default": default_extract_mode,
                        },
                        "maxChars": {
                            "type": "integer",
                            "description": f"Max chars to return (default {default_max_chars})",
                            "default": default_max_chars,
                        },
                    },
                    "required": ["url"],
                },
                run=_run_web_fetch,
            )
        )

    return tools


def build_core_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in _build_core_tools():
        registry.register(tool)
    return registry
