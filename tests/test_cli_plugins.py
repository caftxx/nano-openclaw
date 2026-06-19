"""Tests for plugin and hook slash commands in CLI."""

from rich.console import Console

from nano_openclaw.cli import _list_hooks, _list_plugins
from nano_openclaw.config.types import NanoOpenClawConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.core.tools import build_core_registry


def test_list_plugins_no_plugins_loaded():
    console = Console()
    registry = build_core_registry()

    with console.capture() as capture:
        _list_plugins(console, registry)

    output = capture.get()
    assert "no plugins loaded" in output


def test_list_plugins_displays_loaded_plugins():
    console = Console()
    config = NanoOpenClawConfig()
    registry = build_core_registry()
    load_plugins(config.plugins, registry, config)

    with console.capture() as capture:
        _list_plugins(console, registry)

    output = capture.get()
    assert "Plugins" in output
    assert "memory" in output
    assert "web" in output
    assert "subagent" in output
    assert "mcp" in output
    assert "loaded" in output


def test_list_hooks_no_hooks_registered():
    console = Console()
    registry = build_core_registry()

    with console.capture() as capture:
        _list_hooks(console, registry)

    output = capture.get()
    assert "no hooks registered" in output


def test_list_hooks_displays_registered_hooks():
    console = Console()
    config = NanoOpenClawConfig()
    registry = build_core_registry()
    load_plugins(config.plugins, registry, config)

    with console.capture() as capture:
        _list_hooks(console, registry)

    output = capture.get()
    assert "Hooks" in output
    assert "before_prompt_build" in output
    assert "Memory" in output


