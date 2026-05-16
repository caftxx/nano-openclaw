"""Memory tools: memory_get and memory_search (lexical version).

Mirrors openclaw extensions/memory-core/src/tools.ts but without embedding provider.
Uses context-window search (like ripgrep -C) instead of single-line matching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import math
import re
from typing import Any

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "by", "is", "are", "was", "were", "be",
    "been", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "i", "me", "my",
    "we", "our", "you", "your", "it", "its", "this", "that",
    "he", "she", "they", "them", "his", "her", "their",
    "的", "了", "是", "在", "我", "你", "他", "她", "们",
    "和", "或", "但", "而", "就", "都", "也", "很", "这",
    "那", "有", "没", "与", "及", "为", "从", "到", "把",
})

_DATED_MEMORY_PATH_RE = re.compile(r"^memory/(\d{4})-(\d{2})-(\d{2})\.md$")


@dataclass
class MemorySearchResult:
    """One search result from memory_search."""
    path: str
    snippet: str
    score: float
    start_line: int
    end_line: int
    raw_score: float | None = None

    def __post_init__(self) -> None:
        if self.raw_score is None:
            self.raw_score = self.score


def memory_get(args: dict[str, Any], workspace_dir: str | None = None) -> str:
    """Read a specific memory file or excerpt.

    Mirrors openclaw memory_get tool.

    Args:
        path: File path relative to workspace (e.g., "MEMORY.md" or "memory/2026-05-02.md")
        from: Optional starting line number (1-indexed)
        lines: Optional number of lines to read

    Returns:
        File content or excerpt with line markers
    """
    rel_path = args.get("path", "")
    from_line = args.get("from")
    num_lines = args.get("lines")

    if not workspace_dir:
        return "[error: no workspace directory]"

    file_path = Path(workspace_dir) / rel_path
    if not file_path.exists():
        return f"[file not found: {rel_path}]"

    # Security: ensure path stays within workspace
    try:
        file_path.resolve().is_relative_to(Path(workspace_dir).resolve())
    except (ValueError, OSError):
        return f"[path escapes workspace: {rel_path}]"

    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        if from_line is not None:
            start = max(0, int(from_line) - 1)
            if num_lines is not None:
                end = min(len(lines), start + int(num_lines))
            else:
                end = len(lines)
            excerpt = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))
            return f"[{rel_path} lines {start+1}-{end}]\n{excerpt}"

        return f"[{rel_path}]\n{content}"
    except Exception as e:
        return f"[error reading {rel_path}: {e}]"


_VALID_OUTPUT_MODES = ("snippet", "paths_only", "count")
_CONTEXT_LINES_MIN = 0
_CONTEXT_LINES_MAX = 20
_CONTEXT_LINES_DEFAULT = 2


def memory_search(
    args: dict[str, Any],
    workspace_dir: str | None = None,
    config: Any | None = None,
    now: datetime | None = None,
) -> str:
    """Lexical search across memory files.

    Mirrors openclaw memory_search but uses keyword matching instead of embedding.

    Searches MEMORY.md and memory/*.md for keyword matches.
    No embedding - uses simple text/keyword matching.

    Args:
        query: Search query (keywords)
        maxResults: Max results (default 10)
        minScore: Minimum match score (default 0.1)
        contextLines: Lines of context around each hit (default 2, clamped to 0..20)
        caseSensitive: If true, match case-sensitively (default false)
        outputMode: "snippet" (default) | "paths_only" | "count"

    Returns:
        Search results with file paths, snippets, and line numbers
    """
    query = args.get("query", "")
    max_results = int(args.get("maxResults", 10))
    min_score = float(args.get("minScore", 0.1))

    # Knob: context_lines (clamped, never errors)
    try:
        context_lines = int(args.get("contextLines", _CONTEXT_LINES_DEFAULT))
    except (TypeError, ValueError):
        context_lines = _CONTEXT_LINES_DEFAULT
    context_lines = max(_CONTEXT_LINES_MIN, min(_CONTEXT_LINES_MAX, context_lines))

    # Knob: case_sensitive
    case_sensitive = bool(args.get("caseSensitive", False))

    # Knob: output_mode (invalid value silently degrades to "snippet")
    output_mode = args.get("outputMode", "snippet")
    if output_mode not in _VALID_OUTPUT_MODES:
        output_mode = "snippet"

    if not workspace_dir:
        return '{"results": [], "error": "no workspace directory"}'

    results: list[MemorySearchResult] = []
    workspace = Path(workspace_dir)

    # Keywords from query — filter stopwords and single-char noise.
    # Stopword filtering keys off the lowercased form regardless of case_sensitive,
    # so the stopword list stays effective across both modes (scoring unchanged).
    raw_keywords_match = re.findall(r"\w+", query if case_sensitive else query.lower())
    raw_keywords_for_stopword = re.findall(r"\w+", query.lower())

    keywords: list[str] = []
    for kw_match, kw_lower in zip(raw_keywords_match, raw_keywords_for_stopword):
        if kw_lower in _STOPWORDS or len(kw_lower) <= 1:
            continue
        keywords.append(kw_match)
    if not keywords:
        keywords = raw_keywords_match  # fallback: avoid empty results for all-stopword queries
    if not keywords:
        return '{"results": []}'

    # Search MEMORY.md
    memory_md = workspace / "MEMORY.md"
    if memory_md.exists():
        results.extend(_search_file(
            memory_md, keywords, min_score, workspace,
            context_lines=context_lines, case_sensitive=case_sensitive,
        ))

    # Search memory/*.md
    memory_dir = workspace / "memory"
    if memory_dir.exists():
        for entry in sorted(memory_dir.glob("*.md")):
            results.extend(_search_file(
                entry, keywords, min_score, workspace,
                context_lines=context_lines, case_sensitive=case_sensitive,
            ))

    results = _apply_temporal_decay(results, config, now=now)

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    results = results[:max_results]

    # Track recall events for dreaming (always, regardless of dreaming.enabled)
    if results and workspace_dir:
        _track_results(results, query, workspace_dir)

    # Format output
    if not results:
        if output_mode == "count":
            return "Memory search: 0 files, 0 hits."
        return "Memory search: no matches found."

    if output_mode == "count":
        file_count = len({r.path for r in results})
        hit_count = len(results)
        return f"Memory search: {file_count} files, {hit_count} hits."

    if output_mode == "paths_only":
        output_lines = ["Memory search results:"]
        for r in results:
            output_lines.append(f"- {r.path} (score={r.score:.2f})")
        return "\n".join(output_lines)

    # Default: snippet
    output_lines = ["Memory search results:"]
    for r in results:
        output_lines.append(f"- {r.path}:{r.start_line}-{r.end_line} (score={r.score:.2f})")
        snippet_preview = r.snippet[:80] + "..." if len(r.snippet) > 80 else r.snippet
        output_lines.append(f"  {snippet_preview}")

    return "\n".join(output_lines)


def _track_results(results: list[MemorySearchResult], query: str, workspace_dir: str) -> None:
    """Record search hits to the dreaming short-term recall store."""
    try:
        from nano_openclaw.memory.dreaming import is_short_term_memory_path, track_recall
        for r in results:
            if not is_short_term_memory_path(r.path):
                continue
            track_recall(r.path, r.start_line, r.end_line, r.snippet, query, workspace_dir)
    except Exception:
        pass  # Never block the search result on tracking failure


def _search_file(
    file_path: Path,
    keywords: list[str],
    min_score: float,
    workspace: Path,
    context_lines: int = 2,
    case_sensitive: bool = False,
) -> list[MemorySearchResult]:
    """Search one file using context-window matching (like ripgrep -C)."""
    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")
    except (OSError, UnicodeDecodeError):
        return []

    if not lines:
        return []

    rel_path = file_path.relative_to(workspace).as_posix()

    # Precompile whole-word patterns; fall back to substring for CJK (no \b boundary).
    # case_sensitive=False (default) keeps prior behaviour via re.IGNORECASE + lowercase line.
    pattern_flags = 0 if case_sensitive else re.IGNORECASE
    kw_patterns = {
        kw: re.compile(r"\b" + re.escape(kw) + r"\b", pattern_flags)
        for kw in keywords
    }

    # ── Phase 1: hit detection ──────────────────────────────────────────────
    hit_lines: dict[int, set[str]] = {}
    for i, line in enumerate(lines):
        match_line = line if case_sensitive else line.lower()
        hit_kws: set[str] = set()
        for kw, pattern in kw_patterns.items():
            if pattern.search(match_line):
                hit_kws.add(kw)
            elif kw in match_line:
                hit_kws.add(kw)
        if hit_kws:
            hit_lines[i] = hit_kws

    if not hit_lines:
        return []

    # ── Phase 2: build windows and merge adjacent/overlapping ones ──────────
    raw_windows = [
        (max(0, i - context_lines), min(len(lines) - 1, i + context_lines))
        for i in sorted(hit_lines)
    ]

    merged: list[tuple[int, int]] = []
    cur_start, cur_end = raw_windows[0]
    for ws, we in raw_windows[1:]:
        if ws <= cur_end + 1:
            cur_end = max(cur_end, we)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = ws, we
    merged.append((cur_start, cur_end))

    # ── Phase 3: score each merged window ───────────────────────────────────
    results: list[MemorySearchResult] = []
    for win_start, win_end in merged:
        window_kws: set[str] = set()
        for i in range(win_start, win_end + 1):
            if i in hit_lines:
                window_kws |= hit_lines[i]
        coverage = len(window_kws) / len(keywords)

        heading_boost = 0.0
        for i in range(win_start, win_end + 1):
            if i in hit_lines and lines[i].lstrip().startswith("#"):
                heading_boost = 1.0
                break

        core_hits = [i for i in range(win_start, win_end + 1) if i in hit_lines]
        total_words = sum(len(re.findall(r"\w+", lines[i])) for i in core_hits)
        total_kw_hits = sum(len(hit_lines[i]) for i in core_hits)
        density = min(total_kw_hits / max(total_words, 1), 1.0)

        score = 0.60 * coverage + 0.25 * min(density * 3, 1.0) + 0.15 * heading_boost

        if score >= min_score:
            snippet = "\n".join(lines[win_start: win_end + 1])
            results.append(MemorySearchResult(
                path=rel_path,
                snippet=snippet,
                score=round(score, 4),
                raw_score=round(score, 4),
                start_line=win_start + 1,
                end_line=win_end + 1,
            ))

    return results


def _apply_temporal_decay(
    results: list[MemorySearchResult],
    config: Any | None,
    now: datetime | None = None,
) -> list[MemorySearchResult]:
    """Apply optional temporal decay to dated daily memory results."""
    enabled, half_life_days = _resolve_temporal_decay_config(config)
    if not enabled:
        return results

    current = now or datetime.now(timezone.utc)
    current_utc = _as_utc(current)

    decayed: list[MemorySearchResult] = []
    for result in results:
        memory_date = _parse_dated_memory_path(result.path)
        if memory_date is None:
            decayed.append(result)
            continue

        age_days = max(0.0, (current_utc - memory_date).total_seconds() / 86400)
        decayed_score = result.score * _temporal_decay_multiplier(age_days, half_life_days)
        decayed.append(MemorySearchResult(
            path=result.path,
            snippet=result.snippet,
            score=round(decayed_score, 4),
            raw_score=result.raw_score,
            start_line=result.start_line,
            end_line=result.end_line,
        ))

    return decayed


def _resolve_temporal_decay_config(config: Any | None) -> tuple[bool, float]:
    temporal_decay: Any | None = None
    if isinstance(config, dict):
        temporal_decay = config.get("temporalDecay")
    elif config is not None:
        temporal_decay = getattr(config, "temporalDecay", None)

    if temporal_decay is None:
        return False, 30.0

    if isinstance(temporal_decay, dict):
        enabled = bool(temporal_decay.get("enabled", False))
        half_life_days = temporal_decay.get("halfLifeDays", 30)
    else:
        enabled = bool(getattr(temporal_decay, "enabled", False))
        half_life_days = getattr(temporal_decay, "halfLifeDays", 30)

    try:
        half_life_float = float(half_life_days)
    except (TypeError, ValueError):
        half_life_float = 30.0

    return enabled, half_life_float


def _temporal_decay_multiplier(age_days: float, half_life_days: float) -> float:
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        return 1.0
    clamped_age = max(0.0, age_days)
    if not math.isfinite(clamped_age):
        return 1.0
    decay_lambda = math.log(2) / half_life_days
    return math.exp(-decay_lambda * clamped_age)


def _parse_dated_memory_path(path: str) -> datetime | None:
    normalized = path.replace("\\", "/").removeprefix("./")
    match = _DATED_MEMORY_PATH_RE.match(normalized)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
