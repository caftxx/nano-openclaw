"""Active Memory: automatic memory recall before main reply.

Mirrors openclaw extensions/active-memory/index.ts:
- before_prompt_build hook pattern
- Sub-agent execution with restricted tools
- Query modes and prompt styles
- Result injection into main context
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time


class QueryMode(str, Enum):
    """How much conversation history to include in the query."""
    MESSAGE = "message"      # Latest user message only
    RECENT = "recent"        # Last N turns (per-role counts)
    FULL = "full"            # Full conversation history


class PromptStyle(str, Enum):
    """Style of recall instructions for the sub-agent."""
    BALANCED = "balanced"
    STRICT = "strict"
    CONTEXTUAL = "contextual"
    RECALL_HEAVY = "recall-heavy"
    PRECISION_HEAVY = "precision-heavy"
    PREFERENCE_ONLY = "preference-only"


@dataclass
class ActiveMemoryConfig:
    """Configuration for Active Memory behavior.

    Field names and defaults mirror openclaw active-memory plugin schema.
    """
    enabled: bool = True
    query_mode: QueryMode = QueryMode.RECENT
    prompt_style: PromptStyle = PromptStyle.BALANCED
    model: str | None = None              # sub-agent model override (None = use main model)
    thinking: str = "off"                 # thinking level for sub-agent
    timeout_ms: int = 15000
    max_summary_chars: int = 220
    recent_user_turns: int = 2            # RECENT: how many user messages to include
    recent_assistant_turns: int = 1       # RECENT: how many assistant messages to include
    recent_user_chars: int = 220          # RECENT: char limit per user message
    recent_assistant_chars: int = 180     # RECENT: char limit per assistant message
    prompt_override: str | None = None    # fully replace the recall prompt body
    prompt_append: str | None = None      # append extra instructions to the prompt
    cache_ttl_ms: int = 15000             # cache TTL in milliseconds
    logging: bool = False                 # print debug line after each recall


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


def _extract_text(msg: dict[str, Any]) -> str:
    """Extract plain text from a message's content field."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(parts)


def build_query(
    messages: list[dict[str, Any]],
    mode: QueryMode,
    config: ActiveMemoryConfig,
) -> str:
    """Build the search query from conversation history.

    Mirrors openclaw index.ts:311-367 (buildQuery).
    """
    if mode == QueryMode.MESSAGE:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return _extract_text(msg)[:config.recent_user_chars]
        return ""

    elif mode == QueryMode.RECENT:
        # Collect the most recent N user and M assistant messages in reverse order,
        # then flip back to chronological so the query reads naturally.
        user_count = 0
        assistant_count = 0
        parts: list[str] = []

        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "user" and user_count < config.recent_user_turns:
                text = _extract_text(msg)[:config.recent_user_chars]
                parts.append(f"User: {text}")
                user_count += 1
            elif role == "assistant" and assistant_count < config.recent_assistant_turns:
                text = _extract_text(msg)[:config.recent_assistant_chars]
                parts.append(f"Assistant: {text}")
                assistant_count += 1

            if (user_count >= config.recent_user_turns
                    and assistant_count >= config.recent_assistant_turns):
                break

        parts.reverse()
        return "\n".join(parts)

    elif mode == QueryMode.FULL:
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            text = _extract_text(msg)[:200]
            parts.append(f"{role}: {text}")
        return "\n".join(parts)

    return ""


def build_recall_prompt(query: str, style: PromptStyle, config: ActiveMemoryConfig) -> str:
    """Build the sub-agent prompt for memory recall.

    Mirrors openclaw index.ts:369-425 (buildRecallPrompt).
    Supports prompt_override (full replacement) and prompt_append (suffix).
    """
    if config.prompt_override:
        return f"Query: {query}\n\n{config.prompt_override}"

    style_instruction = STYLE_PROMPTS.get(style, STYLE_PROMPTS[PromptStyle.BALANCED])

    base = f"""
You are a memory recall assistant. Your task is to search memory files and return relevant information.

Query: {query}

{style_instruction}

Use memory_search to find matches, then memory_get to read specific content.
Output format:
- If found: Return <found> followed by concise summary (<200 chars)
- If nothing relevant: Return NONE

Do not explain your search process. Just output the result.
"""

    if config.prompt_append:
        return f"{base}\n{config.prompt_append}"
    return base


# ──────────────────────────── Tool dispatch ────────────────────────────

def _dispatch_tool(name: str, args: dict[str, Any], workspace_dir: str) -> str:
    """Execute a memory tool call and return its string result."""
    from nano_openclaw.memory.tools import memory_search, memory_get
    if name == "memory_search":
        return memory_search(args, workspace_dir)
    if name == "memory_get":
        return memory_get(args, workspace_dir)
    return f"[unknown tool: {name}]"


def _anthropic_tools_schema() -> list[dict]:
    return [
        {
            "name": "memory_search",
            "description": "Search memory files for keywords.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "maxResults": {"type": "integer"},
                    "minScore": {"type": "number"},
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


def _openai_tools_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": "Search memory files for keywords.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "maxResults": {"type": "integer"},
                        "minScore": {"type": "number"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_get",
                "description": "Read a specific memory file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to workspace"},
                        "from": {"type": "integer", "description": "Starting line number (1-indexed)"},
                        "lines": {"type": "integer", "description": "Number of lines to read"},
                    },
                    "required": ["path"],
                },
            },
        },
    ]


# ──────────────────────────── Backends ────────────────────────────

_MAX_TOOL_ITERS = 5


class RecallBackend(ABC):
    """Abstraction over LLM provider for the memory recall sub-agent.

    Each backend runs a small agentic loop: send prompt → handle tool
    calls → return final text.  Tool results are dispatched locally so
    no network round-trips are needed for file I/O.
    """

    @abstractmethod
    def run(
        self,
        prompt: str,
        system: str,
        workspace_dir: str,
        config: ActiveMemoryConfig,
    ) -> str | None:
        """Run the agentic recall loop. Returns text result or None."""
        ...


class AnthropicRecallBackend(RecallBackend):
    """Recall backend for the Anthropic Messages API."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def run(self, prompt: str, system: str, workspace_dir: str, config: ActiveMemoryConfig) -> str | None:
        tools = _anthropic_tools_schema()
        messages: list[dict] = [{"role": "user", "content": prompt}]

        # Build thinking params if requested
        thinking_params: dict = {}
        if config.thinking and config.thinking != "off":
            from nano_openclaw.loop import THINKING_BUDGETS
            budget = THINKING_BUDGETS.get(config.thinking, 1024)  # type: ignore[arg-type]
            if budget:
                thinking_params = {"thinking": {"type": "enabled", "budget_tokens": budget}}

        for _ in range(_MAX_TOOL_ITERS):
            response = self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=system,
                messages=messages,
                tools=tools,
                **thinking_params,
            )

            assistant_content: list[dict] = []
            text_parts: list[str] = []
            tool_results: list[dict] = []

            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                    result = _dispatch_tool(block.name, block.input, workspace_dir)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            if tool_results:
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": tool_results})
            else:
                return "\n".join(text_parts) if text_parts else None

        return None


class OpenAIRecallBackend(RecallBackend):
    """Recall backend for OpenAI-compatible chat completions API."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def run(self, prompt: str, system: str, workspace_dir: str, config: ActiveMemoryConfig) -> str | None:
        tools = _openai_tools_schema()
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        for _ in range(_MAX_TOOL_ITERS):
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=300,
                messages=messages,
                tools=tools,
            )
            choice = response.choices[0]
            msg = choice.message

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    result = _dispatch_tool(tc.function.name, args, workspace_dir)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
            else:
                return msg.content or None

        return None


def _create_backend(client: Any, model: str) -> RecallBackend:
    """Return the appropriate backend based on the client type."""
    try:
        from anthropic import Anthropic
        if isinstance(client, Anthropic):
            return AnthropicRecallBackend(client, model)
    except ImportError:
        pass
    return OpenAIRecallBackend(client, model)


# ──────────────────────────── Sub-agent entry point ────────────────────────────

def run_recall_subagent(
    client: Any,
    model: str,
    query: str,
    style: PromptStyle,
    workspace_dir: str,
    config: ActiveMemoryConfig,
) -> ActiveMemoryResult:
    """Execute the memory recall sub-agent.

    Mirrors openclaw index.ts:427-521 (runRecallSubagent).
    Supports both Anthropic and OpenAI-compatible clients.
    """
    start_time = time.time()

    prompt = build_recall_prompt(query, style, config)
    system = (
        f"Workspace: {workspace_dir}\n"
        "You have access to memory_search and memory_get tools. "
        "Use them to search MEMORY.md and memory/*.md files."
    )

    # config.model overrides the main model for the sub-agent
    effective_model = config.model or model

    try:
        backend = _create_backend(client, effective_model)
        result_text = backend.run(prompt, system, workspace_dir, config)
    except Exception:  # noqa: BLE001 — errors become NONE result
        result_text = None

    elapsed = int((time.time() - start_time) * 1000)

    if not result_text or "NONE" in result_text.upper():
        result = ActiveMemoryResult(context=None, query_used=query, elapsed_ms=elapsed)
    else:
        if "<found>" in result_text:
            summary = result_text.split("<found>")[-1].strip()
        else:
            summary = result_text.strip()
        summary = summary[:config.max_summary_chars]
        result = ActiveMemoryResult(
            context=f"[Active Memory Recall: {summary}]",
            query_used=query,
            elapsed_ms=elapsed,
        )

    if config.logging:
        hit = "FOUND" if result.context else "NONE"
        print(f"[active-memory] {hit} elapsed={elapsed}ms model={effective_model} query={query[:60]!r}")

    return result


# ──────────────────────────── Manager ────────────────────────────

@dataclass
class ActiveMemoryManager:
    """Manages Active Memory state and execution for a session.

    Mirrors openclaw index.ts:523-680 (ActiveMemoryPlugin class).
    """
    client: Any
    model: str
    workspace_dir: str
    config: ActiveMemoryConfig = field(default_factory=ActiveMemoryConfig)
    # Cache entries: query+style key → (result, insertion_timestamp)
    _cache: dict[str, tuple[ActiveMemoryResult, float]] = field(default_factory=dict)

    def toggle(self) -> bool:
        """Toggle Active Memory on/off. Returns new state."""
        self.config.enabled = not self.config.enabled
        return self.config.enabled

    def set_query_mode(self, mode: QueryMode) -> None:
        self.config.query_mode = mode

    def set_prompt_style(self, style: PromptStyle) -> None:
        self.config.prompt_style = style

    def run(self, messages: list[dict[str, Any]]) -> ActiveMemoryResult | None:
        """Run Active Memory recall if enabled.

        Returns None if disabled or no relevant memories found.
        """
        if not self.config.enabled:
            return None

        query = build_query(messages, self.config.query_mode, self.config)
        if not query:
            return None

        cache_key = f"{query}:{self.config.prompt_style.value}"
        now = time.time()

        if cache_key in self._cache:
            cached_result, ts = self._cache[cache_key]
            if now - ts < self.config.cache_ttl_ms / 1000:
                cached_result.cached = True
                return cached_result
            # expired — fall through to fresh recall
            del self._cache[cache_key]

        result = run_recall_subagent(
            self.client,
            self.model,
            query,
            self.config.prompt_style,
            self.workspace_dir,
            self.config,
        )

        self._cache[cache_key] = (result, now)
        return result
