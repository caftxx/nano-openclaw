"""Memory tools: memory_get and memory_search (lexical version).

Mirrors openclaw extensions/memory-core/src/tools.ts but without embedding provider.
Uses context-window search (like ripgrep -C) instead of single-line matching.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


@dataclass
class MemorySearchResult:
    """One search result from memory_search."""
    path: str
    snippet: str
    score: float
    start_line: int
    end_line: int


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


def memory_search(args: dict[str, Any], workspace_dir: str | None = None) -> str:
    """Lexical search across memory files.

    Mirrors openclaw memory_search but uses keyword matching instead of embedding.

    Searches MEMORY.md and memory/*.md for keyword matches.
    No embedding - uses simple text/keyword matching.

    Args:
        query: Search query (keywords)
        maxResults: Max results (default 10)
        minScore: Minimum match score (default 0.1)

    Returns:
        Search results with file paths, snippets, and line numbers
    """
    query = args.get("query", "")
    max_results = int(args.get("maxResults", 10))
    min_score = float(args.get("minScore", 0.1))

    if not workspace_dir:
        return '{"results": [], "error": "no workspace directory"}'

    results: list[MemorySearchResult] = []
    workspace = Path(workspace_dir)

    # Keywords from query — filter stopwords and single-char noise
    raw_keywords = re.findall(r"\w+", query.lower())
    keywords = [kw for kw in raw_keywords if kw not in _STOPWORDS and len(kw) > 1]
    if not keywords:
        keywords = raw_keywords  # fallback: avoid empty results for all-stopword queries
    if not keywords:
        return '{"results": []}'

    # Search MEMORY.md
    memory_md = workspace / "MEMORY.md"
    if memory_md.exists():
        results.extend(_search_file(memory_md, keywords, min_score, workspace))

    # Search memory/*.md
    memory_dir = workspace / "memory"
    if memory_dir.exists():
        for entry in sorted(memory_dir.glob("*.md")):
            results.extend(_search_file(entry, keywords, min_score, workspace))

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    results = results[:max_results]

    # Track recall events for dreaming (always, regardless of dreaming.enabled)
    if results and workspace_dir:
        _track_results(results, query, workspace_dir)

    # Format output
    if not results:
        return "Memory search: no matches found."

    output_lines = ["Memory search results:"]
    for r in results:
        output_lines.append(f"- {r.path}:{r.start_line}-{r.end_line} (score={r.score:.2f})")
        snippet_preview = r.snippet[:80] + "..." if len(r.snippet) > 80 else r.snippet
        output_lines.append(f"  {snippet_preview}")

    return "\n".join(output_lines)


def _track_results(results: list[MemorySearchResult], query: str, workspace_dir: str) -> None:
    """Record search hits to the dreaming short-term recall store."""
    try:
        from nano_openclaw.memory.dreaming import track_recall
        for r in results:
            track_recall(r.path, r.start_line, r.end_line, r.snippet, query, workspace_dir)
    except Exception:
        pass  # Never block the search result on tracking failure


def _search_file(
    file_path: Path,
    keywords: list[str],
    min_score: float,
    workspace: Path,
    context_lines: int = 2,
) -> list[MemorySearchResult]:
    """Search one file using context-window matching (like ripgrep -C)."""
    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")
    except (OSError, UnicodeDecodeError):
        return []

    if not lines:
        return []

    rel_path = str(file_path.relative_to(workspace))

    # Precompile whole-word patterns; fall back to substring for CJK (no \b boundary)
    kw_patterns = {kw: re.compile(r"\b" + re.escape(kw) + r"\b") for kw in keywords}

    # ── Phase 1: hit detection ──────────────────────────────────────────────
    hit_lines: dict[int, set[str]] = {}
    for i, line in enumerate(lines):
        line_lower = line.lower()
        hit_kws: set[str] = set()
        for kw, pattern in kw_patterns.items():
            if pattern.search(line_lower):
                hit_kws.add(kw)
            elif kw in line_lower:
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
                start_line=win_start + 1,
                end_line=win_end + 1,
            ))

    return results