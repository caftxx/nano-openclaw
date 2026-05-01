"""Configuration loading and resolution.

Mirrors openclaw's src/config/io.ts:
- resolve_config_path: locate config file (delegates to paths.py)
- load_config: load + parse JSON5 + env substitution + validation
- resolve_model_config: resolve provider/model-id reference
- resolve_api_key: resolve API key with env var priority

Path resolution aligns with openclaw:
1. OPENCLAW_CONFIG_PATH environment variable
2. {stateDir}/nano-openclaw.json5
3. {cwd}/workspace/nano-openclaw.json5
4. ~/.openclaw/nano-openclaw.json5
"""

from __future__ import annotations

import json5
import os
from pathlib import Path
from typing import Any, Optional

from .types import NanoOpenClawConfig, ModelProvider
from .env_substitution import resolve_config_env_vars, EnvSubstitutionWarning
from .paths import resolve_config_path as _resolve_config_path


# Default config filename for backward compatibility
DEFAULT_CONFIG_FILENAME = "nano-openclaw.json5"

DEFAULT_INPUT_CAPABILITIES = ("text",)

DEFAULT_MAX_TOKENS = 4096

BUILTIN_PROVIDERS = {
    "anthropic": {
        "api": "anthropic-messages",
        "baseUrl": None,
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-5-20250929",
    },
    "openai": {
        "api": "openai-completions",
        "baseUrl": None,
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
}


def _resolve_model_input(provider_id: str, model_id: str, config: NanoOpenClawConfig) -> list[str]:
    """Resolve model input modalities from config or builtin defaults."""
    provider_config = config.models.providers.get(provider_id)
    if provider_config:
        for m in provider_config.models:
            if m.id == model_id:
                return m.input if m.input else ["text"]
    
    builtin = BUILTIN_PROVIDERS.get(provider_id)
    if builtin:
        return list(DEFAULT_INPUT_CAPABILITIES)
    
    return ["text"]


def _resolve_model_max_tokens(provider_id: str, model_id: str, config: NanoOpenClawConfig) -> int:
    """Resolve max output tokens from model config or default."""
    provider_config = config.models.providers.get(provider_id)
    if provider_config:
        for m in provider_config.models:
            if m.id == model_id:
                return m.maxTokens if m.maxTokens > 0 else DEFAULT_MAX_TOKENS
    
    return DEFAULT_MAX_TOKENS


def find_config_file(config_path: Optional[str] = None, env: Optional[dict[str, str]] = None) -> Optional[Path]:
    """
    Find config file using openclaw-aligned path resolution.
    
    Args:
        config_path: Explicit config path (from --config argument)
        env: Environment variables (defaults to os.environ)
    
    Returns:
        Path to config file if it exists, None otherwise
    """
    resolved = _resolve_config_path(config_path, env)
    return resolved if resolved.exists() else None


def load_config(
    config_path: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> tuple[NanoOpenClawConfig, list[EnvSubstitutionWarning]]:
    """
    Load config file with environment variable substitution.
    
    Mirrors openclaw's loadConfig() flow:
    1. Resolve config path
    2. Read and parse JSON5
    3. Apply environment variable substitution
    4. Validate with Pydantic schema
    
    Args:
        config_path: Explicit config path (from --config argument)
        env: Environment variables for substitution
    
    Returns:
        Tuple of (config, warnings) where warnings are missing env var references
    """
    if env is None:
        env = dict(os.environ)
    
    path = find_config_file(config_path, env)
    if not path:
        return NanoOpenClawConfig(), []
    
    raw = path.read_text(encoding="utf-8")
    parsed = json5.loads(raw)
    
    warnings: list[EnvSubstitutionWarning] = []
    resolved = resolve_config_env_vars(parsed, env, on_missing=lambda v, p: warnings.append((v, p)))
    
    config = NanoOpenClawConfig.model_validate(resolved)
    
    return config, warnings


def resolve_model_config(
    model_ref: str,
    config: NanoOpenClawConfig,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """
    Resolve model reference to provider config.
    
    Args:
        model_ref: Model reference in provider/model-id format
        config: nano-openclaw config
        env: Environment variables
    
    Returns:
        Dict with provider_id, model_id, api_type, base_url, api_key, model_input, max_tokens
    """
    if env is None:
        env = dict(os.environ)
    
    if "/" not in model_ref:
        raise ValueError(f"Model reference must be in provider/model-id format: {model_ref}")
    
    provider_id, model_id = model_ref.split("/", 1)
    
    provider_config = config.models.providers.get(provider_id)
    
    if provider_config:
        base_url = provider_config.baseUrl
        api_type = provider_config.api
    elif provider_id in BUILTIN_PROVIDERS:
        builtin = BUILTIN_PROVIDERS[provider_id]
        base_url = builtin["baseUrl"]
        api_type = builtin["api"]
    else:
        raise ValueError(f"Unknown provider: {provider_id}")
    
    api_key = resolve_api_key(provider_id, provider_config, env)
    model_input = _resolve_model_input(provider_id, model_id, config)
    max_tokens = _resolve_model_max_tokens(provider_id, model_id, config)
    
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "api_type": api_type,
        "base_url": base_url,
        "api_key": api_key,
        "model_input": model_input,
        "max_tokens": max_tokens,
    }


def resolve_api_key(
    provider_id: str,
    provider_config: Optional[ModelProvider],
    env: Optional[dict[str, str]] = None,
) -> str:
    """
    Resolve API key for a provider.
    
    Priority:
    1. Environment variable (highest)
    2. Config file apiKey
    
    Args:
        provider_id: Provider ID
        provider_config: Provider config from models.providers
        env: Environment variables
    
    Returns:
        API key string
    
    Raises:
        ValueError: If no API key is available
    """
    if env is None:
        env = dict(os.environ)
    
    env_key = BUILTIN_PROVIDERS.get(provider_id, {}).get("env_key")
    if env_key is None:
        env_key = f"{provider_id.upper().replace('-', '_')}_API_KEY"
    
    if env.get(env_key):
        return env[env_key]
    
    if provider_config and provider_config.apiKey:
        return provider_config.apiKey
    
    hint = ""
    if provider_id == "anthropic":
        hint = "Get a key at https://console.anthropic.com"
    elif provider_id == "openai":
        hint = "Get a key at https://platform.openai.com/api-keys"
    else:
        hint = f"Set {env_key} environment variable or add apiKey to config"
    
    raise ValueError(
        f"No API key for provider '{provider_id}'.\n"
        f"  {hint}\n"
        f"    export {env_key}=...   (Linux/macOS/Git Bash)\n"
        f"    setx   {env_key} ...   (Windows — open a new terminal after)"
    )
