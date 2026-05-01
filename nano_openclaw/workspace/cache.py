"""Session-scoped cache for workspace bootstrap files.

Mirrors openclaw bootstrap-cache.ts: caches loaded files per session
key to avoid redundant filesystem I/O.

Usage:
    files = get_or_load_bootstrap_files(workspace_dir, session_key)

Cache is cleared when:
    - clear_session_cache(session_key) is called
    - clear_all_cache() is called
    - Process exits
"""

from __future__ import annotations

from pathlib import Path

from nano_openclaw.workspace.loader import (
    WorkspaceBootstrapFile,
    build_bootstrap_context,
    load_workspace_bootstrap_files,
)

_cache: dict[str, list[WorkspaceBootstrapFile]] = {}


def get_or_load_bootstrap_files(
    workspace_dir: Path,
    session_key: str,
    max_chars: int = 12_000,
    total_max_chars: int = 60_000,
) -> list[WorkspaceBootstrapFile]:
    """Get cached or freshly loaded bootstrap files for a session.

    Mirrors openclaw getOrLoadBootstrapFiles().

    Args:
        workspace_dir: Path to the workspace directory
        session_key: Unique session identifier for caching
        max_chars: Per-file character budget (passed to truncation)
        total_max_chars: Total character budget (passed to truncation)

    Returns:
        List of loaded and truncated bootstrap files
    """
    # Return cached snapshot if available
    if session_key in _cache:
        return _cache[session_key]

    # Load fresh from filesystem
    files = load_workspace_bootstrap_files(workspace_dir)

    # Apply budget truncation
    files = build_bootstrap_context(files, max_chars, total_max_chars)

    # Cache the result
    _cache[session_key] = files

    return files


def clear_session_cache(session_key: str) -> None:
    """Clear cached bootstrap files for one session.

    Call this when:
      - Session is reset (daily/idle reset)
      - User explicitly requests a fresh session
      - Workspace files are known to have changed
    """
    _cache.pop(session_key, None)


def clear_all_cache() -> None:
    """Clear all cached bootstrap files."""
    _cache.clear()


def is_session_cached(session_key: str) -> bool:
    """Check if a session has cached bootstrap files."""
    return session_key in _cache
