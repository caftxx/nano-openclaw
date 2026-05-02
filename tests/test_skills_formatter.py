"""Tests for skills formatter module."""

from nano_openclaw.skills import (
    Skill,
    apply_skills_prompt_limits,
    escape_xml,
    format_skills_compact,
    format_skills_for_prompt,
)


def test_escape_xml():
    """Escape XML special characters."""
    assert escape_xml("test") == "test"
    assert escape_xml("a & b") == "a &amp; b"
    assert escape_xml("<tag>") == "&lt;tag&gt;"
    assert escape_xml('"quote"') == "&quot;quote&quot;"
    assert escape_xml("'single'") == "&apos;single&apos;"


def test_format_skills_empty():
    """Return empty string for no skills."""
    assert format_skills_for_prompt([]) == ""
    assert format_skills_compact([]) == ""


def test_format_skills_single():
    """Format single skill."""
    skill = Skill(
        name="test-skill",
        description="A test skill description",
        filePath="/path/to/SKILL.md",
        baseDir="/path/to",
        source="bundled",
    )
    
    result = format_skills_for_prompt([skill])
    
    assert "<available_skills>" in result
    assert "</available_skills>" in result
    assert "<name>test-skill</name>" in result
    assert "<description>A test skill description</description>" in result
    assert "<location>/path/to/SKILL.md</location>" in result


def test_format_skills_multiple():
    """Format multiple skills."""
    skills = [
        Skill(name="skill1", description="First skill", filePath="/p1/SKILL.md", baseDir="/p1", source="bundled"),
        Skill(name="skill2", description="Second skill", filePath="/p2/SKILL.md", baseDir="/p2", source="bundled"),
    ]
    
    result = format_skills_for_prompt(skills)
    
    assert "<name>skill1</name>" in result
    assert "<name>skill2</name>" in result
    assert result.count("<skill>") == 2


def test_format_skills_compact():
    """Compact format excludes description."""
    skill = Skill(
        name="compact-skill",
        description="This description should not appear",
        filePath="/path/SKILL.md",
        baseDir="/path",
        source="bundled",
    )
    
    result = format_skills_compact([skill])
    
    assert "<name>compact-skill</name>" in result
    assert "<location>/path/SKILL.md</location>" in result
    assert "This description should not appear" not in result


def test_format_skills_escapes_content():
    """Escape special chars in skill content."""
    skill = Skill(
        name="xml-skill",
        description="Skill with <special> chars & entities",
        filePath="/path/SKILL.md",
        baseDir="/path",
        source="bundled",
    )
    
    result = format_skills_for_prompt([skill])
    
    # Should be escaped
    assert "&lt;special&gt;" in result
    assert "&amp; entities" in result
    assert "<special>" not in result


def test_apply_skills_prompt_limits_no_limits():
    """Return all skills when within limits."""
    skills = [
        Skill(name=f"skill{i}", description=f"Skill {i}", filePath=f"/p{i}/SKILL.md", baseDir=f"/p{i}", source="bundled")
        for i in range(10)
    ]
    
    result, truncated, compact = apply_skills_prompt_limits(
        skills,
        max_skills=100,
        max_chars=100_000,
    )
    
    assert len(result) == 10
    assert truncated is False
    assert compact is False


def test_apply_skills_prompt_limits_count_truncate():
    """Truncate by count."""
    skills = [
        Skill(name=f"skill{i}", description=f"Skill {i}", filePath=f"/p{i}/SKILL.md", baseDir=f"/p{i}", source="bundled")
        for i in range(20)
    ]
    
    result, truncated, compact = apply_skills_prompt_limits(
        skills,
        max_skills=10,
        max_chars=100_000,
    )
    
    assert len(result) == 10
    assert truncated is True
    assert compact is False


def test_apply_skills_prompt_limits_char_budget():
    """Switch to compact when char budget exceeded."""
    # Create skills with long descriptions
    skills = [
        Skill(
            name=f"skill{i}",
            description="This is a very long description that will exceed the character budget when formatted in full mode " * 10,
            filePath=f"/p{i}/SKILL.md",
            baseDir=f"/p{i}",
            source="bundled",
        )
        for i in range(10)
    ]
    
    result, truncated, compact = apply_skills_prompt_limits(
        skills,
        max_skills=100,
        max_chars=500,
    )
    
    # Should use compact format
    assert compact is True
    # Should still have some skills
    assert len(result) > 0


def test_apply_skills_prompt_limits_drops_skills():
    """Drop skills when even compact exceeds budget."""
    # Create many skills
    skills = [
        Skill(name=f"skill{i}", description="Desc", filePath=f"/p{i}/SKILL.md", baseDir=f"/p{i}", source="bundled")
        for i in range(100)
    ]
    
    # Very small budget
    result, truncated, compact = apply_skills_prompt_limits(
        skills,
        max_skills=100,
        max_chars=200,
    )
    
    assert compact is True
    assert truncated is True
    assert len(result) < 100