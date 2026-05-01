"""Tests for session/paths.py.

Tests session path resolution.
Mirrors openclaw's src/config/sessions/paths.ts logic.
"""

import pytest
from pathlib import Path

from nano_openclaw.session.paths import (
    DEFAULT_AGENT_ID,
    resolve_agent_sessions_dir,
    resolve_session_store_path,
    resolve_session_transcript_path,
)


# =============================================================================
# Constants Tests
# =============================================================================

class TestConstants:
    def test_default_agent_id(self):
        """DEFAULT_AGENT_ID is 'default'."""
        assert DEFAULT_AGENT_ID == "default"


# =============================================================================
# resolve_agent_sessions_dir Tests
# =============================================================================

class TestResolveAgentSessionsDir:
    def test_default_agent(self, tmp_path):
        """Default agent sessions dir is {stateDir}/agents/default/sessions."""
        state_dir = tmp_path / ".openclaw"
        sessions_dir = resolve_agent_sessions_dir(state_dir)
        assert sessions_dir == state_dir / "agents" / "default" / "sessions"

    def test_explicit_agent_id(self, tmp_path):
        """Explicit agent ID creates agent-specific dir."""
        state_dir = tmp_path / ".openclaw"
        sessions_dir = resolve_agent_sessions_dir(state_dir, "coder")
        assert sessions_dir == state_dir / "agents" / "coder" / "sessions"

    def test_custom_agent_id(self, tmp_path):
        """Custom agent IDs work correctly."""
        state_dir = tmp_path / ".openclaw"
        for agent_id in ["analyst", "writer", "reviewer"]:
            sessions_dir = resolve_agent_sessions_dir(state_dir, agent_id)
            assert sessions_dir == state_dir / "agents" / agent_id / "sessions"

    def test_none_uses_default(self, tmp_path):
        """None agent_id defaults to 'default'."""
        state_dir = tmp_path / ".openclaw"
        sessions_dir = resolve_agent_sessions_dir(state_dir, None)
        assert sessions_dir == state_dir / "agents" / "default" / "sessions"

    def test_empty_string_uses_default(self, tmp_path):
        """Empty string agent_id defaults to 'default'."""
        state_dir = tmp_path / ".openclaw"
        sessions_dir = resolve_agent_sessions_dir(state_dir, "")
        # Empty string is falsy, should use default
        assert sessions_dir == state_dir / "agents" / "default" / "sessions"

    def test_path_structure_is_correct(self, tmp_path):
        """Sessions dir has correct nested structure."""
        state_dir = tmp_path / "custom-state"
        sessions_dir = resolve_agent_sessions_dir(state_dir, "test-agent")
        
        parts = sessions_dir.parts
        assert "custom-state" in parts
        assert "agents" in parts
        assert "test-agent" in parts
        assert "sessions" in parts[-1]


# =============================================================================
# resolve_session_store_path Tests
# =============================================================================

class TestResolveSessionStorePath:
    def test_returns_sessions_json(self, tmp_path):
        """Returns sessions.json path within sessions dir."""
        sessions_dir = tmp_path / "sessions"
        store_path = resolve_session_store_path(sessions_dir)
        assert store_path == sessions_dir / "sessions.json"

    def test_preserves_parent_directory(self, tmp_path):
        """Store path is directly under sessions dir."""
        sessions_dir = tmp_path / ".openclaw" / "agents" / "default" / "sessions"
        store_path = resolve_session_store_path(sessions_dir)
        assert store_path.name == "sessions.json"
        assert store_path.parent == sessions_dir


# =============================================================================
# resolve_session_transcript_path Tests
# =============================================================================

class TestResolveSessionTranscriptPath:
    def test_returns_jsonl_file(self, tmp_path):
        """Returns {sessionId}.jsonl path."""
        sessions_dir = tmp_path / "sessions"
        session_id = "abc123-def456"
        transcript_path = resolve_session_transcript_path(sessions_dir, session_id)
        assert transcript_path == sessions_dir / f"{session_id}.jsonl"

    def test_uuid_format_session_id(self, tmp_path):
        """Works with UUID format session IDs."""
        sessions_dir = tmp_path / "sessions"
        session_id = "35330e47-b986-48ba-bc8a-1d9d03dc6684"
        transcript_path = resolve_session_transcript_path(sessions_dir, session_id)
        assert transcript_path == sessions_dir / f"{session_id}.jsonl"
        assert transcript_path.suffix == ".jsonl"

    def test_different_sessions_different_paths(self, tmp_path):
        """Different session IDs produce different paths."""
        sessions_dir = tmp_path / "sessions"
        path1 = resolve_session_transcript_path(sessions_dir, "session-1")
        path2 = resolve_session_transcript_path(sessions_dir, "session-2")
        assert path1 != path2
        assert path1.name == "session-1.jsonl"
        assert path2.name == "session-2.jsonl"

    def test_path_is_under_sessions_dir(self, tmp_path):
        """Transcript path is always under sessions directory."""
        sessions_dir = tmp_path / "sessions"
        session_id = "test-session"
        transcript_path = resolve_session_transcript_path(sessions_dir, session_id)
        assert transcript_path.parent == sessions_dir


# =============================================================================
# Integration Tests
# =============================================================================

class TestSessionPathsIntegration:
    def test_complete_path_chain(self, tmp_path):
        """Full path resolution chain works correctly."""
        state_dir = tmp_path / ".openclaw"
        agent_id = "coder"
        
        # Step 1: Resolve sessions directory
        sessions_dir = resolve_agent_sessions_dir(state_dir, agent_id)
        assert sessions_dir == state_dir / "agents" / agent_id / "sessions"
        
        # Step 2: Resolve store path
        store_path = resolve_session_store_path(sessions_dir)
        assert store_path == sessions_dir / "sessions.json"
        
        # Step 3: Resolve transcript path
        session_id = "test-session-123"
        transcript_path = resolve_session_transcript_path(sessions_dir, session_id)
        assert transcript_path == sessions_dir / f"{session_id}.jsonl"
        
        # Verify all paths are related
        assert store_path.parent == sessions_dir
        assert transcript_path.parent == sessions_dir

    def test_multiple_agents_isolated(self, tmp_path):
        """Different agents have isolated session directories."""
        state_dir = tmp_path / ".openclaw"
        agent_ids = ["default", "coder", "analyst"]
        
        sessions_dirs = {}
        for agent_id in agent_ids:
            sessions_dirs[agent_id] = resolve_agent_sessions_dir(state_dir, agent_id)
        
        # All session dirs should be different
        dirs_list = list(sessions_dirs.values())
        assert len(dirs_list) == len(set(dirs_list)), "Session dirs should be unique"
        
        # Verify structure
        for agent_id, sessions_dir in sessions_dirs.items():
            assert agent_id in str(sessions_dir)
            assert sessions_dir.parts[-1] == "sessions"
            assert sessions_dir.parts[-2] == agent_id
