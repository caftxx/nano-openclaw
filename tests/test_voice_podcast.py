from __future__ import annotations

import asyncio
import base64
import random
from types import SimpleNamespace

from nano_openclaw.adapters.webui.server import _webui_payloads_from_push
from nano_openclaw.api.methods.podcast import (
    podcast_add_agent,
    podcast_remove_agent,
    podcast_start,
    podcast_update_agent,
    podcast_update_host,
)
from nano_openclaw.core.loop import TurnCancelled
from nano_openclaw.features.voice.podcast import (
    AGENT_ROLES,
    HOST_VOICE_ID,
    _voice_gender,
    assign_agents,
    build_paper_fallback_utterance,
    build_host_prompt,
    build_paper_reference_query,
    build_discussion_context,
    build_research_prompt,
    build_speaker_prompt,
    build_start_summary,
    choose_speakers,
    discussion_mode_for_attachments,
    normalize_paper_scope_claims,
    normalize_utterance,
    normalize_rounds,
    podcast_model_options,
    select_reference_context,
    validate_paper_utterance,
)
from nano_openclaw.features.voice.voice_catalog import voice_score
from nano_openclaw.services.backend import PushEvent
from nano_openclaw.services.backend_embedded import EmbeddedBackend


def test_podcast_attachment_context_describes_uploaded_image(monkeypatch):
    calls = []

    async def fake_describe_image(b64, mime, **kwargs):
        calls.append({"b64": b64, "mime": mime, **kwargs})
        return "一张蓝色产品发布计划图"

    monkeypatch.setattr("nano_openclaw.core.images.describe_image", fake_describe_image)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    backend = SimpleNamespace(runtime=SimpleNamespace(
        cfg=SimpleNamespace(image_model="vision-model", model_input=("text",), api="openai"),
        client=object(),
        model_id="main-model",
    ))

    result = asyncio.run(EmbeddedBackend._podcast_attachment_context(
        backend,
        "请分析",
        [{
            "name": "plan.png",
            "mime": "image/png",
            "size": len(png),
            "data": base64.b64encode(png).decode(),
        }],
    ))

    assert result == "请分析\n\n[参考图片：plan.png]\n一张蓝色产品发布计划图"
    assert calls[0]["model"] == "vision-model"
    assert calls[0]["mime"] == "image/png"


def test_podcast_attachment_context_infers_image_mime_from_suffix(monkeypatch):
    calls = []

    async def fake_describe_image(b64, mime, **kwargs):
        calls.append((b64, mime))
        return "一张图片"

    monkeypatch.setattr("nano_openclaw.core.images.describe_image", fake_describe_image)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    backend = SimpleNamespace(runtime=SimpleNamespace(
        cfg=SimpleNamespace(image_model="vision-model", model_input=("text",), api="openai"),
        client=object(),
        model_id="main-model",
    ))

    result = asyncio.run(EmbeddedBackend._podcast_attachment_context(
        backend,
        "",
        [{
            "name": "photo.png",
            "mime": "application/octet-stream",
            "size": len(png),
            "data": base64.b64encode(png).decode(),
        }],
    ))

    assert result == "[参考图片：photo.png]\n一张图片"
    assert calls[0][1] == "image/png"


def test_podcast_attachment_context_describes_images_concurrently(monkeypatch):
    calls = []

    async def fake_describe_image(*args, **kwargs):
        calls.append(args)
        while len(calls) < 2:
            await asyncio.sleep(0.001)
        return "图片描述"

    monkeypatch.setattr("nano_openclaw.core.images.describe_image", fake_describe_image)
    monkeypatch.setattr(
        "nano_openclaw.services.backend_embedded.PODCAST_ATTACHMENT_TIMEOUT_SECONDS",
        0.2,
    )
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    encoded = base64.b64encode(png).decode()
    backend = SimpleNamespace(runtime=SimpleNamespace(
        cfg=SimpleNamespace(image_model="vision-model", model_input=("text",), api="openai"),
        client=object(),
        model_id="main-model",
    ))

    result = asyncio.run(EmbeddedBackend._podcast_attachment_context(
        backend,
        "",
        [
            {"name": "one.png", "mime": "image/png", "size": len(png), "data": encoded},
            {"name": "two.png", "mime": "image/png", "size": len(png), "data": encoded},
        ],
    ))

    assert len(calls) == 2
    assert "[参考图片：one.png]" in result
    assert "[参考图片：two.png]" in result


def test_podcast_attachment_context_times_out_image_description(monkeypatch):
    async def stalled_describe_image(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("nano_openclaw.core.images.describe_image", stalled_describe_image)
    monkeypatch.setattr(
        "nano_openclaw.services.backend_embedded.PODCAST_ATTACHMENT_TIMEOUT_SECONDS",
        0.01,
    )
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    backend = SimpleNamespace(runtime=SimpleNamespace(
        cfg=SimpleNamespace(image_model="vision-model", model_input=("text",), api="openai"),
        client=object(),
        model_id="main-model",
    ))

    try:
        asyncio.run(EmbeddedBackend._podcast_attachment_context(
            backend,
            "",
            [{
                "name": "stalled.png",
                "mime": "image/png",
                "size": len(png),
                "data": base64.b64encode(png).decode(),
            }],
        ))
    except ValueError as exc:
        assert "图片理解超时" in str(exc)
        assert "stalled.png" in str(exc)
    else:
        raise AssertionError("stalled image description should time out")


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


def test_podcast_voice_assignment_prefers_high_rated_voices():
    agents = assign_agents(
        [
            {"id": "a1", "role": "作家"},
            {"id": "a2", "role": "云计算架构师"},
            {"id": "a3", "role": "AI Agent研发工程师"},
            {"id": "a4", "role": "硬件工程师"},
        ],
        "AI 播客",
        rng=random.Random("score-priority"),
    )

    assert [voice_score(agent.voice_id) for agent in agents] == [5, 5, 5, 5]
    assert {agent.voice_id for agent in agents} <= {
        "zhishuo",
        "aixiang",
        "zhixiaoxia",
        "zhixiaomei",
        "zhigui",
        "zhimiao_emo",
        "zhiya",
    }


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


def test_podcast_auto_role_with_existing_roles_prefers_unused_identity():
    existing_roles = [role for role in AGENT_ROLES if role not in {"自动", "相声演员"}]
    agents = assign_agents(
        [{"role": "自动"}],
        "讨论 AI Agent 和云计算",
        existing_roles=existing_roles,
        rng=random.Random("unused-role"),
    )

    assert agents[0].role == "相声演员"


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
    assert "说人话" in prompt
    assert "少用黑话" in prompt
    assert "短比喻、小例子或轻微幽默" in prompt


def test_podcast_prompts_keep_original_topic_primary_over_role_metaphors():
    topic = "只讨论城市屋顶花园如何通过蒸腾、遮阴和基质热容量降低室温"
    agent = assign_agents([{"role": "AI Agent研发工程师"}], topic)[0]

    host_prompt = build_host_prompt(
        topic=topic,
        round_index=4,
        total_rounds=10,
        speakers=[agent],
    )
    speaker_prompt = build_speaker_prompt(
        topic=topic,
        agent=agent,
        round_index=4,
        context="主持人: 上一轮把遮阴比作 Agent 工具调用。",
        research="遮阴减少屋面吸收的太阳辐射。",
    )
    research_prompt = build_research_prompt(
        topic=topic,
        agent=agent,
        round_index=1,
        context="主持人开场",
    )

    assert "下一问必须直接拉回原始问题" in host_prompt
    assert "不要继续追问该类比" in host_prompt
    assert "身份只是观察角度" in speaker_prompt
    assert "不能把话题改写成你的职业问题" in speaker_prompt
    assert "最多使用一个简短类比" in speaker_prompt
    assert "播客主题是研究对象" in research_prompt
    assert "不得把研究对象替换成该身份所在行业的问题" in research_prompt
    assert topic in host_prompt
    assert topic in speaker_prompt
    assert topic in research_prompt


def test_paper_reference_selection_combines_relevance_with_round_coverage():
    reference = "\n\n".join([
        "[参考文档：paper.pdf]",
        "[第 1 页]\n研究背景与问题定义。",
        "[第 2 页]\n相关工作与理论基础。",
        "[第 3 页]\n实验方法使用对照组和消融实验。",
        "[第 4 页]\n数据集与评价指标。",
        "[第 5 页]\n结果显示准确率显著提升。",
    ])

    first = select_reference_context(
        reference,
        query="实验方法和消融实验",
        round_index=1,
        max_chunks=2,
    )
    fourth = select_reference_context(
        reference,
        query="实验方法和消融实验",
        round_index=4,
        max_chunks=2,
    )

    assert "paper.pdf｜第 3 页" in first
    assert "paper.pdf｜第 1 页" in first
    assert "paper.pdf｜第 3 页" in fourth
    assert "paper.pdf｜第 4 页" in fourth
    assert first != fourth


def test_paper_prompts_require_source_locations_and_fresh_analysis():
    topic = "讨论论文的方法和实验结果"
    agent = assign_agents([{"role": "作家"}], topic)[0]
    context = "[本轮论文依据：第 2 轮]\n[参考文档：paper.pdf｜第 6 页]\n消融实验结果"

    speaker = build_speaker_prompt(
        topic=topic,
        agent=agent,
        round_index=2,
        context=context,
        research="第 6 页报告了消融实验。",
    )
    research = build_research_prompt(
        topic=topic,
        agent=agent,
        round_index=2,
        context=context,
    )

    assert "文档名及页码/片段" in speaker
    assert "不得编造页码" in speaker
    assert "当前轮次唯一议程" in speaker
    assert "不得把模型训练 checkpoint 改写成多 Agent checkpoint" in speaker
    assert "待验证的工程推断" in speaker
    assert "这里只是类比，不是论文结论" in speaker
    assert "本轮尚未讨论" in research
    assert "当前轮次唯一议程" in research
    assert "论文讨论模式禁止使用 Web" in research
    assert "部署拓扑、显存容量、成本、延迟" in research


def test_pdf_attachment_automatically_enables_paper_discussion_mode():
    assert discussion_mode_for_attachments("自由讨论", [{
        "name": "paper.pdf",
        "mime": "application/pdf",
    }]) == "paper"
    assert discussion_mode_for_attachments("论文研讨", [{
        "name": "draft.docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }]) == "paper"
    assert discussion_mode_for_attachments("项目周报", [{
        "name": "report.docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }]) == "group"


def test_paper_round_agenda_drives_cross_language_section_retrieval():
    topic = (
        "第一轮分析总体架构；第二轮分析 CSA/HCA 的效率机制；"
        "第三轮分析训练与推理基础设施；第四轮审视评测证据和局限"
    )
    reference = "\n\n".join([
        "[参考文档：paper.pdf]",
        "[第 2 页]\nContents\nArchitecture . . . 6\nInfrastructure . . . 15\nEvaluation . . . 36",
        "[第 6 页]\nOverall architecture and residual connections.",
        "[第 9 页]\nHybrid Attention with CSA and HCA improves efficiency.",
        "[第 23 页]\nInference framework and on-disk KV cache storage.",
        "[第 26 页]\nMitigating Training Instability.",
        "[第 44 页]\nConclusion, Limitations, and Future Directions.",
    ])

    second_query = build_paper_reference_query(
        topic=topic,
        context="主持人：进入第二轮。",
        round_index=2,
        total_rounds=4,
    )
    third_query = build_paper_reference_query(
        topic=topic,
        context="主持人：进入第三轮。",
        round_index=3,
        total_rounds=4,
    )
    fourth_query = build_paper_reference_query(
        topic=topic,
        context="主持人：进入第四轮。",
        round_index=4,
        total_rounds=4,
    )

    second = select_reference_context(reference, query=second_query, round_index=2, max_chunks=1)
    third = select_reference_context(reference, query=third_query, round_index=3, max_chunks=1)
    fourth = select_reference_context(reference, query=fourth_query, round_index=4, max_chunks=1)

    assert "第 9 页" in second
    assert "第 23 页" in third or "第 26 页" in third
    assert "第 44 页" in fourth
    assert "Contents" not in second + third + fourth


def test_paper_reference_query_does_not_reintroduce_later_agendas_from_anchor():
    topic = (
        "第一轮分析核心主张与总体架构；第二轮分析 CSA/HCA 的效率机制；"
        "第三轮分析训练与推理基础设施；第四轮审视评测证据和局限"
    )
    context = build_discussion_context(
        topic=topic,
        entries=["主持人: 先讨论总体架构。"],
    )

    query = build_paper_reference_query(
        topic=topic,
        context=context,
        round_index=1,
        total_rounds=4,
    )

    assert "核心主张与总体架构" in query
    assert "CSA/HCA" not in query
    assert "训练与推理基础设施" not in query
    assert "评测证据和局限" not in query


def test_paper_reference_query_keeps_only_explicit_user_followup():
    topic = "第一轮总体架构；第二轮效率机制"
    context = build_discussion_context(
        topic=topic,
        entries=[
            "主持人: 上一轮聊了架构。",
            "用户插话: 请结合第9页解释 HCA。",
        ],
    )

    query = build_paper_reference_query(
        topic=topic,
        context=context,
        round_index=2,
        total_rounds=2,
    )

    assert "请结合第9页解释 HCA" in query
    assert "上一轮聊了架构" not in query


def test_paper_reference_uses_top_level_toc_entry_without_dot_leaders():
    reference = "\n\n".join([
        "[参考文档：paper.pdf]",
        (
            "[第 2 页]\nContents\n"
            "5.3 Standard Benchmark Evaluation . . . . . . 36\n"
            "5.3.2 Evaluation Results . . . . . . 37\n"
            "6 Conclusion, Limitations, and Future Directions 44"
        ),
        "[第 36 页]\nStandard benchmark evaluation setup.",
        "[第 37 页]\nEvaluation results on standard benchmarks.",
        "[第 44 页]\nConclusion, limitations, and future directions.",
    ])

    selected = select_reference_context(
        reference,
        query="评测证据、局限和仍未回答的问题",
        round_index=4,
        max_chunks=3,
    )

    assert "第 44 页" in selected
    assert "Contents" not in selected


def test_paper_reference_scans_all_chunks_from_a_detected_toc_page():
    long_toc_prefix = "\n".join(
        f"2.{index} Architecture Section . . . . . . {index + 3}"
        for index in range(1, 45)
    )
    reference = "\n\n".join([
        "[参考文档：paper.pdf]",
        (
            "[第 2 页]\nContents\n"
            + long_toc_prefix
            + "\n2.3.1 Compressed Sparse Attention . . . . . . 9"
            + "\n2.3.2 Heavily Compressed Attention 11"
            + "\n3.4.3 Contextual Parallelism for Long-Context Attention 20"
        ),
        "[第 9 页]\n" + ("Compressed Sparse Attention details. " * 80),
        "[第 11 页]\nHeavily Compressed Attention details.",
        "[第 20 页]\nTraining infrastructure for contextual parallelism.",
    ])

    selected = select_reference_context(
        reference,
        query="CSA HCA efficiency mechanism",
        round_index=2,
        max_chunks=3,
    )

    assert "第 9 页" in selected
    assert "第 11 页" in selected
    assert "第 20 页" not in selected


def test_paper_reference_does_not_pull_conclusion_into_architecture_round():
    reference = "\n\n".join([
        "[参考文档：paper.pdf]",
        "[第 2 页]\nContents\nOverall Architecture . . . . . . 6\nConclusion 44",
        "[第 1 页]\nAbstract and core contribution summary.",
        "[第 4 页]\nIntroduction and overall architecture contribution.",
        "[第 6 页]\nOverall Architecture and model blocks.",
        "[第 44 页]\nConclusion repeats the overall architecture and adds user limitations.",
    ])

    selected = select_reference_context(
        reference,
        query="核心主张与总体架构 architecture contribution",
        round_index=1,
        max_chunks=3,
    )

    assert "第 6 页" in selected
    assert "第 4 页" in selected
    assert "第 44 页" not in selected


def test_page_range_in_latest_host_question_overrides_general_relevance():
    reference = "\n\n".join([
        "[参考文档：paper.pdf]",
        "[第 1 页]\nHighly relevant architecture overview.",
        "[第 4 页]\nResearch problem and introduction.",
        "[第 5 页]\nContributions and scope.",
    ])
    selected = select_reference_context(
        reference,
        query="请回到第4到5页说明研究问题 architecture",
        round_index=1,
        max_chunks=2,
    )

    assert "第 4 页" in selected
    assert "第 5 页" in selected
    assert "第 1 页" not in selected


def test_paper_utterance_validator_rejects_missing_evidence_and_false_absence():
    reference = (
        "[本轮论文依据]\n"
        "[参考文档：paper.pdf｜第 1 页]\n"
        "The model uses 27% FLOPs and 10% KV cache."
    )

    assert validate_paper_utterance("论文第1页报告使用27% FLOPs。", reference) == (True, "")
    valid, reason = validate_paper_utterance("论文第23页介绍磁盘缓存。", reference)
    assert valid is False
    assert "未提供的页码" in reason
    valid, reason = validate_paper_utterance("论文第1页查无出处。", reference)
    assert valid is False
    assert "不能根据局部检索" in reason
    valid, reason = validate_paper_utterance("论文第1页之外原文没给具体数据。", reference)
    assert valid is False
    assert "不能根据局部检索" in reason
    valid, reason = validate_paper_utterance("第1页之外论文没解释为什么。", reference)
    assert valid is False
    assert "不能根据局部检索" in reason
    valid, reason = validate_paper_utterance("第1页之外论文没交代调用方式。", reference)
    assert valid is False
    assert "不能根据局部检索" in reason
    valid, reason = validate_paper_utterance("论文第1页报告使用99% FLOPs。", reference)
    assert valid is False
    assert "不存在的量化值" in reason

    valid, reason = validate_paper_utterance(
        "第1页说明百万 token 的 KV Cache 轻松吃掉几百GB显存。",
        reference,
    )
    assert valid is False
    assert "估算量级" in reason

    valid, reason = validate_paper_utterance(
        "第1页的压缩比意味着可以直接改成单机多卡部署。",
        reference,
    )
    assert valid is False
    assert "工程外推" in reason

    valid, reason = validate_paper_utterance(
        "第1页的压缩方式在 Agent 系统里存在上下文丢失风险。",
        reference,
    )
    assert valid is False
    assert "工程外推" in reason

    assert validate_paper_utterance(
        "第1页给出压缩结果；单机多卡只是待验证的工程推断，不是论文结论。",
        reference,
    ) == (True, "")

    valid, reason = validate_paper_utterance(
        "第1页的 MoE 相当于只叫少数专家干活。",
        reference,
    )
    assert valid is False
    assert "角色类比" in reason

    assert validate_paper_utterance(
        "第1页的 MoE 好比只叫少数专家干活；这里只是类比，不是论文结论。",
        reference,
    ) == (True, "")

    valid, reason = validate_paper_utterance(
        "第1页的压缩设计像给注意力上了双保险。",
        reference,
    )
    assert valid is False
    assert "角色类比" in reason

    valid, reason = validate_paper_utterance(
        "第1页的索引器等于一个侦察兵负责选目标。",
        reference,
    )
    assert valid is False
    assert "角色类比" in reason


def test_paper_scope_normalizer_keeps_absence_claims_local_to_evidence_window():
    text = (
        "论文没给延迟数据，原文没解释调度策略，"
        "论文没验证多机恢复，论文没拆开测索引器。"
    )

    normalized = normalize_paper_scope_claims(text)

    assert "论文没" not in normalized
    assert "原文没" not in normalized
    assert "本轮页面未给出延迟数据" in normalized
    assert "本轮页面未解释调度策略" in normalized
    assert normalized.count("本轮页面未显示相关验证") == 2


def test_paper_fallback_remains_useful_and_role_specific():
    reference = (
        "[参考文档：paper.pdf｜第 19 页｜片段 2]\nTraining infrastructure.\n"
        "[参考文档：paper.pdf｜第 21 页｜片段 3]\nInference infrastructure."
    )
    cloud = build_paper_fallback_utterance(
        topic="第一轮分析训练与推理基础设施",
        role="云计算架构师",
        round_index=1,
        reference_context=reference,
    )
    agent = build_paper_fallback_utterance(
        topic="第一轮分析训练与推理基础设施",
        role="AI Agent研发工程师",
        round_index=1,
        reference_context=reference,
    )

    assert "第19页、第21页" in cloud
    assert "训练与推理基础设施" in cloud
    assert "资源占用、并行效率与故障恢复稳定性" in cloud
    assert "状态保持、检索准确性与工具调用一致性" in agent
    assert cloud != agent
    assert validate_paper_utterance(cloud, reference) == (True, "")
    assert validate_paper_utterance(agent, reference) == (True, "")

def test_paper_host_prompt_keeps_each_round_on_its_agenda():
    topic = "第一轮分析架构；第二轮分析训练；第三轮分析评测和局限"
    agent = assign_agents([{"role": "作家"}], topic)[0]
    prompt = build_host_prompt(
        topic=topic,
        round_index=2,
        total_rounds=3,
        speakers=[agent],
        discussion_mode="paper",
    )

    assert "当前轮次议程：分析训练" in prompt
    assert "下一轮议程：分析评测和局限" in prompt
    assert "不得因为上一位发言不完整" in prompt
    assert "不要认可主讲人声称的“纠错”" in prompt
    assert "不得把主讲人的工程猜测升级成论文事实" in prompt


def test_paper_speaker_turn_refreshes_research_for_each_evidence_window():
    class Backend:
        def __init__(self):
            self.research_contexts = []
            self.speaker_prompts = []
            self.speaker_requests = []

        def _emit_podcast(self, _payload):
            return None

        async def _run_podcast_research_subagent(self, **kwargs):
            self.research_contexts.append(kwargs["context"])
            return f"research round {kwargs['round_index']}"

        async def _generate_podcast_utterance(self, **kwargs):
            self.speaker_prompts.append(kwargs["system_prompt"])
            self.speaker_requests.append(kwargs["user_text"])
            return f"speaker round {kwargs['round_index']}"

    async def run_case():
        backend = Backend()
        agent = assign_agents(
            [{"id": "agent-1", "role": "作家", "model_ref": "p/m"}],
            "论文实验方法",
        )[0]
        cache = {}
        reference = "\n\n".join([
            "[参考文档：paper.pdf]",
            "[第 1 页]\n研究背景。",
            "[第 2 页]\n理论基础。",
            "[第 3 页]\n实验方法。",
            "[第 4 页]\n数据集。",
            "[第 5 页]\n实验结果。",
        ])
        for round_index in (1, 2):
            await EmbeddedBackend._run_podcast_speaker_turn(
                backend,
                run_id="run-1",
                session=SimpleNamespace(session_id="session-1"),
                topic="论文实验方法",
                agent=agent,
                round_index=round_index,
                sequence=round_index,
                context="讨论主题锚点：论文实验方法",
                research_cache=cache,
                token=SimpleNamespace(is_cancelled=False),
                generation=0,
                is_generation_current=lambda _generation: True,
                is_agent_active=lambda _agent: True,
                reference_context=reference,
            )
        return backend, cache

    backend, cache = asyncio.run(run_case())

    assert len(backend.research_contexts) == 2
    assert backend.research_contexts[0] != backend.research_contexts[1]
    assert "第 1 页" in backend.research_contexts[0]
    assert "第 3 页" in backend.research_contexts[1]
    assert all("文档名及页码/片段" in prompt for prompt in backend.speaker_prompts)
    assert any("原文事实：第N页" in request for request in backend.speaker_requests)
    assert any("待验证的工程推断：" in request for request in backend.speaker_requests)
    assert len(cache) == 2


def test_discussion_context_keeps_original_anchor_when_long():
    entries = [f"主持人: 最初提出主题里的关键约束 {idx}" for idx in range(4)]
    entries.extend(f"中段角色{idx}: 中段展开观点 {idx}" for idx in range(20))
    entries.extend(f"最近角色{idx}: 最新推进观点 {idx}" for idx in range(12))

    context = build_discussion_context(topic="最初主题：群聊不要忘记原始探讨方向", entries=entries)

    assert "讨论主题锚点：最初主题：群聊不要忘记原始探讨方向" in context
    assert "最初讨论锚点" in context
    assert "主持人: 最初提出主题里的关键约束 0" in context
    assert "中间讨论已压缩：省略 20 条发言" in context
    assert "最近角色11: 最新推进观点 11" in context
    assert "中段展开观点 19" not in context


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
        async def podcast_start(
            self,
            *,
            session_key,
            topic,
            agents,
            rounds,
            host_voice_id="",
            host_voice_label="",
            host_model_ref="",
            host_model_label="",
        ):
            self.call = {
                "session_key": session_key,
                "topic": topic,
                "agents": agents,
                "rounds": rounds,
                "host_voice_id": host_voice_id,
                "host_voice_label": host_voice_label,
                "host_model_ref": host_model_ref,
                "host_model_label": host_model_label,
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
            "host_model_ref": "p/m2",
            "host_model_label": "m2",
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
        "host_model_ref": "p/m2",
        "host_model_label": "m2",
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


def test_podcast_add_agent_rpc_handler_delegates_to_backend():
    class Backend:
        async def podcast_add_agent(self, *, run_id, agent):
            self.call = {"run_id": run_id, "agent": agent}
            return {"ok": True, "agent_id": agent["id"], "generation": 2}

    backend = Backend()
    result = asyncio.run(
        podcast_add_agent(
            SimpleNamespace(backend=backend),
            {
                "run_id": "run-1",
                "agent": {
                    "id": "agent-3",
                    "role": "作家",
                    "voice_id": "zhishuo",
                    "model_ref": "p/m",
                },
            },
        )
    )

    assert result == {"ok": True, "agent_id": "agent-3", "generation": 2}
    assert backend.call == {
        "run_id": "run-1",
        "agent": {
            "id": "agent-3",
            "role": "作家",
            "voice_id": "zhishuo",
            "model_ref": "p/m",
        },
    }


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


def test_podcast_update_host_rpc_handler_delegates_to_backend():
    class Backend:
        async def podcast_update_host(
            self,
            *,
            run_id,
            host_voice_id="",
            host_voice_label="",
            model_ref="",
            model_label="",
        ):
            self.call = {
                "run_id": run_id,
                "host_voice_id": host_voice_id,
                "host_voice_label": host_voice_label,
                "model_ref": model_ref,
                "model_label": model_label,
            }
            return {"ok": True, "generation": 3}

    backend = Backend()
    result = asyncio.run(
        podcast_update_host(
            SimpleNamespace(backend=backend),
            {
                "run_id": "run-1",
                "model_ref": "p/m2",
                "model_label": "m2",
            },
        )
    )

    assert result == {"ok": True, "generation": 3}
    assert backend.call == {
        "run_id": "run-1",
        "host_voice_id": "",
        "host_voice_label": "",
        "model_ref": "p/m2",
        "model_label": "m2",
    }


def test_podcast_update_agent_voice_only_keeps_generation():
    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(
                config=SimpleNamespace(models=SimpleNamespace(providers={})),
                model_ref="p/m",
                model_id="m",
            )
            self.events = []
            self._podcast_runs = {
                "run-1": {
                    "session_id": "session-1",
                    "topic": "话题",
                    "host_voice_id": "xiaoxian",
                    "run_state": {"generation": 3},
                    "agents": assign_agents(
                        [
                            {
                                "id": "agent-1",
                                "role": "作家",
                                "voice_id": "zhishuo",
                                "model_ref": "p/m",
                            }
                        ],
                        "话题",
                        model_refs=["p/m"],
                        model_labels={"p/m": "m"},
                    ),
                }
            }

        def _emit_podcast(self, payload):
            self.events.append(payload)

    backend = Backend()
    result = asyncio.run(
        EmbeddedBackend.podcast_update_agent(
            backend,
            run_id="run-1",
            agent={
                "id": "agent-1",
                "role": "作家",
                "voice_id": "xiaogang",
                "model_ref": "p/m",
            },
        )
    )

    assert result["generation"] == 3
    assert result["content_changed"] is False
    assert result["voice_only"] is True
    assert backend._podcast_runs["run-1"]["run_state"]["generation"] == 3
    assert backend.events[-1]["content_changed"] is False
    assert backend.events[-1]["agent"]["voice_id"] == "xiaogang"


def test_podcast_update_host_model_keeps_generation():
    class Backend:
        def __init__(self):
            self.events = []
            self._podcast_runs = {
                "run-1": {
                    "session_id": "session-1",
                    "topic": "话题",
                    "host_voice_id": "xiaoxian",
                    "host_voice_label": "小仙",
                    "host_model_ref": "p/m",
                    "host_model_label": "m",
                    "run_state": {
                        "generation": 3,
                        "host_model_ref": "p/m",
                        "host_model_label": "m",
                    },
                    "agents": [],
                }
            }

        def _emit_podcast(self, payload):
            self.events.append(payload)

    backend = Backend()
    result = asyncio.run(
        EmbeddedBackend.podcast_update_host(
            backend,
            run_id="run-1",
            model_ref="p/m2",
            model_label="m2",
        )
    )

    assert result == {
        "ok": True,
        "generation": 3,
        "host": {
            "voice_id": "xiaoxian",
            "voice_label": "小仙",
            "model_ref": "p/m2",
            "model_label": "m2",
        },
    }
    assert backend._podcast_runs["run-1"]["run_state"]["generation"] == 3
    assert backend._podcast_runs["run-1"]["run_state"]["host_model_ref"] == "p/m2"
    assert backend.events[-1]["type"] == "podcast.host.updated"


def test_podcast_add_agent_appends_without_generation_reset():
    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(
                config=SimpleNamespace(models=SimpleNamespace(providers={})),
                model_ref="p/m",
                model_id="m",
            )
            self.events = []
            self._podcast_runs = {
                "run-1": {
                    "session_id": "session-1",
                    "topic": "话题",
                    "host_voice_id": "xiaoxian",
                    "run_state": {"generation": 3, "removed_agent_ids": set()},
                    "agents": assign_agents(
                        [{"id": "agent-1", "role": "作家", "voice_id": "zhishuo", "model_ref": "p/m"}],
                        "话题",
                        model_refs=["p/m"],
                        model_labels={"p/m": "m"},
                    ),
                }
            }

        def _emit_podcast(self, payload):
            self.events.append(payload)

    backend = Backend()
    agents = backend._podcast_runs["run-1"]["agents"]
    result = asyncio.run(
        EmbeddedBackend.podcast_add_agent(
            backend,
            run_id="run-1",
            agent={
                "id": "agent-2",
                "role": "云计算架构师",
                "voice_id": "xiaogang",
                "model_ref": "p/m",
            },
        )
    )

    assert result["generation"] == 3
    assert result["agent"]["id"] == "agent-2"
    assert result["agent"]["role"] == "云计算架构师"
    assert len(agents) == 2
    assert agents[-1].id == "agent-2"
    assert backend._podcast_runs["run-1"]["run_state"]["generation"] == 3
    assert backend.events[-1]["type"] == "podcast.agent.added"
    assert backend.events[-1]["generation"] == 3


def test_podcast_add_auto_agent_prefers_unused_active_role():
    existing_roles = [role for role in AGENT_ROLES if role not in {"自动", "相声演员"}]

    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(
                config=SimpleNamespace(models=SimpleNamespace(providers={})),
                model_ref="p/m",
                model_id="m",
            )
            self.events = []
            self._podcast_runs = {
                "run-1": {
                    "session_id": "session-1",
                    "topic": "讨论 AI Agent 和云计算",
                    "host_voice_id": "xiaoxian",
                    "run_state": {"generation": 3, "removed_agent_ids": set()},
                    "agents": assign_agents(
                        [
                            {"id": f"agent-{idx + 1}", "role": role, "model_ref": "p/m"}
                            for idx, role in enumerate(existing_roles)
                        ],
                        "讨论 AI Agent 和云计算",
                        model_refs=["p/m"],
                        model_labels={"p/m": "m"},
                    ),
                }
            }

        def _emit_podcast(self, payload):
            self.events.append(payload)

    backend = Backend()
    result = asyncio.run(
        EmbeddedBackend.podcast_add_agent(
            backend,
            run_id="run-1",
            agent={"id": "agent-9", "role": "自动", "model_ref": "p/m"},
        )
    )

    assert result["ok"] is True
    assert result["agent"]["requested_role"] == "自动"
    assert result["agent"]["role"] == "相声演员"
    assert backend._podcast_runs["run-1"]["agents"][-1].role == "相声演员"
    assert backend.events[-1]["agent"]["role"] == "相声演员"


def test_podcast_update_agent_content_change_increments_generation():
    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(
                config=SimpleNamespace(models=SimpleNamespace(providers={})),
                model_ref="p/m",
                model_id="m",
            )
            self.events = []
            self._podcast_runs = {
                "run-1": {
                    "session_id": "session-1",
                    "topic": "话题",
                    "host_voice_id": "xiaoxian",
                    "run_state": {"generation": 3},
                    "agents": assign_agents(
                        [{"id": "agent-1", "role": "作家", "voice_id": "zhishuo", "model_ref": "p/m"}],
                        "话题",
                        model_refs=["p/m"],
                        model_labels={"p/m": "m"},
                    ),
                }
            }

        def _emit_podcast(self, payload):
            self.events.append(payload)

    backend = Backend()
    result = asyncio.run(
        EmbeddedBackend.podcast_update_agent(
            backend,
            run_id="run-1",
            agent={
                "id": "agent-1",
                "role": "云计算架构师",
                "voice_id": "zhishuo",
                "model_ref": "p/m",
            },
        )
    )

    assert result["generation"] == 4
    assert result["content_changed"] is True
    assert result["voice_only"] is False
    assert backend._podcast_runs["run-1"]["run_state"]["generation"] == 4
    assert backend.events[-1]["content_changed"] is True


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


def test_podcast_stale_utterance_emits_skipped_after_start(monkeypatch):
    async def fake_generate_utterance(**kwargs):
        current["value"] = False
        return "过期内容"

    from nano_openclaw.features.voice import podcast as podcast_module

    monkeypatch.setattr(podcast_module, "generate_utterance", fake_generate_utterance)

    class Registry:
        def clone(self, **kwargs):
            return self

    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(registry=Registry())
            self.events = []

        def _emit_podcast(self, payload):
            self.events.append(payload)

        def _podcast_model_runtime(self, model_ref):
            return None, None, False

    current = {"value": True}
    backend = Backend()
    result = asyncio.run(
        EmbeddedBackend._generate_podcast_utterance(
            backend,
            run_id="run-1",
            session=SimpleNamespace(session_id="session-1"),
            round_index=1,
            phase="speaker",
            sequence=5,
            agent_id="agent-1",
            role="作家",
            voice_id="zhishuo",
            voice_label="知硕",
            system_prompt="system",
            user_text="user",
            token=SimpleNamespace(is_cancelled=False),
            use_research_tools=False,
            generation=2,
            is_generation_current=lambda generation: current["value"],
            persist=False,
            model_ref="p/m",
        )
    )

    assert result == ""
    assert [event["type"] for event in backend.events] == [
        "podcast.utterance.started",
        "podcast.utterance.skipped",
    ]
    skipped = backend.events[-1]
    assert skipped["sequence"] == 5
    assert skipped["agent_id"] == "agent-1"
    assert skipped["generation"] == 2


def test_podcast_run_syncs_generation_after_live_agent_update():
    class Guard:
        def reader(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(runtime_guard=Guard())
            self.events = []
            self.speaker_generations = []

        def _emit_podcast(self, payload):
            self.events.append(payload)

        async def _append_podcast_message(self, *args, **kwargs):
            return None

        async def _generate_podcast_utterance(self, **kwargs):
            return f"{kwargs['phase']} gen {kwargs['generation']}"

        async def _run_podcast_speaker_turn(self, **kwargs):
            generation = kwargs["generation"]
            self.speaker_generations.append(generation)
            if generation == 0:
                run_state["generation"] = 1
                return kwargs["agent"], "stale speaker"
            return kwargs["agent"], "fresh speaker"

        def _drain_podcast_inputs(self, queue):
            return EmbeddedBackend._drain_podcast_inputs(self, queue)

    backend = Backend()
    run_state = {"generation": 0, "removed_agent_ids": set()}
    agent = assign_agents([{"id": "agent-1", "role": "作家"}], "话题")[0]

    asyncio.run(
        EmbeddedBackend._run_podcast(
            backend,
            run_id="run-1",
            session=SimpleNamespace(session_id="session-1"),
            topic="话题",
            agents=[agent],
            rounds=1,
            host_voice_id="xiaoxian",
            host_voice_label="小仙",
            host_model_ref="p/m",
            host_model_label="m",
            token=SimpleNamespace(is_cancelled=False),
            input_queue=asyncio.Queue(),
            run_state=run_state,
        )
    )

    assert backend.speaker_generations == [0, 1]
    assert any(event.get("type") == "podcast.round.started" and event.get("generation") == 1 for event in backend.events)
    assert backend.events[-1]["type"] == "podcast.done"
    assert backend.events[-1]["generation"] == 1


def test_podcast_run_passes_context_with_opening_anchor_after_many_rounds():
    class Guard:
        def reader(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(runtime_guard=Guard())
            self.events = []
            self.speaker_contexts = []

        def _emit_podcast(self, payload):
            self.events.append(payload)

        async def _append_podcast_message(self, *args, **kwargs):
            return None

        async def _generate_podcast_utterance(self, **kwargs):
            return f"{kwargs['phase']} round {kwargs['round_index']}"

        async def _run_podcast_speaker_turn(self, **kwargs):
            self.speaker_contexts.append(kwargs["context"])
            return kwargs["agent"], f"speaker round {kwargs['round_index']}"

        def _drain_podcast_inputs(self, queue):
            return EmbeddedBackend._drain_podcast_inputs(self, queue)

    backend = Backend()
    agent = assign_agents([{"id": "agent-1", "role": "作家"}], "长期群聊主题")[0]

    asyncio.run(
        EmbeddedBackend._run_podcast(
            backend,
            run_id="run-1",
            session=SimpleNamespace(session_id="session-1"),
            topic="长期群聊主题",
            agents=[agent],
            rounds=10,
            host_voice_id="xiaoxian",
            host_voice_label="小仙",
            host_model_ref="p/m",
            host_model_label="m",
            token=SimpleNamespace(is_cancelled=False),
            input_queue=asyncio.Queue(),
            run_state={"generation": 0, "removed_agent_ids": set()},
        )
    )

    late_context = backend.speaker_contexts[-1]
    assert "讨论主题锚点：长期群聊主题" in late_context
    assert "最初讨论锚点" in late_context
    assert "主持人: opening round 0" in late_context
    assert "中间讨论已压缩" in late_context
    assert "主持人: summary round 9" in late_context
    assert backend.events[-1]["type"] == "podcast.done"


def test_podcast_run_persists_paper_uploaded_after_discussion_started():
    class Guard:
        def reader(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Backend:
        def __init__(self):
            self.runtime = SimpleNamespace(runtime_guard=Guard())
            self.events = []
            self.references = []

        def _emit_podcast(self, payload):
            self.events.append(payload)

        async def _append_podcast_message(self, *args, **kwargs):
            return None

        async def _generate_podcast_utterance(self, **kwargs):
            return f"{kwargs['phase']} round {kwargs['round_index']}"

        async def _run_podcast_speaker_turn(self, **kwargs):
            self.references.append(kwargs["reference_context"])
            return kwargs["agent"], f"speaker round {kwargs['round_index']}"

        def _drain_podcast_inputs(self, queue):
            return EmbeddedBackend._drain_podcast_inputs(self, queue)

    backend = Backend()
    agent = assign_agents([{"id": "agent-1", "role": "研究员"}], "论文研讨")[0]
    queue = asyncio.Queue()
    queue.put_nowait(
        "请分析局限\n\n[参考文档：paper.pdf]\n"
        "[第 1 页]\nArchitecture\n\n[第 44 页]\nConclusion and Limitations"
    )
    run_state = {"generation": 0, "removed_agent_ids": set(), "discussion_mode": "paper"}

    asyncio.run(
        EmbeddedBackend._run_podcast(
            backend,
            run_id="run-1",
            session=SimpleNamespace(session_id="session-1"),
            topic="论文研讨",
            agents=[agent],
            rounds=2,
            host_voice_id="xiaoxian",
            host_voice_label="小仙",
            host_model_ref="p/m",
            host_model_label="m",
            token=SimpleNamespace(is_cancelled=False),
            input_queue=queue,
            run_state=run_state,
        )
    )

    assert len(backend.references) == 2
    assert all("[第 44 页]\nConclusion and Limitations" in item for item in backend.references)
    assert "[第 44 页]\nConclusion and Limitations" in run_state["initial_context"]


def test_podcast_speaker_generation_cancels_when_agent_removed():
    class Backend:
        def __init__(self):
            self.events = []
            self.started = asyncio.Event()
            self.cancelled = False

        def _emit_podcast(self, payload):
            self.events.append(payload)

        async def _generate_podcast_utterance(self, **kwargs):
            self.started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def run_case():
        backend = Backend()
        active = {"value": True}
        agent = assign_agents([{"id": "agent-1", "role": "作家", "model_ref": "p/m"}], "话题")[0]
        task = asyncio.create_task(
            EmbeddedBackend._run_podcast_speaker_turn(
                backend,
                run_id="run-1",
                session=SimpleNamespace(session_id="session-1"),
                topic="话题",
                agent=agent,
                round_index=1,
                sequence=4,
                context="",
                research_cache={"agent-1:作家:p/m": "已有 research"},
                token=SimpleNamespace(is_cancelled=False),
                generation=0,
                is_generation_current=lambda generation: True,
                is_agent_active=lambda current: active["value"],
            )
        )
        await backend.started.wait()
        active["value"] = False
        result = await task
        return backend, result

    backend, result = asyncio.run(run_case())

    assert result[1] == ""
    assert backend.cancelled is True
    assert backend.events == [
        {
            "type": "podcast.utterance.skipped",
            "run_id": "run-1",
            "session_id": "session-1",
            "round": 1,
            "phase": "speaker",
            "sequence": 4,
            "agent_id": "agent-1",
            "role": "作家",
            "voice_id": result[0].voice_id,
            "voice_label": result[0].voice_label,
            "model_ref": "p/m",
            "generation": 0,
        }
    ]


def test_podcast_speaker_generation_is_drained_when_parent_is_cancelled():
    class Backend:
        def __init__(self):
            self.started = asyncio.Event()
            self.child_task = None

        async def _generate_podcast_utterance(self, **kwargs):
            self.child_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise TurnCancelled() from exc

    async def run_case():
        backend = Backend()
        agent = assign_agents([{"id": "agent-1", "role": "作家", "model_ref": "p/m"}], "话题")[0]
        parent_task = asyncio.create_task(
            EmbeddedBackend._run_podcast_speaker_turn(
                backend,
                run_id="run-1",
                session=SimpleNamespace(session_id="session-1"),
                topic="话题",
                agent=agent,
                round_index=1,
                sequence=4,
                context="",
                research_cache={"agent-1:作家:p/m": "已有 research"},
                token=SimpleNamespace(is_cancelled=False),
                generation=0,
                is_generation_current=lambda generation: True,
                is_agent_active=lambda current: True,
            )
        )
        await backend.started.wait()
        parent_task.cancel()
        parent_result = (await asyncio.gather(parent_task, return_exceptions=True))[0]
        child_was_drained = backend.child_task.done()
        if not child_was_drained:
            backend.child_task.cancel()
            await asyncio.gather(backend.child_task, return_exceptions=True)
        return parent_result, child_was_drained, backend.child_task.exception()

    parent_result, child_was_drained, child_error = asyncio.run(run_case())

    assert isinstance(parent_result, asyncio.CancelledError)
    assert child_was_drained is True
    assert isinstance(child_error, TurnCancelled)


def test_podcast_research_is_drained_when_parent_is_cancelled():
    class Backend:
        def __init__(self):
            self.started = asyncio.Event()
            self.child_task = None

        async def _run_podcast_research_subagent(self, **kwargs):
            self.child_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise TurnCancelled() from exc

    async def run_case():
        backend = Backend()
        agent = assign_agents([{"id": "agent-1", "role": "作家", "model_ref": "p/m"}], "话题")[0]
        parent_task = asyncio.create_task(
            EmbeddedBackend._run_podcast_speaker_turn(
                backend,
                run_id="run-1",
                session=SimpleNamespace(session_id="session-1"),
                topic="话题",
                agent=agent,
                round_index=1,
                sequence=4,
                context="",
                research_cache={},
                token=SimpleNamespace(is_cancelled=False),
                generation=0,
                is_generation_current=lambda generation: True,
                is_agent_active=lambda current: True,
            )
        )
        await backend.started.wait()
        parent_task.cancel()
        parent_result = (await asyncio.gather(parent_task, return_exceptions=True))[0]
        child_was_drained = backend.child_task.done()
        if not child_was_drained:
            backend.child_task.cancel()
            await asyncio.gather(backend.child_task, return_exceptions=True)
        return parent_result, child_was_drained, backend.child_task.exception()

    parent_result, child_was_drained, child_error = asyncio.run(run_case())

    assert isinstance(parent_result, asyncio.CancelledError)
    assert child_was_drained is True
    assert isinstance(child_error, TurnCancelled)
