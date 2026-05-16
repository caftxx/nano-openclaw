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

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .logger import get_logger

if TYPE_CHECKING:
    from .config.types import MemoryFlushConfig
    from .loop import Message

logger = get_logger(__name__)

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

# How long to skip the summarizer LLM after a failure. Pre-prune still runs
# in cooldown; only the LLM call is gated.
_SUMMARY_FAILURE_COOLDOWN_S = 60.0

# Reasonable cap for the structured summary's max_tokens. Hermes scales the
# budget with content size and caps at 12K; nano keeps it simpler — the
# template is verbose enough that 4K covers most realistic compactions and
# nothing in nano needs the full 12K extreme.
_SUMMARY_MAX_TOKENS_DEFAULT = 4096


@dataclass
class CompactionState:
    """Per-session compaction tracking carried alongside ``AgentSession``.

    Three pieces of state survive across iterations of one conversation:

      * ``previous_summary`` — last summary text produced for this session.
        Passed back to ``summarize_history`` so the model UPDATES the prior
        summary instead of rewriting from scratch. Preserves continuity
        across multiple compactions in a long conversation.
      * ``summary_cooldown_until`` — ``time.monotonic()`` deadline. While
        in cooldown ``compact_if_needed`` still runs the local prune pass
        but skips the LLM summary call (drops middle turns with a
        placeholder note instead).
      * ``last_summary_error`` — short human-readable failure reason for
        introspection / logging.

    All in-process state; reset when the session is reset.
    """

    previous_summary: str | None = None
    summary_cooldown_until: float = 0.0
    last_summary_error: str | None = None

    def in_cooldown(self) -> bool:
        return time.monotonic() < self.summary_cooldown_until


# ---------------------------------------------------------------------------
# Structured summary template (Stage 3)
#
# Replaces the original 5-line freeform prompt with a typed checkpoint
# format that survives iterative updates and keeps the most important field
# ("## Active Task") at the top. Trimmed from hermes
# ``agent/context_compressor.py:840-913`` — fields irrelevant to nano (e.g.
# Working Directory / branch) are dropped.
# ---------------------------------------------------------------------------


_SUMMARIZER_PREAMBLE = (
    "You are a summarization agent creating a context checkpoint. "
    "Treat the conversation turns below as source material for a compact "
    "record of prior work. Produce only the structured summary; do not add "
    "a greeting, preamble, or prefix. "
    "Write the summary in the same language the user was using in the "
    "conversation — do not translate or switch to English. "
    "NEVER include API keys, tokens, passwords, secrets, credentials, or "
    "connection strings in the summary — replace any that appear with "
    "[REDACTED]. Note that the user had credentials present, but do not "
    "preserve their values."
)


_SUMMARY_TEMPLATE = """## Active Task
[THE SINGLE MOST IMPORTANT FIELD. Copy the user's most recent request or
task assignment verbatim — the exact words they used. If multiple tasks
were requested and only some are done, list only the ones NOT yet completed.
Continuation should pick up exactly here. If no outstanding task exists,
write "None."]

## Goal
[What the user is trying to accomplish overall]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target,
and outcome. Format each as: N. ACTION target — outcome [tool: name].
Be specific with file paths, commands, line numbers, and results.]

## In Progress
[Work currently underway — what was being done when compaction fired]

## Blocked
[Any blockers, errors, or issues not yet resolved. Include exact error
messages.]

## Key Decisions
[Important technical decisions and WHY they were made]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer
so it is not repeated]

## Pending User Asks
[Questions or requests from the user that have NOT yet been answered or
fulfilled. If none, write "None."]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Remaining Work
[What remains to be done — framed as context, not instructions]

## Critical Context
[Specific values, error messages, configuration details that would be lost
without explicit preservation. NEVER include API keys, tokens, passwords,
or credentials — write [REDACTED] instead.]

Write only the summary body. Do not include any preamble or prefix."""


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


# ---------------------------------------------------------------------------
# Pre-summarization prune (Stage 2)
#
# Before spending an LLM call to summarize the older region, do a cheap local
# pass to (1) dedupe identical tool outputs, (2) replace large old tool
# results with a one-line tool-aware summary, (3) strip image blocks from
# old tool results, and (4) shrink large tool_use input dicts. Often this is
# enough to bring the conversation back under threshold and skip the LLM
# summary call entirely.
# ---------------------------------------------------------------------------


_PRUNE_RESULT_THRESHOLD_CHARS = 200       # tool_result text shorter than this is left alone
_PRUNE_INPUT_THRESHOLD_CHARS = 500        # tool_use input JSON shorter than this is left alone
_PRUNE_INPUT_LEAF_HEAD_CHARS = 200        # max chars per string leaf inside a shrunk input
_DUPLICATE_TOOL_RESULT_TEXT = "[Duplicate tool output — same content as a more recent call]"
_IMAGE_REMOVED_PLACEHOLDER = "[image removed to save context]"

# Tools whose tool_result / tool_use bodies are safe to microcompact (summarize
# or truncate). Limited to read-style I/O whose output is reproducible by
# re-running the call and whose payload is not load-bearing state.
#
# Tools deliberately EXCLUDED (preserved verbatim by passes 2 and 3):
#   - ``todo``: maintains the live task list; truncation drops the plan
#   - ``apply_patch``: surfaces conflict / hunk feedback the model must read
#   - ``skill`` / ``skill_install``: side-effectful, may carry install state
#   - ``sessions_spawn`` / ``subagents``: sub-agent ids + status payloads
#   - ``current_time`` / ``session_status``: short, already informative
#   - Any unrecognized name (MCP / skill / plugin tools): unknown semantics
#
# Mirrors claude-code's ``COMPACTABLE_TOOLS`` in
# ``src/services/compact/microCompact.ts``.
_COMPACTABLE_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "list_dir",
    "bash",
    "web_fetch",
    "web_search",
    "memory_get",
    "memory_search",
})


def _tool_result_text(block: dict[str, Any]) -> str:
    """Concatenated text from a tool_result block's content list (or "")."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _strip_image_blocks_from_tool_result(content: Any) -> tuple[Any, bool]:
    """Replace image blocks inside a tool_result content list with a text
    placeholder. Returns (new_content, had_image)."""
    if not isinstance(content, list):
        return content, False
    had_image = False
    out: list[Any] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            had_image = True
            out.append({"type": "text", "text": _IMAGE_REMOVED_PLACEHOLDER})
        else:
            out.append(block)
    return out, had_image


def _summarize_tool_result(tool_name: str, tool_input: dict[str, Any], content: str) -> str:
    """Replace a verbose tool_result with a 1-line informative summary.

    Tool list mirrors nano's built-in registry (``tools.py``). Unknown tools
    fall through to a generic format that still names the tool and the
    leading args.
    """
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "bash":
        cmd = tool_input.get("command", "") or ""
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        # nano's bash tool may include exit_code in JSON-shaped output; pluck
        # it out if present, otherwise just report line count.
        exit_match = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
        exit_code = exit_match.group(1) if exit_match else "?"
        return f"[bash] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"

    if tool_name == "read_file":
        path = tool_input.get("path", "?")
        offset = tool_input.get("offset", 1)
        return f"[read_file] read {path} from line {offset} ({content_len:,} chars)"

    if tool_name == "write_file":
        path = tool_input.get("path", "?")
        body = tool_input.get("content", "") or ""
        wlines = body.count("\n") + 1 if body else 0
        return f"[write_file] wrote {path} ({wlines} lines)"

    if tool_name == "list_dir":
        path = tool_input.get("path", ".")
        return f"[list_dir] {path} ({line_count} entries)"

    if tool_name == "web_search":
        query = tool_input.get("query", "?")
        return f"[web_search] '{query}' ({content_len:,} chars result)"

    if tool_name == "web_fetch":
        url = tool_input.get("url", "?")
        return f"[web_fetch] {url} ({content_len:,} chars)"

    if tool_name == "skill":
        name = tool_input.get("name", "?")
        return f"[skill] {name} ({content_len:,} chars)"

    if tool_name == "skill_install":
        name = tool_input.get("name", "?")
        return f"[skill_install] {name}"

    if tool_name == "memory_get":
        target = tool_input.get("path", tool_input.get("name", "?"))
        return f"[memory_get] {target} ({content_len:,} chars)"

    if tool_name == "memory_search":
        query = tool_input.get("query", "?")
        return f"[memory_search] '{query}' ({content_len:,} chars)"

    if tool_name == "current_time":
        return "[current_time] queried"

    if tool_name == "session_status":
        return "[session_status] queried"

    # Generic fallback: name + first 2 args, capped.
    args_str = ""
    for k, v in list(tool_input.items())[:2]:
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:40] + "..."
        args_str += f" {k}={sv}"
    return f"[{tool_name}]{args_str} ({content_len:,} chars)"


def _truncate_tool_use_input(inp: dict[str, Any], head_chars: int = _PRUNE_INPUT_LEAF_HEAD_CHARS) -> dict[str, Any]:
    """Recursively shrink long string leaves in a tool_use input dict.

    Keeps the dict structure intact (so the LLM can still parse it as a
    valid call) but caps individual string values. Numbers, booleans, and
    short strings pass through unchanged.

    nano's ``tool_use.input`` is already a dict (decoded from JSON in
    ``loop.py:1419``), so unlike hermes we don't need to parse first.
    """

    def _shrink(obj: Any) -> Any:
        if isinstance(obj, str):
            if len(obj) > head_chars:
                return obj[:head_chars] + "...[truncated]"
            return obj
        if isinstance(obj, dict):
            return {k: _shrink(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_shrink(v) for v in obj]
        return obj

    return _shrink(inp)


def _prune_old_tool_results(
    history: list[Message],
    *,
    protect_tail_count: int = 6,
) -> int:
    """Cheap, no-LLM pre-pass that replaces old verbose tool I/O with
    informative shorthand. Modifies ``history`` in place; returns the
    number of blocks pruned (not including dedupe-only and input-only
    tweaks, which are reported but not included in the count beyond the
    hash dedupe pass).

    Three passes:

      1. Dedupe identical tool_result text bodies (oldest duplicates →
         back-reference text). This survives even inside the protected
         tail because it's safe and cheap.
      2. For tool_result blocks OUTSIDE the protected tail, replace large
         text bodies with a 1-line ``_summarize_tool_result`` and strip
         image blocks.
      3. For tool_use blocks OUTSIDE the protected tail with large input
         dicts, recursively shrink long string leaves while keeping the
         JSON structure intact.

    Mirrors hermes ``agent/context_compressor.py:_prune_old_tool_results``
    but adapted to nano's Anthropic-native dict shape (no OpenAI
    tool_calls list, ``tool_use.input`` is already a dict).
    """
    n = len(history)
    if n == 0:
        return 0

    # Build tool_use_id -> (tool_name, tool_input) lookup so Pass 2 can
    # generate a tool-aware summary instead of a generic placeholder.
    call_id_to_tool: dict[str, tuple[str, dict[str, Any]]] = {}
    for msg in history:
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            tid = block.get("id")
            if not tid:
                continue
            raw_input = block.get("input")
            tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            call_id_to_tool[tid] = (block.get("name", "unknown"), tool_input)

    prune_until = max(0, n - protect_tail_count)
    pruned = 0

    # Pass 1: dedupe identical tool_result text bodies (whole history).
    # Walking newest-first means we keep the most recent full copy and
    # replace older duplicates with a back-reference.
    seen_hashes: set[str] = set()
    for i in range(n - 1, -1, -1):
        msg = history[i]
        if msg.role != "user":
            continue
        for j, block in enumerate(msg.content):
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            text = _tool_result_text(block)
            if len(text) < _PRUNE_RESULT_THRESHOLD_CHARS:
                continue
            h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]
            if h in seen_hashes:
                msg.content[j] = {
                    **block,
                    "content": [{"type": "text", "text": _DUPLICATE_TOOL_RESULT_TEXT}],
                }
                pruned += 1
            else:
                seen_hashes.add(h)

    # Pass 2: replace large tool_result text bodies (older region only).
    # Also strip image blocks from old tool_results — they survive every
    # compaction otherwise (base64 PNGs are several KB each).
    #
    # Whitelist gate: only summarize / image-strip results whose originating
    # tool_use is in ``_COMPACTABLE_TOOL_NAMES``. Non-whitelisted (e.g.
    # ``todo``, ``apply_patch``) and orphan tool_results are preserved
    # verbatim — their bodies carry state the model needs intact on recovery.
    for i in range(prune_until):
        msg = history[i]
        if msg.role != "user":
            continue
        for j, block in enumerate(msg.content):
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            tid = block.get("tool_use_id", "")
            lookup = call_id_to_tool.get(tid)
            if lookup is None:
                # Orphan tool_result — cannot identify originating tool, skip
                # to stay safe (e.g. todo output would lose its task list).
                continue
            tool_name, tool_input = lookup
            if tool_name not in _COMPACTABLE_TOOL_NAMES:
                continue
            # Strip images regardless of text size (they're always heavy).
            new_content, had_image = _strip_image_blocks_from_tool_result(
                block.get("content")
            )
            if had_image:
                block = {**block, "content": new_content}
                msg.content[j] = block
                pruned += 1
            text = _tool_result_text(block)
            if not text:
                continue
            # Skip already-pruned blocks (avoid double-summarizing).
            if text.startswith(_DUPLICATE_TOOL_RESULT_TEXT) or text.startswith("["):
                continue
            if len(text) > _PRUNE_RESULT_THRESHOLD_CHARS:
                summary = _summarize_tool_result(tool_name, tool_input, text)
                msg.content[j] = {
                    **block,
                    "content": [{"type": "text", "text": summary}],
                }
                pruned += 1

    # Pass 3: shrink large tool_use input dicts (older region only).
    # naive truncation breaks JSON; we recurse into the structure and only
    # cap long string leaves.
    #
    # Whitelist gate: skip tool_use blocks whose name is not in
    # ``_COMPACTABLE_TOOL_NAMES``. Truncating e.g. a ``todo`` input would
    # erase the task list the model wrote.
    for i in range(prune_until):
        msg = history[i]
        if msg.role != "assistant":
            continue
        for j, block in enumerate(msg.content):
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            if block.get("name") not in _COMPACTABLE_TOOL_NAMES:
                continue
            inp = block.get("input")
            if not isinstance(inp, dict):
                continue
            inp_size = len(json.dumps(inp, ensure_ascii=False))
            if inp_size <= _PRUNE_INPUT_THRESHOLD_CHARS:
                continue
            new_inp = _truncate_tool_use_input(inp)
            if new_inp != inp:
                msg.content[j] = {**block, "input": new_inp}

    return pruned


def _build_summary_prompt(
    formatted_conversation: str,
    *,
    previous_summary: str | None = None,
) -> str:
    """Assemble the LLM prompt used by ``summarize_history``.

    Two modes:
      * ``previous_summary is None`` — first compaction, summarize from scratch.
      * ``previous_summary is not None`` — iterative update; the model is
        asked to UPDATE the prior summary instead of rewriting, preserving
        Completed Actions numbering and Resolved Questions across passes.

    Mirrors hermes ``agent/context_compressor.py:899-925``.
    """
    if previous_summary:
        return (
            f"{_SUMMARIZER_PREAMBLE}\n\n"
            "You are updating a context compaction summary. A previous "
            "compaction produced the summary below. New conversation turns "
            "have occurred since then and need to be incorporated.\n\n"
            "PREVIOUS SUMMARY:\n"
            f"{previous_summary}\n\n"
            "NEW TURNS TO INCORPORATE:\n"
            f"{formatted_conversation}\n\n"
            "Update the summary using this exact structure. PRESERVE all "
            "existing information that is still relevant. ADD new completed "
            "actions to the numbered list (continue numbering). Move items "
            "from \"In Progress\" to \"Completed Actions\" when done. Move "
            "answered questions to \"Resolved Questions\". Remove information "
            "only if it is clearly obsolete. CRITICAL: Update \"## Active "
            "Task\" to reflect the user's most recent unfulfilled request — "
            "this is the most important field for task continuity.\n\n"
            f"{_SUMMARY_TEMPLATE}"
        )
    return (
        f"{_SUMMARIZER_PREAMBLE}\n\n"
        "Create a structured checkpoint summary for the conversation after "
        "earlier turns are compacted. The summary should preserve enough "
        "detail for continuity without re-reading the original turns.\n\n"
        "TURNS TO SUMMARIZE:\n"
        f"{formatted_conversation}\n\n"
        "Use this exact structure:\n\n"
        f"{_SUMMARY_TEMPLATE}"
    )


async def summarize_history(
    messages: list[Message],
    *,
    client: Any,
    model: str,
    api: str = "anthropic",
    max_tokens: int = _SUMMARY_MAX_TOKENS_DEFAULT,
    previous_summary: str | None = None,
) -> str:
    """Call LLM to generate a structured checkpoint summary.

    Produces a typed multi-section summary (``## Active Task`` / ``## Goal``
    / ``## Completed Actions`` / ...) — see ``_SUMMARY_TEMPLATE``. When
    ``previous_summary`` is supplied the model is asked to UPDATE that
    summary rather than rewrite from scratch, preserving info across
    multiple compactions.

    Raises on API failure — the caller (``compact_if_needed``) catches and
    enters cooldown.
    """
    if not messages:
        return ""

    formatted = _format_messages_for_summary(messages)
    summary_prompt = _build_summary_prompt(formatted, previous_summary=previous_summary)

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


async def _safe_summarize_history(
    messages: list[Message],
    *,
    client: Any,
    model: str,
    api: str,
    state: CompactionState | None,
) -> tuple[str | None, bool]:
    """Wrap ``summarize_history`` with cooldown + failure tracking.

    Returns ``(summary_text_or_none, did_call_llm)``. On success returns
    the summary string. On failure or cooldown returns ``None`` and updates
    ``state.summary_cooldown_until`` / ``state.last_summary_error``.

    The fallback when the LLM is skipped is up to the caller — typically
    inject a placeholder summary message so context is still trimmed.
    """
    if state is not None and state.in_cooldown():
        return None, False

    try:
        previous = state.previous_summary if state is not None else None
        summary = await summarize_history(
            messages,
            client=client,
            model=model,
            api=api,
            previous_summary=previous,
        )
    except Exception as exc:  # noqa: BLE001 — recover from any LLM error
        err_text = str(exc).strip() or exc.__class__.__name__
        if len(err_text) > 220:
            err_text = err_text[:217].rstrip() + "..."
        logger.warning(
            "compact.summarize.failed",
            f"summarize_history raised {type(exc).__name__}: {err_text}; "
            f"entering {_SUMMARY_FAILURE_COOLDOWN_S:.0f}s cooldown",
        )
        if state is not None:
            state.summary_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_S
            state.last_summary_error = err_text
        return None, True

    if state is not None:
        state.previous_summary = summary
        state.last_summary_error = None
        state.summary_cooldown_until = 0.0
    return summary, True


def _fallback_summary_text(
    summary: str | None,
    *,
    dropped_count: int,
    state: CompactionState | None,
) -> str:
    """Return either the LLM-generated summary or a fallback placeholder.

    When ``_safe_summarize_history`` returns ``None`` (cooldown or failure)
    we still trim the conversation, but the summary message body is a short
    note explaining that N earlier turns were dropped without summary so
    the next model turn doesn't pretend continuity that isn't there.
    """
    if summary:
        return summary
    reason = ""
    if state is not None and state.last_summary_error:
        reason = f": {state.last_summary_error}"
    return (
        f"[Note: {dropped_count} earlier turn(s) were dropped without summary "
        f"due to summarizer failure or cooldown{reason}]"
    )


async def compact_if_needed(
    history: list[Message],
    *,
    budget: int,
    client: Any,
    model: str,
    api: str = "anthropic",
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    recent_turns: int = DEFAULT_RECENT_TURNS,
    last_prompt_tokens: int | None = None,
    state: CompactionState | None = None,
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
        last_prompt_tokens: Real total prompt-token count from the previous
            turn's ``MessageEnd.usage`` — i.e. ``input_tokens +
            cache_read_input_tokens + cache_creation_input_tokens``, the
            actual size the model saw. NOT just billable input. When > 0
            this replaces the char-based ``estimate_tokens`` fallback for
            the trigger decision — real counts are ~30% more accurate so
            compaction fires at the right time. Using prompt_total (not
            billable input) is critical when prompt caching is on: input
            alone shrinks dramatically on cached turns, but the model still
            sees the full prompt against context_window — billable-only
            would never trigger compaction on cached sessions.
        state: Optional per-session ``CompactionState``. When provided:
            * ``state.previous_summary`` is passed to the summarizer for
              iterative updates (Stage 3.2)
            * On summary failure ``state.summary_cooldown_until`` /
              ``state.last_summary_error`` are updated so subsequent calls
              skip the LLM but still run local prune + trim (Stage 3.3)

    Returns:
        Tuple of (possibly modified history, summary if compaction occurred else None)
    """
    if last_prompt_tokens is not None and last_prompt_tokens > 0:
        current_tokens = last_prompt_tokens
    else:
        current_tokens = estimate_tokens(history)
    threshold = int(budget * threshold_ratio)

    if current_tokens < threshold:
        return history, None

    # Cheap pre-prune (no LLM call). Replaces verbose old tool I/O with
    # short summaries and dedupes identical results. Often pulls the
    # estimate back under threshold, letting us skip the LLM summary entirely.
    # Use estimate_tokens for the post-prune check — the API hasn't seen
    # the pruned shape yet so last_prompt_tokens is stale.
    keep_count = recent_turns * 2
    _prune_old_tool_results(history, protect_tail_count=keep_count)
    post_prune_tokens = estimate_tokens(history)
    if post_prune_tokens < threshold:
        return history, None
    current_tokens = post_prune_tokens

    # Import here to avoid circular import at runtime
    from .loop import Message

    # When severely over budget, force aggressive compaction. Same path is
    # taken when the history is shorter than the requested tail size.
    # Both must still preserve the user's latest real request (Stage 1.2
    # invariant) — anchor the tail at the last real user message instead of
    # discarding everything into a single summary message.
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

        summary, _called = await _safe_summarize_history(
            older,
            client=client,
            model=model,
            api=api,
            state=state,
        )
        summary_text = _fallback_summary_text(summary, dropped_count=len(older), state=state)
        summary_msg: Message = Message(
            role="user",
            content=[{
                "type": "text",
                "text": f"{SUMMARY_PREFIX}\n{summary_text}",
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

    summary, _called = await _safe_summarize_history(
        older_messages,
        client=client,
        model=model,
        api=api,
        state=state,
    )
    summary_text = _fallback_summary_text(
        summary, dropped_count=len(older_messages), state=state
    )

    summary_msg = Message(
        role="user",
        content=[{
            "type": "text",
            "text": f"{SUMMARY_PREFIX}\n{summary_text}",
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
        len(summary_text or "") // CHARS_PER_TOKEN + estimate_tokens(recent_messages)
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
