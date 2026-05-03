"""Pure-Python tests for the tool registry. No LLM call required."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from nano_openclaw.approvals import ApprovalDecision, ApprovalPolicy
from nano_openclaw.config.types import ToolsConfig
from nano_openclaw.tools import build_default_registry


@pytest.fixture
def registry():
    return build_default_registry()


def test_read_write_roundtrip(tmp_path, registry):
    target = tmp_path / "hello.txt"
    write = registry.dispatch(
        "id-w", "write_file", {"path": str(target), "content": "你好 nano"}
    )
    assert write.get("is_error") is None
    assert "wrote" in write["content"][0]["text"]

    read = registry.dispatch("id-r", "read_file", {"path": str(target)})
    assert read.get("is_error") is None
    assert read["content"][0]["text"] == "你好 nano"


def test_list_dir_marks_directories(tmp_path, registry):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "sub").mkdir()

    out = registry.dispatch("id-l", "list_dir", {"path": str(tmp_path)})
    text = out["content"][0]["text"]
    lines = text.splitlines()

    assert "a.txt" in lines
    assert "b.txt" in lines
    assert "sub/" in lines


def test_dispatch_unknown_tool_returns_error(registry):
    out = registry.dispatch("id-x", "does_not_exist", {})
    assert out["is_error"] is True
    assert "unknown tool" in out["content"][0]["text"]
    assert out["tool_use_id"] == "id-x"


def test_dispatch_handler_exception_becomes_error(registry):
    out = registry.dispatch("id-e", "read_file", {"path": "/no/such/path/__nope__"})
    assert out["is_error"] is True
    text = out["content"][0]["text"]
    assert "FileNotFoundError" in text or "Error" in text


def test_bash_captures_exit_code(registry):
    out = registry.dispatch("id-b", "bash", {"command": "exit 7"})
    assert out.get("is_error") is None
    assert "exit=7" in out["content"][0]["text"]


def test_schemas_have_required_anthropic_fields(registry):
    schemas = registry.schemas()
    assert {s["name"] for s in schemas} == {
        "read_file", "write_file", "list_dir", "bash",
        "session_status", "Skill", "memory_get", "memory_search",
        "web_search", "web_fetch"
    }
    for s in schemas:
        assert "description" in s and isinstance(s["description"], str)
        assert s["input_schema"]["type"] == "object"


def test_session_status_without_context(registry):
    out = registry.dispatch("id-s", "session_status", {})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Clock:" in text


def test_session_status_with_context(registry):
    registry.set_session_status_context(
        model="anthropic/claude-sonnet-4",
        session_id="test-123",
        context_budget=100000,
        current_tokens=12500,
        compaction_count=1,
        message_count=15,
    )
    out = registry.dispatch("id-s", "session_status", {})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Clock:" in text
    assert "Model: anthropic/claude-sonnet-4" in text
    assert "Session: test-123" in text
    assert "Context:" in text and "tokens" in text
    assert "12.5k" in text
    assert "Compactions: 1" in text
    assert "Messages: 15" in text


def test_relative_path_resolves_to_workspace_dir(tmp_path, registry):
    """Mirrors openclaw pi-tools.workspace-paths.test.ts:57."""
    other_dir = tmp_path / "cwd"
    other_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    
    registry.set_workspace_dir(workspace_dir)
    
    test_file = workspace_dir / "test.txt"
    test_file.write_text("workspace content", encoding="utf-8")
    
    out = registry.dispatch("id-r", "read_file", {"path": "test.txt"})
    assert out.get("is_error") is None
    assert "workspace content" in out["content"][0]["text"]
    
    out = registry.dispatch("id-w", "write_file", {"path": "new.txt", "content": "written to workspace"})
    assert out.get("is_error") is None
    assert (workspace_dir / "new.txt").exists()
    assert (workspace_dir / "new.txt").read_text() == "written to workspace"
    
    assert not (other_dir / "new.txt").exists()


def test_absolute_path_not_redirected_to_workspace(tmp_path, registry):
    """Absolute paths should be resolved directly, not to workspace_dir."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside content", encoding="utf-8")
    
    registry.set_workspace_dir(workspace_dir)
    
    out = registry.dispatch("id-r", "read_file", {"path": str(outside_file)})
    assert out.get("is_error") is None
    assert "outside content" in out["content"][0]["text"]


def test_bash_defaults_to_workspace_dir(tmp_path, registry):
    """Mirrors openclaw pi-tools.workspace-paths.test.ts:148."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    
    registry.set_workspace_dir(workspace_dir)
    
    import platform
    cmd = "cd" if platform.system() == "Windows" else "pwd"
    out = registry.dispatch("id-b", "bash", {"command": cmd})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert str(workspace_dir) in text or workspace_dir.name in text


def test_bash_workdir_overrides_workspace(tmp_path, registry):
    """Mirrors openclaw pi-tools.workspace-paths.test.ts:155."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    override_dir = tmp_path / "override"
    override_dir.mkdir()

    registry.set_workspace_dir(workspace_dir)

    import platform
    cmd = "cd" if platform.system() == "Windows" else "pwd"
    out = registry.dispatch("id-b", "bash", {"command": cmd, "workdir": str(override_dir)})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert str(override_dir) in text or override_dir.name in text


def test_skill_tool_requires_skill_name(registry):
    """Skill tool returns error when skill name is missing."""
    out = registry.dispatch("id-s", "Skill", {})
    assert out["is_error"] is True
    assert "skill name required" in out["content"][0]["text"]


def test_skill_tool_returns_error_for_unknown_skill(registry):
    """Skill tool returns error for unknown skill."""
    out = registry.dispatch("id-s", "Skill", {"skill": "unknown-skill"})
    assert out["is_error"] is True
    assert "not found" in out["content"][0]["text"]


def test_skill_tool_returns_content_for_known_skill(registry, tmp_path):
    """Skill tool returns skill content when skill is eligible."""
    from nano_openclaw.skills import Skill

    # Create a skill file
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Test Skill\nThis is the skill content.")

    skill = Skill(
        name="test-skill",
        description="A test skill",
        filePath=str(skill_file),
        baseDir=str(skill_dir),
        source="workspace",
        content="# Test Skill\nThis is the skill content.",
    )

    registry.set_eligible_skills({"test-skill": skill})

    out = registry.dispatch("id-s", "Skill", {"skill": "test-skill"})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Test Skill" in text
    assert "skill content" in text


def test_skill_tool_invokable_when_not_user_invocable(registry, tmp_path):
    """Skill tool works for skills with user-invocable: false.

    These skills are excluded from slash commands but must be reachable via
    the model Skill tool when the loop passes user_invocable_only=False to
    build_skill_registry_from_entries.
    """
    from nano_openclaw.skills import Skill

    skill_dir = tmp_path / "skills" / "mockup"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: mockup\nuser-invocable: false\n---\n# Mockup Skill\nContent here.")

    skill = Skill(
        name="mockup",
        description="Activate when encountering .mu files",
        filePath=str(skill_file),
        baseDir=str(skill_dir),
        source="bundled",
        content="# Mockup Skill\nContent here.",
    )

    # Simulate what the loop does: model_registry built with user_invocable_only=False
    registry.set_eligible_skills({"mockup": skill})

    out = registry.dispatch("id-m", "Skill", {"skill": "mockup"})
    assert out.get("is_error") is None
    assert "Mockup Skill" in out["content"][0]["text"]


def test_skill_tool_loads_from_file_if_content_missing(registry, tmp_path):
    """Skill tool loads content from file when skill.content is None."""
    from nano_openclaw.skills import Skill

    # Create a skill file
    skill_dir = tmp_path / "skills" / "load-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Load Skill\nContent loaded from file.")

    skill = Skill(
        name="load-skill",
        description="A skill to load",
        filePath=str(skill_file),
        baseDir=str(skill_dir),
        source="workspace",
        content=None,  # Content not loaded yet
    )

    registry.set_eligible_skills({"load-skill": skill})

    out = registry.dispatch("id-s", "Skill", {"skill": "load-skill"})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Load Skill" in text
    assert "loaded from file" in text


def test_dispatch_passes_cancellation_token_to_approval_ui(monkeypatch):
    registry = build_default_registry()
    registry.console = Console()
    policy = ApprovalPolicy(ask_mode="always", dangerous_tools=["bash"], allowlist=[])
    registry.approval_manager = __import__(
        "nano_openclaw.approvals", fromlist=["ApprovalManager"]
    ).ApprovalManager(policy)

    class StubToken:
        pass

    captured = {}

    from nano_openclaw.approvals.ui import ApprovalUI

    monkeypatch.setattr(ApprovalUI, "render_request", lambda self, request: None)

    def fake_prompt(self, request, cancellation_token=None):
        captured["token"] = cancellation_token
        return ApprovalDecision.DENY

    monkeypatch.setattr(ApprovalUI, "prompt_decision", fake_prompt)

    out = registry.dispatch(
        "id-b",
        "bash",
        {"command": "rm -rf /"},
        cancellation_token=StubToken(),
    )

    assert out["is_error"] is True
    assert isinstance(captured["token"], StubToken)


def test_build_default_registry_respects_disabled_web_tools():
    cfg = ToolsConfig.model_validate(
        {
            "web": {
                "search": {"enabled": False},
                "fetch": {"enabled": False},
            }
        }
    )

    registry = build_default_registry(cfg)

    assert "web_search" not in registry.names()
    assert "web_fetch" not in registry.names()


def test_build_default_registry_uses_web_tool_defaults_from_config(monkeypatch):
    cfg = ToolsConfig.model_validate(
        {
            "web": {
                "search": {"maxResults": 7, "region": "us-en"},
                "fetch": {
                    "maxChars": 1234,
                    "maxRedirects": 2,
                    "timeoutSeconds": 9,
                    "extractMode": "text",
                },
            }
        }
    )
    registry = build_default_registry(cfg)
    captured = {}

    def fake_search(query, max_results=10, region="wt-wt"):
        captured["search"] = {
            "query": query,
            "max_results": max_results,
            "region": region,
        }
        return {"text": "ok"}

    def fake_fetch(url, extract_mode="markdown", max_chars=20_000, max_redirects=3, timeout_seconds=30):
        captured["fetch"] = {
            "url": url,
            "extract_mode": extract_mode,
            "max_chars": max_chars,
            "max_redirects": max_redirects,
            "timeout_seconds": timeout_seconds,
        }
        return {"text": "ok"}

    monkeypatch.setattr("nano_openclaw.tools.web_search", fake_search)
    monkeypatch.setattr("nano_openclaw.tools.web_fetch", fake_fetch)

    assert registry.get("web_search") is not None
    assert registry.get("web_fetch") is not None

    registry.dispatch("id-1", "web_search", {"query": "python"})
    registry.dispatch("id-2", "web_fetch", {"url": "https://example.com"})

    assert captured["search"] == {
        "query": "python",
        "max_results": 7,
        "region": "us-en",
    }
    assert captured["fetch"] == {
        "url": "https://example.com",
        "extract_mode": "text",
        "max_chars": 1234,
        "max_redirects": 2,
        "timeout_seconds": 9,
    }
