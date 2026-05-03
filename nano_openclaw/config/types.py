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
    skills: Optional[List[str]] = Field(
        default=None,
        description="Skill allowlist for this agent (replaces defaults.skills, not merges. None = inherit, [] = no skills)"
    )


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
    thinkingDefault: Optional[ThinkingLevel] = Field(
        default=None,
        description="Default thinking mode: off|minimal|low|medium|high|xhigh|adaptive|max"
    )
    bootstrapMaxChars: int = Field(
        default=12000,
        ge=100,
        alias="bootstrapMaxChars",
        description="Per-file character budget for bootstrap files (AGENTS.md, SOUL.md, etc.)"
    )
    bootstrapTotalMaxChars: int = Field(
        default=60000,
        ge=100,
        alias="bootstrapTotalMaxChars",
        description="Total character budget across all bootstrap files"
    )
    skills: Optional[List[str]] = Field(
        default=None,
        description="Default skill allowlist for agents (None = unrestricted, [] = no skills)"
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
# Skills Types (aligns with src/config/types.openclaw.ts skills.*)
# ============================================================================

class SkillEntryConfig(BaseModel):
    """Per-skill configuration override (mirrors openclaw skills.entries)."""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=True, description="Enable or disable this skill")
    apiKey: Optional[str] = Field(default=None, description="API key override")
    env: Optional[Dict[str, str]] = Field(default=None, description="Environment variable overrides")


class SkillsLoadConfig(BaseModel):
    """Skills loading configuration (mirrors openclaw skills.load)."""
    model_config = ConfigDict(populate_by_name=True)
    
    extraDirs: List[str] = Field(
        default_factory=list,
        alias="extraDirs",
        description="Additional skill directories to load"
    )
    watch: bool = Field(default=False, description="Watch skill directories for changes")
    maxCandidatesPerRoot: int = Field(
        default=300,
        alias="maxCandidatesPerRoot",
        ge=1,
        description="Max candidate directories to scan per root"
    )
    maxSkillsLoadedPerSource: int = Field(
        default=200,
        alias="maxSkillsLoadedPerSource",
        ge=1,
        description="Max skills to load per source"
    )
    maxSkillsInPrompt: int = Field(
        default=150,
        alias="maxSkillsInPrompt",
        ge=1,
        description="Max skills to include in prompt"
    )
    maxSkillsPromptChars: int = Field(
        default=18_000,
        alias="maxSkillsPromptChars",
        ge=100,
        description="Max characters for skills section in prompt"
    )
    maxSkillFileBytes: int = Field(
        default=256_000,
        alias="maxSkillFileBytes",
        ge=1000,
        description="Max bytes per SKILL.md file"
    )


class SkillsConfig(BaseModel):
    """Skills configuration (mirrors openclaw skills.*)."""
    model_config = ConfigDict(populate_by_name=True)
    
    entries: Dict[str, SkillEntryConfig] = Field(
        default_factory=dict,
        description="Per-skill configuration overrides"
    )
    load: SkillsLoadConfig = Field(
        default_factory=SkillsLoadConfig,
        description="Skills loading configuration"
    )
    allowBundled: Optional[List[str]] = Field(
        default=None,
        alias="allowBundled",
        description="Allowlist for bundled skills (None = allow all)"
    )


# ============================================================================
# Active Memory Config (mirrors openclaw active-memory plugin schema)
# ============================================================================

class ActiveMemoryConfigInput(BaseModel):
    """Active Memory 插件配置，对齐 openclaw active-memory plugin schema。"""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    model: Optional[str] = None
    thinking: ThinkingLevel = "off"
    queryMode: str = Field(default="recent", description="message | recent | full")
    promptStyle: str = Field(default="balanced", description="balanced | strict | contextual | recall-heavy | precision-heavy | preference-only")
    promptOverride: Optional[str] = None
    promptAppend: Optional[str] = None
    timeoutMs: int = Field(default=15000, ge=250, le=120000)
    maxSummaryChars: int = Field(default=220, ge=40, le=1000)
    recentUserTurns: int = Field(default=2, ge=0, le=4)
    recentAssistantTurns: int = Field(default=1, ge=0, le=3)
    recentUserChars: int = Field(default=220, ge=40, le=1000)
    recentAssistantChars: int = Field(default=180, ge=40, le=1000)
    cacheTtlMs: int = Field(default=15000, ge=1000, le=120000)
    logging: bool = False

    @field_validator("queryMode")
    @classmethod
    def validate_query_mode(cls, v: str) -> str:
        allowed = {"message", "recent", "full"}
        if v not in allowed:
            raise ValueError(f"queryMode must be one of {allowed}")
        return v

    @field_validator("promptStyle")
    @classmethod
    def validate_prompt_style(cls, v: str) -> str:
        allowed = {"balanced", "strict", "contextual", "recall-heavy", "precision-heavy", "preference-only"}
        if v not in allowed:
            raise ValueError(f"promptStyle must be one of {allowed}")
        return v


# ============================================================================
# Dreaming Config (mirrors openclaw memory-core dreaming config)
# ============================================================================

class DreamingConfigInput(BaseModel):
    """Dreaming plugin configuration, aligns with openclaw memory-core dreaming schema."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    frequency: str = Field(default="0 3 * * *", description="Cron schedule for dreaming sweep")
    minScore: float = Field(default=0.5, ge=0.0, le=1.0, description="Minimum score to promote")
    minRecallCount: int = Field(default=2, ge=1, description="Minimum recall count to qualify")
    minUniqueQueries: int = Field(default=1, ge=1, description="Minimum unique queries to qualify")
    maxPromotions: int = Field(default=10, ge=1, le=50, description="Max promotions per sweep")
    diary: bool = Field(default=True, description="Generate Dream Diary narrative (requires API call)")
    model: Optional[str] = Field(default=None, description="Model override for Dream Diary generation")


# ============================================================================
# MCP Types (aligns with openclaw types.mcp.ts)
# ============================================================================

class McpServerConfig(BaseModel):
    """MCP server 配置（对应 openclaw types.mcp.ts McpServerConfig）。"""
    model_config = ConfigDict(populate_by_name=True)
    
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, Union[str, int, bool]]] = None
    cwd: Optional[str] = None
    workingDirectory: Optional[str] = None
    url: Optional[str] = None
    transport: Optional[Literal["sse", "streamable-http"]] = None
    headers: Optional[Dict[str, Union[str, int, bool]]] = None
    connectionTimeoutMs: Optional[int] = Field(default=None)


class McpConfig(BaseModel):
    """MCP 全局配置（对应 openclaw McpConfig）。"""
    model_config = ConfigDict(populate_by_name=True)
    
    servers: Dict[str, McpServerConfig] = Field(default_factory=dict)
    sessionIdleTtlMs: Optional[int] = Field(default=None)


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=True)
    maxResults: int = Field(default=10, ge=1, le=50)
    region: str = Field(default="wt-wt", description="DuckDuckGo region code")


class WebFetchConfig(BaseModel):
    """Web fetch tool configuration."""
    model_config = ConfigDict(populate_by_name=True)
    
    enabled: bool = Field(default=True)
    maxChars: int = Field(default=20_000, ge=100, le=500_000)
    maxRedirects: int = Field(default=3, ge=0, le=10)
    timeoutSeconds: int = Field(default=30, ge=1, le=120)
    extractMode: Literal["markdown", "text"] = "markdown"


class ToolsWebConfig(BaseModel):
    """Web tools configuration (tools.web.*)."""
    model_config = ConfigDict(populate_by_name=True)
    
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)


class ToolsConfig(BaseModel):
    """Full tools configuration (mirrors openclaw tools.*)."""
    model_config = ConfigDict(populate_by_name=True)
    
    noTools: bool = Field(default=False, description="Run as plain chatbot, no tools")
    web: ToolsWebConfig = Field(default_factory=ToolsWebConfig)


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
    - tools: { noTools, web }
    - Custom fields: maxIterations, context
    """
    model_config = ConfigDict(populate_by_name=True)
    
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    
    # nano-openclaw custom fields
    noTools: bool = Field(default=False, description="Run as plain chatbot, no tools")
    maxIterations: int = Field(default=12, ge=1, description="Max tool-use rounds per user turn")
    context: ContextConfig = Field(default_factory=ContextConfig)
    activeMemory: Optional[ActiveMemoryConfigInput] = Field(
        default=None,
        description="Active Memory plugin configuration (automatic memory recall)"
    )
    dreaming: DreamingConfigInput = Field(
        default_factory=DreamingConfigInput,
        description="Dreaming plugin configuration (background memory consolidation)"
    )

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
    
    def resolve_skill_filter(self, agent_id: Optional[str] = None) -> Optional[List[str]]:
        """
        Resolve skill filter (allowlist) for an agent.
        
        Priority (mirrors openclaw agents.defaults.skills + agents.list[].skills):
        1. agents.list[].skills (if agent found) — replaces defaults, not merges
        2. agents.defaults.skills — inherited when agent has no skills field
        3. None — unrestricted (all eligible skills available)
        
        Args:
            agent_id: Agent identifier
        
        Returns:
            List of allowed skill names, or None for unrestricted
        """
        # Check agent-specific skills
        if agent_id:
            for agent in self.agents.list:
                if agent.id == agent_id:
                    if agent.skills is not None:
                        # Explicit list replaces defaults (even if empty [])
                        return agent.skills
                    # No skills field = inherit defaults (break to check defaults)
                    break
        
        # Fall back to defaults (None = unrestricted)
        return self.agents.defaults.skills
    
    def resolve_skills_config_for_agent(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Resolve full skills configuration for an agent.
        
        Combines:
        - skill_filter from resolve_skill_filter()
        - extraDirs from skills.load.extraDirs
        - limits from skills.load
        
        Args:
            agent_id: Agent identifier
        
        Returns:
            Dict with skill_filter, extra_dirs, and limits
        """
        return {
            "skill_filter": self.resolve_skill_filter(agent_id),
            "extra_dirs": self.skills.load.extraDirs,
            "max_skill_file_bytes": self.skills.load.maxSkillFileBytes,
            "max_skills_in_prompt": self.skills.load.maxSkillsInPrompt,
            "max_skills_prompt_chars": self.skills.load.maxSkillsPromptChars,
        }
