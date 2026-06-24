import asyncio

from nano_openclaw.core.loop import AgentSession, LoopConfig
from nano_openclaw.core.prompt import VOICE_STYLE_PROMPT
from nano_openclaw.core.tools import ToolRegistry


def _system_for(cfg: LoopConfig) -> str:
    session = AgentSession(
        history=[],
        registry=ToolRegistry(),
        on_event=lambda _event: None,
        client=None,
        cfg=cfg,
    )
    return asyncio.run(session._build_system_for_turn("hi", [], lambda _event: None))


def test_voice_emotion_prompt_injected_for_aliyun_emotion_voice():
    system = _system_for(LoopConfig(
        response_style="voice",
        voice_id="zhimiao_emo",
        voice_output="aliyun-flowing",
    ))
    assert VOICE_STYLE_PROMPT in system
    assert "<voice_ssml_emotion_mode>" in system
    assert "zhimiao_emo" in system
    assert "customer-service" in system
    assert "普通语句直接写文本即可" in system
    assert "不要把整段回复统一包进一个 emotion" in system
    assert "所有朗读内容都必须放在 `<speak>` 内，并用 `<emotion" not in system


def test_voice_emotion_prompt_skipped_for_local_or_plain_voice():
    local_system = _system_for(LoopConfig(
        response_style="voice",
        voice_id="zhimiao_emo",
        voice_output="local",
    ))
    plain_system = _system_for(LoopConfig(
        response_style="voice",
        voice_id="xiaoxian",
        voice_output="aliyun-flowing",
    ))
    assert VOICE_STYLE_PROMPT in local_system
    assert "<voice_ssml_emotion_mode>" not in local_system
    assert "<voice_ssml_emotion_mode>" not in plain_system
