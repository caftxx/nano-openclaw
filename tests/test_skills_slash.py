"""Tests for skills slash commands module."""

from nano_openclaw.skills import (
    Skill,
    SkillEntry,
    SlashCommand,
    build_skill_registry_from_entries,
    build_slash_command_context,
    parse_slash_command,
)
from nano_openclaw.skills.slash_commands import is_skill_user_invocable


def test_parse_slash_command_not_a_command():
    """Return None for non-slash input."""
    skill_registry = {}
    
    cmd, text = parse_slash_command("hello world", skill_registry)
    assert cmd is None
    assert text == "hello world"


def test_parse_slash_command_builtin():
    """Return None for built-in CLI commands."""
    skill_registry = {}
    
    # Built-in commands should not trigger skill lookup
    for builtin in ["quit", "clear", "help", "new"]:
        cmd, text = parse_slash_command(f"/{builtin}", skill_registry)
        assert cmd is None
        assert text == f"/{builtin}"


def test_parse_slash_command_unknown_skill():
    """Return None for unknown skill name."""
    skill_registry = {}
    
    cmd, text = parse_slash_command("/unknown-skill", skill_registry)
    assert cmd is None
    assert text == "/unknown-skill"


def test_parse_slash_command_found():
    """Parse valid slash command."""
    skill = Skill(
        name="github",
        description="GitHub CLI",
        filePath="/path/github/SKILL.md",
        baseDir="/path/github",
        source="bundled",
        content="# GitHub Skill\nInstructions here.",
    )
    
    skill_registry = {"github": skill}
    
    cmd, text = parse_slash_command("/github create a PR", skill_registry)
    
    assert cmd is not None
    assert cmd.name == "github"
    assert cmd.skill == skill
    assert cmd.args == "create a PR"


def test_parse_slash_command_no_args():
    """Parse slash command without args."""
    skill = Skill(
        name="weather",
        description="Weather skill",
        filePath="/path/SKILL.md",
        baseDir="/path",
        source="bundled",
    )
    
    skill_registry = {"weather": skill}
    
    cmd, text = parse_slash_command("/weather", skill_registry)
    
    assert cmd is not None
    assert cmd.name == "weather"
    assert cmd.args == ""


def test_build_slash_command_context():
    """Build context message for slash command."""
    skill = Skill(
        name="github",
        description="GitHub CLI",
        filePath="/skills/github/SKILL.md",
        baseDir="/skills/github",
        source="bundled",
        content="# GitHub Skill\nUse gh CLI for GitHub operations.",
    )
    
    command = SlashCommand(name="github", skill=skill, args="create PR")
    
    context = build_slash_command_context(command)
    
    assert "[Skill invoked: github]" in context
    assert "/skills/github/SKILL.md" in context
    assert "Use gh CLI for GitHub operations." in context
    assert "User arguments: create PR" in context


def test_build_slash_command_context_no_args():
    """Build context without args."""
    skill = Skill(
        name="test",
        description="Test",
        filePath="/test/SKILL.md",
        baseDir="/test",
        source="bundled",
        content="# Test",
    )
    
    command = SlashCommand(name="test", skill=skill, args="")
    
    context = build_slash_command_context(command)
    
    assert "No user arguments provided." in context


def test_build_slash_command_context_no_content():
    """Build context when skill content not loaded."""
    skill = Skill(
        name="empty",
        description="Empty",
        filePath="/empty/SKILL.md",
        baseDir="/empty",
        source="bundled",
        content=None,
    )
    
    command = SlashCommand(name="empty", skill=skill, args="test")
    
    context = build_slash_command_context(command)
    
    assert "(Skill content not loaded)" in context


def test_build_skill_registry_from_entries():
    """Build registry from entries."""
    skills = [
        SkillEntry(
            skill=Skill(name="skill1", description="1", filePath="/p1", baseDir="/p1", source="bundled"),
            eligible=True,
        ),
        SkillEntry(
            skill=Skill(name="skill2", description="2", filePath="/p2", baseDir="/p2", source="bundled"),
            eligible=False,  # Not eligible
        ),
        SkillEntry(
            skill=Skill(name="skill3", description="3", filePath="/p3", baseDir="/p3", source="bundled"),
            eligible=True,
        ),
    ]
    
    registry = build_skill_registry_from_entries(skills)
    
    # Only eligible skills
    assert "skill1" in registry
    assert "skill3" in registry
    assert "skill2" not in registry


def test_build_skill_registry_excludes_non_user_invocable_by_default():
    """user-invocable: false skills are excluded from the slash command registry by default."""
    from nano_openclaw.skills import SkillInvocationPolicy

    skill_model_only = Skill(
        name="mockup",
        description="Model-only skill",
        filePath="/p/SKILL.md",
        baseDir="/p",
        source="bundled",
    )
    skill_user = Skill(
        name="github",
        description="User-invocable skill",
        filePath="/p2/SKILL.md",
        baseDir="/p2",
        source="bundled",
    )

    entries = [
        SkillEntry(
            skill=skill_model_only,
            eligible=True,
            invocation=SkillInvocationPolicy(userInvocable=False),
        ),
        SkillEntry(
            skill=skill_user,
            eligible=True,
            invocation=SkillInvocationPolicy(userInvocable=True),
        ),
    ]

    # Default: user_invocable_only=True — slash command registry
    slash_registry = build_skill_registry_from_entries(entries)
    assert "mockup" not in slash_registry
    assert "github" in slash_registry


def test_build_skill_registry_includes_non_user_invocable_when_unrestricted():
    """user-invocable: false skills ARE included when user_invocable_only=False (model Skill tool registry)."""
    from nano_openclaw.skills import SkillInvocationPolicy

    skill_model_only = Skill(
        name="mockup",
        description="Model-only skill",
        filePath="/p/SKILL.md",
        baseDir="/p",
        source="bundled",
    )

    entries = [
        SkillEntry(
            skill=skill_model_only,
            eligible=True,
            invocation=SkillInvocationPolicy(userInvocable=False),
        ),
    ]

    # user_invocable_only=False — model Skill tool registry
    model_registry = build_skill_registry_from_entries(entries, user_invocable_only=False)
    assert "mockup" in model_registry


def test_is_skill_user_invocable_default():
    """Default to invocable."""
    skill = Skill(name="test", description="Test", filePath="/p", baseDir="/p", source="bundled")
    
    assert is_skill_user_invocable(skill) is True


def test_is_skill_user_invocable_from_entry():
    """Check from entry exposure/invocation."""
    from nano_openclaw.skills import SkillExposure, SkillInvocationPolicy
    
    skill = Skill(name="test", description="Test", filePath="/p", baseDir="/p", source="bundled")
    
    # Invocable via exposure
    entry = SkillEntry(skill=skill, exposure=SkillExposure(userInvocable=True))
    assert is_skill_user_invocable(skill, entry) is True
    
    # Not invocable via exposure
    entry = SkillEntry(skill=skill, exposure=SkillExposure(userInvocable=False))
    assert is_skill_user_invocable(skill, entry) is False
    
    # Invocable via invocation
    entry = SkillEntry(skill=skill, invocation=SkillInvocationPolicy(userInvocable=True))
    assert is_skill_user_invocable(skill, entry) is True
    
    # Not invocable via invocation
    entry = SkillEntry(skill=skill, invocation=SkillInvocationPolicy(userInvocable=False))
    assert is_skill_user_invocable(skill, entry) is False