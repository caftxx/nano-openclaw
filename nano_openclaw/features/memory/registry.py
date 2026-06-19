"""Tool registration helpers for the memory feature."""

from __future__ import annotations

from typing import Any

from nano_openclaw.core.tools import Tool
from nano_openclaw.features.memory.tools import memory_get, memory_search


def build_memory_tools(memory_search_config: Any | None = None) -> list[Tool]:
    return [
        Tool(
            name="memory_get",
            description="Read a specific memory file (MEMORY.md or memory/*.md). Use to retrieve exact content by path.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace (e.g., MEMORY.md or memory/2026-05-02.md)"},
                    "from": {"type": "integer", "description": "Starting line number (1-indexed)"},
                    "lines": {"type": "integer", "description": "Number of lines to read"},
                },
                "required": ["path"],
            },
            run=lambda args, workspace_dir=None: memory_get(args, workspace_dir),
        ),
        Tool(
            name="memory_search",
            description="Search memory files (MEMORY.md + memory/*.md) for keywords. Use before answering questions about prior work or decisions.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "maxResults": {"type": "integer", "description": "Max results (default 10)"},
                    "minScore": {"type": "number", "description": "Min match score 0-1 (default 0.1)"},
                    "contextLines": {
                        "type": "integer",
                        "description": "Lines of context around each hit (default 2, clamped to 0..20).",
                        "default": 2,
                        "minimum": 0,
                        "maximum": 20,
                    },
                    "caseSensitive": {
                        "type": "boolean",
                        "description": "If true, match case-sensitively (default false).",
                        "default": False,
                    },
                    "outputMode": {
                        "type": "string",
                        "description": "Result formatting: snippet (default) | paths_only | count.",
                        "enum": ["snippet", "paths_only", "count"],
                        "default": "snippet",
                    },
                },
                "required": ["query"],
            },
            run=lambda args, workspace_dir=None: memory_search(
                args,
                workspace_dir,
                config=memory_search_config,
            ),
        ),
    ]
