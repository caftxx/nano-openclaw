"""Tests for subagent announce."""

import pytest

from nano_openclaw.subagent.announce import (
    AnnounceEvent,
    build_announce_content,
    format_announce_for_display,
    should_announce,
    create_announce_event,
)
from nano_openclaw.subagent.runner import SubagentRunnerResult
from nano_openclaw.subagent.types import (
    SubagentRunRecord,
    SubagentStatus,
    SubagentCleanupMode,
)


class TestSubagentAnnounce:
    """Test subagent announce mechanism."""
    
    def test_build_announce_content_completed(self):
        """Test building announce content for completed run."""
        record = SubagentRunRecord(
            run_id="abc123",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="test task",
            label="my task",
        )
        result = SubagentRunnerResult(
            run_id="abc123",
            status=SubagentStatus.COMPLETED,
            result_text="task completed successfully",
            elapsed_ms=1500,
        )
        
        content = build_announce_content(result, record)
        
        assert "abc123" in content
        assert "completed" in content
        assert "my task" in content
        assert "1.5s" in content
        assert "task completed successfully" in content
    
    def test_build_announce_content_error(self):
        """Test building announce content for error run."""
        record = SubagentRunRecord(
            run_id="abc",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="test",
        )
        result = SubagentRunnerResult(
            run_id="abc",
            status=SubagentStatus.ERROR,
            error_message="API timeout",
            elapsed_ms=500,
        )
        
        content = build_announce_content(result, record)
        
        assert "failed" in content
        assert "API timeout" in content
    
    def test_build_announce_content_timeout(self):
        """Test building announce content for timeout."""
        record = SubagentRunRecord(
            run_id="abc",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="test",
        )
        result = SubagentRunnerResult(
            run_id="abc",
            status=SubagentStatus.TIMEOUT,
            elapsed_ms=60000,
        )
        
        content = build_announce_content(result, record)
        
        assert "timed out" in content
        assert "1m 0s" in content
    
    def test_format_announce_for_display(self):
        """Test formatting for TUI display."""
        record = SubagentRunRecord(
            run_id="abc",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="search for documentation",
            label="docs search",
        )
        result = SubagentRunnerResult(
            run_id="abc",
            status=SubagentStatus.COMPLETED,
            result_text="found relevant docs in README.md",
            elapsed_ms=2000,
        )
        
        display = format_announce_for_display(result, record)
        
        assert "✓" in display
        assert "docs search" in display
        assert "2.0s" in display
        assert "completed" in display
    
    def test_should_announce(self):
        """Test should_announce logic."""
        completed = SubagentRunnerResult(run_id="x", status=SubagentStatus.COMPLETED)
        error = SubagentRunnerResult(run_id="x", status=SubagentStatus.ERROR)
        timeout = SubagentRunnerResult(run_id="x", status=SubagentStatus.TIMEOUT)
        killed = SubagentRunnerResult(run_id="x", status=SubagentStatus.KILLED)
        
        assert should_announce(completed)
        assert should_announce(error)
        assert should_announce(timeout)
        assert not should_announce(killed)
    
    def test_create_announce_event(self):
        """Test creating announce event."""
        record = SubagentRunRecord(
            run_id="abc",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="test task",
            label="test",
        )
        result = SubagentRunnerResult(
            run_id="abc",
            status=SubagentStatus.COMPLETED,
            result_text="done",
            elapsed_ms=100,
        )
        
        event = create_announce_event(result, record)
        
        assert event.run_id == "abc"
        assert event.status == SubagentStatus.COMPLETED
        assert event.task == "test task"
        assert event.label == "test"
        assert event.result_text == "done"
        assert event.elapsed_ms == 100