"""Configuration and state directory resolution.

Mirrors openclaw's src/config/paths.ts and src/agents/agent-scope-config.ts:
- resolve_home: resolve user home directory
- resolve_state_dir: resolve state directory (.openclaw)
- resolve_config_path: resolve config file path
- resolve_agent_workspace_dir: resolve agent workspace directory

Path resolution priority:
1. OPENCLAW_HOME / OPENCLAW_STATE_DIR / OPENCLAW_CONFIG_PATH environment variables
2. Project-level .openclaw/ or workspace/ directory
3. Global ~/.openclaw/ directory

Workspace resolution priority (aligns with openclaw agent-scope-config.ts:154-177):
1. agents.list[<agentId>].workspace (per-agent explicit override)
2. agents.defaults.workspace (default agent uses directly)
3. agents.defaults.workspace/<agentId> (non-default agents get subdirectory)
4. {stateDir}/workspace-<agentId> (fallback to state dir)
5. ~/.openclaw/workspace (ultimate default)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .types import NanoOpenClawConfig

STATE_DIRNAME = ".openclaw"
CONFIG_FILENAME = "nano-openclaw.json5"
DEFAULT_AGENT_ID = "default"


def resolve_home(env: Optional[dict[str, str]] = None) -> Path:
    """
    Resolve user home directory.
    
    Priority:
    1. OPENCLAW_HOME environment variable
    2. System home directory (Path.home())
    """
    if env is None:
        env = os.environ
    
    env_home = env.get("OPENCLAW_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    
    return Path.home()


def resolve_state_dir(env: Optional[dict[str, str]] = None) -> Path:
    """
    Resolve state directory.
    
    Priority:
    1. OPENCLAW_STATE_DIR environment variable
    2. {cwd}/.openclaw (project-level, if exists)
    3. ~/.openclaw (global)
    """
    if env is None:
        env = os.environ
    
    # 1. Environment variable override
    state_dir = env.get("OPENCLAW_STATE_DIR")
    if state_dir:
        return Path(state_dir).expanduser().resolve()
    
    # 2. Project-level state directory
    cwd_state = Path(Path.cwd()) / STATE_DIRNAME
    if cwd_state.exists():
        return cwd_state.resolve()
    
    # 3. Global state directory
    return resolve_home(env) / STATE_DIRNAME


def resolve_config_path(
    config_path: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> Path:
    """
    Resolve configuration file path.
    
    Priority:
    1. --config explicit argument
    2. OPENCLAW_CONFIG_PATH environment variable
    3. {stateDir}/nano-openclaw.json5
    4. {cwd}/workspace/nano-openclaw.json5
    5. ~/.openclaw/nano-openclaw.json5
    
    Returns:
        Path to config file (may not exist yet)
    """
    if env is None:
        env = os.environ
    
    # 1. Explicit path from --config
    if config_path:
        return Path(config_path).expanduser().resolve()
    
    # 2. Environment variable
    env_path = env.get("OPENCLAW_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()
    
    # 3. State directory
    state_dir = resolve_state_dir(env)
    state_config = state_dir / CONFIG_FILENAME
    if state_config.exists():
        return state_config
    
    # 4. Project workspace directory
    workspace_config = Path.cwd() / "workspace" / CONFIG_FILENAME
    if workspace_config.exists():
        return workspace_config.resolve()
    
    # 5. Global default location
    return resolve_home(env) / STATE_DIRNAME / CONFIG_FILENAME


def resolve_default_agent_workspace_dir(env: Optional[dict[str, str]] = None) -> Path:
    """
    Resolve default agent workspace directory.
    
    Mirrors openclaw's resolveDefaultAgentWorkspaceDir() in workspace-default.ts:
    - OPENCLAW_PROFILE env var → ~/.openclaw/workspace-{profile}
    - Otherwise → ~/.openclaw/workspace
    """
    if env is None:
        env = os.environ
    
    profile = env.get("OPENCLAW_PROFILE", "").strip().lower()
    if profile and profile != "default":
        return resolve_home(env) / STATE_DIRNAME / f"workspace-{profile}"
    
    return resolve_home(env) / STATE_DIRNAME / "workspace"


def resolve_agent_workspace_dir(
    config: "NanoOpenClawConfig",
    agent_id: str = DEFAULT_AGENT_ID,
    env: Optional[dict[str, str]] = None,
) -> Path:
    """
    Resolve agent workspace directory.
    
    Mirrors openclaw's resolveAgentWorkspaceDir() in agent-scope-config.ts:154-177
    
    Priority:
    1. agents.list[<agentId>].workspace (per-agent explicit override)
    2. agents.defaults.workspace (default agent uses directly)
    3. agents.defaults.workspace/<agentId> (non-default agents get subdirectory)
    4. {stateDir}/workspace-<agentId> (fallback to state dir)
    5. ~/.openclaw/workspace (ultimate default)
    
    Args:
        config: Parsed configuration
        agent_id: Agent identifier
        env: Environment variables
    
    Returns:
        Resolved workspace directory path
    """
    if env is None:
        env = os.environ
    
    # Strip null bytes from paths (security hardening, aligns with openclaw)
    agent_id = agent_id.replace("\x00", "")
    
    # 1. Check per-agent workspace config
    for agent in config.agents.list:
        if agent.id == agent_id and agent.workspace:
            workspace_path = agent.workspace.replace("\x00", "").strip()
            if workspace_path:
                return Path(workspace_path).expanduser().resolve()
    
    # 2. Check defaults.workspace
    defaults_workspace = config.agents.defaults.workspace
    if defaults_workspace:
        workspace_path = defaults_workspace.replace("\x00", "").strip()
        if workspace_path:
            base_dir = Path(workspace_path).expanduser().resolve()
            
            # Default agent uses base_dir directly
            if agent_id == DEFAULT_AGENT_ID:
                return base_dir
            
            # Non-default agents get subdirectory
            return base_dir / agent_id
    
    # 3. Fallback to state dir
    state_dir = resolve_state_dir(env)
    
    # 4. Ultimate default
    if agent_id == DEFAULT_AGENT_ID:
        return resolve_default_agent_workspace_dir(env)
    
    # Non-default agents get workspace-{agentId} under state dir
    return state_dir / f"workspace-{agent_id}"
