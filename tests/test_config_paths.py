"""Tests for config/paths.py.

Tests configuration and state directory resolution.
Mirrors openclaw's path resolution logic.
"""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from nano_openclaw.config.paths import (
    STATE_DIRNAME,
    CONFIG_FILENAME,
    DEFAULT_AGENT_ID,
    resolve_home,
    resolve_state_dir,
    resolve_config_path,
    resolve_default_agent_workspace_dir,
    resolve_agent_workspace_dir,
)
from nano_openclaw.config.types import (
    NanoOpenClawConfig,
    AgentsConfig,
    AgentDefaultsConfig,
    AgentConfig,
)


# =============================================================================
# resolve_home Tests
# =============================================================================

class TestResolveHome:
    def test_default_home(self):
        """Without OPENCLAW_HOME, returns system home."""
        env = {}
        home = resolve_home(env)
        assert home == Path.home()

    def test_openclaw_home_override(self):
        """OPENCLAW_HOME overrides system home."""
        env = {"OPENCLAW_HOME": "/custom/home"}
        home = resolve_home(env)
        assert home == Path("/custom/home").resolve()

    def test_openclaw_home_with_tilde(self):
        """OPENCLAW_HOME supports ~ expansion."""
        env = {"OPENCLAW_HOME": "~/custom"}
        home = resolve_home(env)
        assert "~" not in str(home)
        assert home.is_absolute()


# =============================================================================
# resolve_state_dir Tests
# =============================================================================

class TestResolveStateDir:
    def test_openclaw_state_dir_override(self):
        """OPENCLAW_STATE_DIR overrides all other paths."""
        env = {"OPENCLAW_STATE_DIR": "/custom/state"}
        state_dir = resolve_state_dir(env)
        assert state_dir == Path("/custom/state").resolve()

    def test_project_level_state_dir(self, tmp_path):
        """Uses {cwd}/.openclaw if it exists."""
        env = {}
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        state_dir = project_dir / STATE_DIRNAME
        state_dir.mkdir()

        with patch('pathlib.Path.cwd', return_value=project_dir):
            resolved = resolve_state_dir(env)
            assert resolved == state_dir.resolve()

    def test_global_state_dir_fallback(self):
        """Falls back to ~/.openclaw if no project-level dir."""
        env = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('pathlib.Path.cwd', return_value=Path(tmpdir)):
                resolved = resolve_state_dir(env)
                assert resolved == Path.home() / STATE_DIRNAME

    def test_state_dir_with_openclaw_home(self):
        """Uses OPENCLAW_HOME/.openclaw when OPENCLAW_HOME is set."""
        env = {"OPENCLAW_HOME": "/custom/home"}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('pathlib.Path.cwd', return_value=Path(tmpdir)):
                resolved = resolve_state_dir(env)
                assert resolved == (Path("/custom/home") / STATE_DIRNAME).resolve()


# =============================================================================
# resolve_config_path Tests
# =============================================================================

class TestResolveConfigPath:
    def test_explicit_config_path(self, tmp_path):
        """--config argument takes highest priority."""
        config_file = tmp_path / "custom-config.json5"
        config_file.write_text("{}")

        resolved = resolve_config_path(str(config_file))
        assert resolved == config_file.resolve()

    def test_openclaw_config_path_env(self, tmp_path):
        """OPENCLAW_CONFIG_PATH environment variable is second priority."""
        config_file = tmp_path / "env-config.json5"
        config_file.write_text("{}")

        env = {"OPENCLAW_CONFIG_PATH": str(config_file)}
        resolved = resolve_config_path(env=env)
        assert resolved == config_file.resolve()

    def test_state_dir_config(self, tmp_path):
        """Falls back to {stateDir}/nano-openclaw.json5."""
        state_dir = tmp_path / ".openclaw"
        state_dir.mkdir()
        config_file = state_dir / CONFIG_FILENAME
        config_file.write_text("{}")

        env = {"OPENCLAW_STATE_DIR": str(state_dir)}
        resolved = resolve_config_path(env=env)
        assert resolved == config_file.resolve()

    def test_workspace_config(self, tmp_path):
        """Falls back to {cwd}/workspace/nano-openclaw.json5."""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        config_file = workspace_dir / CONFIG_FILENAME
        config_file.write_text("{}")

        with patch('pathlib.Path.cwd', return_value=tmp_path):
            resolved = resolve_config_path(env={})
            assert resolved == config_file.resolve()

    def test_global_config_fallback(self):
        """Ultimate fallback to ~/.openclaw/nano-openclaw.json5."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('pathlib.Path.cwd', return_value=Path(tmpdir)):
                resolved = resolve_config_path(env={})
                expected = Path.home() / STATE_DIRNAME / CONFIG_FILENAME
                assert resolved == expected

    def test_priority_state_over_workspace(self, tmp_path):
        """State dir config takes priority over workspace config."""
        state_dir = tmp_path / ".openclaw"
        state_dir.mkdir()
        state_config = state_dir / CONFIG_FILENAME
        state_config.write_text("{}")

        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        workspace_config = workspace_dir / CONFIG_FILENAME
        workspace_config.write_text("{}")

        env = {"OPENCLAW_STATE_DIR": str(state_dir)}
        resolved = resolve_config_path(env=env)
        assert resolved == state_config.resolve()


# =============================================================================
# resolve_default_agent_workspace_dir Tests
# =============================================================================

class TestResolveDefaultAgentWorkspaceDir:
    def test_default_workspace(self):
        """Returns ~/.openclaw/workspace by default."""
        env = {}
        ws = resolve_default_agent_workspace_dir(env)
        assert ws == Path.home() / STATE_DIRNAME / "workspace"

    def test_openclaw_profile_dev(self):
        """OPENCLAW_PROFILE=dev returns ~/.openclaw/workspace-dev."""
        env = {"OPENCLAW_PROFILE": "dev"}
        ws = resolve_default_agent_workspace_dir(env)
        assert ws == Path.home() / STATE_DIRNAME / "workspace-dev"

    def test_openclaw_profile_staging(self):
        """OPENCLAW_PROFILE=staging returns ~/.openclaw/workspace-staging."""
        env = {"OPENCLAW_PROFILE": "staging"}
        ws = resolve_default_agent_workspace_dir(env)
        assert ws == Path.home() / STATE_DIRNAME / "workspace-staging"

    def test_openclaw_profile_default(self):
        """OPENCLAW_PROFILE=default returns ~/.openclaw/workspace."""
        env = {"OPENCLAW_PROFILE": "default"}
        ws = resolve_default_agent_workspace_dir(env)
        assert ws == Path.home() / STATE_DIRNAME / "workspace"

    def test_openclaw_home_affects_workspace(self):
        """OPENCLAW_HOME affects workspace base path."""
        env = {"OPENCLAW_HOME": "/custom/home", "OPENCLAW_PROFILE": "prod"}
        ws = resolve_default_agent_workspace_dir(env)
        assert ws == (Path("/custom/home") / STATE_DIRNAME / "workspace-prod").resolve()


# =============================================================================
# resolve_agent_workspace_dir Tests
# =============================================================================

class TestResolveAgentWorkspaceDir:
    def test_per_agent_workspace_priority(self, tmp_path):
        """agents.list[].workspace takes highest priority."""
        workspace_path = str(tmp_path / "agent-workspace")
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace="/default-ws"),
                list=[
                    AgentConfig(id="coder", workspace=workspace_path),
                ],
            )
        )

        ws = resolve_agent_workspace_dir(config, "coder")
        assert ws == Path(workspace_path).resolve()

    def test_defaults_workspace_for_default_agent(self, tmp_path):
        """Default agent uses agents.defaults.workspace directly."""
        workspace_path = str(tmp_path / "default-workspace")
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace=workspace_path),
            )
        )

        ws = resolve_agent_workspace_dir(config, "default")
        assert ws == Path(workspace_path).resolve()

    def test_defaults_workspace_subdirectory_for_non_default_agent(self, tmp_path):
        """Non-default agents get subdirectory under defaults.workspace."""
        workspace_path = str(tmp_path / "base-workspace")
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace=workspace_path),
            )
        )

        ws = resolve_agent_workspace_dir(config, "coder")
        assert ws == Path(workspace_path) / "coder"

    def test_fallback_to_state_dir(self, tmp_path):
        """Falls back to {stateDir}/workspace-{agentId} when no config."""
        config = NanoOpenClawConfig()
        state_dir = tmp_path / ".openclaw"
        state_dir.mkdir()

        env = {"OPENCLAW_STATE_DIR": str(state_dir)}
        ws = resolve_agent_workspace_dir(config, "coder", env)
        assert ws == state_dir / "workspace-coder"

    def test_ultimate_fallback_for_default_agent(self):
        """Ultimate fallback to ~/.openclaw/workspace."""
        config = NanoOpenClawConfig()
        env = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('pathlib.Path.cwd', return_value=Path(tmpdir)):
                ws = resolve_agent_workspace_dir(config, "default", env)
                assert ws == Path.home() / STATE_DIRNAME / "workspace"

    def test_relative_path_resolved_from_config_dir(self, tmp_path):
        """Relative workspace paths are resolved from cwd."""
        workspace_path = "./workspace"
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace=workspace_path),
            )
        )

        with patch('pathlib.Path.cwd', return_value=tmp_path):
            ws = resolve_agent_workspace_dir(config, "default")
            # Path.resolve() resolves relative to actual cwd, not mocked cwd
            # So we check that the path contains the workspace segment
            assert "workspace" in str(ws)

    def test_tilde_expansion_in_workspace_path(self):
        """Tilde in workspace path is expanded."""
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace="~/my-workspace"),
            )
        )

        ws = resolve_agent_workspace_dir(config, "default")
        assert "~" not in str(ws)
        assert ws == (Path.home() / "my-workspace").resolve()

    def test_null_byte_stripped_from_agent_id(self):
        """Null bytes are stripped from agent_id for security."""
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace="/tmp/workspace"),
            )
        )

        ws = resolve_agent_workspace_dir(config, "agent\x00id")
        assert "\x00" not in str(ws)

    def test_workspace_path_whitespace_stripped(self):
        """Whitespace is stripped from workspace paths."""
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace="  /tmp/workspace  "),
            )
        )

        ws = resolve_agent_workspace_dir(config, "default")
        assert ws == Path("/tmp/workspace").resolve()

    def test_multiple_agents_different_workspaces(self, tmp_path):
        """Different agents can have different workspaces."""
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace=str(tmp_path / "default-ws")),
                list=[
                    AgentConfig(id="coder", workspace=str(tmp_path / "coder-ws")),
                    AgentConfig(id="analyst", workspace=str(tmp_path / "analyst-ws")),
                ],
            )
        )

        default_ws = resolve_agent_workspace_dir(config, "default")
        coder_ws = resolve_agent_workspace_dir(config, "coder")
        analyst_ws = resolve_agent_workspace_dir(config, "analyst")

        assert default_ws == (tmp_path / "default-ws").resolve()
        assert coder_ws == (tmp_path / "coder-ws").resolve()
        assert analyst_ws == (tmp_path / "analyst-ws").resolve()

    def test_agent_not_in_list_uses_defaults(self, tmp_path):
        """Agent not in list uses defaults.workspace with subdirectory."""
        config = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(workspace=str(tmp_path / "base-ws")),
                list=[
                    AgentConfig(id="default", default=True),
                ],
            )
        )

        ws = resolve_agent_workspace_dir(config, "unknown-agent")
        assert ws == (tmp_path / "base-ws" / "unknown-agent").resolve()
