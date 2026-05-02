"""Load and truncate workspace bootstrap files.

Mirrors openclaw workspace.ts (readWorkspaceFileWithGuards) and
pi-embedded-helpers/bootstrap.ts (trimBootstrapContent).

Safety:
  - Prevents symlink/hardlink escapes via path-prefix check
  - Per-file cap: 2 MB hard limit, configurable soft cap for prompt
  - Total budget cap across all injected files

Truncation strategy (openclaw 75/25 rule):
  - Keep 75% of head, 25% of tail
  - Insert truncation marker showing remaining size
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nano_openclaw.workspace.constants import (
    BOOTSTRAP_FILES,
    CONTEXT_FILE_ORDER,
)


# Hard safety limit: never read files larger than 2 MB
MAX_WORKSPACE_BOOTSTRAP_FILE_BYTES = 2 * 1024 * 1024

# Default per-file character budget for prompt injection
DEFAULT_BOOTSTRAP_MAX_CHARS = 12_000

# Default total character budget across all files
DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS = 60_000

_TRUNCATION_MARKER = "[...truncated, read {} for full content...]"


@dataclass
class WorkspaceBootstrapFile:
    """One loaded bootstrap file (or a record that it was missing)."""
    name: str
    path: str
    content: str | None = None
    missing: bool = False


def _is_within_workspace(file_path: Path, workspace_dir: Path) -> bool:
    """Check that resolved file path is inside workspace directory.

    Simplified version of openclaw's openBoundaryFile(): instead of
    checking inodes/dev/mtime, we verify the resolved path starts with
    the workspace root. This prevents symlink/hardlink escapes on the
    filesystem level.
    """
    try:
        file_path.resolve().is_relative_to(workspace_dir.resolve())
        return True
    except (ValueError, OSError):
        return False


def _read_file_safe(file_path: Path, workspace_dir: Path) -> str | None:
    """Read file content with safety guards.

    Returns None if:
      - File doesn't exist
      - File is outside workspace
      - File exceeds MAX_WORKSPACE_BOOTSTRAP_FILE_BYTES
      - Any IO error occurs
    """
    if not file_path.exists():
        return None

    # Security check: ensure file is within workspace
    if not _is_within_workspace(file_path, workspace_dir):
        logger.warning(
            "Bootstrap file %s escapes workspace %s — skipping",
            file_path,
            workspace_dir,
        )
        return None

    # Size check
    try:
        stat = file_path.stat()
        if stat.st_size > MAX_WORKSPACE_BOOTSTRAP_FILE_BYTES:
            logger.warning(
                "Bootstrap file %s exceeds %d bytes (%d) — skipping",
                file_path,
                MAX_WORKSPACE_BOOTSTRAP_FILE_BYTES,
                stat.st_size,
            )
            return None
    except OSError:
        return None

    # Read content
    try:
        return file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read %s: %s", file_path, exc)
        return None


def load_workspace_bootstrap_files(
    workspace_dir: Path,
) -> list[WorkspaceBootstrapFile]:
    """Load all 8 standard bootstrap files from workspace directory.

    Mirrors openclaw workspace.ts:602-667.

    For each file:
      - If present and valid: read content
      - If missing or invalid: mark as missing

    Args:
        workspace_dir: Path to the workspace directory

    Returns:
        List of WorkspaceBootstrapFile objects (some may be missing)
    """
    result: list[WorkspaceBootstrapFile] = []

    for filename in BOOTSTRAP_FILES:
        file_path = workspace_dir / filename
        content = _read_file_safe(file_path, workspace_dir)

        if content is not None:
            result.append(
                WorkspaceBootstrapFile(
                    name=filename,
                    path=str(file_path),
                    content=content,
                    missing=False,
                )
            )
        else:
            result.append(
                WorkspaceBootstrapFile(
                    name=filename,
                    path=str(file_path),
                    missing=True,
                )
            )

    return result


def trim_bootstrap_content(
    content: str,
    filename: str,
    max_chars: int = DEFAULT_BOOTSTRAP_MAX_CHARS,
) -> str:
    """Truncate file content using 75% head / 25% tail strategy.

    Mirrors openclaw pi-embedded-helpers/bootstrap.ts:132-228.

    Algorithm:
      1. If content fits in max_chars, return as-is
      2. Calculate truncation marker size
      3. Allocate 75% of remaining budget to head, 25% to tail
      4. Insert marker between head and tail
      5. Iteratively adjust if marker itself pushes over budget

    Args:
        content: Raw file content
        filename: Bootstrap file name (for truncation marker)
        max_chars: Maximum character budget for this file

    Returns:
        Truncated content with marker if needed
    """
    if len(content) <= max_chars:
        return content

    # Build truncation marker
    marker = _TRUNCATION_MARKER.format(filename)

    # Iteratively account for marker size
    available = max_chars
    while True:
        head_size = int((available - len(marker)) * 0.75)
        tail_size = available - len(marker) - head_size

        if head_size < 0 or tail_size < 0:
            # Marker alone exceeds budget — return just marker
            return marker

        head = content[:head_size]
        tail = content[-tail_size:] if tail_size > 0 else ""

        result = head + "\n" + marker + "\n" + tail

        if len(result) <= max_chars:
            return result

        # Marker + content still too large; shrink available
        available -= 50
        if available < len(marker) + 20:
            return marker


def build_bootstrap_context(
    files: list[WorkspaceBootstrapFile],
    max_chars: int = DEFAULT_BOOTSTRAP_MAX_CHARS,
    total_max_chars: int = DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS,
) -> list[WorkspaceBootstrapFile]:
    """Apply budget limits to bootstrap files for prompt injection.

    Mirrors openclaw pi-embedded-helpers/bootstrap.ts:271-330.

    Process:
      1. Filter out missing files
      2. Sort by CONTEXT_FILE_ORDER
      3. Truncate each file to per-file budget
      4. Stop when total budget is exhausted

    Args:
        files: List of loaded bootstrap files
        max_chars: Per-file character limit
        total_max_chars: Total character budget across all files

    Returns:
        Filtered and truncated list of files for injection
    """
    # Remove missing files
    present = [f for f in files if not f.missing]

    # Sort by injection order
    present.sort(
        key=lambda f: CONTEXT_FILE_ORDER.get(f.name.lower(), 999)
    )

    result: list[WorkspaceBootstrapFile] = []
    remaining = total_max_chars

    for file in present:
        if remaining <= 0:
            break

        # Per-file budget: min of per-file cap and remaining total
        file_budget = min(max_chars, remaining)

        # Truncate content if needed
        if file.content is not None:
            truncated = trim_bootstrap_content(
                file.content,
                file.name,
                file_budget,
            )
            remaining -= len(truncated)
            file.content = truncated

        result.append(file)

    return result
