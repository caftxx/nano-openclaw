"""Tests for config/io.py.

Tests config file loading and model resolution.
"""

import json5
import pytest
from pathlib import Path
from unittest.mock import patch
import tempfile
import os

from nano_openclaw.config.io import (
    DEFAULT_CONFIG_FILENAME,
    find_config_file,
    load_config,
    resolve_model_config,
    resolve_api_key,
    BUILTIN_PROVIDERS,
)
from nano_openclaw.config.types import (
    NanoOpenClawConfig,
    ModelDefinition,
    ModelProvider,
    ModelsConfig,
)


class TestFindConfigFile:
    def test_explicit_path_exists(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json5', delete=False) as f:
            f.write('{}')
            f.close()
            path = find_config_file(f.name)
            assert path is not None
            assert path.name.endswith('.json5')
            os.unlink(f.name)
    
    def test_explicit_path_not_exists(self):
        path = find_config_file("/nonexistent/path/config.json5")
        assert path is None
    
    def test_default_path_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / DEFAULT_CONFIG_FILENAME
            config_path.write_text('{}')
            env = {"OPENCLAW_STATE_DIR": tmpdir}
            path = find_config_file(env=env)
            assert path is not None
            assert path.name == DEFAULT_CONFIG_FILENAME
    
    def test_default_path_not_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"OPENCLAW_STATE_DIR": tmpdir}
            path = find_config_file(env=env)
            assert path is None


class TestLoadConfig:
    def test_no_config_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"OPENCLAW_STATE_DIR": tmpdir}
            cfg, warnings = load_config(env=env)
            assert cfg.agents.defaults.model == "anthropic/claude-sonnet-4-5-20250929"
            assert len(warnings) == 0
    
    def test_load_simple_config(self):
        content = '''
        {
            agents: {
                defaults: {
                    model: "openai/gpt-4o",
                },
            },
        }
        '''
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / DEFAULT_CONFIG_FILENAME
            config_path.write_text(content)
            env = {"OPENCLAW_STATE_DIR": tmpdir}
            cfg, warnings = load_config(env=env)
            assert cfg.agents.defaults.model == "openai/gpt-4o"
            assert len(warnings) == 0
    
    def test_env_var_substitution(self):
        content = '''
        {
            models: {
                providers: {
                    "custom": {
                        baseUrl: "https://api.custom.com/v1",
                        apiKey: "${TEST_API_KEY}",
                    },
                },
            },
        }
        '''
        env = {"TEST_API_KEY": "secret123", "OPENCLAW_STATE_DIR": tempfile.mkdtemp()}
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / DEFAULT_CONFIG_FILENAME
            config_path.write_text(content)
            env["OPENCLAW_STATE_DIR"] = tmpdir
            cfg, warnings = load_config(env=env)
            assert cfg.models.providers["custom"].apiKey == "secret123"
            assert len(warnings) == 0
    
    def test_missing_env_var_warning(self):
        content = '''
        {
            models: {
                providers: {
                    "custom": {
                        baseUrl: "https://api.custom.com/v1",
                        apiKey: "${MISSING_KEY}",
                    },
                },
            },
        }
        '''
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / DEFAULT_CONFIG_FILENAME
            config_path.write_text(content)
            env = {"OPENCLAW_STATE_DIR": tmpdir}
            cfg, warnings = load_config(env=env)
            assert len(warnings) == 1
            assert warnings[0][0] == "MISSING_KEY"
    
    def test_comments_and_trailing_commas(self):
        content = '''
        {
            // This is a comment
            agents: {
                defaults: {
                    model: "anthropic/claude-sonnet",
                    imageModel: null,  // trailing comma
                },
            },
        }
        '''
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / DEFAULT_CONFIG_FILENAME
            config_path.write_text(content)
            env = {"OPENCLAW_STATE_DIR": tmpdir}
            cfg, warnings = load_config(env=env)
            assert cfg.agents.defaults.model == "anthropic/claude-sonnet"


class TestResolveModelConfig:
    def test_builtin_anthropic(self):
        cfg = NanoOpenClawConfig()
        env = {"ANTHROPIC_API_KEY": "test_key"}
        result = resolve_model_config("anthropic/claude-sonnet-4", cfg, env)
        assert result["provider_id"] == "anthropic"
        assert result["model_id"] == "claude-sonnet-4"
        assert result["api_type"] == "anthropic-messages"
        assert result["base_url"] is None
    
    def test_builtin_openai(self):
        cfg = NanoOpenClawConfig()
        env = {"OPENAI_API_KEY": "test_key"}
        result = resolve_model_config("openai/gpt-4o", cfg, env)
        assert result["provider_id"] == "openai"
        assert result["model_id"] == "gpt-4o"
        assert result["api_type"] == "openai-completions"
    
    def test_custom_provider(self):
        cfg = NanoOpenClawConfig(
            models=ModelsConfig(
                providers={
                    "custom": ModelProvider(
                        baseUrl="https://api.custom.com/v1",
                        api="openai-completions",
                        apiKey="custom_key",
                    )
                }
            )
        )
        env = {}
        result = resolve_model_config("custom/my-model", cfg, env)
        assert result["provider_id"] == "custom"
        assert result["model_id"] == "my-model"
        assert result["base_url"] == "https://api.custom.com/v1"
    
    def test_unknown_provider(self):
        cfg = NanoOpenClawConfig()
        with pytest.raises(ValueError) as exc_info:
            resolve_model_config("unknown/model", cfg)
        assert "Unknown provider" in str(exc_info.value)
    
    def test_invalid_format_no_slash(self):
        cfg = NanoOpenClawConfig()
        with pytest.raises(ValueError) as exc_info:
            resolve_model_config("invalid-model", cfg)
        assert "provider/model-id format" in str(exc_info.value)
    
    def test_builtin_model_input_has_image(self):
        cfg = NanoOpenClawConfig()
        env = {"ANTHROPIC_API_KEY": "test_key"}
        result = resolve_model_config("anthropic/claude-sonnet-4", cfg, env)
        assert "image" in result["model_input"]
    
    def test_custom_provider_model_input(self):
        cfg = NanoOpenClawConfig(
            models=ModelsConfig(
                providers={
                    "custom": ModelProvider(
                        baseUrl="https://api.custom.com/v1",
                        api="openai-completions",
                        apiKey="custom_key",
                        models=[
                            ModelDefinition(id="text-only", input=["text"]),
                            ModelDefinition(id="vision-model", input=["text", "image"]),
                        ],
                    )
                }
            )
        )
        env = {}
        result1 = resolve_model_config("custom/text-only", cfg, env)
        assert result1["model_input"] == ["text"]
        
        result2 = resolve_model_config("custom/vision-model", cfg, env)
        assert result2["model_input"] == ["text", "image"]
    
    def test_unknown_model_defaults_to_text(self):
        cfg = NanoOpenClawConfig(
            models=ModelsConfig(
                providers={
                    "custom": ModelProvider(
                        baseUrl="https://api.custom.com/v1",
                        apiKey="custom_key",
                    )
                }
            )
        )
        env = {}
        result = resolve_model_config("custom/unknown-model", cfg, env)
        assert result["model_input"] == ["text"]


class TestResolveApiKey:
    def test_env_var_priority(self):
        env = {"ANTHROPIC_API_KEY": "env_key"}
        provider_config = ModelProvider(apiKey="config_key")
        result = resolve_api_key("anthropic", provider_config, env)
        assert result == "env_key"
    
    def test_config_key_when_no_env(self):
        env = {}
        provider_config = ModelProvider(apiKey="config_key")
        result = resolve_api_key("anthropic", provider_config, env)
        assert result == "config_key"
    
    def test_builtin_provider_env_key(self):
        env = {"ANTHROPIC_API_KEY": "test_key"}
        result = resolve_api_key("anthropic", None, env)
        assert result == "test_key"
    
    def test_custom_provider_env_key_convention(self):
        env = {"CUSTOM_API_KEY": "custom_key"}
        result = resolve_api_key("custom", None, env)
        assert result == "custom_key"
    
    def test_missing_key_error(self):
        env = {}
        with pytest.raises(ValueError) as exc_info:
            resolve_api_key("anthropic", None, env)
        assert "No API key" in str(exc_info.value)


class TestNanoOpenClawConfig:
    def test_full_config_scenario(self):
        content = '''
        {
            agents: {
                defaults: {
                    model: "openrouter/anthropic/claude-sonnet-4",
                    imageModel: "openai/gpt-4o-mini",
                },
            },
            models: {
                mode: "merge",
                providers: {
                    "openrouter": {
                        baseUrl: "https://openrouter.ai/api/v1",
                        apiKey: "${OPENROUTER_API_KEY}",
                        api: "openai-completions",
                        models: [
                            { id: "anthropic/claude-sonnet-4", name: "Claude Sonnet 4" },
                            { id: "openai/gpt-4o", name: "GPT-4o" },
                        ],
                    },
                },
            },
            noTools: false,
            maxIterations: 15,
            context: {
                budget: 80000,
                threshold: 0.7,
            },
        }
        '''
        env = {"OPENROUTER_API_KEY": "router_key"}
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / DEFAULT_CONFIG_FILENAME
            config_path.write_text(content)
            env["OPENCLAW_STATE_DIR"] = tmpdir
            cfg, warnings = load_config(env=env)
            
            assert cfg.agents.defaults.model == "openrouter/anthropic/claude-sonnet-4"
            assert cfg.agents.defaults.imageModel == "openai/gpt-4o-mini"
            assert cfg.resolve_primary_model() == "openrouter/anthropic/claude-sonnet-4"
            assert cfg.resolve_image_model() == "openai/gpt-4o-mini"
            assert cfg.maxIterations == 15
            assert cfg.context.budget == 80000
            
            resolved = resolve_model_config("openrouter/anthropic/claude-sonnet-4", cfg, env)
            assert resolved["base_url"] == "https://openrouter.ai/api/v1"
            assert resolved["api_key"] == "router_key"