"""Tests for config/types.py.

Tests Pydantic configuration types validation.
"""

import pytest
from nano_openclaw.config.types import (
    AgentDefaultsConfig,
    AgentModelListConfig,
    ContextConfig,
    ModelDefinition,
    ModelProvider,
    ModelsConfig,
    NanoOpenClawConfig,
)


class TestContextConfig:
    def test_default_values(self):
        cfg = ContextConfig()
        assert cfg.budget == 100000
        assert cfg.threshold == 0.8
        assert cfg.recent_turns == 3
    
    def test_custom_values(self):
        cfg = ContextConfig(budget=50000, threshold=0.5, recent_turns=5)
        assert cfg.budget == 50000
        assert cfg.threshold == 0.5
        assert cfg.recent_turns == 5
    
    def test_invalid_threshold(self):
        with pytest.raises(Exception):
            ContextConfig(threshold=1.5)
        with pytest.raises(Exception):
            ContextConfig(threshold=0.05)


class TestAgentModelListConfig:
    def test_primary_model(self):
        cfg = AgentModelListConfig(primary="anthropic/claude-sonnet")
        assert cfg.primary == "anthropic/claude-sonnet"
    
    def test_with_fallbacks(self):
        cfg = AgentModelListConfig(
            primary="anthropic/claude-sonnet",
            fallbacks=["openai/gpt-4o"]
        )
        assert cfg.fallbacks == ["openai/gpt-4o"]
    
    def test_invalid_model_ref_no_slash(self):
        with pytest.raises(Exception):
            AgentModelListConfig(primary="claude-sonnet")


class TestModelDefinition:
    def test_minimal(self):
        md = ModelDefinition(id="gpt-4o")
        assert md.id == "gpt-4o"
    
    def test_with_aliases(self):
        md = ModelDefinition(id="gpt-4o", contextWindow=128000, maxTokens=4096)
        assert md.context_window == 128000
        assert md.max_tokens == 4096


class TestModelProvider:
    def test_default_api(self):
        mp = ModelProvider()
        assert mp.api == "openai-completions"
    
    def test_custom_base_url(self):
        mp = ModelProvider(baseUrl="https://api.example.com/v1")
        assert mp.base_url == "https://api.example.com/v1"
    
    def test_with_models(self):
        mp = ModelProvider(models=[ModelDefinition(id="gpt-4o")])
        assert len(mp.models) == 1


class TestModelsConfig:
    def test_default_merge_mode(self):
        mc = ModelsConfig()
        assert mc.mode == "merge"
    
    def test_custom_providers(self):
        mc = ModelsConfig(
            providers={
                "openrouter": ModelProvider(baseUrl="https://openrouter.ai/api/v1")
            }
        )
        assert "openrouter" in mc.providers


class TestAgentDefaultsConfig:
    def test_default_model(self):
        ad = AgentDefaultsConfig()
        assert ad.model == "anthropic/claude-sonnet-4-5-20250929"
    
    def test_custom_model_string(self):
        ad = AgentDefaultsConfig(model="openai/gpt-4o")
        assert ad.model == "openai/gpt-4o"
    
    def test_custom_model_object(self):
        ad = AgentDefaultsConfig(
            model=AgentModelListConfig(primary="anthropic/claude-sonnet", fallbacks=["openai/gpt-4o"])
        )
        assert isinstance(ad.model, AgentModelListConfig)
    
    def test_image_model_alias(self):
        ad = AgentDefaultsConfig(imageModel="openai/gpt-4o-mini")
        assert ad.image_model == "openai/gpt-4o-mini"
    
    def test_invalid_model_no_slash(self):
        with pytest.raises(Exception):
            AgentDefaultsConfig(model="claude-sonnet")


class TestNanoOpenClawConfig:
    def test_default_values(self):
        cfg = NanoOpenClawConfig()
        assert cfg.agents.model == "anthropic/claude-sonnet-4-5-20250929"
        assert cfg.max_iterations == 12
        assert cfg.max_tokens == 4096
    
    def test_resolve_primary_model(self):
        cfg = NanoOpenClawConfig(agents=AgentDefaultsConfig(model="openai/gpt-4o"))
        assert cfg.resolve_primary_model() == "openai/gpt-4o"
    
    def test_resolve_primary_model_with_fallbacks(self):
        cfg = NanoOpenClawConfig(
            agents=AgentDefaultsConfig(
                model=AgentModelListConfig(primary="openai/gpt-4o", fallbacks=["anthropic/claude-sonnet"])
            )
        )
        assert cfg.resolve_primary_model() == "openai/gpt-4o"
    
    def test_resolve_image_model_none(self):
        cfg = NanoOpenClawConfig()
        assert cfg.resolve_image_model() is None
    
    def test_resolve_image_model_string(self):
        cfg = NanoOpenClawConfig(agents=AgentDefaultsConfig(imageModel="openai/gpt-4o-mini"))
        assert cfg.resolve_image_model() == "openai/gpt-4o-mini"
    
    def test_aliases_work(self):
        cfg = NanoOpenClawConfig(
            agents=AgentDefaultsConfig(model="openai/gpt-4o", imageModel="openai/gpt-4o-mini"),
            noTools=True,
            maxIterations=20,
            context=ContextConfig(budget=50000),
        )
        assert cfg.no_tools is True
        assert cfg.max_iterations == 20
        assert cfg.context.budget == 50000
    
    def test_custom_provider_config(self):
        cfg = NanoOpenClawConfig(
            models=ModelsConfig(
                providers={
                    "custom": ModelProvider(
                        baseUrl="https://api.custom.com/v1",
                        apiKey="${CUSTOM_API_KEY}",
                        models=[ModelDefinition(id="custom-model")]
                    )
                }
            )
        )
        assert cfg.models.providers["custom"].base_url == "https://api.custom.com/v1"