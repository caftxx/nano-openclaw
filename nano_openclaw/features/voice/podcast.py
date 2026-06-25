"""AI podcast orchestration helpers for Web Voice.

The module owns the product rules for multi-agent voice discussions: role
catalog, prompt text, voice assignment, and small output normalization helpers.
Runtime/task ownership stays in ``services.backend_embedded``.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from nano_openclaw.features.voice.voice_catalog import ALIYUN_TTS_VOICES


HOST_ROLE = "主持人"
HOST_VOICE_ID = "xiaoxian"
HOST_VOICE_LABEL = "小仙·亲切女声"
DEFAULT_ROUNDS = 20
MAX_UTTERANCE_CHARS = 200

AGENT_ROLES = [
    "自动",
    "作家",
    "脱口秀工作者",
    "相声演员",
    "IT后台研发工程师",
    "IT前端研发工程师",
    "AI Agent研发工程师",
    "云计算架构师",
    "高性能网络协议设计师",
    "硬件工程师",
]

PREFERRED_SPEAKER_VOICES = [
    "zhishuo",
    "xiaogang",
    "sicheng",
    "aicheng",
    "aida",
    "aixiang",
    "aimo",
    "aiye",
    "aishuo",
    "stanley",
    "kenny",
    "zhixiang",
    "zhide",
    "zhifeng_emo",
    "zhibing_emo",
]

ROLE_PROMPTS = {
    "作家": "你像一位严谨的非虚构作家，擅长把复杂议题提炼成清晰、有画面感但克制的观点。",
    "脱口秀工作者": "你像一位理性脱口秀工作者，用轻松表达解释观点，但不能为了好笑牺牲事实准确性。",
    "相声演员": "你像一位有学识的相声演员，表达生动、有包袱感，但观点必须稳健、主流、可被验证。",
    "IT后台研发工程师": "你从后端系统、稳定性、数据一致性、服务治理和工程落地角度分析。",
    "IT前端研发工程师": "你从用户体验、交互性能、前端工程化、可访问性和端侧限制角度分析。",
    "AI Agent研发工程师": "你从 Agent 架构、工具调用、规划执行、记忆、评测和安全边界角度分析。",
    "云计算架构师": "你从云基础设施、弹性、成本、可观测性、网络与安全架构角度分析。",
    "高性能网络协议设计师": "你从高性能网络协议、RDMA、RoCE/IB、拥塞控制、低延迟、可靠传输和协议演进角度分析。",
    "硬件工程师": "你从电路、PCB、芯片选型、传感器、电源、信号完整性、热设计、量产可靠性和成本角度分析。",
}


@dataclass(frozen=True)
class PodcastAgent:
    id: str
    role: str
    requested_role: str
    voice_id: str
    voice_label: str


def voice_label(voice_id: str) -> str:
    for item in ALIYUN_TTS_VOICES:
        if item.get("value") == voice_id:
            return str(item.get("label") or voice_id)
    return voice_id


def normalize_rounds(value: Any) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS
    return min(100, max(1, rounds))


def resolve_role(requested: str, topic: str, index: int) -> str:
    requested = requested if requested in AGENT_ROLES else "自动"
    if requested != "自动":
        return requested
    text = topic.lower()
    candidates = [
        (("rdma", "roce", "infiniband", "数据中心", "低延迟", "网卡", "高性能网络", "网络协议"), "高性能网络协议设计师"),
        (("硬件", "pcb", "芯片", "电路", "传感器", "电源", "射频", "信号完整性", "热设计", "量产"), "硬件工程师"),
        (("agent", "智能体", "工具调用", "规划", "memory", "mcp"), "AI Agent研发工程师"),
        (("云", "kubernetes", "k8s", "容器", "弹性", "架构"), "云计算架构师"),
        (("前端", "ui", "ux", "浏览器", "交互", "页面"), "IT前端研发工程师"),
        (("后端", "数据库", "服务", "api", "高并发", "微服务"), "IT后台研发工程师"),
        (("写作", "小说", "叙事", "内容", "表达"), "作家"),
        (("喜剧", "脱口秀", "幽默"), "脱口秀工作者"),
        (("相声", "曲艺"), "相声演员"),
    ]
    for keywords, role in candidates:
        if any(k in text for k in keywords):
            return role
    fallback = [
        "AI Agent研发工程师",
        "云计算架构师",
        "IT后台研发工程师",
        "IT前端研发工程师",
        "硬件工程师",
        "作家",
    ]
    return fallback[index % len(fallback)]


def assign_agents(
    raw_agents: list[dict[str, Any]],
    topic: str,
    *,
    excluded_voice_id: str | None = HOST_VOICE_ID,
) -> list[PodcastAgent]:
    voice_ids = _speaker_voice_pool(excluded_voice_id=excluded_voice_id)
    agents: list[PodcastAgent] = []
    for idx, raw in enumerate(raw_agents or []):
        requested = str(raw.get("role") or "自动").strip() or "自动"
        role = resolve_role(requested, topic, idx)
        voice_id = voice_ids[idx % len(voice_ids)]
        agents.append(PodcastAgent(
            id=str(raw.get("id") or f"agent-{idx + 1}"),
            role=role,
            requested_role=requested if requested in AGENT_ROLES else "自动",
            voice_id=voice_id,
            voice_label=voice_label(voice_id),
        ))
    if not agents:
        return assign_agents([{"role": "自动"}, {"role": "自动"}], topic, excluded_voice_id=excluded_voice_id)
    return agents


def _speaker_voice_pool(*, excluded_voice_id: str | None = HOST_VOICE_ID) -> list[str]:
    catalog_values = [str(item.get("value")) for item in ALIYUN_TTS_VOICES if item.get("value")]
    excluded = str(excluded_voice_id or "")
    preferred = [v for v in PREFERRED_SPEAKER_VOICES if v in catalog_values and v != excluded]
    rest = [v for v in catalog_values if v != excluded and v not in preferred]
    return preferred + rest


def choose_speakers(agents: list[PodcastAgent], round_index: int, rng: random.Random) -> list[PodcastAgent]:
    if not agents:
        return []
    if len(agents) == 1:
        return [agents[0]]
    if round_index % 3 == 0:
        return list(agents)
    count = rng.randint(2, len(agents))
    return rng.sample(agents, count)


def build_start_summary(
    topic: str,
    agents: list[PodcastAgent],
    rounds: int,
    *,
    host_voice_id: str = HOST_VOICE_ID,
    host_voice_label: str = HOST_VOICE_LABEL,
) -> str:
    lines = [
        f"启动 AI 播客：{topic.strip() or '自由讨论'}",
        f"对话轮数：{rounds}",
        f"主持人：女主持人（{host_voice_label or host_voice_id or HOST_VOICE_LABEL} / {host_voice_id or HOST_VOICE_ID}）",
        "主讲 Agent：",
    ]
    for agent in agents:
        lines.append(f"- {agent.role}（{agent.voice_label} / {agent.voice_id}）")
    return "\n".join(lines)


def build_host_prompt(*, topic: str, round_index: int, speakers: list[PodcastAgent], user_input: str = "") -> str:
    speaker_names = "、".join(a.role for a in speakers) or "一位主讲人"
    input_clause = f"\n用户刚刚插话：{user_input.strip()}" if user_input.strip() else ""
    return f"""\
你是 AI 播客的女主持人，负责串讲、承接、引导，不做长篇分析。
当前主题：{topic}
当前轮次：{round_index}
本轮将由这些主讲人发言：{speaker_names}{input_clause}

要求：
- 只输出你要说的话，不要 Markdown、标题、列表、括号舞台说明。
- 如果有用户插话，先自然回应用户的问题或观点，再把话题交给主讲人。
- 语气亲切、简洁，最多 120 个中文字符。
"""


def build_speaker_prompt(*, topic: str, agent: PodcastAgent, round_index: int, context: str, research: str = "") -> str:
    role_prompt = ROLE_PROMPTS.get(agent.role, ROLE_PROMPTS["AI Agent研发工程师"])
    research_block = research.strip() or "暂无可用研究结果；请降低断言强度，只基于已知主流共识发言。"
    return f"""\
你正在参加一个多人 AI 播客。你的身份是：{agent.role}。
{role_prompt}

当前主题：{topic}
当前轮次：{round_index}
近期讨论上下文：
{context or "暂无。"}

你的专属 research 子 Agent 已返回以下研究摘要：
{research_block}

观点要求：
- 基于 research 摘要和你的身份提炼观点。
- 观点必须是主流、被广泛认可、可解释的；不要输出未经证实的小众判断。
- 如果资料不足，要明确降低断言强度。

输出要求：
- 只输出最终发言，不要展示研究过程、引用列表、Markdown、标题或项目符号。
- 口语化但信息密度高，不超过 200 个中文字符。
- 不要自称“作为某某”，直接发表观点。
"""


async def generate_utterance(
    *,
    runtime: Any,
    registry: Any,
    system_prompt: str,
    user_text: str,
    cancellation_token: CancellationToken,
    on_delta: Any | None = None,
) -> str:
    from dataclasses import replace

    from nano_openclaw.core.loop import AgentSession, Message

    temp_history: list[Message] = []

    def handle_event(event: Any) -> None:
        if on_delta is not None and type(event).__name__ == "TextDelta":
            on_delta(getattr(event, "text", ""))

    cfg = runtime.cfg

    agent_session = AgentSession(
        history=temp_history,
        registry=registry,
        on_event=handle_event,
        client=runtime.client,
        cfg=replace(
            cfg,
            system_prompt_override=system_prompt,
            response_style="",
            turn_source="webui",
            session_key=f"{cfg.session_key}:voice-podcast",
            hook_registry=None,
            active_memory_recall=None,
            max_tokens=min(cfg.max_tokens, 900),
        ),
        transcript_writer=None,
        cancellation_token=cancellation_token,
    )
    await agent_session.run_turn(user_text)
    for message in reversed(temp_history):
        if message.role == "assistant":
            return normalize_utterance(_message_text(message), limit=MAX_UTTERANCE_CHARS)
    return ""


def _message_text(message: Any) -> str:
    parts = []
    for block in getattr(message, "content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(part for part in parts if part)


def normalize_utterance(text: str, *, limit: int = MAX_UTTERANCE_CHARS) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^【[^】]+】\s*", "", value)
    value = value.strip("`#*- \n\t")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip("，。；、,.!！？? ") + "。"
