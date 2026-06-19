"""Pure-Python tests for the tool registry. No LLM call required."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from nano_openclaw.approvals import ApprovalDecision, ApprovalPolicy
from nano_openclaw.config.types import NanoOpenClawConfig, PluginsConfig, ToolsConfig
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.core.tools import ToolRegistry, build_core_registry

# Patch ToolRegistry.dispatch to be synchronous for tests — avoids changing
# every call site while still exercising the full dispatch logic.
_orig_dispatch = ToolRegistry.dispatch


def _sync_dispatch(self, *args, **kwargs):
    return asyncio.run(_orig_dispatch(self, *args, **kwargs))


ToolRegistry.dispatch = _sync_dispatch  # type: ignore[method-assign]


@pytest.fixture
def registry():
    return _registry_with_plugins("memory", "web")


def _registry_with_plugins(*plugins: str, tools_config: ToolsConfig | None = None) -> ToolRegistry:
    registry = build_core_registry()
    config = NanoOpenClawConfig(tools=tools_config) if tools_config else NanoOpenClawConfig()
    load_plugins(PluginsConfig(load=list(plugins)), registry, config)
    return registry


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


def test_apply_patch_validation_failure_is_tool_error(tmp_path):
    registry = build_core_registry()
    registry.set_workspace_dir(tmp_path)

    out = registry.dispatch(
        "id-p",
        "apply_patch",
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: missing.py\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            )
        },
    )

    assert out["is_error"] is True
    assert "Patch failed" in out["content"][0]["text"]


def test_bash_captures_exit_code(registry):
    out = registry.dispatch("id-b", "bash", {"command": "exit 7"})
    assert out.get("is_error") is None
    assert "exit=7" in out["content"][0]["text"]


def test_schemas_have_required_anthropic_fields(registry):
    schemas = registry.schemas()
    assert {s["name"] for s in schemas} == {
        "current_time", "read_file", "write_file", "list_dir", "bash",
        "apply_patch",
        "session_status", "skill", "skill_install", "memory_get", "memory_search",
        "web_search", "web_fetch", "sessions_spawn", "subagents",
        "todo",
    }
    for s in schemas:
        assert "description" in s and isinstance(s["description"], str)
        assert s["input_schema"]["type"] == "object"


def test_session_status_without_context(registry):
    out = registry.dispatch("id-s", "session_status", {})
    assert out.get("is_error") is None


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


def test_bash_protects_bare_pip_install(registry, tmp_path):
    registry.set_state_dir(tmp_path / "state")

    out = registry.dispatch("id-pip", "bash", {"command": "pip install --help", "timeout": 20})

    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "PIP_REQUIRE_VIRTUALENV=true" in text
    assert "skill_install tool" in text


def test_bash_protects_python_m_pip_install(registry, tmp_path):
    registry.set_state_dir(tmp_path / "state")

    out = registry.dispatch("id-pip", "bash", {"command": "python -m pip install --help", "timeout": 20})

    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "PIP_REQUIRE_VIRTUALENV=true" in text
    assert "metadata.openclaw.install" in text


def test_bash_protected_pip_bypasses_approval_prompt(tmp_path):
    registry = build_core_registry()
    registry.set_state_dir(tmp_path / "state")
    policy = ApprovalPolicy(ask_mode="always", dangerous_tools=["bash"], allowlist=[])
    registry.approval_manager = __import__(
        "nano_openclaw.approvals", fromlist=["ApprovalManager"]
    ).ApprovalManager(policy)

    out = registry.dispatch("id-pip", "bash", {"command": "pip install --help", "timeout": 20})

    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "approval denied" not in text
    assert "PIP_REQUIRE_VIRTUALENV=true" in text


def test_bash_non_install_python_command_not_protected(registry):
    out = registry.dispatch("id-py", "bash", {"command": "python --version", "timeout": 20})

    assert out.get("is_error") is None
    assert "PIP_REQUIRE_VIRTUALENV=true" not in out["content"][0]["text"]


def test_skill_tool_requires_skill_name(registry):
    """Skill tool returns error when skill name is missing."""
    out = registry.dispatch("id-s", "skill", {})
    assert out["is_error"] is True
    assert "skill name required" in out["content"][0]["text"]


def test_skill_tool_returns_error_for_unknown_skill(registry):
    """Skill tool returns error for unknown skill."""
    out = registry.dispatch("id-s", "skill", {"skill": "unknown-skill"})
    assert out["is_error"] is True
    assert "not found" in out["content"][0]["text"]


def test_skill_tool_returns_content_for_known_skill(registry, tmp_path):
    """Skill tool returns skill content when skill is eligible."""
    from nano_openclaw.features.skills import Skill

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

    out = registry.dispatch("id-s", "skill", {"skill": "test-skill"})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Test Skill" in text
    assert "skill content" in text
    assert f"Skill directory: {skill_dir}" in text
    assert "Resolve any relative paths mentioned by the skill against the skill directory." in text


def test_skill_tool_invokable_when_not_user_invocable(registry, tmp_path):
    """Skill tool works for skills with user-invocable: false.

    These skills are excluded from slash commands but must be reachable via
    the model Skill tool when the loop passes user_invocable_only=False to
    build_skill_registry_from_entries.
    """
    from nano_openclaw.features.skills import Skill

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

    out = registry.dispatch("id-m", "skill", {"skill": "mockup"})
    assert out.get("is_error") is None
    assert "Mockup Skill" in out["content"][0]["text"]


def test_skill_tool_loads_from_file_if_content_missing(registry, tmp_path):
    """Skill tool loads content from file when skill.content is None."""
    from nano_openclaw.features.skills import Skill

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

    out = registry.dispatch("id-s", "skill", {"skill": "load-skill"})
    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "Load Skill" in text
    assert "loaded from file" in text
    assert f"Skill file location: {skill_file}" in text


def test_skill_install_tool_returns_install_result(registry, tmp_path, monkeypatch):
    from nano_openclaw.features.skills.install import SkillInstallResult

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    registry.set_workspace_dir(workspace)
    registry.set_state_dir(state)

    async def fake_install_skill(**kwargs):
        assert kwargs["workspace_dir"] == str(workspace)
        assert kwargs["state_dir"] == str(state)
        assert kwargs["skill_name"] == "demo"
        assert kwargs["install_id"] == "deps"
        return SkillInstallResult(ok=True, message="Installed", stdout="ok", stderr="", code=0)

    monkeypatch.setattr("nano_openclaw.features.skills.install.install_skill", fake_install_skill)

    out = registry.dispatch("id-i", "skill_install", {"skill": "demo", "installId": "deps"})

    assert out.get("is_error") is None
    text = out["content"][0]["text"]
    assert "ok=true" in text
    assert "message=Installed" in text
    import sys as _sys
    venv_base = state / 'tools' / 'python' / 'skills' / 'demo' / 'venv'
    if _sys.platform.startswith("win"):
        expected_python = venv_base / 'Scripts' / 'python.exe'
    else:
        expected_python = venv_base / 'bin' / 'python'
    assert f"python={expected_python}" in text
    assert f"venv={venv_base}" in text
    assert "--- stdout ---\nok" in text


def test_dispatch_passes_cancellation_token_to_approval_ui(monkeypatch):
    registry = build_core_registry()
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


def test_dispatch_rejects_approval_without_interactive_console():
    registry = build_core_registry()
    policy = ApprovalPolicy(ask_mode="always", dangerous_tools=["write_file"], allowlist=[])
    registry.approval_manager = __import__(
        "nano_openclaw.approvals", fromlist=["ApprovalManager"]
    ).ApprovalManager(policy)

    out = registry.dispatch(
        "id-w",
        "write_file",
        {"path": "report.md", "content": "content"},
    )

    assert out["is_error"] is True
    assert out["_denied"] is True
    assert "approval denied for write_file" in out["content"][0]["text"]
    assert "non-interactive background execution cannot request approval" in out["content"][0]["text"]


def test_web_plugin_respects_disabled_web_tools():
    cfg = ToolsConfig.model_validate(
        {
            "web": {
                "search": {"enabled": False},
                "fetch": {"enabled": False},
            }
        }
    )

    registry = _registry_with_plugins("web", tools_config=cfg)

    assert "web_search" not in registry.names()
    assert "web_fetch" not in registry.names()


def test_web_plugin_uses_web_tool_defaults_from_config(monkeypatch):
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
    registry = _registry_with_plugins("web", tools_config=cfg)
    captured = {}

    def fake_search(query, max_results=10, region="wt-wt"):
        captured["search"] = {
            "query": query,
            "max_results": max_results,
            "region": region,
        }
        return {"text": "ok"}

    async def fake_fetch(url, extract_mode="markdown", max_chars=20_000, max_redirects=3, timeout_seconds=30):
        captured["fetch"] = {
            "url": url,
            "extract_mode": extract_mode,
            "max_chars": max_chars,
            "max_redirects": max_redirects,
            "timeout_seconds": timeout_seconds,
        }
        return {"text": "ok"}

    monkeypatch.setattr("nano_openclaw.features.web.service.web_search", fake_search)
    monkeypatch.setattr("nano_openclaw.features.web.service.web_fetch", fake_fetch)

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


def test_tool_registry_clone_preserves_workspace_write_hook():
    registry = ToolRegistry()
    calls = []

    def hook(tool_name, ctx):
        calls.append((tool_name, ctx.workspace_dir))

    registry.set_workspace_dir("workspace")
    registry.set_before_workspace_write(hook)

    clone = registry.clone()
    ctx = clone.execution_context()
    assert clone.before_workspace_write is hook

    clone.before_workspace_write("write_file", ctx)

    assert calls == [("write_file", "workspace")]
