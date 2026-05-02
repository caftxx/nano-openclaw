"""Active Memory: automatic memory recall before main reply.

Mirrors openclaw extensions/active-memory/index.ts:
- before_prompt_build hook pattern
- Sub-agent execution with restricted tools
- Query modes and prompt styles
- Result injection into main context
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time


class QueryMode(str, Enum):
    """How much conversation history to include in the query."""
    MESSAGE = "message"      # Latest user message only
    RECENT = "recent"        # Last N messages + current
    FULL = "full"            # Full conversation history


class PromptStyle(str, Enum):
    """Style of recall instructions for the sub-agent."""
    BALANCED = "balanced"        # Default: broad recall, moderate precision
    STRICT = "strict"            # High precision, narrow recall
    CONTEXTUAL = "contextual"    # Focus on context and relationships
    RECALL_HEAVY = "recall-heavy"  # Maximize recall breadth
    PRECISION_HEAVY = "precision-heavy"  # Maximize precision
    PREFERENCE_ONLY = "preference-only"  # Only search preferences


@dataclass
class ActiveMemoryConfig:
    """Configuration for Active Memory behavior."""
    enabled: bool = True
    query_mode: QueryMode = QueryMode.RECENT
    prompt_style: PromptStyle = PromptStyle.BALANCED
    max_query_chars: int = 800
    result_max_chars: int = 220
    timeout_seconds: int = 30
    recent_message_count: int = 5  # For RECENT mode


@dataclass
class ActiveMemoryResult:
    """Result from Active Memory recall."""
    context: str | None  # Injected context (None means no relevant memories)
    query_used: str
    elapsed_ms: int
    cached: bool = False


# Prompt templates for each style (from openclaw index.ts:197-309)
STYLE_PROMPTS: dict[PromptStyle, str] = {
    PromptStyle.BALANCED: """
Search memory files for relevant information about:
- Prior work, decisions, dates, people, preferences, todos mentioned in the query
- Any related context that might inform the current request
Return a concise summary (<200 chars) or NONE if nothing relevant found.
""",
    PromptStyle.STRICT: """
Search ONLY for information directly answering the query.
Be precise - only return facts that clearly match.
Return a concise summary (<200 chars) or NONE if no direct matches.
""",
    PromptStyle.CONTEXTUAL: """
Search for contextual relationships: people, projects, timelines, dependencies.
Find connections between the query and prior work.
Return a concise summary (<200 chars) or NONE if no context found.
""",
    PromptStyle.RECALL_HEAVY: """
Broadly search for ANY potentially relevant information.
Cast wide net - preferences, decisions, todos, dates, people, anything mentioned.
Return a concise summary (<200 chars) or NONE if nothing at all found.
""",
    PromptStyle.PRECISION_HEAVY: """
Only return highly confident, precise matches.
Discard weak or tangential results.
Return a concise summary (<200 chars) or NONE if not highly confident.
""",
    PromptStyle.PREFERENCE_ONLY: """
Search ONLY for user preferences: coding style, tool choices, formats, conventions.
Ignore all other content types.
Return a concise summary (<200 chars) or NONE if no preferences found.
""",
}


def build_query(
    messages: list[dict[str, Any]],
    mode: QueryMode,
    config: ActiveMemoryConfig,
) -> str:
    """Build the search query from conversation history.

    Mirrors openclaw index.ts:311-367 (buildQuery).
    """
    if mode == QueryMode.MESSAGE:
        # Only the latest user message
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content[:config.max_query_chars]
                # Handle content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                return " ".join(text_parts)[:config.max_query_chars]
        return ""

    elif mode == QueryMode.RECENT:
        # Last N messages + current
        recent = messages[-config.recent_message_count:]
        parts = []
        for msg in recent:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(f"User: {content}")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(f"User: {block.get('text', '')}")
            elif msg.get("role") == "assistant":
                # Brief summary of assistant replies
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(f"Assistant: {content[:100]}...")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            parts.append(f"Assistant: {text[:100]}...")
        return "\n".join(parts)[:config.max_query_chars]

    elif mode == QueryMode.FULL:
        # Full conversation (truncated)
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(f"{role}: {content[:200]}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(f"{role}: {block.get('text', '')[:200]}")
        return "\n".join(parts)[:config.max_query_chars]

    return ""


def build_recall_prompt(query: str, style: PromptStyle) -> str:
    """Build the sub-agent prompt for memory recall.

    Mirrors openclaw index.ts:369-425 (buildRecallPrompt).
    """
    style_instruction = STYLE_PROMPTS.get(style, STYLE_PROMPTS[PromptStyle.BALANCED])

    return f"""
You are a memory recall assistant. Your task is to search memory files and return relevant information.

Query: {query}

{style_instruction}

Use memory_search to find matches, then memory_get to read specific content.
Output format:
- If found: Return <found> followed by concise summary (<200 chars)
- If nothing relevant: Return NONE

Do not explain your search process. Just output the result.
"""


def run_recall_subagent(
    client: Any,
    query: str,
    style: PromptStyle,
    workspace_dir: str,
    config: ActiveMemoryConfig,
) -> ActiveMemoryResult:
    """Execute the memory recall sub-agent.

    Mirrors openclaw index.ts:427-521 (runRecallSubagent).

    Uses Anthropic API directly with restricted tools (memory_search, memory_get only).
    """
    start_time = time.time()

    # Build sub-agent prompt
    prompt = build_recall_prompt(query, style)

    # Create restricted tool registry (only memory_search, memory_get)
    tools = [
        {
            "name": "memory_search",
            "description": "Search memory files for keywords.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "maxResults": {"type": "integer", "default": 10},
                    "minScore": {"type": "number", "default": 0.1},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_get",
            "description": "Read a specific memory file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace"},
                    "from": {"type": "integer", "description": "Starting line number (1-indexed)"},
                    "lines": {"type": "integer", "description": "Number of lines to read"},
                },
                "required": ["path"],
            },
        },
    ]

    # Execute sub-agent
    try:
        from anthropic import Anthropic
        if not isinstance(client, Anthropic):
            # Skip if not Anthropic client (OpenAI doesn't support this pattern)
            elapsed = int((time.time() - start_time) * 1000)
            return ActiveMemoryResult(
                context=None,
                query_used=query,
                elapsed_ms=elapsed,
            )

        # Build tool definitions that include workspace_dir context
        system_prompt = f"Workspace: {workspace_dir}\nYou have access to memory_search and memory_get tools. Use them to search MEMORY.md and memory/*.md files."

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",  # Use current model for recall
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            tools=tools,
        )

        # Process response
        content_blocks = response.content
        result_text = ""
        for block in content_blocks:
            if hasattr(block, "type") and block.type == "text":
                result_text += block.text

        # Check for NONE result
        if "NONE" in result_text.upper() or not result_text.strip():
            elapsed = int((time.time() - start_time) * 1000)
            return ActiveMemoryResult(
                context=None,
                query_used=query,
                elapsed_ms=elapsed,
            )

        # Extract summary
        if "<found>" in result_text:
            summary = result_text.split("<found>")[-1].strip()
        else:
            summary = result_text.strip()

        # Truncate to max chars
        summary = summary[:config.result_max_chars]

        elapsed = int((time.time() - start_time) * 1000)

        return ActiveMemoryResult(
            context=f"[Active Memory Recall: {summary}]",
            query_used=query,
            elapsed_ms=elapsed,
        )

    except Exception:  # noqa: BLE001 — errors become NONE result
        elapsed = int((time.time() - start_time) * 1000)
        # On error, return NONE (no injection)
        return ActiveMemoryResult(
            context=None,
            query_used=query,
            elapsed_ms=elapsed,
        )


@dataclass
class ActiveMemoryManager:
    """Manages Active Memory state and execution for a session.

    Mirrors openclaw index.ts:523-680 (ActiveMemoryPlugin class).
    """
    client: Any
    workspace_dir: str
    config: ActiveMemoryConfig = field(default_factory=ActiveMemoryConfig)
    _cache: dict[str, ActiveMemoryResult] = field(default_factory=dict)

    def toggle(self) -> bool:
        """Toggle Active Memory on/off. Returns new state."""
        self.config.enabled = not self.config.enabled
        return self.config.enabled

    def set_query_mode(self, mode: QueryMode) -> None:
        """Set the query mode."""
        self.config.query_mode = mode

    def set_prompt_style(self, style: PromptStyle) -> None:
        """Set the prompt style."""
        self.config.prompt_style = style

    def run(self, messages: list[dict[str, Any]]) -> ActiveMemoryResult | None:
        """Run Active Memory recall if enabled.

        Returns None if disabled or no relevant memories found.
        """
        if not self.config.enabled:
            return None

        # Build query
        query = build_query(messages, self.config.query_mode, self.config)

        if not query:
            return None

        # Check cache (simplified)
        cache_key = f"{query}:{self.config.prompt_style.value}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            cached.cached = True
            return cached

        # Run recall
        result = run_recall_subagent(
            self.client,
            query,
            self.config.prompt_style,
            self.workspace_dir,
            self.config,
        )

        # Cache result
        self._cache[cache_key] = result

        return result