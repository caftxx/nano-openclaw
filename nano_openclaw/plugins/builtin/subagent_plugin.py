"""Built-in subagent tools plugin."""

from nano_openclaw.plugins.api import PluginApi


class SubagentPlugin:
    id = "nano-subagent"
    name = "Subagent Tools"

    def register(self, api: PluginApi) -> None:
        from nano_openclaw.subagent.tools import build_spawn_tool, build_subagents_tool

        api.register_tool(build_spawn_tool())
        api.register_tool(build_subagents_tool())
