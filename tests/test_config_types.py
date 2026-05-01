"""Tests for config/types.py.

Tests Pydantic configuration types validation.
"""

import pytest
from nano_openclaw.config.types import (
    AgentConfig,
    AgentDefaultsConfig,
    AgentModelListConfig,
    AgentsConfig,
    ContextConfig,
    ModelCost,
    ModelDefinition,
    ModelProvider,
    ModelsConfig,
    NanoOpenClawConfig,
    SessionConfig,
    SessionReset,
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
        md = ModelDefinition(id="gpt-4o", name="GPT-4o")
        assert md.id == "gpt-4o"
    
    def test_with_aliases(self):
        md = ModelDefinition(id="gpt-4o", name="GPT-4o", contextWindow=128000, maxTokens=4096)
        assert md.contextWindow == 128000
        assert md.maxTokens == 4096


class TestModelProvider:
    def test_default_api(self):
        mp = ModelProvider(baseUrl="https://api.example.com/v1")
        assert mp.api == "openai-completions"
    
    def test_custom_base_url(self):
        mp = ModelProvider(baseUrl="https://api.example.com/v1")
        assert mp.baseUrl == "https://api.example.com/v1"
    
    def test_with_models(self):
        mp = ModelProvider(
            baseUrl="https://api.example.com/v1",
            models=[ModelDefinition(id="gpt-4o", name="GPT-4o")]
        )
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
        assert ad.imageModel == "openai/gpt-4o-mini"
    
    def test_workspace_field(self):
        ad = AgentDefaultsConfig(workspace="./workspace")
        assert ad.workspace == "./workspace"
    
    def test_context_tokens_field(self):
        ad = AgentDefaultsConfig(contextTokens=128000)
        assert ad.contextTokens == 128000
    
    def test_thinking_default_field(self):
        ad = AgentDefaultsConfig(thinkingDefault="low")
        assert ad.thinkingDefault == "low"
    
    def test_invalid_model_no_slash(self):
        with pytest.raises(Exception):
            AgentDefaultsConfig(model="claude-sonnet")


class TestNanoOpenClawConfig:
    def test_default_values(self):
        cfg = NanoOpenClawConfig()
        assert cfg.agents.defaults.model == "anthropic/claude-sonnet-4-5-20250929"
        assert cfg.maxIterations == 12
        assert cfg.maxTokens == 4096
    
    def test_agents_structure(self):
        cfg = NanoOpenClawConfig()
        assert isinstance(cfg.agents, AgentsConfig)
        assert isinstance(cfg.agents.defaults, AgentDefaultsConfig)
        assert isinstance(cfg.agents.list, list)
    
    def test_session_config(self):
        cfg = NanoOpenClawConfig()
        assert isinstance(cfg.session, SessionConfig)
        assert cfg.session.idleMinutes == 60
        assert cfg.session.reset.mode == "idle"
        assert cfg.session.reset.idleMinutes == 120
    
    def test_resolve_primary_model_default_agent(self):
        cfg = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(model="openai/gpt-4o")
            )
        )
        assert cfg.resolve_primary_model() == "openai/gpt-4o"
    
    def test_resolve_primary_model_with_agent_id(self):
        cfg = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(model="openai/gpt-4o"),
                list=[
                    AgentConfig(id="coder", model="anthropic/claude-sonnet"),
                ],
            )
        )
        assert cfg.resolve_primary_model("coder") == "anthropic/claude-sonnet"
        assert cfg.resolve_primary_model("default") == "openai/gpt-4o"
    
    def test_resolve_primary_model_with_fallbacks(self):
        cfg = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(
                    model=AgentModelListConfig(primary="openai/gpt-4o", fallbacks=["anthropic/claude-sonnet"])
                )
            )
        )
        assert cfg.resolve_primary_model() == "openai/gpt-4o"
    
    def test_resolve_image_model_none(self):
        cfg = NanoOpenClawConfig()
        assert cfg.resolve_image_model() is None
    
    def test_resolve_image_model_string(self):
        cfg = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(imageModel="openai/gpt-4o-mini")
            )
        )
        assert cfg.resolve_image_model() == "openai/gpt-4o-mini"
    
    def test_aliases_work(self):
        cfg = NanoOpenClawConfig(
            agents=AgentsConfig(
                defaults=AgentDefaultsConfig(model="openai/gpt-4o", imageModel="openai/gpt-4o-mini")
            ),
            noTools=True,
            maxIterations=20,
            context=ContextConfig(budget=50000),
        )
        assert cfg.noTools is True
        assert cfg.maxIterations == 20
        assert cfg.context.budget == 50000
    
    def test_custom_provider_config(self):
        cfg = NanoOpenClawConfig(
            models=ModelsConfig(
                providers={
                    "custom": ModelProvider(
                        baseUrl="https://api.custom.com/v1",
                        apiKey="${CUSTOM_API_KEY}",
                        models=[ModelDefinition(id="custom-model", name="Custom Model")]
                    )
                }
            )
        )
        assert cfg.models.providers["custom"].baseUrl == "https://api.custom.com/v1"


# =============================================================================
# New Type Tests (AgentsConfig, AgentConfig, ModelCost, SessionConfig)
# =============================================================================

class TestModelCost:
    def test_default_values(self):
        cost = ModelCost()
        assert cost.input == 0
        assert cost.output == 0
        assert cost.cacheRead == 0
        assert cost.cacheWrite == 0
    
    def test_custom_values(self):
        cost = ModelCost(input=3.0, output=15.0, cacheRead=0.3, cacheWrite=0.5)
        assert cost.input == 3.0
        assert cost.output == 15.0
    
    def test_aliases_work(self):
        cost = ModelCost(cacheRead=1.5, cacheWrite=2.0)
        assert cost.cacheRead == 1.5
        assert cost.cacheWrite == 2.0


class TestModelDefinitionExtended:
    def test_full_definition(self):
        md = ModelDefinition(
            id="claude-sonnet",
            name="Claude Sonnet",
            input=["text", "image"],
            reasoning=True,
            contextWindow=131072,
            maxTokens=8192,
            cost=ModelCost(input=3.0, output=15.0),
        )
        assert md.id == "claude-sonnet"
        assert md.name == "Claude Sonnet"
        assert "image" in md.input
        assert md.reasoning is True
        assert md.contextWindow == 131072
        assert md.maxTokens == 8192
    
    def test_defaults(self):
        md = ModelDefinition(id="simple-model", name="Simple")
        assert md.input == ["text"]
        assert md.reasoning is False
        assert md.contextWindow == 8192
        assert md.maxTokens == 4096
        assert md.cost.input == 0


class TestAgentsConfig:
    def test_default_structure(self):
        ac = AgentsConfig()
        assert isinstance(ac.defaults, AgentDefaultsConfig)
        assert ac.list == []
    
    def test_with_agents_list(self):
        ac = AgentsConfig(
            defaults=AgentDefaultsConfig(model="openai/gpt-4o"),
            list=[
                AgentConfig(id="default", default=True, name="Default Agent"),
                AgentConfig(id="coder", name="Coding Agent"),
            ],
        )
        assert len(ac.list) == 2
        assert ac.list[0].id == "default"
        assert ac.list[0].default is True
        assert ac.list[1].id == "coder"


class TestAgentConfig:
    def test_minimal_config(self):
        ac = AgentConfig(id="test-agent")
        assert ac.id == "test-agent"
        assert ac.default is False
        assert ac.name is None
        assert ac.workspace is None
        assert ac.model is None
    
    def test_full_config(self):
        ac = AgentConfig(
            id="coder",
            default=False,
            name="Coding Agent",
            workspace="./workspace-coder",
            model="anthropic/claude-sonnet",
            imageModel="openai/gpt-4o",
        )
        assert ac.id == "coder"
        assert ac.name == "Coding Agent"
        assert ac.workspace == "./workspace-coder"
        assert ac.model == "anthropic/claude-sonnet"
        assert ac.imageModel == "openai/gpt-4o"


class TestSessionConfig:
    def test_default_values(self):
        sc = SessionConfig()
        assert sc.idleMinutes == 60
        assert sc.reset.mode == "idle"
        assert sc.reset.idleMinutes == 120
    
    def test_custom_values(self):
        sc = SessionConfig(
            idleMinutes=120,
            reset=SessionReset(mode="daily", idleMinutes=180),
        )
        assert sc.idleMinutes == 120
        assert sc.reset.mode == "daily"
        assert sc.reset.idleMinutes == 180
    
    def test_reset_mode_validation(self):
        """Reset mode must be either 'daily' or 'idle'."""
        with pytest.raises(Exception):
            SessionReset(mode="invalid")
