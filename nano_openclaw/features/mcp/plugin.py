"""Built-in MCP plugin."""

from dataclasses import dataclass
from typing import Any

from nano_openclaw.logger import get_logger
from nano_openclaw.plugins.api import PluginApi

logger = get_logger(__name__)


def _simple_error(exc: BaseException) -> str:
    text = " ".join(str(exc).splitlines()).strip() or "unknown error"
    if len(text) > 200:
        text = text[:197].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"


@dataclass
class McpFeatureState:
    id: str = "mcp"
    configured_servers: tuple[str, ...] = ()
    runtime: Any | None = None
    initialized: bool = False
    load_error: str = ""

    def status(self) -> dict[str, Any]:
        if self.runtime is not None:
            payload = self.runtime.status_snapshot()
        else:
            payload = {
                "configured": bool(self.configured_servers),
                "initialized": self.initialized,
                "servers": [
                    {
                        "name": name,
                        "transport": "unknown",
                        "status": "pending" if not self.initialized else "failed",
                        "tools": 0,
                        "error": self.load_error,
                    }
                    for name in self.configured_servers
                ],
                "connected": 0,
                "failed": len(self.configured_servers) if self.load_error else 0,
                "starting": 0 if self.initialized else len(self.configured_servers),
                "total_tools": 0,
            }
        payload["load_error"] = self.load_error
        return payload


def mcp_status_for_runtime(runtime: Any) -> dict[str, Any]:
    config = getattr(runtime, "config", None)
    servers = getattr(getattr(config, "mcp", None), "servers", {}) or {}
    fallback = McpFeatureState(configured_servers=tuple(sorted(servers)))
    hooks = getattr(runtime, "hook_registry", None) or getattr(
        getattr(runtime, "registry", None), "hook_registry", lambda: None
    )()
    features_fn = getattr(hooks, "features", None)
    if features_fn is None:
        return fallback.status()
    for feature in features_fn():
        if isinstance(feature, McpFeatureState):
            return feature.status()
        if isinstance(feature, dict) and feature.get("id") == "mcp":
            status_fn = feature.get("status")
            if callable(status_fn):
                return status_fn()
    return fallback.status()


class McpPlugin:
    id = "nano-mcp"
    name = "MCP"

    def register(self, api: PluginApi) -> None:
        state = McpFeatureState(configured_servers=tuple(sorted(api.config.mcp.servers)))
        api.register_feature(state)

        if not api.config.mcp.servers:
            return

        initialized = False

        async def initialize_mcp(_payload: dict[str, Any]) -> None:
            nonlocal initialized
            if initialized:
                return None
            initialized = True
            state.initialized = True

            from nano_openclaw.features.mcp.materialize import materialize_mcp_tools
            from nano_openclaw.features.mcp.runtime import McpRuntime

            state.runtime = McpRuntime()
            try:
                await state.runtime.initialize(api.config.mcp.servers)
                mcp_tools = materialize_mcp_tools(state.runtime, existing_names=set(api.tool_names()))
                for tool in mcp_tools:
                    api.register_tool(tool)
                logger.info(
                    "mcp.loaded",
                    f"MCP: loaded {len(mcp_tools)} tools from {len(api.config.mcp.servers)} server(s)",
                )
            except Exception as exc:  # noqa: BLE001
                state.load_error = _simple_error(exc)
                logger.warning("mcp.load_failed", state.load_error)
            return None

        async def close_mcp(_payload: dict[str, Any]) -> None:
            if state.runtime is not None:
                await state.runtime.close()
            return None

        api.register_hook("session_start", initialize_mcp, priority=0)
        api.register_hook("session_end", close_mcp, priority=0)
