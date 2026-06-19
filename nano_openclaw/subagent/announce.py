"""Announce mechanism for subagent results.

Delivers subagent completion results back to the requester session
as a user message, formatted with status and summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from nano_openclaw.core.loop import Message
from nano_openclaw.subagent.registry import SubagentRegistry
from nano_openclaw.subagent.runner import SubagentRunnerResult
from nano_openclaw.subagent.types import SubagentRunRecord, SubagentStatus


@dataclass
class AnnounceEvent:
    """Event representing a subagent completion announcement."""
    run_id: str
    status: SubagentStatus
    task: str
    label: Optional[str] = None
    result_text: Optional[str] = None
    elapsed_ms: Optional[int] = None
    error_message: Optional[str] = None
    transcript_path: Optional[str] = None


def build_announce_content(result: SubagentRunnerResult, record: SubagentRunRecord) -> str:
    """Build announcement content for a completed subagent run."""
    lines = []
    
    label = record.label or record.task[:60]
    if len(record.task) > 60 and not record.label:
        label += "..."
    
    status_text = {
        SubagentStatus.COMPLETED: "completed",
        SubagentStatus.ERROR: "failed",
        SubagentStatus.TIMEOUT: "timed out",
        SubagentStatus.KILLED: "killed",
    }.get(result.status, result.status.value)
    
    lines.append(f"<subagent_completion runId=\"{result.run_id}\" status=\"{status_text}\">")
    lines.append(f"  <task>{label}</task>")
    
    if result.elapsed_ms:
        elapsed_sec = result.elapsed_ms / 1000
        if elapsed_sec < 60:
            lines.append(f"  <elapsed>{elapsed_sec:.1f}s</elapsed>")
        else:
            minutes = int(elapsed_sec / 60)
            seconds = int(elapsed_sec % 60)
            lines.append(f"  <elapsed>{minutes}m {seconds}s</elapsed>")
    
    if result.result_text:
        max_chars = 500
        text = result.result_text[:max_chars]
        if len(result.result_text) > max_chars:
            text += "..."
        lines.append(f"  <result>{text}</result>")
    
    if result.error_message:
        lines.append(f"  <error>{result.error_message}</error>")
    
    if result.transcript_path:
        lines.append(f"  <transcript>{result.transcript_path}</transcript>")
    
    lines.append("</subagent_completion>")
    
    return "\n".join(lines)


def build_announce_message(result: SubagentRunnerResult, record: SubagentRunRecord) -> Message:
    """Build a user message containing the announcement."""
    content = build_announce_content(result, record)
    return Message("user", [{"type": "text", "text": content}])


def format_announce_for_display(result: SubagentRunnerResult, record: SubagentRunRecord) -> str:
    """Format announcement for TUI display (human-readable)."""
    label = record.label or record.task[:50]
    if len(record.task) > 50 and not record.label:
        label += "..."
    
    status_icon = {
        SubagentStatus.COMPLETED: "✓",
        SubagentStatus.ERROR: "✗",
        SubagentStatus.TIMEOUT: "⏱",
        SubagentStatus.KILLED: "💀",
    }.get(result.status, "?")
    
    elapsed_str = ""
    if result.elapsed_ms:
        elapsed_sec = result.elapsed_ms / 1000
        if elapsed_sec < 60:
            elapsed_str = f" ({elapsed_sec:.1f}s)"
        else:
            elapsed_str = f" ({int(elapsed_sec / 60)}m {int(elapsed_sec % 60)}s)"
    
    lines = [
        f"Subagent {status_icon} {label}{elapsed_str}",
        f"Run ID: {result.run_id}",
        f"Status: {result.status.value}",
    ]
    
    if result.result_text:
        preview = result.result_text[:300]
        if len(result.result_text) > 300:
            preview += "..."
        lines.append(f"Result: {preview}")
    
    if result.error_message:
        lines.append(f"Error: {result.error_message}")
    
    return "\n".join(lines)


def should_announce(result: SubagentRunnerResult) -> bool:
    """Check if result should be announced (not killed or silent)."""
    return result.status != SubagentStatus.KILLED


def create_announce_event(result: SubagentRunnerResult, record: SubagentRunRecord) -> AnnounceEvent:
    """Create an announce event from result."""
    return AnnounceEvent(
        run_id=result.run_id,
        status=result.status,
        task=record.task,
        label=record.label,
        result_text=result.result_text,
        elapsed_ms=result.elapsed_ms,
        error_message=result.error_message,
        transcript_path=str(result.transcript_path) if result.transcript_path else None,
    )