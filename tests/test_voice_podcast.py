from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

from nano_openclaw.adapters.webui.server import _webui_payloads_from_push
from nano_openclaw.api.methods.podcast import podcast_remove_agent, podcast_start, podcast_update_agent
from nano_openclaw.features.voice.podcast import (
    HOST_VOICE_ID,
    _voice_gender,
    assign_agents,
    build_host_prompt,
    build_speaker_prompt,
    build_start_summary,
    choose_speakers,
    normalize_utterance,
    normalize_rounds,
    podcast_model_options,
)
from nano_openclaw.services.backend import PushEvent
from nano_openclaw.services.backend_embedded import EmbeddedBackend


def test_podcast_assigns_distinct_speaker_voices_and_excludes_host_voice():
    agents = assign_agents(
        [
            {"id": "a1", "role": "自动"},
            {"id": "a2", "role": "云计算架构师"},
            {"id": "a3", "role": "AI Agent研发工程师"},
        ],
        "讨论 AI Agent 在云上部署和 RDMA 数据中心网络",
        excluded_voice_id="zhishuo",
        rng=random.Random("assigned-voices"),
    )

    voice_ids = [agent.voice_id for agent in agents]
    assert HOST_VOICE_ID == "xiaoxian"
    assert "zhishuo" not in voice_ids
    assert len(set(voice_ids)) == len(voice_ids)
    assert agents[0].role == "高性能网络协议设计师"
    assert agents[1].role == "云计算架构师"


def test_podcast_voice_assignment_is_randomized_at_binding_time():
    raw_agents = [
        {"id": "a1", "role": "作家"},
        {"id": "a2", "role": "云计算架构师"},
        {"id": "a3", "role": "AI Agent研发工程师"},
        {"id": "a4", "role": "硬件工程师"},
    ]

    first = assign_agents(raw_agents, "AI 播客", rng=random.Random("voice-seed"))
    second = assign_agents(raw_agents, "AI 播客", rng=random.Random("voice-seed"))
    different = assign_agents(raw_agents, "AI 播客", rng=random.Random("other-seed"))

    assert [agent.voice_id for agent in first] == [agent.voice_id for agent in second]
    assert [agent.voice_id for agent in first] != [agent.voice_id for agent in different]
    assert len({agent.voice_id for agent in first}) == len(raw_agents)


def test_podcast_model_assignment_uses_configured_text_models():
    config = SimpleNamespace(
        models=SimpleNamespace(
            providers={
                "p1": SimpleNamespace(
                    models=[
                        SimpleNamespace(id="m1", name="Model One", input=["text"]),
                        SimpleNamespace(id="vision", name="Vision", input=["image"]),
                    ]
                ),
                "p2": SimpleNamespace(
                    models=[
                        SimpleNamespace(id="m2", name="", input=["text", "image"]),
                    ]
                ),
            }
        )
    )

    refs, labels = podcast_model_options(config)
    agents = assign_agents(
        [{"role": "作家"}, {"role": "云计算架构师"}],
        "AI 播客",
        model_refs=refs,
        model_labels=labels,
        rng=random.Random("models"),
    )

    assert refs == ["p1/m1", "p2/m2"]
    assert labels["p1/m1"] == "Model One"
    assert {agent.model_ref for agent in agents} == {"p1/m1", "p2/m2"}
    assert agents[0].model_label


def test_podcast_agent_can_request_voice_and_model():
    agents = assign_agents(
        [
            {
                "id": "a1",
                "role": "作家",
                "voice_id": "zhishuo",
                "voice_label": "知硕",
                "model_ref": "p2/m2",
                "model_label": "Model Two",
            }
        ],
        "AI 播客",
        excluded_voice_id="zhishuo",
        model_refs=["p1/m1"],
        model_labels={"p1/m1": "Model One", "p2/m2": "Model Two"},
        rng=random.Random("requested-agent"),
    )

    assert agents[0].voice_id == "zhishuo"
    assert agents[0].voice_label == "知硕"
    assert agents[0].model_ref == "p2/m2"
    assert agents[0].model_label == "Model Two"


def test_podcast_voice_assignment_balances_male_and_female_voices():
    agents = assign_agents(
        [
            {"id": "a1", "role": "作家"},
            {"id": "a2", "role": "云计算架构师"},
            {"id": "a3", "role": "AI Agent研发工程师"},
            {"id": "a4", "role": "硬件工程师"},
            {"id": "a5", "role": "IT后台研发工程师"},
            {"id": "a6", "role": "IT前端研发工程师"},
        ],
        "AI 播客",
        rng=random.Random("balanced-voices"),
    )

    genders = [_voice_gender(agent.voice_id) for agent in agents]

    assert genders.count("male") == 3
    assert genders.count("female") == 3


def test_podcast_hardware_engineer_role_is_selectable_and_auto_matched():
    explicit = assign_agents([{"role": "硬件工程师"}], "聊一个设备方案")[0]
    auto = assign_agents([{"role": "自动"}], "PCB 电源和传感器的硬件可靠性")[0]

    assert explicit.role == "硬件工程师"
    assert auto.role == "硬件工程师"


def test_podcast_auto_roles_prefer_distinct_identities():
    agents = assign_agents(
        [{"role": "自动"}, {"role": "自动"}, {"role": "自动"}, {"role": "自动"}],
        "讨论 AI Agent 在云上部署、后端服务和 RDMA 数据中心网络",
        rng=random.Random("auto-roles"),
    )

    roles = [agent.role for agent in agents]

    assert len(set(roles)) == len(roles)
    assert roles[0] == "高性能网络协议设计师"
    assert "AI Agent研发工程师" in roles
    assert "云计算架构师" in roles
    assert "IT后台研发工程师" in roles


def test_podcast_round_speaker_selection_uses_multiple_agents():
    agents = assign_agents(
        [{"role": "作家"}, {"role": "云计算架构师"}, {"role": "AI Agent研发工程师"}],
        "AI播客",
    )

    selected = choose_speakers(agents, 1, random.Random(1))
    assert 2 <= len(selected) <= len(agents)
    assert len(choose_speakers(agents, 3, random.Random(1))) == len(agents)


def test_podcast_rounds_and_start_summary():
    agents = assign_agents([{"role": "作家"}], "写作")

    assert normalize_rounds(None) == 20
    assert normalize_rounds(0) == 1
    assert normalize_rounds(500) == 100

    summary = build_start_summary("话题", agents, 3)
    assert "主持人：女主持人（小仙·亲切女声 / xiaoxian）" in summary
    assert "作家" in summary
    assert agents[0].voice_id in summary

    custom_summary = build_start_summary(
        "话题",
        agents,
        3,
        host_voice_id="zhishuo",
        host_voice_label="知硕",
    )
    assert "主持人：女主持人（知硕 / zhishuo）" in custom_summary


def test_speaker_prompt_consumes_subagent_research_result():
    agent = assign_agents([{"role": "云计算架构师"}], "云上 AI Agent")[0]

    prompt = build_speaker_prompt(
        topic="云上 AI Agent",
        agent=agent,
        round_index=1,
        context="主持人开场",
        research="Kubernetes 和可观测性是主流落地关注点。",
    )

    assert "你的专属 research 子 Agent 已返回以下研究摘要" in prompt
    assert "Kubernetes 和可观测性" in prompt
    assert "不超过 200 个中文字符" in prompt
    assert "完整句子收尾" in prompt
    assert "不要复述主持人或其他 Agent 已经说过的内容" in prompt


def test_host_prompt_prevents_mid_run_closing_and_allows_final_closing():
    agent = assign_agents([{"role": "云计算架构师"}], "云上 AI Agent")[0]

    mid_prompt = build_host_prompt(
        topic="云上 AI Agent",
        round_index=5,
        total_rounds=20,
        speakers=[agent],
    )
    final_prompt = build_host_prompt(
        topic="云上 AI Agent",
        round_index=20,
        total_rounds=20,
        speakers=[agent],
    )

    assert "禁止说“本期结束”" in mid_prompt
    assert "必须继续引导讨论" in mid_prompt
    assert "最后一轮" in final_prompt
    assert "最终收束" in final_prompt


def test_normalize_utterance_cleans_without_truncating():
    text = (
        "【AI Agent研发工程师｜小刚】  主持人提到协作和可靠性怎么交汇，我给一个具体的技术落点："
        "这两个方向的交汇处就是可观测的多智能体编排层。"
        "现在多Agent协作最大的坑不是模型不够聪明，而是你根本不知道哪个环节出了问题——"
        "一个Agent把错误结果传给下一个，链条就断了。"
        "所以真正该投入的是Agent链路的状态追踪、中间产物检查点和工具调用的重试容错机制。"
        "MCP解决了工具接入标准化，但没有解决调用失败后怎么办。"
        "谁先把这个可观测加容错机制产品化，谁就更可能胜出。"
    )

    value = normalize_utterance(text, limit=200)

    assert len(value) > 200
    assert not value.startswith("【")
    assert "谁先把这个可观测加容错机制产品化" in value


def test_webui_forwards_podcast_push_payload():
    event = PushEvent(
        event="podcast.event",
        payload={"type": "podcast.done", "run_id": "run-1"},
        seq=1,
    )

    assert _webui_payloads_from_push(event, manager=None, turn_sessions={}) == [
        {"type": "podcast.done", "run_id": "run-1"}
    ]


def test_podcast_start_rpc_handler_delegates_to_backend():
    class Backend:
        async def podcast_start(self, *, session_key, topic, agents, rounds, host_voice_id="", host_voice_label=""):
            self.call = {
                "session_key": session_key,
                "topic": topic,
                "agents": agents,
                "rounds": rounds,
                "host_voice_id": host_voice_id,
                "host_voice_label": host_voice_label,
            }
            return {"run_id": "run-1", "agents": agents}

    backend = Backend()
    result = asyncio.run(
        podcast_start(
            SimpleNamespace(backend=backend),
            {
            "session_id": "s1",
            "topic": "AI播客",
            "rounds": 7,
            "host_voice_id": "zhishuo",
            "host_voice_label": "知硕",
            "agents": [{"id": "a1", "role": "作家"}],
            },
        )
    )

    assert result["run_id"] == "run-1"
    assert backend.call == {
        "session_key": "s1",
        "topic": "AI播客",
        "agents": [{"id": "a1", "role": "作家"}],
        "rounds": 7,
        "host_voice_id": "zhishuo",
        "host_voice_label": "知硕",
    }


def test_podcast_remove_agent_rpc_handler_delegates_to_backend():
    class Backend:
        async def podcast_remove_agent(self, *, run_id, agent_id):
            self.call = {"run_id": run_id, "agent_id": agent_id}
            return {"ok": True, "agent_id": agent_id}

    backend = Backend()
    result = asyncio.run(
        podcast_remove_agent(
            SimpleNamespace(backend=backend),
            {"run_id": "run-1", "agent_id": "agent-2"},
        )
    )

    assert result == {"ok": True, "agent_id": "agent-2"}
    assert backend.call == {"run_id": "run-1", "agent_id": "agent-2"}


def test_podcast_update_agent_rpc_handler_delegates_to_backend():
    class Backend:
        async def podcast_update_agent(self, *, run_id, agent):
            self.call = {"run_id": run_id, "agent": agent}
            return {"ok": True, "agent_id": agent["id"], "generation": 2}

    backend = Backend()
    result = asyncio.run(
        podcast_update_agent(
            SimpleNamespace(backend=backend),
            {
                "run_id": "run-1",
                "agent": {
                    "id": "agent-2",
                    "role": "作家",
                    "voice_id": "zhishuo",
                    "model_ref": "p/m",
                },
            },
        )
    )

    assert result == {"ok": True, "agent_id": "agent-2", "generation": 2}
    assert backend.call == {
        "run_id": "run-1",
        "agent": {
            "id": "agent-2",
            "role": "作家",
            "voice_id": "zhishuo",
            "model_ref": "p/m",
        },
    }


def test_podcast_skipped_utterance_event_preserves_sequence_for_playback_queue():
    class Backend:
        def __init__(self):
            self.events = []

        def _emit_podcast(self, payload):
            self.events.append(payload)

    backend = Backend()
    session = SimpleNamespace(session_id="session-1")
    agent = SimpleNamespace(
        id="agent-2",
        role="作家",
        voice_id="zhishuo",
        voice_label="知硕",
        model_ref="p/m",
    )

    EmbeddedBackend._emit_podcast_utterance_skipped(
        backend,
        run_id="run-1",
        session=session,
        round_index=3,
        phase="speaker",
        sequence=7,
        agent=agent,
        generation=2,
    )

    assert backend.events == [
        {
            "type": "podcast.utterance.skipped",
            "run_id": "run-1",
            "session_id": "session-1",
            "round": 3,
            "phase": "speaker",
            "sequence": 7,
            "agent_id": "agent-2",
            "role": "作家",
            "voice_id": "zhishuo",
            "voice_label": "知硕",
            "model_ref": "p/m",
            "generation": 2,
        }
    ]
