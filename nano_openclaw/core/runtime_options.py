"""Shared runtime option helpers for model and thinking selection."""

from __future__ import annotations

from typing import Any


# Mirrors ``loop.ThinkingLevel`` Literal as a runtime-iterable set.
THINKING_LEVELS: tuple[str, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max",
)


def resolve_model_option(config: Any, query: str) -> dict[str, Any]:
    """Resolve a user query into a single configured model."""
    query = (query or "").strip()
    if not query:
        raise KeyError("empty model query")

    providers = getattr(getattr(config, "models", None), "providers", None) or {}
    candidates: list[dict[str, Any]] = []
    for provider_id, provider in providers.items():
        for model in getattr(provider, "models", []) or []:
            ref = f"{provider_id}/{model.id}"
            candidates.append({
                "ref": ref,
                "id": model.id,
                "provider": provider_id,
                "name": model.name or model.id,
            })

    for candidate in candidates:
        if candidate["ref"] == query:
            return candidate

    if "/" in query:
        raise KeyError(f"unknown model: {query}")

    matches = [candidate for candidate in candidates if candidate["id"] == query]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        refs = ", ".join(match["ref"] for match in matches)
        raise ValueError(f"ambiguous: {query} - try one of {refs}")
    raise KeyError(f"unknown model: {query}")
