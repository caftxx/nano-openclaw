"""Memory Dreaming: background consolidation of short-term recall into long-term memory.

Mirrors openclaw extensions/memory-core/src/dreaming.ts and dreaming-phases.ts:
- Track recall events from memory_search (always, not conditional on config.enabled)
- Light phase: collect and filter candidates from recall state
- Deep phase: score and promote qualified entries to MEMORY.md
- Dream Diary: append narrative summary to DREAMS.md
- Scheduled auto-trigger: check at startup via cron-style frequency
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# ============================================================================
# Config (runtime dataclass, mirrors DreamingConfigInput from config/types.py)
# ============================================================================

@dataclass
class DreamingConfig:
    enabled: bool = False
    frequency: str = "0 3 * * *"
    min_score: float = 0.5
    min_recall_count: int = 2
    min_unique_queries: int = 1
    max_promotions: int = 10
    diary: bool = True
    model: str | None = None


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class ShortTermRecallEntry:
    path: str
    start_line: int
    end_line: int
    snippet: str
    recall_count: int = 0
    query_hashes: list[str] = field(default_factory=list)
    last_recalled_at: str = ""
    first_recalled_at: str = ""
    promoted_at: str | None = None


@dataclass
class DreamingState:
    version: int = 1
    last_run_at: str | None = None
    entries: dict[str, ShortTermRecallEntry] = field(default_factory=dict)


@dataclass
class DreamingResult:
    candidates: list[ShortTermRecallEntry]
    promoted: list[tuple[ShortTermRecallEntry, float, str]]  # (entry, score, content)
    elapsed_ms: int


# ============================================================================
# State persistence
# ============================================================================

_DREAMS_DIR = "memory/.dreams"
_STATE_FILE = "short-term-recall.json"


def _state_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / _DREAMS_DIR / _STATE_FILE


def load_dreaming_state(workspace_dir: str) -> DreamingState:
    path = _state_path(workspace_dir)
    if not path.exists():
        return DreamingState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries: dict[str, ShortTermRecallEntry] = {}
        for key, e in raw.get("entries", {}).items():
            entries[key] = ShortTermRecallEntry(
                path=e.get("path", ""),
                start_line=e.get("start_line", 1),
                end_line=e.get("end_line", 1),
                snippet=e.get("snippet", ""),
                recall_count=e.get("recall_count", 0),
                query_hashes=e.get("query_hashes", []),
                last_recalled_at=e.get("last_recalled_at", ""),
                first_recalled_at=e.get("first_recalled_at", ""),
                promoted_at=e.get("promoted_at"),
            )
        return DreamingState(
            version=raw.get("version", 1),
            last_run_at=raw.get("last_run_at"),
            entries=entries,
        )
    except Exception:
        return DreamingState()


def _save_dreaming_state(workspace_dir: str, state: DreamingState) -> None:
    path = _state_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries_dict = {
        key: {
            "path": e.path,
            "start_line": e.start_line,
            "end_line": e.end_line,
            "snippet": e.snippet,
            "recall_count": e.recall_count,
            "query_hashes": e.query_hashes,
            "last_recalled_at": e.last_recalled_at,
            "first_recalled_at": e.first_recalled_at,
            "promoted_at": e.promoted_at,
        }
        for key, e in state.entries.items()
    }
    data = {
        "version": state.version,
        "last_run_at": state.last_run_at,
        "updated_at": datetime.now().isoformat(),
        "entries": entries_dict,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================================================================
# Recall tracking (called by memory_search on every hit)
# ============================================================================

def track_recall(
    path: str,
    start_line: int,
    end_line: int,
    snippet: str,
    query: str,
    workspace_dir: str,
) -> None:
    """Record a memory_search hit. Always called regardless of dreaming.enabled."""
    key = f"{path}:{start_line}-{end_line}"
    query_hash = hashlib.md5(query.encode()).hexdigest()[:8]
    now = datetime.now().isoformat()

    state = load_dreaming_state(workspace_dir)

    if key in state.entries:
        e = state.entries[key]
        e.recall_count += 1
        if query_hash not in e.query_hashes:
            e.query_hashes.append(query_hash)
        e.last_recalled_at = now
    else:
        state.entries[key] = ShortTermRecallEntry(
            path=path,
            start_line=start_line,
            end_line=end_line,
            snippet=snippet[:200],
            recall_count=1,
            query_hashes=[query_hash],
            last_recalled_at=now,
            first_recalled_at=now,
        )

    try:
        _save_dreaming_state(workspace_dir, state)
    except (OSError, PermissionError):
        pass


# ============================================================================
# Scheduling
# ============================================================================

def _parse_cron_field(field: str, min_val: int, max_val: int) -> list[int]:
    """Parse a single cron field into a sorted list of matching integers.

    Supports: '*' (all), '*/n' (step), 'n' (exact value).
    """
    if field == "*":
        return list(range(min_val, max_val + 1))
    if field.startswith("*/"):
        step = int(field[2:])
        return list(range(min_val, max_val + 1, step))
    return [int(field)]


def _last_cron_occurrence(frequency: str, now: datetime) -> datetime | None:
    """Return the most recent scheduled datetime at or before now.

    Only supports "minute hour * * *" format (day/month/weekday must be '*').
    """
    parts = frequency.strip().split()
    if len(parts) < 5 or parts[2] != "*" or parts[3] != "*" or parts[4] != "*":
        return None
    try:
        hours = _parse_cron_field(parts[1], 0, 23)
        minutes = _parse_cron_field(parts[0], 0, 59)
    except (ValueError, IndexError):
        return None

    for days_back in range(2):
        check_date = (now - timedelta(days=days_back)).date()
        for h in reversed(hours):
            for m in reversed(minutes):
                candidate = datetime(check_date.year, check_date.month, check_date.day, h, m)
                if candidate <= now:
                    return candidate
    return None


def _next_cron_occurrence(frequency: str, now: datetime) -> datetime | None:
    """Return the next scheduled datetime strictly after now."""
    parts = frequency.strip().split()
    if len(parts) < 5 or parts[2] != "*" or parts[3] != "*" or parts[4] != "*":
        return None
    try:
        hours = _parse_cron_field(parts[1], 0, 23)
        minutes = _parse_cron_field(parts[0], 0, 59)
    except (ValueError, IndexError):
        return None

    for days_ahead in range(2):
        check_date = (now + timedelta(days=days_ahead)).date()
        for h in hours:
            for m in minutes:
                candidate = datetime(check_date.year, check_date.month, check_date.day, h, m)
                if candidate > now:
                    return candidate
    return None


def is_dreaming_due(frequency: str, last_run_at: str | None) -> bool:
    """Return True if a dreaming sweep is due.

    Supports "minute hour * * *" cron format including step expressions
    (e.g. "*/5 * * * *", "0 */3 * * *", "*/5 */3 * * *").
    Triggers when the most recent scheduled occurrence has not yet been run.
    """
    now = datetime.now()
    last_occurrence = _last_cron_occurrence(frequency, now)
    if last_occurrence is None:
        return False
    if last_run_at is None:
        return True
    try:
        last = datetime.fromisoformat(last_run_at)
        return last < last_occurrence
    except (ValueError, TypeError):
        return True


def update_last_run_at(workspace_dir: str) -> None:
    state = load_dreaming_state(workspace_dir)
    state.last_run_at = datetime.now().isoformat()
    _save_dreaming_state(workspace_dir, state)


def next_scheduled_seconds(frequency: str) -> float:
    """Return seconds until next scheduled run based on cron frequency.

    Supports "minute hour * * *" format including step expressions like */5.
    Falls back to 86400 (24h) for unsupported formats.
    """
    now = datetime.now()
    next_occ = _next_cron_occurrence(frequency, now)
    if next_occ is None:
        return 86400.0
    return (next_occ - now).total_seconds()


def start_dreaming_scheduler(
    workspace_dir: str,
    config: DreamingConfig,
    model: str,
    api_client: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    """Start a background daemon thread that runs dreaming sweeps on schedule.

    The thread wakes at the configured frequency (cron-style), independent of
    user interaction. Uses stop_event.wait(timeout) so it exits cleanly when
    the main process sets stop_event before shutdown.
    """

    def _run_once() -> None:
        try:
            run_dreaming(workspace_dir, config, model, api_client=api_client, blocking=False)
            # Returns None silently if /dreaming run is already in progress.
        except Exception:
            pass

    def _loop() -> None:
        # If already due at startup (e.g. last ran yesterday), fire immediately.
        state = load_dreaming_state(workspace_dir)
        if is_dreaming_due(config.frequency, state.last_run_at):
            _run_once()

        while not stop_event.is_set():
            wait_secs = next_scheduled_seconds(config.frequency)
            # wait() returns True if event was set (shutdown), False on timeout (time to run)
            if stop_event.wait(timeout=wait_secs):
                break
            _run_once()

    t = threading.Thread(target=_loop, daemon=True, name="dreaming-scheduler")
    t.start()
    return t


# ============================================================================
# Light phase
# ============================================================================

def run_light_phase(workspace_dir: str) -> list[ShortTermRecallEntry]:
    """Collect and filter recall candidates. No writes."""
    state = load_dreaming_state(workspace_dir)
    workspace = Path(workspace_dir)

    candidates = [
        e
        for e in state.entries.values()
        if not e.promoted_at
        and e.recall_count >= 1
        and (workspace / e.path).exists()
    ]

    candidates.sort(key=lambda e: e.recall_count, reverse=True)
    return candidates[:50]


# ============================================================================
# Deep phase scoring and promotion
# ============================================================================

def _compute_score(entry: ShortTermRecallEntry, max_recall: int) -> float:
    """Score a recall entry using 3 weighted signals (simplified from openclaw's 6)."""
    freq_score = entry.recall_count / max(max_recall, 1)

    n_unique = len(entry.query_hashes)
    div_score = n_unique / (n_unique + 1.0)

    recency_score = 0.5
    if entry.last_recalled_at:
        try:
            last = datetime.fromisoformat(entry.last_recalled_at)
            days = (datetime.now() - last).days
            recency_score = math.exp(-0.1 * days)
        except (ValueError, TypeError):
            pass

    return 0.40 * freq_score + 0.35 * div_score + 0.25 * recency_score


def _rehydrate_snippet(entry: ShortTermRecallEntry, workspace_dir: str) -> str | None:
    """Read current content of the entry's source lines. Returns None if stale."""
    try:
        file_path = Path(workspace_dir) / entry.path
        if not file_path.exists():
            return None
        lines = file_path.read_text(encoding="utf-8").split("\n")
        start = entry.start_line - 1
        end = entry.end_line
        if start < 0 or end > len(lines):
            return None
        return "\n".join(lines[start:end]).strip()
    except Exception:
        return None


def run_deep_phase(
    workspace_dir: str,
    config: DreamingConfig,
    candidates: list[ShortTermRecallEntry],
) -> list[tuple[ShortTermRecallEntry, float, str]]:
    """Score candidates, promote qualified entries to MEMORY.md.

    Returns list of (entry, score, rehydrated_content) for promoted entries.
    """
    if not candidates:
        return []

    max_recall = max(e.recall_count for e in candidates)
    today = date.today().isoformat()

    scored = [
        (e, _compute_score(e, max_recall))
        for e in candidates
        if _compute_score(e, max_recall) >= config.min_score
        and e.recall_count >= config.min_recall_count
        and len(e.query_hashes) >= config.min_unique_queries
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    scored = scored[:config.max_promotions]

    if not scored:
        return []

    memory_path = Path(workspace_dir) / "MEMORY.md"
    promoted: list[tuple[ShortTermRecallEntry, float, str]] = []
    promotion_blocks: list[str] = []

    for entry, score in scored:
        content = _rehydrate_snippet(entry, workspace_dir)
        if content is None:
            continue
        promotion_blocks.append(
            f"<!-- dreaming:promoted {today} score={score:.2f} recalls={entry.recall_count} -->\n"
            f"{content}\n"
        )
        promoted.append((entry, score, content))

    if promotion_blocks:
        existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
        with memory_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"\n## Dreaming Promotions {today}\n\n")
            for block in promotion_blocks:
                f.write(block + "\n")

        state = load_dreaming_state(workspace_dir)
        now_iso = datetime.now().isoformat()
        for entry, score, content in promoted:
            key = f"{entry.path}:{entry.start_line}-{entry.end_line}"
            if key in state.entries:
                state.entries[key].promoted_at = now_iso
        _save_dreaming_state(workspace_dir, state)

    return promoted


# ============================================================================
# Dream Diary
# ============================================================================

def generate_dream_diary(
    workspace_dir: str,
    promoted: list[tuple[ShortTermRecallEntry, float, str]],
    candidates: list[ShortTermRecallEntry],
    config: DreamingConfig,
    model: str,
    api_client: Any,
) -> None:
    """Generate a narrative Dream Diary entry and append to DREAMS.md."""
    today = date.today().isoformat()
    dreams_path = Path(workspace_dir) / "DREAMS.md"

    narrative: str | None = None

    if api_client is not None and promoted:
        promoted_summary = "\n".join(
            f"- [{e.path}:{e.start_line}] (score={score:.2f}, recalls={e.recall_count}): "
            f"{content[:100].replace(chr(10), ' ')}..."
            for e, score, content in promoted[:5]
        )
        prompt = (
            f"Write a 2-4 sentence Dream Diary entry for {today} summarizing today's memory "
            f"consolidation. These memories were promoted to long-term storage:\n\n"
            f"{promoted_summary}\n\n"
            f"Write in a reflective, first-person style. Be concise and specific. "
            f"Do not use markdown headers or bullet points."
        )
        diary_model = config.model or model

        try:
            from anthropic import Anthropic
            if isinstance(api_client, Anthropic):
                response = api_client.messages.create(
                    model=diary_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                narrative = response.content[0].text.strip()
        except Exception:
            pass

        if narrative is None:
            try:
                from openai import OpenAI
                if isinstance(api_client, OpenAI):
                    response = api_client.chat.completions.create(
                        model=diary_model,
                        max_tokens=200,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    narrative = response.choices[0].message.content.strip()
            except Exception:
                pass

    if narrative is None:
        narrative = f"Promoted {len(promoted)} memory entries to long-term storage."

    with dreams_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## Dream Diary {today}\n\n")
        f.write(narrative)
        f.write(f"\n\n_Candidates: {len(candidates)} | Promoted: {len(promoted)}_\n")


# ============================================================================
# Main entry point
# ============================================================================

# Serializes concurrent sweeps. Regular Lock is sufficient: callers use the
# blocking parameter on run_dreaming() rather than pre-acquiring here.
_sweep_lock = threading.Lock()


def run_dreaming(
    workspace_dir: str,
    config: DreamingConfig,
    model: str,
    api_client: Any = None,
    blocking: bool = True,
) -> DreamingResult | None:
    """Run a full dreaming sweep (light + deep phases + optional diary).

    blocking=True  (default, used by /dreaming run): waits for any in-progress
                   sweep to finish, then runs. Never skips.
    blocking=False (used by scheduler): skips immediately if a sweep is running,
                   returns None. Prevents scheduled runs from queuing behind a
                   manual /dreaming run.
    """
    import time

    acquired = _sweep_lock.acquire(blocking=blocking)
    if not acquired:
        return None  # another sweep is in progress; caller requested non-blocking
    try:
        start = time.monotonic()

        candidates = run_light_phase(workspace_dir)
        promoted = run_deep_phase(workspace_dir, config, candidates)

        if config.diary and api_client is not None:
            try:
                generate_dream_diary(workspace_dir, promoted, candidates, config, model, api_client)
            except Exception:
                pass

        update_last_run_at(workspace_dir)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return DreamingResult(candidates=candidates, promoted=promoted, elapsed_ms=elapsed_ms)
    finally:
        _sweep_lock.release()


# ============================================================================
# Status helpers
# ============================================================================

def get_dreaming_status(workspace_dir: str, config: DreamingConfig) -> dict[str, Any]:
    """Return a dict with current dreaming status for display."""
    state = load_dreaming_state(workspace_dir)
    workspace = Path(workspace_dir)

    active = [
        e for e in state.entries.values()
        if not e.promoted_at and e.recall_count >= 1 and (workspace / e.path).exists()
    ]
    active.sort(key=lambda e: e.recall_count, reverse=True)

    top_candidates = []
    if active:
        max_recall = max(e.recall_count for e in active)
        for e in active[:5]:
            score = _compute_score(e, max_recall)
            top_candidates.append({
                "path": e.path,
                "start_line": e.start_line,
                "recall_count": e.recall_count,
                "unique_queries": len(e.query_hashes),
                "score": score,
            })

    due = is_dreaming_due(config.frequency, state.last_run_at)

    return {
        "enabled": config.enabled,
        "frequency": config.frequency,
        "last_run_at": state.last_run_at,
        "total_tracked": len(state.entries),
        "active_candidates": len(active),
        "promoted_total": sum(1 for e in state.entries.values() if e.promoted_at),
        "due": due,
        "top_candidates": top_candidates,
    }
