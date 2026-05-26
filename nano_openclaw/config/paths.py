"""Configuration and state directory resolution.

Mirrors openclaw's src/config/paths.ts and src/agents/agent-scope-config.ts:
- resolve_home: resolve user home directory
- resolve_state_dir: resolve state directory (.nano-openclaw)
- resolve_config_path: resolve config file path
- resolve_agent_workspace_dir: resolve agent workspace directory

Path resolution priority:
1. NANO_OPENCLAW_HOME / NANO_OPENCLAW_STATE_DIR / NANO_OPENCLAW_CONFIG_PATH environment variables
2. Project-level .nano-openclaw/ or workspace/ directory
3. Global ~/.nano-openclaw/ directory

Workspace resolution priority:
1. agents.list[<agentId>].workspace (per-agent explicit override)
2. agents.defaults.workspace (default agent uses directly)
3. agents.defaults.workspace/<agentId> (non-default agents get subdirectory)
4. When state_dir is explicit (NANO_OPENCLAW_STATE_DIR or project-level
   {cwd}/.nano-openclaw): {stateDir}/workspace for default agent,
   {stateDir}/workspace-<agentId> for non-default agents
5. When state_dir is the global home fallback: ~/.nano-openclaw/workspace
   (profile-aware via NANO_OPENCLAW_PROFILE) for default agent,
   ~/.nano-openclaw/workspace-<agentId> for non-default agents

Step 4 is the key alignment: if a project has a ``.nano-openclaw/`` of its
own, its workspace should stay project-local too — otherwise config loads
from the project dir but workspace silently leaks to ``~/.nano-openclaw/``,
splitting state across two locations.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional, Tuple

StateDirSource = Literal["env", "cwd", "home"]

if TYPE_CHECKING:
    from .types import NanoOpenClawConfig

STATE_DIRNAME = ".nano-openclaw"
CONFIG_FILENAME = "nano-openclaw.json5"
DEFAULT_AGENT_ID = "default"


def resolve_home(env: Optional[dict[str, str]] = None) -> Path:
    """
    Resolve user home directory.

    Priority:
    1. NANO_OPENCLAW_HOME environment variable
    2. System home directory (Path.home())
    """
    if env is None:
        env = os.environ

    env_home = env.get("NANO_OPENCLAW_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    return Path.home()


def resolve_state_dir_with_source(
    env: Optional[dict[str, str]] = None,
) -> Tuple[Path, StateDirSource]:
    """
    Same priority chain as :func:`resolve_state_dir`, but also reports which
    branch produced the answer. Bootstrap logic uses this to distinguish
    "fell through to ~/.nano-openclaw" (auto-init from package template is OK)
    from "user explicitly pointed us at a directory" (don't write files
    unprompted — could pollute a user project).
    """
    if env is None:
        env = os.environ

    # 1. Environment variable override
    state_dir = env.get("NANO_OPENCLAW_STATE_DIR")
    if state_dir:
        return Path(state_dir).expanduser().resolve(), "env"

    # 2. Project-level state directory — must look like a real one
    cwd_state = Path(Path.cwd()) / STATE_DIRNAME
    if (cwd_state / CONFIG_FILENAME).exists():
        return cwd_state.resolve(), "cwd"

    # 3. Global state directory
    return resolve_home(env) / STATE_DIRNAME, "home"


def resolve_state_dir(env: Optional[dict[str, str]] = None) -> Path:
    """
    Resolve state directory.

    Priority:
    1. NANO_OPENCLAW_STATE_DIR environment variable
    2. {cwd}/.nano-openclaw (project-level, only if it contains
       ``nano-openclaw.json5`` — empty / partial subdirs that get auto-created
       by tools or workspace bootstrap don't qualify, otherwise the same
       machine resolves to different state dirs depending on what happens
       to be on disk at lookup time)
    3. ~/.nano-openclaw (global)
    """
    return resolve_state_dir_with_source(env)[0]


def resolve_config_path(
    config_path: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> Path:
    """
    Resolve configuration file path.

    Priority:
    1. --config explicit argument
    2. NANO_OPENCLAW_CONFIG_PATH environment variable
    3. {stateDir}/nano-openclaw.json5
    4. {cwd}/workspace/nano-openclaw.json5
    5. ~/.nano-openclaw/nano-openclaw.json5

    Returns:
        Path to config file (may not exist yet)
    """
    if env is None:
        env = os.environ

    # 1. Explicit path from --config
    if config_path:
        return Path(config_path).expanduser().resolve()

    # 2. Environment variable
    env_path = env.get("NANO_OPENCLAW_CONFIG_PATH")
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
    - NANO_OPENCLAW_PROFILE env var → ~/.nano-openclaw/workspace-{profile}
    - Otherwise → ~/.nano-openclaw/workspace
    """
    if env is None:
        env = os.environ

    profile = env.get("NANO_OPENCLAW_PROFILE", "").strip().lower()
    if profile and profile != "default":
        return resolve_home(env) / STATE_DIRNAME / f"workspace-{profile}"

    return resolve_home(env) / STATE_DIRNAME / "workspace"


def _resolve_config_path(raw: str, base: Path) -> Path:
    """Resolve a config-supplied path.

    Absolute and ``~``-prefixed paths win as-is. Relative paths are
    anchored to *base* (typically state_dir) instead of the current cwd —
    that's the whole point of this helper. Without it, daemon-vs-CLI vs
    systemd-vs-docker each get a different cwd and the same config string
    produces different filesystem locations.
    """
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (base / p).resolve()


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
    5. ~/.nano-openclaw/workspace (ultimate default)
    
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

    # Pre-resolve state_dir so we can anchor relative config paths to it
    # (rather than cwd). Why: config-driven relative paths like
    # ``./.nano-openclaw/workspace`` would otherwise resolve against whatever
    # cwd the daemon happened to be launched from — systemd / docker / a
    # user shell each give a different answer, leading to silently
    # divergent workspace dirs across runs.
    state_dir_for_relative = resolve_state_dir(env)

    # 1. Check per-agent workspace config
    for agent in config.agents.list:
        if agent.id == agent_id and agent.workspace:
            workspace_path = agent.workspace.replace("\x00", "").strip()
            if workspace_path:
                return _resolve_config_path(workspace_path, state_dir_for_relative)

    # 2. Check defaults.workspace
    defaults_workspace = config.agents.defaults.workspace
    if defaults_workspace:
        workspace_path = defaults_workspace.replace("\x00", "").strip()
        if workspace_path:
            base_dir = _resolve_config_path(workspace_path, state_dir_for_relative)

            # Default agent uses base_dir directly
            if agent_id == DEFAULT_AGENT_ID:
                return base_dir

            # Non-default agents get subdirectory
            return base_dir / agent_id

    # 3. Fallback. Two sub-cases for the default agent based on whether
    # state_dir is explicit or the global home fallback — see the module
    # docstring for the rationale. Non-default agents always follow
    # state_dir (already the case before this change).
    state_dir = state_dir_for_relative
    home_state_dir = resolve_home(env) / STATE_DIRNAME

    if agent_id == DEFAULT_AGENT_ID:
        # state_dir explicit (env var or project-level) → keep workspace
        # under it so project state stays cohesive. Compare resolved paths
        # because env-supplied state_dir is ``.resolve()``-d but the home
        # fallback in ``resolve_state_dir`` is not.
        if state_dir.resolve() != home_state_dir.resolve():
            return state_dir / "workspace"
        # Global home state_dir → profile-aware workspace
        # (NANO_OPENCLAW_PROFILE), matching openclaw semantics.
        return resolve_default_agent_workspace_dir(env)

    # Non-default agents get workspace-{agentId} under state dir
    return state_dir / f"workspace-{agent_id}"
