"""Plugin loader for nano-openclaw."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

from nano_openclaw.config.types import NanoOpenClawConfig, PluginEntryConfig, PluginsConfig
from nano_openclaw.plugins.registry import HookRegistry, LoadedHook, LoadedPlugin
from nano_openclaw.plugins.types import PluginApi
from nano_openclaw.tools import ToolRegistry

BUILTIN_PLUGINS = {
    "memory": "nano_openclaw.plugins.builtin.memory_plugin.MemoryPlugin",
    "web": "nano_openclaw.plugins.builtin.web_plugin.WebPlugin",
    "mcp": "nano_openclaw.plugins.builtin.mcp_plugin.McpPlugin",
    "subagent": "nano_openclaw.plugins.builtin.subagent_plugin.SubagentPlugin",
    "schedule": "nano_openclaw.plugins.builtin.schedule_plugin.SchedulePlugin",
}


def load_plugins(
    config: PluginsConfig,
    tool_registry: ToolRegistry,
    nano_config: NanoOpenClawConfig,
    base_dir: Path | None = None,
) -> HookRegistry:
    """Load configured plugins and return their hook registry."""
    hook_registry = HookRegistry()
    tool_registry.set_hook_registry(hook_registry)

    if not config.enabled:
        return hook_registry

    for entry in config.load:
        plugin = _resolve_plugin(entry, base_dir=base_dir)
        before_tools = set(tool_registry.names())
        before_hooks = hook_registry.handler_counts()
        before_hook_ids = {
            event: {id(handler) for _priority, handler in handlers}
            for event, handlers in hook_registry._handlers.items()
        }
        plugin_config = entry.config if isinstance(entry, PluginEntryConfig) else {}
        api = PluginApi(
            id=plugin.id,
            config=nano_config,
            plugin_config=plugin_config,
            _tool_registry=tool_registry,
            _hook_registry=hook_registry,
        )
        plugin.register(api)
        after_hooks = hook_registry.handler_counts()
        registered_hooks = [
            LoadedHook(
                event=event,
                plugin_id=plugin.id,
                plugin_name=getattr(plugin, "name", plugin.id),
                priority=priority,
            )
            for event, handlers in hook_registry._handlers.items()
            for priority, handler in handlers
            if id(handler) not in before_hook_ids.get(event, set())
        ]
        hook_registry.record_plugin(
            LoadedPlugin(
                id=plugin.id,
                name=getattr(plugin, "name", plugin.id),
                source=_entry_source(entry),
                entry=_entry_label(entry),
                tools=tuple(sorted(set(tool_registry.names()) - before_tools)),
                hooks=tuple(
                    f"{event} (+{after_hooks[event] - before_hooks.get(event, 0)})"
                    for event in sorted(after_hooks)
                    if after_hooks[event] > before_hooks.get(event, 0)
                ),
            )
        )
        hook_registry.record_plugin_hooks(registered_hooks)

    return hook_registry


def _entry_source(entry: str | PluginEntryConfig) -> str:
    if isinstance(entry, str):
        return "builtin" if entry in BUILTIN_PLUGINS else "module"
    if entry.module:
        return "module"
    if entry.path:
        return "path"
    return "unknown"


def _entry_label(entry: str | PluginEntryConfig) -> str:
    if isinstance(entry, str):
        return entry
    if entry.module:
        return entry.module
    if entry.path:
        return entry.path
    return "(unknown)"


def _resolve_plugin(entry: str | PluginEntryConfig, base_dir: Path | None = None) -> Any:
    if isinstance(entry, str):
        if entry in BUILTIN_PLUGINS:
            return _load_object(BUILTIN_PLUGINS[entry])
        return _load_from_module(entry)

    if entry.module:
        return _load_from_module(entry.module)
    if entry.path:
        return _load_from_path(entry.path, base_dir=base_dir)
    raise ValueError("plugin entry requires 'module' or 'path'")


def _load_object(target: str) -> Any:
    module_name, _, attr = target.rpartition(".")
    if not module_name or not attr:
        return _load_from_module(target)
    module = importlib.import_module(module_name)
    obj = getattr(module, attr)
    return obj() if isinstance(obj, type) else obj


def _load_from_module(module_name: str) -> Any:
    module = importlib.import_module(module_name)
    for attr in ("plugin", "Plugin"):
        if hasattr(module, attr):
            obj = getattr(module, attr)
            return obj() if isinstance(obj, type) else obj
    if hasattr(module, "get_plugin"):
        return module.get_plugin()
    raise ValueError(f"plugin module {module_name!r} must export plugin, Plugin, or get_plugin()")


def _load_from_path(path: str, base_dir: Path | None = None) -> Any:
    plugin_path = Path(path).expanduser()
    if not plugin_path.is_absolute():
        plugin_path = (base_dir or Path.cwd()) / plugin_path
    spec = importlib.util.spec_from_file_location(f"nano_openclaw_external_plugin_{plugin_path.stem}", plugin_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load plugin from path: {plugin_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for attr in ("plugin", "Plugin"):
        if hasattr(module, attr):
            obj = getattr(module, attr)
            return obj() if isinstance(obj, type) else obj
    if hasattr(module, "get_plugin"):
        return module.get_plugin()
    raise ValueError(f"plugin file {plugin_path} must export plugin, Plugin, or get_plugin()")
