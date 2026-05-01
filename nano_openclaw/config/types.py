"""Pydantic configuration types for nano-openclaw.

Mirrors openclaw's config types:
- AgentModelConfig: string or {primary, fallbacks} (same as openclaw AgentModelConfig)
- agents.defaults.imageModel: AgentModelConfig type
- models.providers: custom provider definitions with baseUrl
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContextConfig(BaseModel):
    """Context compaction settings (mirrors openclaw's compaction config)."""
    budget: int = Field(default=100000, ge=1000, description="Maximum token budget for context window")
    threshold: float = Field(default=0.8, ge=0.1, le=1.0, description="Trigger compaction at this fraction of budget")
    recent_turns: int = Field(default=3, ge=1, description="Number of recent turns to preserve during compaction")


class AgentModelListConfig(BaseModel):
    """Model list with primary and fallbacks (mirrors openclaw AgentModelListConfig)."""
    primary: Optional[str] = Field(default=None, description="Primary model (provider/model-id)")
    fallbacks: list[str] = Field(default_factory=list, description="Fallback models (provider/model-id)")

    @field_validator("primary", "fallbacks", mode="before")
    @classmethod
    def validate_model_ref(cls, v: Any) -> Any:
        if isinstance(v, str) and v and "/" not in v:
            raise ValueError(f"Model reference must be in provider/model-id format: {v}")
        return v


AgentModelConfig = Union[str, AgentModelListConfig]


class ModelDefinition(BaseModel):
    """Model definition within a provider (mirrors openclaw models.providers.*.models[])."""
    model_config = ConfigDict(populate_by_name=True)
    
    id: str = Field(description="Model ID within this provider")
    name: Optional[str] = Field(default=None, description="Display name")
    context_window: Optional[int] = Field(default=None, alias="contextWindow", description="Context window size")
    max_tokens: Optional[int] = Field(default=None, alias="maxTokens", description="Max output tokens")
    input: list[str] = Field(default_factory=lambda: ["text"], description="Input modalities")


class ModelProvider(BaseModel):
    """Provider configuration (mirrors openclaw models.providers.*)."""
    model_config = ConfigDict(populate_by_name=True)
    
    base_url: Optional[str] = Field(default=None, alias="baseUrl", description="Custom endpoint URL")
    api_key: Optional[str] = Field(default=None, alias="apiKey", description="API key, supports ${VAR} syntax")
    api: Literal["openai-completions", "openai-responses", "anthropic-messages"] = Field(
        default="openai-completions",
        description="API type"
    )
    models: list[ModelDefinition] = Field(default_factory=list, description="Model catalog")


class ModelsConfig(BaseModel):
    """Models configuration (mirrors openclaw models.*)."""
    mode: Literal["merge", "replace"] = Field(default="merge", description="Provider catalog mode")
    providers: dict[str, ModelProvider] = Field(default_factory=dict, description="Custom providers")


class AgentDefaultsConfig(BaseModel):
    """Agent defaults configuration (mirrors openclaw agents.defaults)."""
    model_config = ConfigDict(populate_by_name=True)
    
    model: AgentModelConfig = Field(
        default="anthropic/claude-sonnet-4-5-20250929",
        description="Primary model (provider/model-id or {primary, fallbacks})"
    )
    image_model: Optional[AgentModelConfig] = Field(
        default=None,
        alias="imageModel",
        description="Image model for Media Understanding path. None = Native Vision"
    )

    @field_validator("model", mode="before")
    @classmethod
    def validate_model(cls, v: Any) -> Any:
        if isinstance(v, str) and "/" not in v:
            raise ValueError(f"Model reference must be in provider/model-id format: {v}")
        return v


class NanoOpenClawConfig(BaseModel):
    """nano-openclaw configuration (mirrors openclaw's config structure)."""
    model_config = ConfigDict(populate_by_name=True)
    
    agents: AgentDefaultsConfig = Field(default_factory=AgentDefaultsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    
    no_tools: bool = Field(default=False, alias="noTools", description="Run as plain chatbot, no tools")
    max_iterations: int = Field(default=12, alias="maxIterations", ge=1, description="Max tool-use rounds per user turn")
    max_tokens: int = Field(default=4096, alias="maxTokens", ge=1, description="Max tokens per assistant response")
    
    context: ContextConfig = Field(default_factory=ContextConfig)

    thinking_budget_tokens: Optional[int] = Field(
        default=None,
        alias="thinkingBudgetTokens",
        ge=512,
        description=(
            "Extended thinking budget in tokens. None = disabled. "
            "Requires a model that supports extended thinking (Claude 3.7+). "
            "max_tokens is auto-adjusted to be at least thinkingBudgetTokens + 1024."
        ),
    )

    def resolve_primary_model(self) -> str:
        """Resolve primary model from agents.model config."""
        model_config = self.agents.model
        if isinstance(model_config, str):
            return model_config
        if isinstance(model_config, AgentModelListConfig):
            return model_config.primary or "anthropic/claude-sonnet-4-5-20250929"
        return "anthropic/claude-sonnet-4-5-20250929"

    def resolve_image_model(self) -> Optional[str]:
        """Resolve primary image model from agents.image_model config."""
        image_model_config = self.agents.image_model
        if image_model_config is None:
            return None
        if isinstance(image_model_config, str):
            return image_model_config
        if isinstance(image_model_config, AgentModelListConfig):
            return image_model_config.primary
        return None