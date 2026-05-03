"""Subagent tools for nano-openclaw.

Provides sessions_spawn tool for spawning background subagent runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from nano_openclaw.loop import LoopConfig
from nano_openclaw.subagent import (
    SpawnParams,
    SubagentContextMode,
    SubagentCleanupMode,
    SubagentRunner,
    get_runner,
)
from nano_openclaw.tools import Tool, ToolRegistry


@dataclass
class SpawnToolContext:
    """Context needed for sessions_spawn tool execution."""
    requester_session_key: str
    session_dir: Path
    workspace_dir: Path
    client: Any
    base_cfg: LoopConfig
    on_event: Optional[Callable[[Any], None]] = None
    parent_registry: Optional[ToolRegistry] = field(default=None)


def sessions_spawn_tool(
    args: dict[str, Any],
    *,
    context: SpawnToolContext,
) -> str | list[dict[str, Any]]:
    """Execute sessions_spawn tool.
    
    Parameters (from args):
        task: str - The task description for the subagent (required)
        label: str - Optional human-readable label
        model: str - Override model for subagent
        thinking: str - Override thinking level
        runTimeoutSeconds: int - Timeout for the run
        cleanup: "keep" | "delete" - Cleanup mode after completion
        context: "isolated" | "fork" - Context mode
    
    Returns:
        JSON-like result string with status and run info
    """
    task = args.get("task")
    if not task:
        return "Error: 'task' parameter is required"
    
    params = SpawnParams(
        task=task,
        label=args.get("label"),
        model=args.get("model"),
        thinking=args.get("thinking"),
        run_timeout_seconds=args.get("runTimeoutSeconds"),
        cleanup=SubagentCleanupMode(args.get("cleanup", "keep")),
        context=SubagentContextMode(args.get("context", "isolated")),
    )
    
    runner = get_runner()
    
    if not runner.can_spawn(context.requester_session_key):
        active = runner.registry.count_active()
        return f"Error: Max concurrent subagents reached ({active}/{runner.config.max_concurrent})"
    
    try:
        record = runner.spawn(
            params=params,
            requester_session_key=context.requester_session_key,
            client=context.client,
            base_cfg=context.base_cfg,
            session_dir=context.session_dir,
            workspace_dir=context.workspace_dir,
            on_event=context.on_event,
            parent_registry=context.parent_registry,
        )
        
        return (
            f"Subagent spawned successfully.\n"
            f"Run ID: {record.run_id}\n"
            f"Session: {record.child_session_key}\n"
            f"Task: {params.task[:100]}{'...' if len(params.task) > 100 else ''}\n"
            f"Model: {record.model or context.base_cfg.model}\n"
            f"Status: {record.status.value}\n"
            f"Use /subagents to list and control runs."
        )
        
    except Exception as exc:
        return f"Error spawning subagent: {type(exc).__name__}: {exc}"


def build_spawn_tool() -> Tool:
    """Build the sessions_spawn tool definition."""
    return Tool(
        name="sessions_spawn",
        description=(
            "Spawn a fresh isolated sub-agent session to execute a task independently. "
            "Use when the work is complex, slow, or can run in parallel with the current session. "
            "The sub-agent inherits the workspace directory and runs with filtered tools "
            "(no sessions_spawn or session-management tools). "
            "Completion is push-based: results are auto-announced as a user message — no polling needed. "
            "Use this when the work should happen in a fresh child session instead of the current one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description for the subagent (required). Be specific and clear."
                },
                "label": {
                    "type": "string",
                    "description": "Optional human-readable label for the run."
                },
                "model": {
                    "type": "string",
                    "description": "Override model for this subagent run."
                },
                "thinking": {
                    "type": "string",
                    "enum": ["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"],
                    "description": "Override thinking level."
                },
                "runTimeoutSeconds": {
                    "type": "integer",
                    "description": "Timeout in seconds (0 = no timeout). Default from config."
                },
                "cleanup": {
                    "type": "string",
                    "enum": ["keep", "delete"],
                    "default": "keep",
                    "description": "Cleanup mode: 'keep' preserves transcript, 'delete' archives immediately."
                },
                "context": {
                    "type": "string",
                    "enum": ["isolated"],
                    "default": "isolated",
                    "description": "Context mode: 'isolated' (fresh transcript). Fork mode is reserved for future use."
                },
            },
            "required": ["task"],
        },
        run=sessions_spawn_tool,
    )


def subagents_list_tool(
    args: dict[str, Any],
    *,
    context: SpawnToolContext,
) -> str:
    """List subagent runs."""
    runner = get_runner()
    
    show_all = args.get("all", False)
    requester_only = args.get("requesterOnly", True)
    
    if requester_only:
        runs = runner.registry.list_for_requester(context.requester_session_key)
    else:
        runs = runner.registry.list_all() if show_all else runner.registry.list_active()
    
    if not runs:
        return "No subagent runs."
    
    lines = ["Subagent runs:"]
    for run in runs:
        elapsed = ""
        if run.elapsed_ms:
            elapsed = f" ({run.elapsed_ms}ms)"
        
        status_icon = {
            "pending": "⏳",
            "running": "🔄",
            "completed": "✓",
            "error": "✗",
            "timeout": "⏱",
            "killed": "💀",
        }.get(run.status.value, "?")
        
        label = run.label or run.task[:40]
        if len(run.task) > 40 and not run.label:
            label += "..."
        
        lines.append(
            f"  {status_icon} {run.run_id}: {label}{elapsed} [{run.status.value}]"
        )
    
    return "\n".join(lines)


def build_subagents_tool() -> Tool:
    """Build the subagents control tool."""
    return Tool(
        name="subagents",
        description=(
            "List sub-agent runs spawned in the current session. "
            "Use for on-demand status checks, debugging, or to review completed results. "
            "Do NOT call this in a loop to poll for status — completions arrive automatically as user messages."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Show all runs including terminated."
                },
                "requesterOnly": {
                    "type": "boolean",
                    "default": True,
                    "description": "Only show runs from current session."
                },
            },
        },
        run=subagents_list_tool,
    )