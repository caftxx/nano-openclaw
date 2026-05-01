"""Configuration module for nano-openclaw.

Mirrors openclaw's config system:
- JSON5 format (supports comments, trailing commas)
- Environment variable substitution with ${VAR_NAME} syntax
- Provider/model-id format for model references
- Pydantic validation (similar to openclaw's Zod schema)
- Path resolution aligns with openclaw (OPENCLAW_CONFIG_PATH, OPENCLAW_STATE_DIR, etc.)
"""

from .types import (
    AgentModelConfig,
    AgentModelListConfig,
    AgentConfig,
    AgentDefaultsConfig,
    AgentsConfig,
    ContextConfig,
    ModelCost,
    ModelDefinition,
    ModelProvider,
    ModelsConfig,
    SessionConfig,
    SessionReset,
    NanoOpenClawConfig,
)
from .env_substitution import (
    MissingEnvVarError,
    EnvSubstitutionWarning,
    resolve_config_env_vars,
)
from .paths import (
    STATE_DIRNAME,
    CONFIG_FILENAME,
    DEFAULT_AGENT_ID,
    resolve_home,
    resolve_state_dir,
    resolve_config_path,
    resolve_default_agent_workspace_dir,
    resolve_agent_workspace_dir,
)
from .io import (
    DEFAULT_CONFIG_FILENAME,
    find_config_file,
    load_config,
    resolve_model_config,
    resolve_api_key,
)

__all__ = [
    # Types
    "AgentModelConfig",
    "AgentModelListConfig",
    "AgentConfig",
    "AgentDefaultsConfig",
    "AgentsConfig",
    "ContextConfig",
    "ModelCost",
    "ModelDefinition",
    "ModelProvider",
    "ModelsConfig",
    "SessionConfig",
    "SessionReset",
    "NanoOpenClawConfig",
    # Env substitution
    "MissingEnvVarError",
    "EnvSubstitutionWarning",
    "resolve_config_env_vars",
    # Path resolution
    "STATE_DIRNAME",
    "CONFIG_FILENAME",
    "DEFAULT_AGENT_ID",
    "resolve_home",
    "resolve_state_dir",
    "resolve_config_path",
    "resolve_default_agent_workspace_dir",
    "resolve_agent_workspace_dir",
    # Config IO
    "DEFAULT_CONFIG_FILENAME",
    "find_config_file",
    "load_config",
    "resolve_model_config",
    "resolve_api_key",
]
