import asyncio
import inspect
import sys

from nano_openclaw.config.types import NanoOpenClawConfig, PluginEntryConfig, PluginsConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.core.tools import Tool, build_core_registry


def _dispatch(registry, *args):
    result = registry.dispatch(*args)
    return asyncio.run(result) if inspect.iscoroutine(result) else result


def test_builtin_web_plugin_registers_web_tools():
    registry = build_core_registry()

    load_plugins(PluginsConfig(load=["web"]), registry, NanoOpenClawConfig())

    assert "web_search" in registry.names()
    assert "web_fetch" in registry.names()


def test_default_config_loads_builtin_plugins():
    config = NanoOpenClawConfig()
    registry = build_core_registry()

    hooks = load_plugins(config.plugins, registry, config)

    assert "memory_get" in registry.names()
    assert "memory_search" in registry.names()
    assert "web_search" in registry.names()
    assert "web_fetch" in registry.names()
    assert "sessions_spawn" in registry.names()
    assert "subagents" in registry.names()
    assert [plugin.id for plugin in hooks.plugins()] == [
        "nano-memory",
        "nano-web",
        "nano-subagent",
        "nano-mcp",
        "nano-schedule",
        "nano-review-fork",
    ]
    assert [plugin.entry for plugin in hooks.plugins()] == [
        "memory",
        "web",
        "subagent",
        "mcp",
        "schedule",
        "review-fork",
    ]


def test_explicit_empty_plugin_load_still_loads_builtin_plugins():
    config = NanoOpenClawConfig(plugins=PluginsConfig(load=[]))
    registry = build_core_registry()

    load_plugins(config.plugins, registry, config)

    assert "memory_get" in registry.names()
    assert "web_search" in registry.names()
    assert "sessions_spawn" in registry.names()


def test_custom_plugin_load_keeps_builtin_plugins(tmp_path):
    plugin_file = tmp_path / "custom_plugin.py"
    plugin_file.write_text(
        """
from nano_openclaw.core.tools import Tool

class Plugin:
    id = "custom"
    name = "Custom"

    def register(self, api):
        api.register_tool(Tool(
            name="custom_tool",
            description="Custom tool",
            input_schema={"type": "object"},
            run=lambda _args: "custom",
        ))
""",
        encoding="utf-8",
    )
    config = NanoOpenClawConfig(plugins=PluginsConfig(load=[PluginEntryConfig(path=str(plugin_file))]))
    registry = build_core_registry()

    load_plugins(config.plugins, registry, config)

    assert "memory_get" in registry.names()
    assert "web_search" in registry.names()
    assert "sessions_spawn" in registry.names()
    assert "custom_tool" in registry.names()


def test_tool_hooks_can_modify_args_and_result():
    registry = build_core_registry()
    registry.register(Tool(
        name="echo",
        description="Echo text",
        input_schema={"type": "object"},
        run=lambda args: args["text"],
    ))
    hooks = load_plugins(PluginsConfig(load=[]), registry, NanoOpenClawConfig())

    async def before(payload):
        args = dict(payload["tool_args"])
        args["text"] = "changed"
        return {"tool_args": args}

    def after(payload):
        return {"result": f"{payload['result']}!"}

    hooks.register("before_tool_call", before)
    hooks.register("after_tool_call", after)

    result = _dispatch(registry, "tool-1", "echo", {"text": "original"})

    assert result["content"][0]["text"] == "changed!"


def test_before_tool_call_hook_can_deny():
    registry = build_core_registry()
    registry.register(Tool(
        name="echo",
        description="Echo text",
        input_schema={"type": "object"},
        run=lambda args: args["text"],
    ))
    hooks = load_plugins(PluginsConfig(load=[]), registry, NanoOpenClawConfig())
    hooks.register("before_tool_call", lambda _payload: {"deny": True, "reason": "blocked"})

    result = _dispatch(registry, "tool-1", "echo", {"text": "original"})

    assert result["is_error"] is True
    assert result["content"][0]["text"] == "blocked"


def test_after_tool_call_error_hook_receives_raw_output():
    registry = build_core_registry()
    registry.register(Tool(
        name="boom",
        description="Raise",
        input_schema={"type": "object"},
        run=lambda _args: (_ for _ in ()).throw(ValueError("bad")),
    ))
    hooks = load_plugins(PluginsConfig(load=[]), registry, NanoOpenClawConfig())
    seen = {}

    def after(payload):
        seen["result_type"] = type(payload["result"])
        seen["error"] = payload["error"]
        return {"result": "rewritten error"}

    hooks.register("after_tool_call", after)

    result = _dispatch(registry, "tool-1", "boom", {})

    assert seen == {"result_type": str, "error": True}
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "rewritten error"


def test_plugin_api_exposes_tool_names(tmp_path):
    plugin_file = tmp_path / "names_plugin.py"
    plugin_file.write_text(
        """
class Plugin:
    id = "names"
    name = "Names"

    def register(self, api):
        api.register_hook("before_prompt_build", lambda _payload: {"append": ",".join(api.tool_names())})
""",
        encoding="utf-8",
    )
    registry = build_core_registry()
    hooks = load_plugins(
        PluginsConfig(load=[PluginEntryConfig(path=str(plugin_file))]),
        registry,
        NanoOpenClawConfig(),
    )
    result = asyncio.run(hooks.run("before_prompt_build", {"system": "base"}))

    assert "read_file" in result["append"]


def test_path_plugin_can_register_tool(tmp_path):
    plugin_file = tmp_path / "custom_plugin.py"
    plugin_file.write_text(
        """
from nano_openclaw.core.tools import Tool

class Plugin:
    id = "custom"
    name = "Custom"

    def register(self, api):
        prefix = api.plugin_config["prefix"]
        api.register_tool(Tool(
            name="custom_echo",
            description="Custom echo",
            input_schema={"type": "object"},
            run=lambda args: prefix + args["text"],
        ))
""",
        encoding="utf-8",
    )
    registry = build_core_registry()

    load_plugins(
        PluginsConfig(load=[PluginEntryConfig(path=str(plugin_file), config={"prefix": "p:"})]),
        registry,
        NanoOpenClawConfig(),
    )
    result = _dispatch(registry, "tool-1", "custom_echo", {"text": "hello"})

    assert result["content"][0]["text"] == "p:hello"


def test_module_string_plugin_loads_module_export(tmp_path, monkeypatch):
    package_dir = tmp_path / "plugpkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "my_plugin.py").write_text(
        """
class Plugin:
    id = "module-plugin"
    name = "Module Plugin"

    def register(self, api):
        api.register_hook("before_prompt_build", lambda payload: {"append": "module hook"})
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("plugpkg.my_plugin", None)

    registry = build_core_registry()
    hooks = load_plugins(
        PluginsConfig(load=["plugpkg.my_plugin"]),
        registry,
        NanoOpenClawConfig(),
    )
    result = asyncio.run(hooks.run("before_prompt_build", {"system": "base"}))

    assert result["append"] == "module hook"


def test_loaded_plugin_metadata_includes_registered_tools_and_hooks(tmp_path):
    plugin_file = tmp_path / "custom_plugin.py"
    plugin_file.write_text(
        """
from nano_openclaw.core.tools import Tool

class Plugin:
    id = "custom"
    name = "Custom"

    def register(self, api):
        api.register_tool(Tool(
            name="custom_tool",
            description="Custom tool",
            input_schema={"type": "object"},
            run=lambda _args: "custom",
        ))
        api.register_hook("before_prompt_build", lambda payload: {"append": "custom"})
""",
        encoding="utf-8",
    )
    registry = build_core_registry()

    hooks = load_plugins(
        PluginsConfig(load=[PluginEntryConfig(path=str(plugin_file))]),
        registry,
        NanoOpenClawConfig(),
    )

    custom = next(plugin for plugin in hooks.plugins() if plugin.id == "custom")
    assert custom.name == "Custom"
    assert custom.source == "path"
    assert custom.entry == str(plugin_file)
    assert custom.tools == ("custom_tool",)
    assert custom.hooks == ("before_prompt_build (+1)",)
    prompt_hooks = hooks.hooks_by_event()["before_prompt_build"]
    assert any(hook.plugin_id == "custom" for hook in prompt_hooks)
    assert any(hook.plugin_name == "Custom" for hook in prompt_hooks)
