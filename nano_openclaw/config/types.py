"""Pydantic configuration types for nano-openclaw.

Mirrors openclaw's config types from src/config/types.*.ts:
- OpenClawConfig structure alignment
- agents.defaults + agents.list multi-agent support
- models.providers provider catalog
- Session configuration
- Model definition with full openclaw fields
"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ============================================================================
# Thinking Types (aligns with openclaw ThinkLevel)
# ============================================================================

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"]


class ModelThinkingParams(BaseModel):
    """Model thinking parameters (mirrors openclaw model params.thinking)."""
    model_config = ConfigDict(populate_by_name=True)
    
    thinking: Optional[ThinkingLevel] = Field(
        default=None,
        description="Thinking level for this model: off|minimal|low|medium|high|xhigh|adaptive|max"
    )


# ============================================================================
# Model Types (aligns with src/config/types.models.ts)
# ============================================================================

class ModelCost(BaseModel):
    """Model pricing cost (mirrors openclaw ModelDefinitionConfig.cost)."""
    model_config = ConfigDict(populate_by_name=True)
    
    input: float = Field(default=0, description="Input cost per 1M tokens")
    output: float = Field(default=0, description="Output cost per 1M tokens")
    cacheRead: float = Field(default=0, alias="cacheRead", description="Cache read cost")
    cacheWrite: float = Field(default=0, alias="cacheWrite", description="Cache write cost")


class ModelDefinition(BaseModel):
    """Model definition within a provider (mirrors openclaw ModelDefinitionConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    id: str = Field(description="Model ID within this provider")
    name: Optional[str] = Field(default=None, description="Display name")
    input: List[Literal["text", "image", "video", "audio"]] = Field(
        default_factory=lambda: ["text"],
        description="Input modalities"
    )
    reasoning: bool = Field(default=False, description="Whether model supports reasoning")
    contextWindow: int = Field(default=8192, alias="contextWindow", description="Context window size")
    maxTokens: int = Field(default=4096, alias="maxTokens", description="Max output tokens")
    cost: ModelCost = Field(default_factory=ModelCost, description="Pricing cost")
    params: Optional[ModelThinkingParams] = Field(
        default=None,
        description="Model-level params (e.g., thinking)"
    )


class ModelProvider(BaseModel):
    """Provider configuration (mirrors openclaw ModelProviderConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    baseUrl: Optional[str] = Field(default=None, description="Custom endpoint URL")
    apiKey: Optional[str] = Field(default=None, description="API key, supports ${VAR} syntax")
    api: Literal["openai-completions", "openai-responses", "anthropic-messages"] = Field(
        default="openai-completions",
        description="API protocol type"
    )
    models: List[ModelDefinition] = Field(default_factory=list, description="Model catalog")


class ModelsConfig(BaseModel):
    """Models configuration (mirrors openclaw ModelsConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    mode: Literal["merge", "replace"] = Field(
        default="merge",
        description="Provider catalog mode: merge adds to builtins, replace uses only custom"
    )
    providers: Dict[str, ModelProvider] = Field(
        default_factory=dict,
        description="Custom provider definitions"
    )


# ============================================================================
# Agent Types (aligns with src/config/types.agents.ts)
# ============================================================================

class AgentModelListConfig(BaseModel):
    """Model with primary and fallbacks (mirrors openclaw AgentModelListConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    primary: Optional[str] = Field(default=None, description="Primary model (provider/model-id)")
    fallbacks: list[str] = Field(
        default_factory=list,
        description="Fallback models (provider/model-id)"
    )
    timeoutMs: Optional[int] = Field(default=None, alias="timeoutMs", description="Request timeout")

    @field_validator("primary", "fallbacks", mode="before")
    @classmethod
    def validate_model_ref(cls, v: Any) -> Any:
        if isinstance(v, str) and v and "/" not in v:
            raise ValueError(f"Model reference must be in provider/model-id format: {v}")
        return v


AgentModelConfig = Union[str, AgentModelListConfig]


class AgentConfig(BaseModel):
    """Individual agent configuration (mirrors openclaw AgentConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    id: str = Field(description="Agent unique identifier")
    default: bool = Field(default=False, description="Whether this is the default agent")
    name: Optional[str] = Field(default=None, description="Display name")
    workspace: Optional[str] = Field(default=None, description="Working directory path")
    model: Optional[AgentModelConfig] = Field(default=None, description="Model override")
    imageModel: Optional[AgentModelConfig] = Field(default=None, description="Image model override")


class AgentDefaultsConfig(BaseModel):
    """Agent defaults configuration (mirrors openclaw AgentDefaultsConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    model: AgentModelConfig = Field(
        default="anthropic/claude-sonnet-4-5-20250929",
        description="Default primary model (provider/model-id or {primary, fallbacks})"
    )
    imageModel: Optional[AgentModelConfig] = Field(
        default=None,
        description="Default image model for Media Understanding. None = Native Vision"
    )
    workspace: Optional[str] = Field(default=None, description="Default workspace directory")
    contextTokens: Optional[int] = Field(default=None, description="Context window token limit")
    thinkingDefault: Optional[str] = Field(
        default=None,
        description="Default thinking mode: off|minimal|low|medium|high|xhigh|adaptive|max"
    )

    @field_validator("model", mode="before")
    @classmethod
    def validate_model(cls, v: Any) -> Any:
        if isinstance(v, str) and "/" not in v:
            raise ValueError(f"Model reference must be in provider/model-id format: {v}")
        return v


class AgentsConfig(BaseModel):
    """Agents configuration (mirrors openclaw AgentsConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    defaults: AgentDefaultsConfig = Field(default_factory=AgentDefaultsConfig)
    list: List[AgentConfig] = Field(default_factory=list)


# ============================================================================
# Session Types (aligns with src/config/types.base.ts SessionConfig)
# ============================================================================

class SessionReset(BaseModel):
    """Session reset configuration (mirrors openclaw SessionConfig.reset)."""
    model_config = ConfigDict(populate_by_name=True)
    
    mode: Literal["daily", "idle"] = Field(default="idle", description="Reset mode")
    idleMinutes: int = Field(default=120, alias="idleMinutes", description="Idle minutes before reset")


class SessionConfig(BaseModel):
    """Session configuration (mirrors openclaw SessionConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    idleMinutes: int = Field(default=60, alias="idleMinutes", description="Idle timeout in minutes")
    reset: SessionReset = Field(default_factory=SessionReset)


# ============================================================================
# Context Types (nano-openclaw specific)
# ============================================================================

class ContextConfig(BaseModel):
    """Context compaction settings (nano-openclaw specific, mirrors openclaw compaction config)."""
    model_config = ConfigDict(populate_by_name=True)
    
    budget: int = Field(default=100000, ge=1000, description="Maximum token budget for context window")
    threshold: float = Field(default=0.8, ge=0.1, le=1.0, description="Trigger compaction at this fraction of budget")
    recent_turns: int = Field(default=3, ge=1, alias="recent_turns", description="Recent turns to preserve during compaction")


# ============================================================================
# Main Config (aligns with src/config/types.openclaw.ts OpenClawConfig)
# ============================================================================

class NanoOpenClawConfig(BaseModel):
    """
    nano-openclaw configuration (aligns with openclaw's OpenClawConfig).
    
    Structure mirrors openclaw:
    - agents: { defaults, list[] }
    - models: { mode, providers{} }
    - session: { idleMinutes, reset }
    - Custom fields: maxIterations, context
    """
    model_config = ConfigDict(populate_by_name=True)
    
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    
    # nano-openclaw custom fields
    noTools: bool = Field(default=False, description="Run as plain chatbot, no tools")
    maxIterations: int = Field(default=12, ge=1, description="Max tool-use rounds per user turn")
    context: ContextConfig = Field(default_factory=ContextConfig)

    def resolve_primary_model(self, agent_id: Optional[str] = None) -> str:
        """
        Resolve primary model for an agent.
        
        Priority:
        1. agents.list[].model (if agent found)
        2. agents.defaults.model
        3. Fallback default
        """
        # Check agent-specific model
        if agent_id:
            for agent in self.agents.list:
                if agent.id == agent_id and agent.model:
                    model_config = agent.model
                    if isinstance(model_config, str):
                        return model_config
                    if isinstance(model_config, AgentModelListConfig):
                        return model_config.primary or "anthropic/claude-sonnet-4-5-20250929"
        
        # Fall back to defaults
        model_config = self.agents.defaults.model
        if isinstance(model_config, str):
            return model_config
        if isinstance(model_config, AgentModelListConfig):
            return model_config.primary or "anthropic/claude-sonnet-4-5-20250929"
        
        return "anthropic/claude-sonnet-4-5-20250929"

    def resolve_image_model(self, agent_id: Optional[str] = None) -> Optional[str]:
        """
        Resolve image model for an agent.
        
        Priority:
        1. agents.list[].imageModel (if agent found)
        2. agents.defaults.imageModel
        """
        # Check agent-specific
        if agent_id:
            for agent in self.agents.list:
                if agent.id == agent_id and agent.imageModel:
                    image_model_config = agent.imageModel
                    if isinstance(image_model_config, str):
                        return image_model_config
                    if isinstance(image_model_config, AgentModelListConfig):
                        return image_model_config.primary
                    return None
        
        # Fall back to defaults
        image_model_config = self.agents.defaults.imageModel
        if image_model_config is None:
            return None
        if isinstance(image_model_config, str):
            return image_model_config
        if isinstance(image_model_config, AgentModelListConfig):
            return image_model_config.primary
        return None

    def resolve_thinking_level(self, model_ref: str) -> ThinkingLevel:
        """
        Resolve thinking level for a model.
        
        Priority (mirrors openclaw):
        1. models.providers[provider].models[id].params.thinking
        2. agents.defaults.thinkingDefault
        3. Fallback: "off" (non-reasoning models) or "low" (reasoning models)
        
        Args:
            model_ref: Model reference in "provider/model-id" format
        
        Returns:
            ThinkingLevel: The resolved thinking level
        """
        # Parse model reference
        if "/" not in model_ref:
            return "off"
        provider_id, model_id = model_ref.split("/", 1)
        
        # Check model-level params.thinking (highest priority)
        provider = self.models.providers.get(provider_id)
        if provider:
            for model in provider.models:
                if model.id == model_id and model.params and model.params.thinking:
                    return model.params.thinking
        
        # Check global default
        if self.agents.defaults.thinkingDefault:
            return self.agents.defaults.thinkingDefault
        
        # Check model's reasoning capability for fallback
        if provider:
            for model in provider.models:
                if model.id == model_id:
                    return "low" if model.reasoning else "off"
        
        return "off"
