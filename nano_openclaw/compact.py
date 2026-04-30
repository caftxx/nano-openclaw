"""Context compaction for nano-openclaw.

Mirrors `src/agents/compaction.ts` — summarizes old conversation history
when approaching token budget limits to keep the context window manageable.

Key concepts:
  1. estimate_tokens(): Approximate token count (4 chars ≈ 1 token)
  2. summarize_history(): Call LLM to generate a concise summary
  3. compact_if_needed(): Check budget and compress if over threshold

This is a simplified version of OpenClaw's compaction for educational purposes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .loop import Message

# Approximation: 4 characters ≈ 1 token (rough average across models)
CHARS_PER_TOKEN = 4

# Default budget threshold (trigger compaction at 80% of budget)
DEFAULT_THRESHOLD_RATIO = 0.8

# Default number of recent turns to preserve (1 turn = user + assistant)
DEFAULT_RECENT_TURNS = 3


def estimate_tokens(messages: list[Message]) -> int:
    """Estimate total tokens in a message history.

    Uses a simple character-based approximation: 4 chars ≈ 1 token.
    This is intentionally simple for educational purposes.
    Real implementations may use tiktoken or model-specific tokenizers.
    """
    total = 0
    for msg in messages:
        for block in msg.content:
            # Handle different block types
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += len(block.get("text", "")) // CHARS_PER_TOKEN
                elif block.get("type") == "tool_use":
                    # Tool use blocks: name + input JSON
                    total += len(block.get("name", "")) // CHARS_PER_TOKEN
                    total += len(str(block.get("input", {}))) // CHARS_PER_TOKEN
                elif block.get("type") == "tool_result":
                    # Tool result blocks: content
                    content = block.get("content", "")
                    if isinstance(content, str):
                        total += len(content) // CHARS_PER_TOKEN
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                total += len(item.get("text", "")) // CHARS_PER_TOKEN
                elif block.get("type") == "image":
                    # Native Vision path: estimate via base64 data length.
                    # base64_len / 4 ≈ raw bytes, which correlates with token cost
                    # (e.g. 1200×800 PNG ≈ 6000 base64 chars / 4 ≈ 1500 tokens).
                    source = block.get("source", {})
                    total += len(source.get("data", "")) // CHARS_PER_TOKEN
            else:
                # Fallback for unexpected types
                total += len(str(block)) // CHARS_PER_TOKEN
    return total


def _format_messages_for_summary(messages: list[Message]) -> str:
    """Format messages into a readable string for summarization."""
    lines = []
    for msg in messages:
        role = msg.role.upper()
        for block in msg.content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    lines.append(f"[{role}]: {block.get('text', '')}")
                elif block.get("type") == "tool_use":
                    lines.append(f"[{role}]: Called tool '{block.get('name', 'unknown')}'")
                elif block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, str):
                        lines.append(f"[{role}]: Tool result: {content[:200]}...")
                    else:
                        lines.append(f"[{role}]: Tool result: (complex content)")
    return "\n".join(lines)


def summarize_history(
    messages: list[Message],
    *,
    client: Any,
    model: str,
    api: str = "anthropic",
    max_tokens: int = 1024,
) -> str:
    """Call LLM to generate a concise summary of conversation history.

    Preserves:
    - Active tasks and their status
    - Decisions made
    - Important identifiers (file paths, URLs, UUIDs)
    - Unresolved questions or TODOs
    """
    if not messages:
        return ""

    formatted = _format_messages_for_summary(messages)

    summary_prompt = f"""Summarize the following conversation history concisely.
Preserve:
- Active tasks and their current status
- Important decisions made
- Key identifiers (file paths, URLs, UUIDs, function names)
- Unresolved questions or TODOs

Conversation:
{formatted}

Reply with the summary only, no meta-commentary."""

    # Use non-streaming API for summarization
    if api == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        # Extract text from response
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                return block.text
        return ""
    elif api == "openai":
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        return response.choices[0].message.content or ""
    else:
        raise ValueError(f"Unsupported api for summarization: {api!r}")


def compact_if_needed(
    history: list[Message],
    *,
    budget: int,
    client: Any,
    model: str,
    api: str = "anthropic",
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    recent_turns: int = DEFAULT_RECENT_TURNS,
) -> tuple[list[Message], str | None]:
    """Check token budget and compact history if over threshold.

    Args:
        history: The conversation history (will be modified in place if compaction occurs)
        budget: Maximum token budget for the context
        client: LLM client (anthropic.Anthropic or openai.OpenAI)
        model: Model identifier for summarization
        api: API type ("anthropic" or "openai")
        threshold_ratio: Trigger compaction when tokens exceed this ratio of budget
        recent_turns: Number of recent turns to preserve (1 turn = user + assistant)

    Returns:
        Tuple of (possibly modified history, summary if compaction occurred else None)
    """
    current_tokens = estimate_tokens(history)
    threshold = int(budget * threshold_ratio)

    if current_tokens < threshold:
        # Under budget, no compaction needed
        return history, None

    # Calculate how many messages to keep (recent_turns * 2 = user + assistant pairs)
    keep_count = recent_turns * 2

    if len(history) <= keep_count:
        # Not enough history to compact, return as-is
        return history, None

    # Split: older messages to summarize, recent messages to keep
    older_messages = history[:-keep_count]
    recent_messages = history[-keep_count:]

    if not older_messages:
        return history, None

    # Generate summary of older messages
    summary = summarize_history(
        older_messages,
        client=client,
        model=model,
        api=api,
    )

    # Create summary message to prepend
    # Import here to avoid circular import at runtime
    from .loop import Message

    summary_msg: Message = Message(
        role="user",
        content=[{
            "type": "text",
            "text": f"[Previous conversation summary]\n{summary}",
        }],
    )

    # Rebuild history: summary + recent messages
    # Note: we modify the list in place for caller consistency
    history.clear()
    history.append(summary_msg)
    history.extend(recent_messages)

    return history, summary


def should_compact(
    history: list[Message],
    *,
    budget: int,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
) -> bool:
    """Check if compaction should be triggered without actually compacting.

    Useful for pre-emptive checks or logging.
    """
    current_tokens = estimate_tokens(history)
    threshold = int(budget * threshold_ratio)
    return current_tokens >= threshold
