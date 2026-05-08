#!/usr/bin/env python3
"""Session transcript analyzer for nano-openclaw.

Single-pass parser: reads a .jsonl transcript once and reports tool errors,
approval denials, interrupted turns, and context pressure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ToolError:
    tool_use_id: str
    tool_name: str
    message: str
    is_approval_denial: bool


@dataclass
class TranscriptData:
    session_id: str
    model: str
    cwd: str
    timestamp: str
    message_count: int
    compaction_count: int
    last_message_id: str
    tool_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    tool_errors: list[ToolError] = field(default_factory=list)
    last_messages: list[dict[str, Any]] = field(default_factory=list)
    # "user" | "assistant" | ""
    last_role: str = ""
    # True when the last assistant message ends with unresolved tool_use blocks
    interrupted: bool = False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _content_text(content: Any, max_len: int = 300) -> str:
    """Extract plain text from a tool_result content value."""
    if isinstance(content, str):
        return content[:max_len]
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return " ".join(parts)[:max_len]
    return str(content)[:max_len]


def parse_transcript(path: Path) -> TranscriptData | str:
    """Parse transcript in a single pass. Returns TranscriptData or an error string."""
    if not path.exists():
        return f"Transcript not found: {path}"

    session_id = model = cwd = timestamp = last_message_id = ""
    message_count = compaction_count = 0
    # assistant tool_use_id -> tool_name, populated as we scan
    tool_call_map: dict[str, str] = {}
    # tool_name -> {calls, errors}
    tool_stats: dict[str, dict[str, int]] = {}
    tool_errors: list[ToolError] = []
    messages: list[dict[str, Any]] = []

    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = entry.get("type")

            if t == "session":
                session_id = entry.get("id", "")
                model = entry.get("model", "")
                cwd = entry.get("cwd", "")
                timestamp = entry.get("timestamp", "")

            elif t == "message":
                messages.append(entry)
                message_count += 1
                last_message_id = entry.get("id", "")

                for block in entry.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")

                    if btype == "tool_use":
                        tid = block.get("id") or ""
                        name = block.get("name") or "unknown"
                        if tid:
                            tool_call_map[tid] = name
                        stats = tool_stats.setdefault(name, {"calls": 0, "errors": 0})
                        stats["calls"] += 1

                    elif btype == "tool_result" and block.get("is_error"):
                        tid = block.get("tool_use_id") or ""
                        tool_name = tool_call_map.get(tid, "unknown")
                        if tool_name in tool_stats:
                            tool_stats[tool_name]["errors"] += 1
                        msg = _content_text(block.get("content", ""))
                        tool_errors.append(ToolError(
                            tool_use_id=tid,
                            tool_name=tool_name,
                            message=msg,
                            is_approval_denial="approval denied" in msg,
                        ))

            elif t == "compaction":
                compaction_count += 1

    # Interrupted: last message is an assistant turn that has tool_use blocks.
    # In a clean session the final message is always assistant text (end_turn);
    # if tool_use exists there it means tool results never came back.
    interrupted = False
    if messages and messages[-1].get("role") == "assistant":
        last_blocks = messages[-1].get("content", [])
        if any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in last_blocks
        ):
            interrupted = True

    last_role = messages[-1].get("role", "") if messages else ""

    return TranscriptData(
        session_id=session_id,
        model=model,
        cwd=cwd,
        timestamp=timestamp,
        message_count=message_count,
        compaction_count=compaction_count,
        last_message_id=last_message_id,
        tool_stats=tool_stats,
        tool_errors=tool_errors,
        last_messages=messages[-5:],
        last_role=last_role,
        interrupted=interrupted,
    )


# ---------------------------------------------------------------------------
# Failure detection
# ---------------------------------------------------------------------------

def detect_failures(data: TranscriptData) -> list[str]:
    """Return diagnostic observations, most severe first."""
    findings: list[str] = []

    # Session ended without an assistant conclusion
    if data.last_role == "user":
        findings.append(
            "No assistant conclusion: session ends with a user/tool_result message "
            "— the assistant never produced a final reply"
        )

    if data.interrupted:
        findings.append(
            "Turn was interrupted: last assistant message has unresolved tool_use "
            "(session was likely cancelled mid-turn)"
        )

    # Approval denials
    denial_counts: Counter = Counter(
        e.tool_name for e in data.tool_errors if e.is_approval_denial
    )
    for tool, n in denial_counts.most_common():
        findings.append(f"Tool `{tool}` blocked by approval gate ({n}x)")

    # Real errors — group by (tool, message) to surface repeating patterns
    real_errors = [e for e in data.tool_errors if not e.is_approval_denial]
    error_groups: Counter = Counter(
        (e.tool_name, e.message[:120]) for e in real_errors
    )
    for (tool, msg), n in error_groups.most_common():
        repeat = f" ({n}x repeated)" if n > 1 else ""
        findings.append(f"Tool `{tool}` error{repeat}: {msg}")

    if data.compaction_count >= 3:
        findings.append(
            f"Context compacted {data.compaction_count}x — "
            "repeated context budget exhaustion"
        )
    elif data.compaction_count > 0:
        findings.append(f"Context was compacted {data.compaction_count} time(s)")

    total_errors = sum(s["errors"] for s in data.tool_stats.values())
    total_calls = sum(s["calls"] for s in data.tool_stats.values())
    if total_calls > 0 and total_errors > 0:
        rate = total_errors / total_calls * 100
        findings.append(
            f"Overall tool error rate: {rate:.0f}% ({total_errors}/{total_calls})"
        )

    return findings


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(data: TranscriptData, failures: list[str]) -> str:
    lines: list[str] = []

    lines.append("# Session Debug Report\n")
    lines.append(f"**Session ID**: `{data.session_id or 'N/A'}`  ")
    lines.append(f"**Model**: `{data.model or 'N/A'}`  ")
    lines.append(f"**Timestamp**: {data.timestamp or 'N/A'}  ")
    lines.append(f"**CWD**: `{data.cwd or 'N/A'}`  ")
    lines.append(
        f"**Messages**: {data.message_count} | "
        f"**Compactions**: {data.compaction_count} | "
        f"**Last msg**: `{data.last_message_id or 'N/A'}`"
    )
    lines.append("")
    lines.append("---\n")

    lines.append("## Failure Analysis\n")
    if failures:
        for f in failures:
            lines.append(f"- {f}")
    else:
        lines.append("No obvious failure patterns detected.")
    lines.append("")

    if data.tool_errors:
        lines.append("## Tool Errors\n")
        for i, err in enumerate(data.tool_errors, 1):
            tag = " *(approval denial)*" if err.is_approval_denial else ""
            lines.append(f"{i}. `{err.tool_name}`{tag}")
            lines.append(f"   > {err.message}")
            lines.append("")

    if data.tool_stats:
        lines.append("## Tool Statistics\n")
        lines.append("| Tool | Calls | Errors |")
        lines.append("|------|------:|-------:|")
        for name, stats in sorted(data.tool_stats.items()):
            lines.append(f"| `{name}` | {stats['calls']} | {stats['errors']} |")
        lines.append("")

    if data.last_messages:
        lines.append("## Last Messages\n")
        for msg in data.last_messages:
            role = msg.get("role", "?")
            lines.append(f"### `{role}`\n")
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if len(text) > 400:
                        text = text[:400] + "…"
                    lines.append(text)
                elif btype == "thinking":
                    thought = block.get("thinking", "")
                    if len(thought) > 200:
                        thought = thought[:200] + "…"
                    lines.append(f"*[thinking]* {thought}")
                elif btype == "tool_use":
                    tid = (block.get("id") or "")[:20]
                    lines.append(f"*[tool_use]* `{block.get('name')}` id=`{tid}…`")
                elif btype == "tool_result":
                    status = "**ERROR**" if block.get("is_error") else "ok"
                    tid = (block.get("tool_use_id") or "")[:20]
                    preview = _content_text(block.get("content", ""), 200)
                    lines.append(f"*[tool_result]* `{tid}…` {status}: {preview}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session selection helpers
# ---------------------------------------------------------------------------

def _load_store(sessions_dir: Path) -> dict[str, Any]:
    store_path = sessions_dir / "sessions.json"
    if not store_path.exists():
        return {"lastSessionId": None, "sessions": {}}
    return json.loads(store_path.read_text(encoding="utf-8"))


def _parse_time(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        # Make naive datetime UTC-aware so comparisons don't crash
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def find_sessions_in_range(
    sessions_dir: Path,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> list[str]:
    store = _load_store(sessions_dir)
    results: list[tuple[float, str]] = []
    for sid, meta in store.get("sessions", {}).items():
        dt = datetime.fromtimestamp(meta.get("updated_at", 0), tz=timezone.utc)
        if start and dt < start:
            continue
        if end and dt > end:
            continue
        results.append((meta.get("updated_at", 0), sid))
    results.sort(reverse=True)
    return [sid for _, sid in results[:limit]]


def get_last_session_id(sessions_dir: Path) -> str | None:
    return _load_store(sessions_dir).get("lastSessionId")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a nano-openclaw session transcript for failure reasons.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Last session for the default agent
  python scripts/analyze.py

  # Specific agent
  python scripts/analyze.py --agent-id coder

  # Specific session
  python scripts/analyze.py --session-id abc-123-def

  # All sessions in a time range (ISO date, UTC assumed)
  python scripts/analyze.py --start-time 2025-05-01 --end-time 2025-05-08
""",
    )
    parser.add_argument(
        "--agent-id", default="default",
        help="Agent ID to inspect (default: default)",
    )
    parser.add_argument(
        "--session-id",
        help="Specific session ID; bypasses sessions.json lookup",
    )
    parser.add_argument(
        "--start-time",
        help="Only sessions updated on/after this ISO timestamp (UTC assumed)",
    )
    parser.add_argument(
        "--end-time",
        help="Only sessions updated on/before this ISO timestamp (UTC assumed)",
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Max sessions to analyze in time-range mode (default: 10)",
    )
    parser.add_argument(
        "--state-dir",
        help="Override state directory (default: auto-resolved via nano_openclaw.config)",
    )
    args = parser.parse_args()

    # Resolve the sessions directory
    if args.state_dir:
        sessions_dir = Path(args.state_dir) / "agents" / args.agent_id / "sessions"
    else:
        try:
            from nano_openclaw.config import resolve_state_dir
            sessions_dir = resolve_state_dir() / "agents" / args.agent_id / "sessions"
        except ImportError:
            sessions_dir = Path(".nano-openclaw") / "agents" / args.agent_id / "sessions"

    if not sessions_dir.exists():
        print(f"Error: sessions directory not found: {sessions_dir}", file=sys.stderr)
        sys.exit(1)

    # Determine session IDs to analyze
    start_dt = _parse_time(args.start_time or "")
    end_dt = _parse_time(args.end_time or "")

    if args.session_id:
        session_ids = [args.session_id]
    elif start_dt or end_dt:
        session_ids = find_sessions_in_range(sessions_dir, start_dt, end_dt, args.limit)
        if not session_ids:
            print("No sessions found in the specified time range.", file=sys.stderr)
            sys.exit(1)
    else:
        last = get_last_session_id(sessions_dir)
        if not last:
            print(
                f"No sessions found for agent '{args.agent_id}'. "
                f"Sessions dir: {sessions_dir}",
                file=sys.stderr,
            )
            sys.exit(1)
        session_ids = [last]

    print(
        f"Analyzing {len(session_ids)} session(s) for agent '{args.agent_id}'...\n",
        file=sys.stderr,
    )

    for i, sid in enumerate(session_ids, 1):
        if len(session_ids) > 1:
            print(f"{'=' * 60}", file=sys.stderr)
            print(f"[{i}/{len(session_ids)}] {sid}", file=sys.stderr)
            print(f"{'=' * 60}\n", file=sys.stderr)

        result = parse_transcript(sessions_dir / f"{sid}.jsonl")
        if isinstance(result, str):
            print(f"# Error\n\n{result}\n")
            continue

        failures = detect_failures(result)
        print(generate_report(result, failures))


if __name__ == "__main__":
    main()
