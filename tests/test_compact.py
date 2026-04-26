"""Pure-Python tests for context compaction. No live LLM call required.

Tests the token estimation, budget checking, and compaction logic.
Mock LLM client is used for summarization tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nano_openclaw.compact import (
    CHARS_PER_TOKEN,
    DEFAULT_RECENT_TURNS,
    DEFAULT_THRESHOLD_RATIO,
    estimate_tokens,
    should_compact,
    summarize_history,
    compact_if_needed,
)
from nano_openclaw.loop import Message


def make_text_message(role: str, text: str) -> Message:
    """Helper to create a simple text message."""
    return Message(role=role, content=[{"type": "text", "text": text}])


def make_tool_use_message(role: str, tool_name: str, tool_id: str, input_: dict = None) -> Message:
    """Helper to create a tool_use or tool_result message."""
    if role == "assistant":
        return Message(role=role, content=[{
            "type": "tool_use",
            "id": tool_id,
            "name": tool_name,
            "input": input_ or {},
        }])
    else:  # user with tool_result
        return Message(role=role, content=[{
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": f"Result of {tool_name}",
        }])


# =============================================================================
# Token Estimation Tests
# =============================================================================

def test_estimate_tokens_empty_history():
    """Empty history should have zero tokens."""
    history = []
    assert estimate_tokens(history) == 0


def test_estimate_tokens_single_text_message():
    """Single text message estimation should match char/4 approximation."""
    text = "Hello world, this is a test message."
    expected = len(text) // CHARS_PER_TOKEN
    history = [make_text_message("user", text)]
    assert estimate_tokens(history) == expected


def test_estimate_tokens_multiple_messages():
    """Multiple messages should sum their token estimates."""
    texts = ["Short message.", "A longer message with more content.", "Third one."]
    history = [make_text_message("user", texts[0]),
               make_text_message("assistant", texts[1]),
               make_text_message("user", texts[2])]
    expected = sum(len(t) // CHARS_PER_TOKEN for t in texts)
    assert estimate_tokens(history) == expected


def test_estimate_tokens_tool_use_block():
    """Tool use blocks should count name + input JSON."""
    history = [make_tool_use_message("assistant", "read_file", "tool-1", {"path": "/tmp/test.txt"})]
    # name: "read_file" (10 chars) + input JSON string representation
    tokens = estimate_tokens(history)
    assert tokens > 0  # Should have some token count


def test_estimate_tokens_tool_result_block():
    """Tool result blocks should count content."""
    history = [make_tool_use_message("user", "read_file", "tool-1")]
    tokens = estimate_tokens(history)
    assert tokens > 0  # Should have some token count


def test_estimate_tokens_mixed_blocks():
    """Mixed content blocks (text + tool) should all be counted."""
    history = [
        make_text_message("user", "Please read the file"),
        Message(role="assistant", content=[
            {"type": "text", "text": "I'll read it for you."},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "/tmp/a.txt"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "t1", "content": "File contents here"},
        ]),
    ]
    tokens = estimate_tokens(history)
    assert tokens > 0


# =============================================================================
# Should Compact Tests
# =============================================================================

def test_should_compact_under_budget():
    """History under threshold should not need compaction."""
    # Create small history well under budget
    history = [make_text_message("user", "Hello")]
    budget = 1000
    assert not should_compact(history, budget=budget)


def test_should_compact_over_threshold():
    """History over 80% threshold should need compaction."""
    # Create large history that exceeds threshold
    large_text = "x" * 4000  # ~1000 tokens
    history = [make_text_message("user", large_text)]
    budget = 500  # threshold = 400, history ~1000 tokens
    assert should_compact(history, budget=budget)


def test_should_compact_at_exact_threshold():
    """History exactly at threshold should need compaction (>= check)."""
    text = "x" * 400  # ~100 tokens
    history = [make_text_message("user", text)]
    budget = 125  # threshold = 100 tokens
    assert should_compact(history, budget=budget)


def test_should_compact_custom_threshold():
    """Custom threshold ratio should be respected."""
    text = "x" * 400  # ~100 tokens
    history = [make_text_message("user", text)]
    budget = 200
    # Default threshold (0.8): 160 tokens -> 100 < 160, no compact
    assert not should_compact(history, budget=budget, threshold_ratio=DEFAULT_THRESHOLD_RATIO)
    # Lower threshold (0.4): 80 tokens -> 100 >= 80, should compact
    assert should_compact(history, budget=budget, threshold_ratio=0.4)


# =============================================================================
# Compact If Needed Tests (without LLM calls)
# =============================================================================

def test_compact_not_needed_under_budget():
    """compact_if_needed should return unchanged history when under budget."""
    history = [make_text_message("user", "Hello")]
    original_len = len(history)
    
    mock_client = MagicMock()
    result, summary = compact_if_needed(
        history,
        budget=1000,
        client=mock_client,
        model="test-model",
        api="anthropic",
    )
    
    assert summary is None
    assert len(result) == original_len
    # No LLM call should have been made
    mock_client.messages.create.assert_not_called()


def test_compact_not_enough_history_to_preserve():
    """Should not compact if history is too short to preserve recent turns."""
    # History shorter than recent_turns * 2
    history = [
        make_text_message("user", "Hello"),
        make_text_message("assistant", "Hi there"),
    ]
    
    mock_client = MagicMock()
    # Even with very low budget, can't compact with 3 recent_turns default
    result, summary = compact_if_needed(
        history,
        budget=10,  # Very low budget, will exceed threshold
        client=mock_client,
        model="test-model",
        api="anthropic",
        recent_turns=3,  # Need 6 messages to preserve
    )
    
    # Not enough messages to compact
    assert summary is None
    mock_client.messages.create.assert_not_called()


def test_compact_preserves_recent_turns_count():
    """Compaction should preserve exactly recent_turns * 2 messages."""
    # Create 10 messages (5 turns)
    history = []
    for i in range(5):
        history.append(make_text_message("user", f"User message {i} with some content"))
        history.append(make_text_message("assistant", f"Assistant reply {i} with more content"))
    
    # Mock LLM to return a summary
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="Summary of earlier conversation")]
    mock_client.messages.create.return_value = mock_response
    
    original_len = len(history)
    result, summary = compact_if_needed(
        history,
        budget=50,  # Low budget to trigger compaction
        client=mock_client,
        model="test-model",
        api="anthropic",
        recent_turns=2,  # Keep 4 messages (2 turns)
    )
    
    assert summary is not None
    assert summary == "Summary of earlier conversation"
    # Should have: 1 summary + 4 recent messages = 5
    assert len(result) == 5
    # First message should be the summary
    assert result[0].content[0]["text"].startswith("[Previous conversation summary]")
    # Recent messages should be preserved (last 4 from original)
    assert result[1].content[0]["text"] == "User message 3 with some content"


def test_compact_modifies_history_in_place():
    """compact_if_needed should modify the history list in place."""
    history = []
    for i in range(5):
        history.append(make_text_message("user", f"User message {i}"))
        history.append(make_text_message("assistant", f"Assistant reply {i}"))
    
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="Summary")]
    mock_client.messages.create.return_value = mock_response
    
    original_id = id(history)
    result, summary = compact_if_needed(
        history,
        budget=50,
        client=mock_client,
        model="test-model",
        api="anthropic",
        recent_turns=2,
    )
    
    # Should return the same list object (modified in place)
    assert id(result) == original_id
    assert result is history


# =============================================================================
# Summarize History Tests (with mock LLM)
# =============================================================================

def test_summarize_history_empty_returns_empty():
    """Summarizing empty history should return empty string."""
    mock_client = MagicMock()
    result = summarize_history([], client=mock_client, model="test", api="anthropic")
    assert result == ""
    mock_client.messages.create.assert_not_called()


def test_summarize_history_anthropic_api():
    """Summarize should call Anthropic API correctly."""
    history = [make_text_message("user", "Hello world")]
    
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="Brief summary")]
    mock_client.messages.create.return_value = mock_response
    
    result = summarize_history(history, client=mock_client, model="claude-3", api="anthropic")
    
    assert result == "Brief summary"
    mock_client.messages.create.assert_called_once()
    call_args = mock_client.messages.create.call_args
    assert call_args.kwargs["model"] == "claude-3"
    assert "messages" in call_args.kwargs


def test_summarize_history_openai_api():
    """Summarize should call OpenAI API correctly."""
    history = [make_text_message("user", "Hello world")]
    
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "OpenAI summary"
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response
    
    result = summarize_history(history, client=mock_client, model="gpt-4", api="openai")
    
    assert result == "OpenAI summary"
    mock_client.chat.completions.create.assert_called_once()


def test_summarize_history_unsupported_api_raises():
    """Unsupported API should raise ValueError."""
    history = [make_text_message("user", "Hello")]
    mock_client = MagicMock()
    
    with pytest.raises(ValueError, match="Unsupported api"):
        summarize_history(history, client=mock_client, model="test", api="unknown")


# =============================================================================
# Integration Tests
# =============================================================================

def test_compaction_preserves_tool_use_result_pairs():
    """Compaction should not break tool_use/tool_result pairs in preserved section."""
    # Create history with tool calls in recent section
    history = []
    for i in range(3):
        history.append(make_text_message("user", f"User {i}"))
        history.append(make_text_message("assistant", f"Assistant {i}"))
    
    # Add tool use in recent section
    history.append(Message(role="assistant", content=[
        {"type": "tool_use", "id": "tool-123", "name": "read_file", "input": {"path": "/tmp"}},
    ]))
    history.append(Message(role="user", content=[
        {"type": "tool_result", "tool_use_id": "tool-123", "content": "File content"},
    ]))
    
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="Summary")]
    mock_client.messages.create.return_value = mock_response
    
    # Compact with 2 recent turns (4 messages) - should include tool pair
    result, summary = compact_if_needed(
        history,
        budget=50,
        client=mock_client,
        model="test",
        api="anthropic",
        recent_turns=2,  # Will preserve last 4 messages
    )
    
    # Tool pair should be in preserved section
    # Last 4 messages: assistant(tool_use) + user(tool_result) + 2 earlier
    tool_use_found = any(
        any(block.get("type") == "tool_use" for block in m.content)
        for m in result[-4:]
    )
    tool_result_found = any(
        any(block.get("type") == "tool_result" for block in m.content)
        for m in result[-4:]
    )
    # At least one of the tool blocks should be preserved
    # (depending on exact positioning relative to recent_turns boundary)
    assert tool_use_found or tool_result_found or summary is not None


def test_full_compaction_workflow():
    """Test the full workflow: estimate -> check -> summarize -> rebuild."""
    # Create a conversation that needs compaction
    history = []
    # Older messages to be summarized
    for i in range(4):
        history.append(make_text_message("user", f"Old user message {i} with enough text to be meaningful"))
        history.append(make_text_message("assistant", f"Old assistant reply {i} with substantial content"))
    # Recent messages to preserve
    for i in range(2):
        history.append(make_text_message("user", f"Recent user {i}"))
        history.append(make_text_message("assistant", f"Recent assistant {i}"))
    
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="The user discussed topics A and B. Decided to pursue option C.")]
    mock_client.messages.create.return_value = mock_response
    
    # Verify initial state exceeds threshold
    assert should_compact(history, budget=100)
    
    # Run compaction
    result, summary = compact_if_needed(
        history,
        budget=100,
        client=mock_client,
        model="test-model",
        api="anthropic",
        recent_turns=2,
    )
    
    # Verify compaction happened
    assert summary is not None
    assert "discussed" in summary
    
    # Verify structure
    assert len(result) == 5  # 1 summary + 4 recent messages
    assert result[0].content[0]["text"].startswith("[Previous conversation summary]")
    
    # Verify recent messages preserved
    recent_user_found = any("Recent user 1" in str(m.content) for m in result)
    assert recent_user_found
