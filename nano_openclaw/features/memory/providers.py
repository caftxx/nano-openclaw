"""Provider abstraction for memory_search.

The default provider stays local and lexical, but the manager gives memory
search a stable seam for semantic/vector backends without changing the tool
entry point.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MemorySearchRequest:
    """Normalized memory_search arguments passed to providers."""

    query: str
    max_results: int = 10
    min_score: float = 0.1
    context_lines: int = 2
    case_sensitive: bool = False
    output_mode: str = "snippet"


@dataclass
class ProviderSearchResult:
    """One normalized hit returned by a MemorySearchProvider."""

    path: str
    snippet: str
    score: float
    start_line: int
    end_line: int
    raw_score: float | None = None
    provider: str = ""

    def __post_init__(self) -> None:
        if self.raw_score is None:
            self.raw_score = self.score


class MemorySearchProvider(ABC):
    """Base class for pluggable memory_search backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider id used by memorySearch.provider."""

    def is_available(self) -> bool:
        """Return whether this provider can run in the current environment."""
        return True

    @abstractmethod
    def search(
        self,
        request: MemorySearchRequest,
        *,
        workspace_dir: str,
        config: Any | None = None,
        now: datetime | None = None,
    ) -> list[ProviderSearchResult]:
        """Return ranked search hits for a normalized request."""


class MemorySearchManager:
    """Routes memory_search requests to the configured provider."""

    def __init__(self) -> None:
        self._providers: dict[str, MemorySearchProvider] = {}
        self._fallback_name = "lexical"

    def register(self, provider: MemorySearchProvider) -> None:
        self._providers[provider.name] = provider

    def names(self) -> list[str]:
        return list(self._providers)

    def get(self, name: str) -> MemorySearchProvider | None:
        return self._providers.get(name)

    def search(
        self,
        request: MemorySearchRequest,
        *,
        workspace_dir: str,
        config: Any | None = None,
        now: datetime | None = None,
    ) -> list[ProviderSearchResult]:
        provider = self._select_provider(config)
        results = provider.search(
            request,
            workspace_dir=workspace_dir,
            config=config,
            now=now,
        )
        for result in results:
            if not result.provider:
                result.provider = provider.name
        return results

    def _select_provider(self, config: Any | None) -> MemorySearchProvider:
        requested = _config_value(config, "provider") or self._fallback_name
        provider = self._providers.get(str(requested))
        if provider and provider.is_available():
            return provider
        fallback = self._providers.get(self._fallback_name)
        if fallback is None:
            raise RuntimeError("no memory search provider registered")
        return fallback


def _config_value(config: Any | None, name: str) -> Any | None:
    if isinstance(config, dict):
        return config.get(name)
    if config is not None:
        return getattr(config, name, None)
    return None
