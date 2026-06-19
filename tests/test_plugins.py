import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

from nano_openclaw.config.types import NanoOpenClawConfig, PluginEntryConfig, PluginsConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import Tool, ToolRegistry, build_core_registry
from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.services.backend_embedded import EmbeddedBackend


def _dispatch(registry, *args):
    result = registry.dispatch(*args)
    return asyncio.run(result) if inspect.iscoroutine(result) else result


def _runtime_for_plugins(tmp_path: Path, hooks) -> SimpleNamespace:
    from nano_openclaw.services.runs import RunRegistry
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "sessions.json"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=None,
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=LoopConfig(model="test-model", workspace_dir=workspace_dir, session_key="default"),
        hook_registry=hooks,
        state_dir=state_dir,
        session_dir=session_dir,
        store_path=store_path,
        workspace_dir=workspace_dir,
        model_ref="test/test-model",
        model_id="test-model",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
    )


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


def test_plugin_api_exposes_narrow_registration_surface(tmp_path):
    plugin_file = tmp_path / "surface_plugin.py"
    plugin_file.write_text(
        """
from nano_openclaw.adapters.channels.base import ChannelAdapter

class SurfaceChannel(ChannelAdapter):
    id = "surface-channel"

    async def start(self, runtime, gateway=None):
        self._state = "running"

    async def stop(self):
        self._state = "stopped"

async def surface_slash(backend, renderer, state, args, cmd):
    renderer.text("surface slash handled")

class Plugin:
    id = "surface"
    name = "Surface"

    def register(self, api):
        assert api.config_snapshot() is api.config
        assert not hasattr(api, "runtime")
        api.register_slash("/surface", surface_slash, "Surface command")
        api.register_channel(SurfaceChannel)
        api.register_feature({"id": "surface"})
        api.register_hook("before_prompt_build", lambda _payload: {
            "append": f"{len(api._slash_registrations)}:{len(api._channel_registrations)}:{len(api._feature_registrations)}"
        })
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

    assert result["append"] == "1:1:1"
    plugin = next(plugin for plugin in hooks.plugins() if plugin.id == "surface")
    assert plugin.slash == ("/surface",)
    assert plugin.channels == ("surface-channel",)
    assert plugin.features == ("surface",)


def test_plugin_registered_slash_command_dispatches(tmp_path):
    plugin_file = tmp_path / "slash_plugin.py"
    plugin_file.write_text(
        """
async def slash_cmd(backend, renderer, state, args, cmd):
    renderer.text("plugin slash ok")

class Plugin:
    id = "slash"
    name = "Slash"

    def register(self, api):
        api.register_slash("/plugin-surface", slash_cmd, "Plugin slash")
""",
        encoding="utf-8",
    )
    registry = build_core_registry()
    hooks = load_plugins(
        PluginsConfig(load=[PluginEntryConfig(path=str(plugin_file))]),
        registry,
        NanoOpenClawConfig(),
    )

    from nano_openclaw.services.slash import handle_slash
    from nano_openclaw.services.slash_renderer import PlainRenderer

    renderer = PlainRenderer()
    backend = SimpleNamespace(runtime=SimpleNamespace(hook_registry=hooks))
    handled = asyncio.run(handle_slash("/plugin-surface", backend, renderer, {"session_key": ""}))

    assert handled is True
    assert "plugin slash ok" in renderer.collect()


def test_plugin_registered_channel_starts_via_embedded_backend(tmp_path):
    plugin_file = tmp_path / "channel_plugin.py"
    plugin_file.write_text(
        """
from nano_openclaw.adapters.channels.base import ChannelAdapter

class PluginChannel(ChannelAdapter):
    id = "plugin-channel"

    async def start(self, runtime, gateway=None):
        self._state = "running"
        self.gateway = gateway

    async def stop(self):
        self._state = "stopped"

class Plugin:
    id = "channel-plugin"
    name = "Channel Plugin"

    def register(self, api):
        api.register_channel(PluginChannel)
""",
        encoding="utf-8",
    )
    registry = build_core_registry()
    hooks = load_plugins(
        PluginsConfig(load=[PluginEntryConfig(path=str(plugin_file))]),
        registry,
        NanoOpenClawConfig(),
    )
    manager = ChannelManager()
    backend = EmbeddedBackend(_runtime_for_plugins(tmp_path, hooks), channel_manager=manager)

    async def run():
        status = await backend.channels_start("plugin-channel")
        channels = await backend.channels_status()
        await backend.aclose()
        return status, channels

    status, channels = asyncio.run(run())

    assert manager.known_channels() == ["plugin-channel"]
    assert status.channel_id == "plugin-channel"
    assert status.state == "running"
    assert [entry.channel_id for entry in channels] == ["plugin-channel"]


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
