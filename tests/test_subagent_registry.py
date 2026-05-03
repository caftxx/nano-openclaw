"""Tests for subagent registry."""

import pytest

from nano_openclaw.subagent.registry import (
    SubagentRegistry,
    get_registry,
    reset_registry,
)
from nano_openclaw.subagent.types import (
    SubagentStatus,
)


class TestSubagentRegistry:
    """Test subagent registry."""
    
    def setup_method(self):
        """Reset registry before each test."""
        reset_registry()
    
    def test_register(self):
        """Test registering a run."""
        registry = SubagentRegistry()
        record = registry.register(
            requester_session_key="agent:default:main",
            task="test task",
            label="my task",
        )
        
        assert record.run_id
        assert record.child_session_key.startswith("agent:default:subagent:")
        assert record.requester_session_key == "agent:default:main"
        assert record.task == "test task"
        assert record.label == "my task"
        assert record.status == SubagentStatus.PENDING
    
    def test_get(self):
        """Test getting a run by ID."""
        registry = SubagentRegistry()
        record = registry.register(
            requester_session_key="agent:default:main",
            task="test",
        )
        
        fetched = registry.get(record.run_id)
        assert fetched is record
    
    def test_get_by_session_key(self):
        """Test getting a run by session key."""
        registry = SubagentRegistry()
        record = registry.register(
            requester_session_key="agent:default:main",
            task="test",
        )
        
        fetched = registry.get_by_session_key(record.child_session_key)
        assert fetched is record
    
    def test_list_for_requester(self):
        """Test listing runs for a requester."""
        registry = SubagentRegistry()
        r1 = registry.register("agent:default:main", "task1")
        r2 = registry.register("agent:default:main", "task2")
        r3 = registry.register("agent:other:main", "task3")
        
        runs = registry.list_for_requester("agent:default:main")
        assert len(runs) == 2
        assert r1 in runs
        assert r2 in runs
        assert r3 not in runs
    
    def test_list_active(self):
        """Test listing active runs."""
        registry = SubagentRegistry()
        r1 = registry.register("agent:default:main", "task1")
        r2 = registry.register("agent:default:main", "task2")
        
        registry.mark_completed(r1.run_id, "done")
        
        active = registry.list_active()
        assert len(active) == 1
        assert r2 in active
        assert r1 not in active
    
    def test_count_active(self):
        """Test counting active runs."""
        registry = SubagentRegistry()
        r1 = registry.register("agent:default:main", "task1")
        r2 = registry.register("agent:default:main", "task2")
        
        assert registry.count_active() == 2
        
        registry.mark_completed(r1.run_id, "done")
        assert registry.count_active() == 1
    
    def test_mark_started(self):
        """Test marking run as started."""
        registry = SubagentRegistry()
        record = registry.register("agent:default:main", "test")
        
        registry.mark_started(record.run_id)
        
        assert record.status == SubagentStatus.RUNNING
        assert record.started_at is not None
    
    def test_mark_completed(self):
        """Test marking run as completed."""
        registry = SubagentRegistry()
        record = registry.register("agent:default:main", "test")
        registry.mark_started(record.run_id)
        
        registry.mark_completed(
            record.run_id,
            result_text="success",
            elapsed_ms=1000,
            input_tokens=100,
            output_tokens=50,
        )
        
        assert record.status == SubagentStatus.COMPLETED
        assert record.outcome == "ok"
        assert record.result_text == "success"
        assert record.elapsed_ms == 1000
        assert record.input_tokens == 100
        assert record.output_tokens == 50
        assert record.is_terminal
    
    def test_mark_error(self):
        """Test marking run as error."""
        registry = SubagentRegistry()
        record = registry.register("agent:default:main", "test")
        registry.mark_started(record.run_id)
        
        registry.mark_error(record.run_id, "something went wrong", elapsed_ms=500)
        
        assert record.status == SubagentStatus.ERROR
        assert record.outcome == "error"
        assert record.error_message == "something went wrong"
        assert record.elapsed_ms == 500
        assert record.is_terminal
    
    def test_mark_timeout(self):
        """Test marking run as timeout."""
        registry = SubagentRegistry()
        record = registry.register("agent:default:main", "test")
        registry.mark_started(record.run_id)
        
        registry.mark_timeout(record.run_id)
        
        assert record.status == SubagentStatus.TIMEOUT
        assert record.outcome == "timeout"
        assert record.is_terminal
    
    def test_mark_killed(self):
        """Test marking run as killed."""
        registry = SubagentRegistry()
        record = registry.register("agent:default:main", "test")
        registry.mark_started(record.run_id)
        
        registry.mark_killed(record.run_id)
        
        assert record.status == SubagentStatus.KILLED
        assert record.outcome == "killed"
        assert record.is_terminal
    
    def test_remove(self):
        """Test removing a run."""
        registry = SubagentRegistry()
        record = registry.register("agent:default:main", "test")
        
        removed = registry.remove(record.run_id)
        assert removed is record
        
        fetched = registry.get(record.run_id)
        assert fetched is None
    
    def test_status_change_callback(self):
        """Test status change callback."""
        registry = SubagentRegistry()
        changes = []
        
        registry.set_status_change_callback(lambda r: changes.append(r.run_id))
        
        record = registry.register("agent:default:main", "test")
        registry.mark_started(record.run_id)
        
        assert record.run_id in changes
    
    def test_get_registry_singleton(self):
        """Test global registry singleton."""
        reset_registry()
        
        r1 = get_registry()
        r2 = get_registry()
        
        assert r1 is r2