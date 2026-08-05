"""Pydantic configuration types for nano-openclaw.

Mirrors openclaw's config types from src/config/types.*.ts:
- OpenClawConfig structure alignment
- agents.defaults + agents.list multi-agent support
- models.providers provider catalog
- Session configuration
- Model definition with full openclaw fields
"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    idleMinutes: int = Field(
        default=360,
        ge=0,
        alias="idleMinutes",
        description="Idle minutes before reset; 0 disables idle rollover",
    )


class SessionConfig(BaseModel):
    """Session configuration (mirrors openclaw SessionConfig)."""
    model_config = ConfigDict(populate_by_name=True)
    
    idleMinutes: int = Field(
        default=360,
        ge=0,
        alias="idleMinutes",
        description="Legacy idle timeout in minutes; prefer reset.idleMinutes",
    )
    reset: SessionReset = Field(default_factory=SessionReset)

    @property
    def effective_idle_minutes(self) -> int:
        """Resolve the preferred reset timeout while honoring legacy configs."""
        if "idleMinutes" in self.reset.model_fields_set:
            return self.reset.idleMinutes
        if "idleMinutes" in self.model_fields_set:
            return self.idleMinutes
        return self.reset.idleMinutes


# ============================================================================
# Context Types (nano-openclaw specific)
# ============================================================================

class ContextConfig(BaseModel):
    """Context compaction settings (nano-openclaw specific, mirrors openclaw compaction config)."""
    model_config = ConfigDict(populate_by_name=True)

    budget: Optional[int] = Field(default=None, ge=1000, description="Maximum token budget for context window; defaults to model contextWindow when unset")
    threshold: float = Field(default=0.8, ge=0.1, le=1.0, description="Trigger compaction at this fraction of budget")
    recent_turns: int = Field(default=3, ge=1, alias="recent_turns", description="Recent turns to preserve during compaction")
    truncate_after_compaction: bool = Field(
        default=True,
        alias="truncate_after_compaction",
        description="After compaction, rewrite the on-disk transcript to drop summarized messages so restarts load the post-compaction history.",
    )


class PromptCachingConfig(BaseModel):
    """Anthropic prompt caching settings (Stage 4).

    Enables the ``system_and_3`` cache_control breakpoint strategy for
    multi-turn Anthropic conversations — typically saves ~75% of input
    token cost. OpenAI provider ignores this entirely.
    """
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(
        default=True,
        description="Enable Anthropic prompt caching (system_and_3 strategy)",
    )
    cache_ttl: Literal["5m", "1h"] = Field(
        default="5m",
        alias="cache_ttl",
        description="Cache TTL: '5m' (ephemeral) or '1h' (long-lived sessions)",
    )


class MemoryFlushConfig(BaseModel):
    """Pre-compaction memory flush settings."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=True, description="Enable silent memory flush before compaction")
    softThresholdTokens: int = Field(default=4000, ge=0, description="Soft threshold before compaction")
    reserveTokensFloor: int = Field(default=20000, ge=0, description="Minimum tokens to reserve")
    prompt: str = Field(
        default=(
            "Pre-compaction memory flush.\n"
            "Store durable memories only in memory/YYYY-MM-DD.md (create memory/ if needed). "
            "Treat workspace bootstrap/reference files such as MEMORY.md, DREAMS.md, SOUL.md, "
            "TOOLS.md, and AGENTS.md as read-only during this flush; never overwrite, replace, "
            "or edit them. "
            "If memory/YYYY-MM-DD.md already exists, APPEND new content only and do not overwrite "
            "existing entries. "
            "Do NOT create timestamped variant files (e.g., YYYY-MM-DD-HHMM.md); always use the "
            "canonical YYYY-MM-DD.md filename. "
            "If nothing to store, reply with NO_REPLY."
        ),
        description="Prompt for silent memory flush turn",
    )


class TemporalDecayConfig(BaseModel):
    """Temporal decay settings for memory_search ranking."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False, description="Enable recency-based score decay")
    halfLifeDays: float = Field(
        default=30,
        alias="halfLifeDays",
        gt=0,
        description="Number of days after which dated memory scores are halved",
    )


class MemorySearchConfig(BaseModel):
    """Memory search ranking configuration."""
    model_config = ConfigDict(populate_by_name=True)

    provider: str = Field(
        default="lexical",
        description="memory_search provider id. Built-in default: lexical.",
    )
    providers: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Provider-specific memory_search settings keyed by provider id.",
    )
    temporalDecay: TemporalDecayConfig = Field(
        default_factory=TemporalDecayConfig,
        alias="temporalDecay",
        description="Optional temporal decay for dated daily memory files",
    )


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


class SkillsInstallConfig(BaseModel):
    """Skill dependency installation policy."""
    model_config = ConfigDict(populate_by_name=True)

    pythonIsolation: Literal["venv"] = Field(
        default="venv",
        alias="pythonIsolation",
        description="Install Python skill dependencies into isolated virtualenvs"
    )
    allowGlobalPip: bool = Field(
        default=False,
        alias="allowGlobalPip",
        description="Allow bare pip install commands to target the global interpreter"
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
    install: SkillsInstallConfig = Field(
        default_factory=SkillsInstallConfig,
        description="Skills dependency installation policy"
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

    enabled: bool = True
    frequency: str = Field(default="0 3 * * *", description="Cron schedule for dreaming sweep")
    minScore: float = Field(default=0.5, ge=0.0, le=1.0, description="Minimum score to promote")
    minRecallCount: int = Field(default=2, ge=1, description="Minimum recall count to qualify")
    minUniqueQueries: int = Field(default=1, ge=1, description="Minimum unique queries to qualify")
    maxPromotions: int = Field(default=10, ge=1, le=50, description="Max promotions per sweep")
    diary: bool = Field(default=True, description="Generate Dream Diary narrative (requires API call)")
    model: Optional[str] = Field(default=None, description="Model override for Dream Diary generation")


# Default extractor prompt template. Imported lazily inside the field default
# factory to avoid pulling the memory subpackage into config import time
# (which would create a cycle: memory → config → memory).
def _default_extract_prompt() -> str:
    from nano_openclaw.features.memory.extractor_prompts import DEFAULT_EXTRACT_PROMPT_TEMPLATE
    return DEFAULT_EXTRACT_PROMPT_TEMPLATE


class ExtractMemoriesConfig(BaseModel):
    """Stop-hook extractor configuration (mirrors claude-code extractMemories.ts).

    Triggers a forked subagent after every eligible turn to distill durable
    memories into ``memory/topics/*.md`` and update ``memory/MEMORY.md``.
    Subject to per-source enable list, cooldown, and mutual-exclusion with
    main-agent topic writes — see ``nano_openclaw/features/memory/extractor.py``.
    """
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=True, description="Enable stop-hook memory extractor")
    triggerSources: List[str] = Field(
        default_factory=lambda: ["tui", "webui", "wechat"],
        alias="triggerSources",
        description="Turn sources that trigger the extractor; default excludes cron / channel_auto",
    )
    maxTurns: int = Field(
        default=5,
        alias="maxTurns",
        ge=1,
        le=20,
        description="Hard turn cap for the extractor subagent",
    )
    cooldownTurns: int = Field(
        default=1,
        alias="cooldownTurns",
        ge=1,
        description="Run extractor once every N eligible turns",
    )
    model: Optional[str] = Field(
        default=None,
        description="Optional model override (provider/model-id); None inherits the parent agent",
    )
    prompt: str = Field(
        default_factory=_default_extract_prompt,
        description="Extractor prompt template (see features/memory/extractor_prompts.py)",
    )


class ScheduleConfigInput(BaseModel):
    """Cron schedule configuration."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=True, description="Enable cron scheduler")
    maxConcurrentRuns: int = Field(default=3, ge=1, le=20, description="Max jobs running in parallel")
    missedJobsLimit: int = Field(default=5, ge=1, le=50, description="Max missed jobs to run immediately on startup")


class ReviewForkConfigInput(BaseModel):
    """Background Review Fork plugin configuration.

    On by default; spawns a restricted review subagent every N end_turns
    (with cooldown) to distill durable lessons into MEMORY.md / existing
    SKILL.md. Implemented by nano_openclaw.features.review_fork.plugin.
    """
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=True, description="Enable Review Fork (on by default)")
    trigger_n: int = Field(default=10, ge=1, alias="trigger_n", description="Trigger every N end_turns")
    cooldown_s: int = Field(default=60, ge=0, alias="cooldown_s", description="Min seconds between review forks")
    timeout_s: int = Field(default=90, ge=10, alias="timeout_s", description="Subagent run timeout in seconds")
    model_aux: Optional[str] = Field(default=None, alias="model_aux", description="Aux model override (provider/id); None = follow parent")



class GatewayConfig(BaseModel):
    """Gateway daemon (``nano-openclaw gateway``) network + supervisor settings.

    The daemon binds to ``host:port`` to expose the WebUI HTTP routes and
    (Phase 4) the WebSocket ``/rpc`` endpoint shared by remote TUI clients.

    v1 has no auth — non-loopback bind triggers a startup warning, since
    anyone reaching the port can drive the agent runtime.
    """
    model_config = ConfigDict(populate_by_name=True)

    host: str = Field(default="127.0.0.1", description="Bind address; default loopback. Non-loopback warns at startup (no auth in v1).")
    port: int = Field(default=5000, ge=1, le=65535, description="TCP port for the daemon HTTP/WebSocket server")
    log_path: str = Field(default="", description="Where the detached daemon writes stdout/stderr; empty → state_dir/gateway.log")
    tls_cert: str = Field(default="", description="Path to a TLS certificate (PEM). Set together with tls_key to serve HTTPS — required for mic/getUserMedia on phones accessing over a LAN IP.")
    tls_key: str = Field(default="", description="Path to the TLS private key (PEM). Must be set together with tls_cert.")
    restart_strategy: Literal["exec", "exit"] = Field(
        default="exec",
        alias="restart_strategy",
        description="How /restart and the restart tool restart the daemon. 'exec' re-execs in place (works in standalone + systemd Type=simple). 'exit' exits 0 and relies on a supervisor (systemd Restart=always, docker restart policy) — do NOT use without one.",
    )


class LoggingConfig(BaseModel):
    """Structured JSON Lines logging configuration."""
    model_config = ConfigDict(populate_by_name=True)

    level: str = Field(
        default="info",
        description="Log level: debug|info|warning|error|critical",
    )


class SubagentConfigInput(BaseModel):
    """Subagent configuration, aligns with openclaw agents.defaults.subagents."""
    model_config = ConfigDict(populate_by_name=True)

    maxConcurrent: int = Field(default=10, ge=1, le=10, description="Max concurrent subagent runs")
    maxSpawnDepth: int = Field(default=1, ge=1, le=1, description="Max nesting depth (always 1 for nano)")
    runTimeoutSeconds: int = Field(default=0, ge=0, description="Run timeout in seconds (0 = no timeout)")
    archiveAfterMinutes: int = Field(default=60, ge=0, description="Auto-archive delay in minutes")
    model: Optional[str] = Field(default=None, description="Default model for subagents")
    thinking: Optional[ThinkingLevel] = Field(default=None, description="Default thinking level for subagents")


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
    transport: Optional[Literal["stdio", "sse", "streamable-http"]] = None
    headers: Optional[Dict[str, Union[str, int, bool]]] = None
    connectionTimeoutMs: Optional[int] = Field(default=None)

    @field_validator("transport", mode="before")
    @classmethod
    def normalize_transport(cls, v):
        if isinstance(v, str) and v.replace("_", "-").lower() in {"streamablehttp", "streamable-http"}:
            return "streamable-http"
        return v


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
# Plugin Types (nano-openclaw lightweight plugin loader)
# ============================================================================

BUILTIN_PLUGIN_IDS = ("memory", "web", "subagent", "mcp", "schedule", "review-fork")


class PluginEntryConfig(BaseModel):
    """Explicit plugin entry for module or file-path plugins."""
    model_config = ConfigDict(populate_by_name=True)

    module: Optional[str] = Field(default=None, description="Python module path containing a plugin")
    path: Optional[str] = Field(default=None, description="Python file path containing a plugin")
    config: Dict[str, Any] = Field(default_factory=dict, description="Plugin-specific configuration")


class PluginsConfig(BaseModel):
    """Lightweight plugin loading configuration."""
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=True, description="Enable plugin loading")
    load: List[Union[str, PluginEntryConfig]] = Field(
        default_factory=lambda: list(BUILTIN_PLUGIN_IDS),
        description="Plugin entries to load; strings reference built-in plugins",
    )

    @field_validator("load", mode="after")
    @classmethod
    def include_builtin_plugins(cls, v: List[Union[str, PluginEntryConfig]]) -> List[Union[str, PluginEntryConfig]]:
        """Built-in plugins are always loaded before user-configured plugins."""
        configured_builtin_ids = {entry for entry in v if isinstance(entry, str) and entry in BUILTIN_PLUGIN_IDS}
        custom_entries = [
            entry
            for entry in v
            if not (isinstance(entry, str) and entry in configured_builtin_ids)
        ]
        return [*BUILTIN_PLUGIN_IDS, *custom_entries]


class VoiceConfig(BaseModel):
    """WebUI 语音识别配置（阿里云实时语音识别）。

    设计为「配置开关二选一」：配齐 appkey + accessKeyId + accessKeySecret 三要素
    后，WebUI 语音模式优先走阿里云实时识别（后端动态签发临时 Token，浏览器经
    WebSocket 直连阿里云 NLS 网关）；任一缺失则前端回退浏览器内置 Web Speech API。

    accessKeyId / accessKeySecret 支持 ${VAR} 语法——和 ModelProvider.apiKey 一样，
    由 config 加载阶段的 resolve_config_env_vars 统一替换，所以这里拿到的已是明文。

    语音合成（TTS）复用同一套 AK/SK / 网关 endpoint / 临时 Token，仅请求 namespace
    不同（识别用 SpeechTranscriber，合成用 FlowingSpeechSynthesizer 流式语音合成）。
    """
    model_config = ConfigDict(populate_by_name=True)

    provider: Literal["aliyun", "openai-compatible"] = Field(
        default="aliyun",
        description="语音服务商：阿里云或 OpenAI-compatible 本地 speech gateway",
    )
    appkey: str = Field(default="", description="阿里云智能语音交互项目 Appkey")
    accessKeyId: str = Field(default="", description="阿里云 AccessKeyId（支持 ${VAR} 语法）")
    accessKeySecret: str = Field(default="", description="阿里云 AccessKeySecret（支持 ${VAR} 语法）")
    region: str = Field(default="cn-shanghai", description="阿里云区域，决定默认网关 endpoint")
    endpoint: str = Field(
        default="",
        description="阿里云实时识别 WebSocket 端点；留空则按 region 推导 wss://nls-gateway-{region}.aliyuncs.com/ws/v1",
    )
    baseUrl: str = Field(default="", description="OpenAI-compatible 语音服务的 /v1 HTTP 基址")
    realtimeUrl: str = Field(default="", description="OpenAI-compatible 实时 ASR WebSocket 地址")
    apiKey: str = Field(default="", description="OpenAI-compatible 语音服务 Bearer Token")
    asrModel: str = Field(default="paraformer-zh-streaming", description="实时 ASR 模型名")
    finalAsrModel: str = Field(default="sensevoice-small", description="离线复核 ASR 模型名")
    ttsModel: str = Field(default="fun-cosyvoice3-0.5b", description="OpenAI-compatible TTS 模型名")
    ttsEnabled: bool = Field(
        default=True,
        description="是否启用阿里云流式语音合成 TTS（关闭则前端朗读回退浏览器 speechSynthesis）",
    )
    ttsVoice: str = Field(
        default="xiaoxian",
        description="阿里云流式语音合成默认音色（StartSynthesis 的 voice 取值，用户可在浮层下拉切换）",
    )
    ttsSampleRate: int = Field(
        default=16000,
        description="阿里云流式语音合成采样率（Hz）；speech-gateway 固定使用 24000 Hz",
    )
    wakeWord: str = Field(
        default="",
        description=(
            "语音浮层唤醒词；配置后免提进入「待唤醒」待机，听到唤醒词才开始对话。"
            "支持逗号分隔多个同音变体（如 \"小克,小可\"），留空则不启用待唤醒模式"
        ),
    )

    @property
    def available(self) -> bool:
        """三要素（appkey/accessKeyId/accessKeySecret）齐全才视为可用。"""
        if self.provider == "openai-compatible":
            return bool(self.baseUrl and self.realtimeUrl)
        return bool(self.appkey and self.accessKeyId and self.accessKeySecret)

    def resolved_endpoint(self) -> str:
        """显式 endpoint 优先，否则按 region 推导默认网关地址。"""
        if self.provider == "openai-compatible":
            return self.realtimeUrl
        if self.endpoint:
            return self.endpoint
        return f"wss://nls-gateway-{self.region}.aliyuncs.com/ws/v1"

    def resolved_rest_tts_url(self) -> str:
        """从识别网关 endpoint 派生 RESTful 语音合成 URL（POST /stream/v1/tts）。

        复用同一网关 host：把 scheme wss→https / ws→http，path 设为 /stream/v1/tts，
        netloc 保留。这样显式 endpoint 覆盖（含内网 -internal 域名）也能正确派生。
        例：region=cn-shanghai → https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/tts。
        """
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.resolved_endpoint())
        scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
        return urlunsplit((scheme, parts.netloc, "/stream/v1/tts", "", ""))

    def resolved_tts_sample_rate(self) -> int:
        """Return the active provider's actual PCM output rate."""
        if self.provider == "openai-compatible":
            return 24000
        return self.ttsSampleRate


class XiaozhiConfig(BaseModel):
    """xiaozhi-esp32 gateway adapter configuration."""

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False, description="Enable the xiaozhi-esp32 adapter")
    token: str = Field(default="", description="Bearer token returned by OTA and required by device endpoints")
    websocketUrl: str = Field(
        default="",
        description="Public ws(s):// URL for the device; empty derives it from the OTA request host",
    )
    mcpTimeoutMs: int = Field(default=10000, ge=1000, le=120000)
    noVoiceTimeoutSeconds: int = Field(
        default=120,
        ge=0,
        le=3600,
        description="Close an actively-listening device connection after this many seconds without recognized speech; 0 disables the timeout",
    )
    maxPhotoBytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    ttsVoice: str = Field(
        default="zhiqi",
        min_length=1,
        description="Aliyun voice used for xiaozhi playback; choose a voice supporting ttsSampleRate",
    )
    ttsSampleRate: Literal[16000, 24000] = Field(
        default=24000,
        description="Xiaozhi TTS and downlink Opus sample rate; 24 kHz matches LICHUANG_DEV_S3 output",
    )
    opusBitrate: int = Field(
        default=64000,
        ge=16000,
        le=128000,
        description="Xiaozhi downlink Opus bitrate in bits per second",
    )
    ttsPrebufferMs: int = Field(
        default=2400,
        ge=0,
        le=10000,
        description="PCM to collect before local TTS playback; 0 disables prebuffering",
    )
    ttsPrebufferMaxWaitMs: int = Field(
        default=1800,
        ge=0,
        le=10000,
        description="Maximum added wait for local TTS prebuffering",
    )


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
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    
    # nano-openclaw custom fields
    noTools: bool = Field(default=False, description="Run as plain chatbot, no tools")
    maxIterations: int = Field(default=12, ge=1, description="Max tool-use rounds per user turn")
    context: ContextConfig = Field(default_factory=ContextConfig)
    promptCaching: PromptCachingConfig = Field(
        default_factory=PromptCachingConfig,
        alias="promptCaching",
        description="Anthropic prompt caching settings (Stage 4)",
    )
    memoryFlush: MemoryFlushConfig = Field(
        default_factory=MemoryFlushConfig,
        description="Pre-compaction silent memory flush configuration",
    )
    memorySearch: MemorySearchConfig = Field(
        default_factory=MemorySearchConfig,
        description="Memory search ranking configuration",
    )
    activeMemory: Optional[ActiveMemoryConfigInput] = Field(
        default=None,
        description="Active Memory plugin configuration (automatic memory recall)"
    )
    dreaming: DreamingConfigInput = Field(
        default_factory=DreamingConfigInput,
        description="Dreaming plugin configuration (background memory consolidation)"
    )
    extractMemories: ExtractMemoriesConfig = Field(
        default_factory=ExtractMemoriesConfig,
        alias="extractMemories",
        description="Stop-hook memory extractor configuration (subagent distills topics after each turn)"
    )
    subagents: SubagentConfigInput = Field(
        default_factory=SubagentConfigInput,
        description="Subagent configuration (background agent runs)"
    )
    schedule: ScheduleConfigInput = Field(
        default_factory=ScheduleConfigInput,
        description="Cron schedule configuration"
    )
    review_fork: ReviewForkConfigInput = Field(
        default_factory=ReviewForkConfigInput,
        alias="reviewFork",
        description="Background Review Fork plugin configuration (off by default)"
    )
    gateway: GatewayConfig = Field(
        default_factory=GatewayConfig,
        description="Gateway daemon (host/port/log_path)"
    )
    voice: VoiceConfig = Field(
        default_factory=VoiceConfig,
        description="WebUI 语音识别（阿里云实时语音识别）配置；配齐三要素后 WebUI 语音模式优先用阿里云，否则回退浏览器 Web Speech API",
    )
    xiaozhi: XiaozhiConfig = Field(
        default_factory=XiaozhiConfig,
        description="xiaozhi-esp32 voice, vision, and device-MCP adapter",
    )
    logging: LoggingConfig = Field(
        default_factory=lambda: LoggingConfig(),
        description="Structured logging configuration",
    )
    # Runtime-resolved state directory (set by __main__.py, not user-configurable)
    state_dir: str = Field(default="", exclude=True, description="Resolved state directory path")

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
