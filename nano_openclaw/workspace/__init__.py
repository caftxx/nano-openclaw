"""Workspace bootstrap file management for nano-openclaw.

Mirrors openclaw's workspace.ts: loads AGENTS.md, SOUL.md, and other
context files from the workspace directory, applies safety guards,
budget-based truncation, and session-scoped caching.

Public API:
    WorkspaceBootstrapFile  — dataclass representing one loaded file
    load_workspace_bootstrap_files()  — read all 8 standard files
    build_bootstrap_context()  — apply per-file and total budget limits
    get_or_load_bootstrap_files() — session-scoped caching
"""

from __future__ import annotations

from nano_openclaw.workspace.constants import (
    BOOTSTRAP_FILES,
    CONTEXT_FILE_ORDER,
    DEFAULT_AGENTS_FILENAME,
    DEFAULT_BOOTSTRAP_FILENAME,
    DEFAULT_HEARTBEAT_FILENAME,
    DEFAULT_IDENTITY_FILENAME,
    DEFAULT_MEMORY_FILENAME,
    DEFAULT_SOUL_FILENAME,
    DEFAULT_TOOLS_FILENAME,
    DEFAULT_USER_FILENAME,
    MINIMAL_BOOTSTRAP_ALLOWLIST,
)
from nano_openclaw.workspace.loader import (
    WorkspaceBootstrapFile,
    build_bootstrap_context,
    load_workspace_bootstrap_files,
    trim_bootstrap_content,
)
from nano_openclaw.workspace.cache import (
    clear_all_cache,
    clear_session_cache,
    get_or_load_bootstrap_files,
    is_session_cached,
)

__all__ = [
    # constants
    "BOOTSTRAP_FILES",
    "CONTEXT_FILE_ORDER",
    "DEFAULT_AGENTS_FILENAME",
    "DEFAULT_BOOTSTRAP_FILENAME",
    "DEFAULT_HEARTBEAT_FILENAME",
    "DEFAULT_IDENTITY_FILENAME",
    "DEFAULT_MEMORY_FILENAME",
    "DEFAULT_SOUL_FILENAME",
    "DEFAULT_TOOLS_FILENAME",
    "DEFAULT_USER_FILENAME",
    "MINIMAL_BOOTSTRAP_ALLOWLIST",
    # loader
    "WorkspaceBootstrapFile",
    "load_workspace_bootstrap_files",
    "build_bootstrap_context",
    "trim_bootstrap_content",
    # cache
    "get_or_load_bootstrap_files",
    "clear_session_cache",
    "clear_all_cache",
    "is_session_cached",
]
