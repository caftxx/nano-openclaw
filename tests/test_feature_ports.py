from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nano_openclaw.core.loop import AgentSession, LoopConfig
from nano_openclaw.core.provider import MessageEnd, TextDelta
from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.features.skills.runtime import SkillRuntime


def test_run_turn_skill_runtime_injects_slash_skill_context(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo Skill\nDo the demo task.\n",
        encoding="utf-8",
    )
    sent_messages: list[list[dict]] = []

    async def fake_stream_response(**kwargs):
        sent_messages.append(kwargs["messages"])
        yield TextDelta(text="done")
        yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    session = AgentSession(
        history=[],
        registry=ToolRegistry(),
        on_event=lambda _event: None,
        client=object(),
        cfg=LoopConfig(
            workspace_dir=tmp_path,
            state_dir=tmp_path / "state",
            session_key="test",
            skill_runtime=SkillRuntime(),
        ),
    )

    asyncio.run(session.run_turn("/demo with args"))

    user_text = "\n".join(
        block.get("text", "")
        for block in sent_messages[0][0]["content"]
    )
    assert "[Skill invoked: demo]" in user_text
    assert "Do the demo task." in user_text
    assert "User arguments: with args" in user_text
    eligible_skills = session.registry.execution_context().eligible_skills
    assert "demo" in eligible_skills


def test_run_turn_active_memory_recall_prepends_context(tmp_path, monkeypatch):
    sent_messages: list[list[dict]] = []

    async def fake_recall(**kwargs):
        assert kwargs["workspace_dir"] == str(tmp_path)
        assert kwargs["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ]
        return SimpleNamespace(context="[memory] remember this")

    async def fake_stream_response(**kwargs):
        sent_messages.append(kwargs["messages"])
        yield TextDelta(text="done")
        yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    session = AgentSession(
        history=[],
        registry=ToolRegistry(),
        on_event=lambda _event: None,
        client=object(),
        cfg=LoopConfig(
            workspace_dir=tmp_path,
            session_key="test",
            active_memory_recall=fake_recall,
        ),
    )

    asyncio.run(session.run_turn("hello"))

    user_blocks = sent_messages[0][0]["content"]
    assert user_blocks[0]["text"] == "[memory] remember this"
    assert user_blocks[1]["text"] == "hello"
