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
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], str]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: ToolHandler


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

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
        tool = self._tools.get(name)
        if tool is None:
            return _error_result(tool_use_id, f"unknown tool: {name!r}")
        try:
            text = tool.run(args)
        except Exception as exc:  # noqa: BLE001 — exceptions become tool_results
            return _error_result(tool_use_id, f"{type(exc).__name__}: {exc}")
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": text or "(no output)"}],
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


def _read_file(args: dict[str, Any]) -> str:
    path = Path(args["path"])
    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) > _READ_MAX_BYTES:
        return data[:_READ_MAX_BYTES] + f"\n[truncated at {_READ_MAX_BYTES} bytes]"
    return data


def _write_file(args: dict[str, Any]) -> str:
    path = Path(args["path"])
    content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path}"


def _list_dir(args: dict[str, Any]) -> str:
    path = Path(args.get("path") or ".")
    entries = sorted(
        f"{p.name}/" if p.is_dir() else p.name
        for p in path.iterdir()
    )
    return "\n".join(entries) if entries else "(empty)"


def _bash(args: dict[str, Any]) -> str:
    command = args["command"]
    timeout = int(args.get("timeout") or 30)
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (
        f"exit={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}"
        f"--- stderr ---\n{result.stderr}"
    )


BUILTIN_TOOLS: list[Tool] = [
    Tool(
        name="read_file",
        description="Read a UTF-8 text file from disk and return its contents.",
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
        description="Run a shell command via /bin/sh -c (or cmd on Windows). Returns exit code, stdout, and stderr.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default 30.",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
        run=_bash,
    ),
]


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    return registry
