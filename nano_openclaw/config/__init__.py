"""Configuration module for nano-openclaw.

Mirrors openclaw's config system:
- JSON5 format (supports comments, trailing commas)
- Environment variable substitution with ${VAR_NAME} syntax
- Provider/model-id format for model references
- Pydantic validation (similar to openclaw's Zod schema)
"""

from .types import (
    AgentModelConfig,
    AgentDefaultsConfig,
    ContextConfig,
    ModelDefinition,
    ModelProvider,
    ModelsConfig,
    NanoOpenClawConfig,
)
from .env_substitution import (
    MissingEnvVarError,
    EnvSubstitutionWarning,
    resolve_config_env_vars,
)
from .io import (
    DEFAULT_CONFIG_FILENAME,
    find_config_file,
    load_config,
    resolve_model_config,
    resolve_api_key,
)

__all__ = [
    "AgentModelConfig",
    "AgentDefaultsConfig",
    "ContextConfig",
    "ModelDefinition",
    "ModelProvider",
    "ModelsConfig",
    "NanoOpenClawConfig",
    "MissingEnvVarError",
    "EnvSubstitutionWarning",
    "resolve_config_env_vars",
    "DEFAULT_CONFIG_FILENAME",
    "find_config_file",
    "load_config",
    "resolve_model_config",
    "resolve_api_key",
]