"""Tests for subagent types."""

import pytest

from nano_openclaw.subagent.types import (
    SubagentConfig,
    SubagentContextMode,
    SubagentCleanupMode,
    SubagentRunRecord,
    SubagentStatus,
    SpawnParams,
    new_subagent_run_id,
    new_subagent_session_id,
    build_subagent_session_key,
    parse_session_key,
)


class TestSubagentTypes:
    """Test subagent type definitions."""
    
    def test_spawn_params_defaults(self):
        """Test SpawnParams default values."""
        params = SpawnParams(task="test task")
        assert params.task == "test task"
        assert params.label is None
        assert params.model is None
        assert params.thinking is None
        assert params.run_timeout_seconds is None
        assert params.cleanup == SubagentCleanupMode.KEEP
        assert params.context == SubagentContextMode.ISOLATED
    
    def test_spawn_params_to_dict(self):
        """Test SpawnParams serialization."""
        params = SpawnParams(
            task="test",
            label="my task",
            model="claude-sonnet",
            run_timeout_seconds=60,
            cleanup=SubagentCleanupMode.DELETE,
            context=SubagentContextMode.FORK,
        )
        d = params.to_dict()
        assert d["task"] == "test"
        assert d["label"] == "my task"
        assert d["model"] == "claude-sonnet"
        assert d["runTimeoutSeconds"] == 60
        assert d["cleanup"] == "delete"
        assert d["context"] == "fork"
    
    def test_subagent_run_record_defaults(self):
        """Test SubagentRunRecord default values."""
        record = SubagentRunRecord(
            run_id="abc123",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="test",
        )
        assert record.status == SubagentStatus.PENDING
        assert record.outcome is None
        assert record.result_text is None
        assert record.elapsed_ms is None
        assert not record.is_terminal
    
    def test_subagent_run_record_is_terminal(self):
        """Test is_terminal property."""
        record = SubagentRunRecord(
            run_id="abc",
            child_session_key="agent:default:subagent:xyz",
            requester_session_key="agent:default:main",
            task="test",
        )
        assert not record.is_terminal
        
        for status in [SubagentStatus.COMPLETED, SubagentStatus.ERROR, SubagentStatus.TIMEOUT, SubagentStatus.KILLED]:
            record.status = status
            assert record.is_terminal
    
    def test_subagent_run_record_session_id(self):
        """Test session_id extraction."""
        record = SubagentRunRecord(
            run_id="abc",
            child_session_key="agent:default:subagent:xyz789",
            requester_session_key="agent:default:main",
            task="test",
        )
        assert record.session_id == "xyz789"
    
    def test_new_subagent_run_id(self):
        """Test run ID generation."""
        id1 = new_subagent_run_id()
        id2 = new_subagent_run_id()
        assert len(id1) == 8
        assert id1 != id2
    
    def test_new_subagent_session_id(self):
        """Test session ID generation."""
        id1 = new_subagent_session_id()
        id2 = new_subagent_session_id()
        assert len(id1) == 32
        assert id1 != id2
    
    def test_build_subagent_session_key(self):
        """Test session key building."""
        key = build_subagent_session_key("my-agent", "session123")
        assert key == "agent:my-agent:subagent:session123"
    
    def test_parse_session_key_main(self):
        """Test parsing main session key."""
        result = parse_session_key("agent:default:main123")
        assert result["agentId"] == "default"
        assert result["type"] == "main"
        assert result["sessionId"] == "main123"
    
    def test_parse_session_key_subagent(self):
        """Test parsing subagent session key."""
        result = parse_session_key("agent:my-agent:subagent:xyz789")
        assert result["agentId"] == "my-agent"
        assert result["type"] == "subagent"
        assert result["sessionId"] == "xyz789"
    
    def test_parse_session_key_invalid(self):
        """Test parsing invalid session key."""
        result = parse_session_key("invalid-key")
        assert result["agentId"] == "default"
        assert result["sessionId"] == "invalid-key"
    
    def test_subagent_config_defaults(self):
        """Test SubagentConfig defaults."""
        config = SubagentConfig()
        assert config.max_concurrent == 3
        assert config.max_spawn_depth == 1
        assert config.run_timeout_seconds == 0
        assert config.archive_after_minutes == 60
        assert config.model is None
        assert config.thinking is None
    
    def test_subagent_config_custom(self):
        """Test SubagentConfig with custom values."""
        config = SubagentConfig(
            max_concurrent=5,
            run_timeout_seconds=120,
            model="claude-opus",
        )
        assert config.max_concurrent == 5
        assert config.run_timeout_seconds == 120
        assert config.model == "claude-opus"