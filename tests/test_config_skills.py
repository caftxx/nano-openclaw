"""Tests for skill filter resolution in config."""

import pytest

from nano_openclaw.config.types import (
    AgentConfig,
    AgentDefaultsConfig,
    AgentsConfig,
    NanoOpenClawConfig,
    SkillsConfig,
)


def test_resolve_skill_filter_unrestricted_default():
    """No defaults.skills = unrestricted (None)."""
    config = NanoOpenClawConfig()
    
    filter_result = config.resolve_skill_filter()
    assert filter_result is None
    
    filter_result = config.resolve_skill_filter("any-agent")
    assert filter_result is None


def test_resolve_skill_filter_defaults_set():
    """defaults.skills is used when agent has no skills field."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github", "weather"]),
        ),
    )
    
    filter_result = config.resolve_skill_filter()
    assert filter_result == ["github", "weather"]
    
    # Agent without skills field inherits defaults
    config.agents.list.append(AgentConfig(id="coder"))
    filter_result = config.resolve_skill_filter("coder")
    assert filter_result == ["github", "weather"]


def test_resolve_skill_filter_agent_replaces_defaults():
    """Agent's skills field replaces defaults (not merges)."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github", "weather"]),
            list=[
                AgentConfig(id="docs", skills=["docs-search"]),
            ],
        ),
    )
    
    # Agent with skills field uses its own list
    filter_result = config.resolve_skill_filter("docs")
    assert filter_result == ["docs-search"]
    
    # Other agent inherits defaults
    filter_result = config.resolve_skill_filter("other")
    assert filter_result == ["github", "weather"]


def test_resolve_skill_filter_agent_empty_list():
    """Agent with skills: [] has no skills."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github", "weather"]),
            list=[
                AgentConfig(id="locked", skills=[]),
            ],
        ),
    )
    
    filter_result = config.resolve_skill_filter("locked")
    assert filter_result == []


def test_resolve_skill_filter_agent_none_inherits():
    """Agent with skills: None (unset) inherits defaults."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github", "weather"]),
            list=[
                AgentConfig(id="inheritor"),
            ],
        ),
    )
    
    filter_result = config.resolve_skill_filter("inheritor")
    assert filter_result == ["github", "weather"]


def test_resolve_skill_filter_no_agent_id():
    """No agent_id returns defaults.skills."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github"]),
        ),
    )
    
    filter_result = config.resolve_skill_filter()
    assert filter_result == ["github"]


def test_resolve_skill_filter_agent_not_found():
    """Unknown agent inherits defaults."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github", "weather"]),
            list=[
                AgentConfig(id="known"),
            ],
        ),
    )
    
    filter_result = config.resolve_skill_filter("unknown")
    assert filter_result == ["github", "weather"]


def test_resolve_skills_config_for_agent():
    """resolve_skills_config_for_agent combines all skills config."""
    config = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["github"]),
            list=[
                AgentConfig(id="docs", skills=["docs-search"]),
            ],
        ),
        skills=SkillsConfig(),
    )
    
    # Default agent
    result = config.resolve_skills_config_for_agent()
    assert result["skill_filter"] == ["github"]
    assert result["extra_dirs"] == []
    
    # Specific agent
    result = config.resolve_skills_config_for_agent("docs")
    assert result["skill_filter"] == ["docs-search"]


def test_resolve_skills_config_with_extra_dirs():
    """Extra dirs from skills.load are included."""
    config = NanoOpenClawConfig(
        skills=SkillsConfig(),
    )
    config.skills.load.extraDirs = ["~/.my-skills", "/path/to/skills"]
    
    result = config.resolve_skills_config_for_agent()
    assert result["extra_dirs"] == ["~/.my-skills", "/path/to/skills"]


def test_skill_filter_priority_chain():
    """Full priority chain test: agent -> defaults -> None."""
    # 1. Unrestricted (no defaults, no agent)
    config1 = NanoOpenClawConfig()
    assert config1.resolve_skill_filter() is None
    
    # 2. Defaults set, agent inherits
    config2 = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["a", "b"]),
        ),
    )
    assert config2.resolve_skill_filter("any") == ["a", "b"]
    
    # 3. Agent replaces defaults
    config3 = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["a", "b"]),
            list=[
                AgentConfig(id="x", skills=["c"]),
            ],
        ),
    )
    assert config3.resolve_skill_filter("x") == ["c"]
    
    # 4. Agent explicitly empty
    config4 = NanoOpenClawConfig(
        agents=AgentsConfig(
            defaults=AgentDefaultsConfig(skills=["a", "b"]),
            list=[
                AgentConfig(id="empty", skills=[]),
            ],
        ),
    )
    assert config4.resolve_skill_filter("empty") == []