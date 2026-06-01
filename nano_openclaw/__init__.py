"""nano-openclaw package."""

from __future__ import annotations


def resolve_version() -> str:
    """Read the installed package version from metadata (single source of
    truth in pyproject.toml). Falls back to ``unknown`` when running from an
    uninstalled checkout without metadata."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("nano-openclaw")
    except PackageNotFoundError:
        return "unknown"
