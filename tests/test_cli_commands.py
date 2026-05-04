from nano_openclaw.cli import _commands_help
from nano_openclaw.config.types import NanoOpenClawConfig, PluginsConfig
from nano_openclaw.loop import LoopConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.tools import build_core_registry


def test_commands_help_shows_builtin_plugin_commands_by_default():
    registry = build_core_registry()
    load_plugins(NanoOpenClawConfig().plugins, registry, NanoOpenClawConfig())

    help_text = _commands_help(registry, LoopConfig())

    assert "/subagents" in help_text
    assert "/active-memory" in help_text
    assert "/dreaming" in help_text


def test_commands_help_shows_builtin_plugin_commands_with_explicit_empty_load():
    registry = build_core_registry()
    load_plugins(PluginsConfig(load=[]), registry, NanoOpenClawConfig())

    help_text = _commands_help(registry, LoopConfig())

    assert "/subagents" in help_text
    assert "/active-memory" in help_text
    assert "/dreaming" in help_text


def test_commands_help_shows_memory_commands_when_memory_plugin_loaded():
    registry = build_core_registry()
    load_plugins(PluginsConfig(load=["memory"]), registry, NanoOpenClawConfig())

    help_text = _commands_help(registry, LoopConfig())

    assert "/active-memory" in help_text
    assert "/dreaming" in help_text
    assert "/subagents" in help_text


def test_commands_help_shows_subagents_when_subagent_plugin_loaded():
    registry = build_core_registry()
    load_plugins(PluginsConfig(load=["subagent"]), registry, NanoOpenClawConfig())

    help_text = _commands_help(registry, LoopConfig())

    assert "/subagents" in help_text
    assert "/active-memory" in help_text
    assert "/dreaming" in help_text
