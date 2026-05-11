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
    from .config.types import MemoryFlushConfig
    from .loop import Message

# Approximation: 4 characters ≈ 1 token (rough average across models)
CHARS_PER_TOKEN = 4

# Default budget threshold (trigger compaction at 80% of budget)
DEFAULT_THRESHOLD_RATIO = 0.8

# Default number of recent turns to preserve (1 turn = user + assistant)
DEFAULT_RECENT_TURNS = 3

# Strong prefix prepended to every compaction summary message. Without this
# the model often re-answers questions that appear inside the summary text.
SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. Treat it as background reference, NOT as "
    "active instructions. Do NOT answer questions or fulfill requests "
    "mentioned in this summary; they were already addressed. "
    "IMPORTANT: Your persistent memory (MEMORY.md, USER.md, AGENTS.md) "
    "in the system prompt is ALWAYS authoritative — never ignore or "
    "deprioritize memory content due to this compaction note. "
    "Respond ONLY to the latest user message that appears AFTER this "
    "summary. The current session state (files, config, etc.) may "
    "reflect work described here — avoid repeating it:"
)
# Older summaries on disk used this prefix; recognized so re-loaded transcripts
# don't get a doubled-up prefix on subsequent compactions.
LEGACY_SUMMARY_PREFIX = "[Previous conversation summary]"


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


# ---------------------------------------------------------------------------
# Boundary-alignment + tool-pair integrity helpers
#
# These keep compaction from producing message lists that the Anthropic API
# rejects as malformed (orphan tool_use without tool_result, or vice versa)
# and from accidentally compressing the user's most recent request.
# ---------------------------------------------------------------------------


def _is_real_user_message(msg: Message) -> bool:
    """True if this is genuine user input, not a tool_result reply.

    In nano's transcript user-role messages serve double duty:
      - actual user input (text / image / external_content blocks)
      - tool_result batch reply from the loop

    Compaction's "last user message" anchor must look at real input only;
    otherwise an automated tool_result reply hides the user's request and
    pulls cut_idx to the wrong place.
    """
    if msg.role != "user":
        return False
    return any(
        isinstance(b, dict) and b.get("type") != "tool_result"
        for b in msg.content
    )


def _is_tool_result_reply(msg: Message) -> bool:
    """True if every content block in this user message is a tool_result.

    A bare tool_result-only user message is part of an assistant tool group
    (assistant tool_use → user tool_result), not a standalone turn.
    """
    if msg.role != "user" or not msg.content:
        return False
    return all(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in msg.content
    )


def _align_boundary_backward(messages: list[Message], idx: int) -> int:
    """Pull cut boundary back to keep an assistant tool_use group intact.

    If ``messages[idx]`` is a tool_result-only reply, the matching
    assistant tool_use lives at ``idx - 1``. Letting the cut split between
    them produces an orphan tool_result in the tail (Anthropic 400) — slide
    the cut back so both go together.
    """
    if idx <= 0 or idx >= len(messages):
        return idx
    if not _is_tool_result_reply(messages[idx]):
        return idx
    prev = messages[idx - 1]
    if prev.role == "assistant" and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in prev.content
    ):
        return idx - 1
    return idx


def _find_last_real_user_message_idx(
    messages: list[Message], head_end: int
) -> int:
    """Return the index of the last genuine user-input message at or after head_end."""
    for i in range(len(messages) - 1, head_end - 1, -1):
        if _is_real_user_message(messages[i]):
            return i
    return -1


def _ensure_last_user_message_in_tail(
    messages: list[Message], cut_idx: int, head_end: int = 0
) -> int:
    """Guarantee the most recent user request lives in the protected tail.

    Without this, a long agent self-run after the user's request can push
    the user message into the compressed middle. The summary then describes
    the request as historical context and SUMMARY_PREFIX tells the next
    model to reply only to user messages *after* the summary — net effect:
    the agent silently drops the user's latest request.

    Mirrors hermes ``agent/context_compressor.py:_ensure_last_user_message_in_tail``
    (issue #10896) but only treats real-input user messages as anchors so
    tool_result replies don't count.
    """
    last = _find_last_real_user_message_idx(messages, head_end)
    if last < 0 or last >= cut_idx:
        return cut_idx
    if last <= head_end:
        # Last real user msg sits in (or is) the head region; pulling cut_idx
        # back would either invade head or leave nothing to summarize. Leave
        # cut_idx alone — the user msg will be reflected in the summary.
        # Stage 3 will add proper head protection so the user msg can be
        # preserved verbatim instead of summarized.
        return cut_idx
    return last


def _sanitize_tool_pairs(history: list[Message]) -> None:
    """Repair orphan tool_use / tool_result pairs in place.

    After compaction a tool_use block may survive with its matching
    tool_result missing, or a tool_result may reference a tool_use that
    was summarized away. Anthropic rejects either with HTTP 400.

    Two passes:
      1. Drop tool_result blocks whose tool_use_id has no surviving tool_use.
         If a user message becomes empty as a result, drop the message.
      2. For each surviving tool_use without a matching tool_result, insert
         a stub tool_result user message immediately after the parent
         assistant message.

    Mirrors hermes ``agent/context_compressor.py:_sanitize_tool_pairs``.
    """
    surviving_call_ids: set[str] = set()
    for msg in history:
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if tid:
                    surviving_call_ids.add(tid)

    referenced_ids: set[str] = set()
    for msg in history:
        if msg.role != "user":
            continue
        for block in msg.content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    referenced_ids.add(tid)

    # Pass 1: drop orphan tool_result blocks; drop the message if it empties.
    orphan_results = referenced_ids - surviving_call_ids
    if orphan_results:
        i = 0
        while i < len(history):
            msg = history[i]
            if msg.role != "user":
                i += 1
                continue
            new_content = [
                b
                for b in msg.content
                if not (
                    isinstance(b, dict)
                    and b.get("type") == "tool_result"
                    and b.get("tool_use_id") in orphan_results
                )
            ]
            if not new_content:
                history.pop(i)
                continue
            if len(new_content) != len(msg.content):
                msg.content = new_content
            i += 1

    # Pass 2: stub-result for orphan tool_use blocks. Group by parent
    # assistant index so multiple tool_uses in one assistant turn share a
    # single user message (matches loop.py's batched dispatch shape).
    missing_results = surviving_call_ids - referenced_ids
    if not missing_results:
        return

    # Deferred import: loop.py imports from this module, so a top-level
    # `from .loop import Message` would be a circular import.
    from .loop import Message as _Message

    insertions: list[tuple[int, Any]] = []
    for idx, msg in enumerate(history):
        if msg.role != "assistant":
            continue
        stub_blocks: list[dict[str, Any]] = []
        for block in msg.content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            tid = block.get("id")
            if tid in missing_results:
                stub_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": "[Result from earlier conversation — see context summary above]",
                })
        if stub_blocks:
            insertions.append((idx, _Message(role="user", content=stub_blocks)))

    # Apply insertions back-to-front so earlier indices stay valid.
    for idx, stub_msg in reversed(insertions):
        history.insert(idx + 1, stub_msg)


async def summarize_history(
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
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                return block.text
        return ""
    elif api == "openai":
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        return response.choices[0].message.content or ""
    else:
        raise ValueError(f"Unsupported api for summarization: {api!r}")


async def compact_if_needed(
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
        client: LLM client (anthropic.AsyncAnthropic or openai.AsyncOpenAI)
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
        return history, None

    # Import here to avoid circular import at runtime
    from .loop import Message

    # When severely over budget, force aggressive compaction. Same path is
    # taken when the history is shorter than the requested tail size.
    # Both must still preserve the user's latest real request (Stage 1.2
    # invariant) — anchor the tail at the last real user message instead of
    # discarding everything into a single summary message.
    keep_count = recent_turns * 2

    if current_tokens >= budget * 2 or len(history) <= 2 or len(history) <= keep_count:
        last_user_idx = _find_last_real_user_message_idx(history, head_end=0)
        # We need to summarize at least 1 message to make progress; if the
        # only real user msg is at idx 0, we have no choice but to fold
        # everything into a single summary (no tail).
        if last_user_idx >= 1:
            cut_idx = _align_boundary_backward(history, last_user_idx)
            older = history[:cut_idx]
            recent = history[cut_idx:]
        else:
            older = list(history)
            recent = []

        summary = await summarize_history(
            older,
            client=client,
            model=model,
            api=api,
        )
        summary_msg: Message = Message(
            role="user",
            content=[{
                "type": "text",
                "text": f"{SUMMARY_PREFIX}\n{summary}",
            }],
        )
        history.clear()
        history.append(summary_msg)
        history.extend(recent)
        _sanitize_tool_pairs(history)
        return history, summary

    # Split: older messages to summarize, recent messages to keep.
    # Adjust the cut so we don't (a) split a tool_use/tool_result group and
    # (b) accidentally compress the user's most recent request into the
    # summary. Without these adjustments compaction can produce malformed
    # message lists (Anthropic 400) or silently drop the user's latest ask.
    cut_idx = len(history) - keep_count
    cut_idx = _align_boundary_backward(history, cut_idx)
    cut_idx = _ensure_last_user_message_in_tail(history, cut_idx, head_end=0)
    # The pull-back may have just moved cut_idx onto a real user message;
    # re-align in case there's still a tool group immediately before it.
    cut_idx = _align_boundary_backward(history, cut_idx)

    older_messages = history[:cut_idx]
    recent_messages = history[cut_idx:]

    summary = await summarize_history(
        older_messages,
        client=client,
        model=model,
        api=api,
    )

    summary_msg = Message(
        role="user",
        content=[{
            "type": "text",
            "text": f"{SUMMARY_PREFIX}\n{summary}",
        }],
    )

    history.clear()
    history.append(summary_msg)
    history.extend(recent_messages)

    # Verify: if still over budget, force aggressive compaction.
    # The trim must NOT drop the user's latest request (Stage 1.2 invariant)
    # and must NOT split a tool_use/tool_result group (Stage 1.1 invariant).
    # Exclude SUMMARY_PREFIX from the trim trigger: it's ~150 tokens of fixed
    # boilerplate, not real conversation content. Counting it would make the
    # trim fire on prefix size alone.
    remaining_tokens = (
        len(summary or "") // CHARS_PER_TOKEN + estimate_tokens(recent_messages)
    )
    if remaining_tokens >= threshold:
        # Find the last real user message inside recent_messages so we never
        # trim past it. Fall back to keeping the last 2 messages otherwise.
        last_user_rel = _find_last_real_user_message_idx(recent_messages, head_end=0)
        default_keep_start = max(0, len(recent_messages) - 2)
        if last_user_rel >= 0:
            keep_start = min(last_user_rel, default_keep_start)
        else:
            keep_start = default_keep_start
        # If the resulting boundary would split a tool group, slide it back.
        keep_start = _align_boundary_backward(recent_messages, keep_start)
        aggressive_keep = recent_messages[keep_start:]
        history.clear()
        history.append(summary_msg)
        history.extend(aggressive_keep)

    # Final safety net: even with boundary alignment, sequences of dropped
    # tool_use/tool_result blocks across the cut can leave orphans. Repair
    # in place before handing the list back to the loop.
    _sanitize_tool_pairs(history)

    return history, summary


def should_compact(
    history: list[Message],
    *,
    budget: int,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
) -> bool:
    """Check if compaction should be triggered without actually compacting."""
    current_tokens = estimate_tokens(history)
    threshold = int(budget * threshold_ratio)
    return current_tokens >= threshold


def should_run_memory_flush(
    current_tokens: int,
    context_window: int,
    config: "MemoryFlushConfig",
    already_flushed: bool,
) -> bool:
    """Check if a pre-compaction memory flush should run."""
    if already_flushed or not config.enabled or context_window <= 0:
        return False
    threshold = max(0, context_window - config.reserveTokensFloor - config.softThresholdTokens)
    if threshold <= 0:
        return False
    return current_tokens >= threshold
