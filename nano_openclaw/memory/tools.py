"""Memory tools: memory_get and memory_search (lexical version).

Mirrors openclaw extensions/memory-core/src/tools.ts but without embedding provider.
Uses simple text matching for search instead of vector similarity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


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

    # Keywords from query
    keywords = re.findall(r"\w+", query.lower())
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

    # Format output
    if not results:
        return "Memory search: no matches found."

    output_lines = ["Memory search results:"]
    for r in results:
        output_lines.append(f"- {r.path}:{r.start_line}-{r.end_line} (score={r.score:.2f})")
        snippet_preview = r.snippet[:80] + "..." if len(r.snippet) > 80 else r.snippet
        output_lines.append(f"  {snippet_preview}")

    return "\n".join(output_lines)


def _search_file(
    file_path: Path,
    keywords: list[str],
    min_score: float,
    workspace: Path,
) -> list[MemorySearchResult]:
    """Search one file for keyword matches."""
    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")
    except (OSError, UnicodeDecodeError):
        return []

    results: list[MemorySearchResult] = []
    rel_path = str(file_path.relative_to(workspace))

    # Line-by-line search
    for i, line in enumerate(lines):
        line_lower = line.lower()
        matches = sum(1 for kw in keywords if kw in line_lower)
        if matches > 0:
            score = matches / len(keywords)
            if score >= min_score:
                results.append(MemorySearchResult(
                    path=rel_path,
                    snippet=line.strip(),
                    score=score,
                    start_line=i + 1,
                    end_line=i + 1,
                ))

    return results