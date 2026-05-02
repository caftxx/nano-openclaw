"""Skills cache - session-scoped caching for skill loading.

Mirrors openclaw's workspace.ts session-scoped cache behavior.

Skills are loaded once per session and cached to avoid repeated
file reads. Cache is invalidated when:
- Session changes (new session key)
- Skills watcher detects changes (future feature)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nano_openclaw.skills.loader import load_skill_entries
from nano_openclaw.skills.types import SkillEntry

logger = logging.getLogger(__name__)

# Global cache instance
_cache: SkillsCache | None = None


@dataclass
class SkillsCache:
    """Session-scoped cache for loaded skills."""
    session_key: str = ""
    workspace_dir: Path | None = None
    entries: list[SkillEntry] = field(default_factory=list)
    loaded: bool = False

    def get_entries(
        self,
        workspace_dir: Path,
        session_key: str,
        extra_dirs: list[str] | None = None,
        max_bytes: int = 256_000,
    ) -> list[SkillEntry]:
        """Get cached skill entries, loading if needed.

        Args:
            workspace_dir: Workspace directory path
            session_key: Session identifier for cache key
            extra_dirs: Extra skill directories from config
            max_bytes: Max file size to load

        Returns:
            List of SkillEntry objects
        """
        # Check if cache is valid for this session/workspace
        if (
            self.loaded
            and self.session_key == session_key
            and self.workspace_dir == workspace_dir
        ):
            logger.debug("Using cached skills for session %s", session_key)
            return self.entries

        # Load fresh
        logger.debug("Loading skills for session %s", session_key)
        self.session_key = session_key
        self.workspace_dir = workspace_dir
        self.entries = load_skill_entries(
            workspace_dir,
            extra_dirs=extra_dirs,
            max_bytes=max_bytes,
        )
        self.loaded = True

        logger.info("Loaded %d skills for session %s", len(self.entries), session_key)
        return self.entries

    def invalidate(self) -> None:
        """Invalidate cache, forcing reload on next access."""
        self.loaded = False
        self.entries = []
        logger.debug("Skills cache invalidated")

    def clear(self) -> None:
        """Clear cache completely."""
        self.session_key = ""
        self.workspace_dir = None
        self.entries = []
        self.loaded = False
        logger.debug("Skills cache cleared")


def get_skills_cache() -> SkillsCache:
    """Get global skills cache instance."""
    global _cache
    if _cache is None:
        _cache = SkillsCache()
    return _cache


def get_or_load_skills(
    workspace_dir: Path,
    session_key: str,
    extra_dirs: list[str] | None = None,
    max_bytes: int = 256_000,
) -> list[SkillEntry]:
    """Get cached or load fresh skill entries.

    Convenience wrapper around global cache.
    """
    cache = get_skills_cache()
    return cache.get_entries(
        workspace_dir,
        session_key,
        extra_dirs=extra_dirs,
        max_bytes=max_bytes,
    )


def invalidate_skills_cache() -> None:
    """Invalidate global skills cache."""
    cache = get_skills_cache()
    cache.invalidate()


def clear_skills_cache() -> None:
    """Clear global skills cache."""
    global _cache
    if _cache is not None:
        _cache.clear()
    _cache = None