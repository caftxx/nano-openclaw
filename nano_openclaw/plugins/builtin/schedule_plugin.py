"""Built-in cron schedule tools plugin."""

from __future__ import annotations

from nano_openclaw.plugins.api import PluginApi


class SchedulePlugin:
    id = "nano-schedule"
    name = "Cron Schedule"

    def register(self, api: PluginApi) -> None:
        from pathlib import Path
        from nano_openclaw.schedule.tools import build_cron_tools

        state_dir = getattr(api.config, "state_dir", "")
        if not state_dir:
            return  # No state_dir configured; skip registration

        cron_dir = Path(state_dir) / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)

        for tool in build_cron_tools(cron_dir):
            api.register_tool(tool)
