"""Tool registration helpers for the web feature."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nano_openclaw.core.tools import Tool
from nano_openclaw.features.web.fetch import web_fetch
from nano_openclaw.features.web.search import web_search

if TYPE_CHECKING:
    from nano_openclaw.config.types import ToolsConfig


def build_web_tools(tools_config: "ToolsConfig | None" = None) -> list[Tool]:
    web_config = tools_config.web if tools_config else None
    search_config = web_config.search if web_config else None
    fetch_config = web_config.fetch if web_config else None
    tools: list[Tool] = []

    if search_config is None or search_config.enabled:
        default_max_results = search_config.maxResults if search_config else 10
        default_region = search_config.region if search_config else "wt-wt"
        tools.append(
            Tool(
                name="web_search",
                description="Search the web using DuckDuckGo. Returns titles, URLs, and snippets. Use before web_fetch to find relevant pages.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "maxResults": {
                            "type": "integer",
                            "description": f"Max results (default {default_max_results})",
                            "default": default_max_results,
                        },
                    },
                    "required": ["query"],
                },
                run=lambda args: web_search(
                    args["query"],
                    max_results=args.get("maxResults", default_max_results),
                    region=default_region,
                ).get("text", "[no results]"),
            )
        )

    if fetch_config is None or fetch_config.enabled:
        default_extract_mode = fetch_config.extractMode if fetch_config else "markdown"
        default_max_chars = fetch_config.maxChars if fetch_config else 20_000
        default_max_redirects = fetch_config.maxRedirects if fetch_config else 3
        default_timeout_seconds = fetch_config.timeoutSeconds if fetch_config else 30

        async def _run_web_fetch(
            args: dict[str, Any],
            _em: str = default_extract_mode,
            _mc: int = default_max_chars,
            _mr: int = default_max_redirects,
            _ts: int = default_timeout_seconds,
        ) -> str:
            result = await web_fetch(
                args["url"],
                extract_mode=args.get("extractMode", _em),
                max_chars=args.get("maxChars", _mc),
                max_redirects=_mr,
                timeout_seconds=_ts,
            )
            return result.get("text", "[fetch failed]")

        tools.append(
            Tool(
                name="web_fetch",
                description="Fetch and extract readable content from a URL (HTML->markdown/text). Use after web_search to read specific pages.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "HTTP/HTTPS URL"},
                        "extractMode": {
                            "type": "string",
                            "enum": ["markdown", "text"],
                            "default": default_extract_mode,
                        },
                        "maxChars": {
                            "type": "integer",
                            "description": f"Max chars to return (default {default_max_chars})",
                            "default": default_max_chars,
                        },
                    },
                    "required": ["url"],
                },
                run=_run_web_fetch,
            )
        )

    return tools
