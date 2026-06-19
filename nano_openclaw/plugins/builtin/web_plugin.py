"""Built-in web tools plugin."""

from nano_openclaw.plugins.api import PluginApi
from nano_openclaw.core.tools import build_web_tools


class WebPlugin:
    id = "nano-web"
    name = "Web Tools"

    def register(self, api: PluginApi) -> None:
        for tool in build_web_tools(api.config.tools):
            api.register_tool(tool)
