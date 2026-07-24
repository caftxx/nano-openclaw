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

from nano_openclaw.features.voice.voice_catalog import ALIYUN_TTS_VOICES, voice_score


HOST_ROLE = "主持人"
HOST_VOICE_ID = "xiaoxian"
HOST_VOICE_LABEL = "小仙·亲切女声"
DEFAULT_ROUNDS = 20
MAX_UTTERANCE_CHARS = 200
PODCAST_CONTEXT_ANCHOR_ENTRIES = 4
PODCAST_CONTEXT_RECENT_ENTRIES = 12
PODCAST_CONTEXT_MAX_CHARS = 3600
PODCAST_CONTEXT_LINE_MAX_CHARS = 280
PODCAST_REFERENCE_CHUNK_CHARS = 1200
PODCAST_REFERENCE_CHUNK_OVERLAP = 160
PODCAST_REFERENCE_CHUNKS_PER_ROUND = 3
PODCAST_REFERENCE_MAX_CHARS = 4200

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

ROLE_PROMPTS = {
    "作家": "你像一位会讲故事的非虚构作家，擅长把抽象问题变成一个清楚、有画面的小场景。",
    "脱口秀工作者": "你像一位理性的脱口秀工作者，表达轻松、有一点俏皮，但不能为了好笑牺牲事实准确性。",
    "相声演员": "你像一位有学识的相声演员，说话有包袱感，能用接地气的类比把观点讲明白。",
    "IT后台研发工程师": "你从系统能不能扛住、出了问题好不好查、团队能不能长期维护的角度说人话。",
    "IT前端研发工程师": "你从用户点起来顺不顺、页面卡不卡、交互会不会让人迷糊的角度说人话。",
    "AI Agent研发工程师": "你从智能体怎么分工、什么时候会犯糊涂、怎么把活交代清楚的角度说人话。",
    "云计算架构师": "你从资源够不够用、成本会不会失控、系统坏了能不能快速恢复的角度说人话。",
    "高性能网络协议设计师": "你从数据传得快不快、会不会堵车、延迟像不像排队等红灯的角度说人话。",
    "硬件工程师": "你从板子稳不稳、发不发热、量产会不会翻车、成本能不能压住的角度说人话。",
}


@dataclass(frozen=True)
class PodcastAgent:
    id: str
    role: str
    requested_role: str
    voice_id: str
    voice_label: str
    model_ref: str = ""
    model_label: str = ""


def voice_label(
    voice_id: str,
    voice_options: list[dict[str, Any]] | None = None,
) -> str:
    for item in _normalized_voice_options(voice_options):
        if item["value"] == voice_id:
            return item["label"]
    return voice_id


def resolve_voice_choice(
    voice_id: str,
    voice_label_value: str = "",
    *,
    voice_options: list[dict[str, Any]] | None = None,
    fallback_voice_id: str = HOST_VOICE_ID,
) -> tuple[str, str]:
    """Resolve a requested voice against the active TTS provider catalog."""

    requested_id = str(voice_id or "").strip()
    requested_label = str(voice_label_value or "").strip()
    fallback_id = str(fallback_voice_id or "").strip()
    if voice_options is None:
        resolved_id = requested_id or fallback_id
        return resolved_id, requested_label or voice_label(resolved_id)

    options = _normalized_voice_options(voice_options)
    labels = {item["value"]: item["label"] for item in options}
    if requested_id in labels:
        return requested_id, requested_label or labels[requested_id]
    if fallback_id in labels:
        return fallback_id, labels[fallback_id]
    if options:
        return options[0]["value"], options[0]["label"]
    resolved_id = requested_id or fallback_id
    return resolved_id, requested_label or resolved_id


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
    candidates = _auto_role_candidates(topic)
    return candidates[index % len(candidates)]


def _auto_role_candidates(topic: str) -> list[str]:
    text = topic.lower()
    keyword_roles = [
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
    matched = [
        role
        for keywords, role in keyword_roles
        if any(k in text for k in keywords)
    ]
    fallback = [
        "AI Agent研发工程师",
        "云计算架构师",
        "IT后台研发工程师",
        "IT前端研发工程师",
        "高性能网络协议设计师",
        "硬件工程师",
        "作家",
        "脱口秀工作者",
        "相声演员",
    ]
    return list(dict.fromkeys(matched + fallback))


def assign_agents(
    raw_agents: list[dict[str, Any]],
    topic: str,
    *,
    excluded_voice_id: str | None = HOST_VOICE_ID,
    model_refs: list[str] | None = None,
    model_labels: dict[str, str] | None = None,
    existing_roles: list[str] | None = None,
    voice_options: list[dict[str, Any]] | None = None,
    rng: random.Random | None = None,
) -> list[PodcastAgent]:
    raw_agent_list = list(raw_agents or [{"role": "自动"}, {"role": "自动"}])
    voice_ids = _balanced_speaker_voice_ids(
        len(raw_agent_list),
        excluded_voice_id=excluded_voice_id,
        voice_options=voice_options,
        rng=rng,
    )
    valid_voice_ids = (
        {item["value"] for item in _normalized_voice_options(voice_options)}
        if voice_options is not None
        else None
    )
    assigned_model_refs = _assigned_model_refs(len(raw_agent_list), model_refs or [], rng=rng)
    model_labels = model_labels or {}
    local_rng = rng or random
    agents: list[PodcastAgent] = []
    auto_roles = _auto_role_candidates(topic)
    used_roles: set[str] = {
        role for role in (existing_roles or [])
        if role in AGENT_ROLES and role != "自动"
    }
    auto_index = 0
    for idx, raw in enumerate(raw_agent_list):
        requested = str(raw.get("role") or "自动").strip() or "自动"
        normalized_requested = requested if requested in AGENT_ROLES else "自动"
        if normalized_requested == "自动":
            if existing_roles is None:
                role = _next_distinct_auto_role(auto_roles, used_roles, auto_index)
            else:
                role = _random_unused_auto_role(auto_roles, used_roles, auto_index, local_rng)
            auto_index += 1
        else:
            role = resolve_role(requested, topic, idx)
        used_roles.add(role)
        requested_voice_id = str(raw.get("voice_id") or raw.get("voiceId") or "").strip()
        if valid_voice_ids is not None and requested_voice_id not in valid_voice_ids:
            requested_voice_id = ""
        automatic_voice_id = voice_ids[idx % len(voice_ids)] if voice_ids else ""
        voice_id = requested_voice_id or automatic_voice_id
        requested_voice_label = str(raw.get("voice_label") or raw.get("voiceLabel") or "").strip()
        requested_model_ref = str(raw.get("model_ref") or raw.get("modelRef") or "").strip()
        model_ref = requested_model_ref or (assigned_model_refs[idx] if idx < len(assigned_model_refs) else "")
        requested_model_label = str(raw.get("model_label") or raw.get("modelLabel") or "").strip()
        agents.append(PodcastAgent(
            id=str(raw.get("id") or f"agent-{idx + 1}"),
            role=role,
            requested_role=requested if requested in AGENT_ROLES else "自动",
            voice_id=voice_id,
            voice_label=(
                requested_voice_label
                if requested_voice_id and requested_voice_label
                else voice_label(voice_id, voice_options)
            ),
            model_ref=model_ref,
            model_label=requested_model_label or model_labels.get(model_ref, model_ref),
        ))
    return agents


def _next_distinct_auto_role(candidates: list[str], used_roles: set[str], index: int) -> str:
    for role in candidates:
        if role not in used_roles:
            return role
    return candidates[index % len(candidates)]


def _random_unused_auto_role(
    candidates: list[str],
    used_roles: set[str],
    index: int,
    rng: random.Random,
) -> str:
    unused = [role for role in candidates if role not in used_roles]
    if unused:
        return rng.choice(unused)
    return candidates[index % len(candidates)]


def podcast_model_options(config: Any) -> tuple[list[str], dict[str, str]]:
    refs: list[str] = []
    labels: dict[str, str] = {}
    providers = getattr(getattr(config, "models", None), "providers", None) or {}
    for provider_id, provider in providers.items():
        for model in getattr(provider, "models", []) or []:
            model_id = str(getattr(model, "id", "") or "").strip()
            if not model_id:
                continue
            model_input = list(getattr(model, "input", None) or ["text"])
            if "text" not in model_input:
                continue
            ref = f"{provider_id}/{model_id}"
            refs.append(ref)
            labels[ref] = str(getattr(model, "name", None) or model_id)
    return refs, labels


def _assigned_model_refs(
    count: int,
    model_refs: list[str],
    *,
    rng: random.Random | None = None,
) -> list[str]:
    if count <= 0 or not model_refs:
        return []
    local_rng = rng or random
    pool = list(dict.fromkeys(ref for ref in model_refs if ref))
    local_rng.shuffle(pool)
    assigned: list[str] = []
    while len(assigned) < count:
        assigned.extend(pool)
    return assigned[:count]


def _balanced_speaker_voice_ids(
    count: int,
    *,
    excluded_voice_id: str | None = HOST_VOICE_ID,
    voice_options: list[dict[str, Any]] | None = None,
    rng: random.Random | None = None,
) -> list[str]:
    local_rng = rng or random
    pool = _speaker_voice_pool(
        excluded_voice_id=excluded_voice_id,
        voice_options=voice_options,
    )
    male = _ranked_voice_ids(
        [voice_id for voice_id in pool if _voice_gender(voice_id, voice_options) == "male"],
        rng=local_rng,
    )
    female = _ranked_voice_ids(
        [voice_id for voice_id in pool if _voice_gender(voice_id, voice_options) == "female"],
        rng=local_rng,
    )
    neutral = _ranked_voice_ids(
        [voice_id for voice_id in pool if _voice_gender(voice_id, voice_options) == "neutral"],
        rng=local_rng,
    )

    male_target = count // 2
    female_target = count // 2
    if count % 2:
        if local_rng.choice([True, False]):
            male_target += 1
        else:
            female_target += 1

    selected = male[:male_target] + female[:female_target]
    if len(selected) < count:
        remaining = [
            voice_id for voice_id in neutral + male[male_target:] + female[female_target:]
            if voice_id not in selected
        ]
        selected.extend(remaining[:count - len(selected)])
    local_rng.shuffle(selected)

    tail = [voice_id for voice_id in pool if voice_id not in selected]
    local_rng.shuffle(tail)
    return selected + tail


def _voice_gender(
    voice_id: str,
    voice_options: list[dict[str, Any]] | None = None,
) -> str:
    label = voice_label(voice_id, voice_options)
    if "男" in label or "老铁" in label or "大虎" in label:
        return "male"
    if "女" in label or "姐姐" in label or "老妹" in label or "柜姐" in label or "萝莉" in label:
        return "female"
    return "neutral"


def _speaker_voice_pool(
    *,
    excluded_voice_id: str | None = HOST_VOICE_ID,
    voice_options: list[dict[str, Any]] | None = None,
) -> list[str]:
    catalog_values = [item["value"] for item in _normalized_voice_options(voice_options)]
    excluded = str(excluded_voice_id or "")
    available = [v for v in catalog_values if v != excluded]
    # A provider may expose only one voice. Sharing the host voice is preferable
    # to leaking an identifier from a different provider into the TTS request.
    return available or catalog_values


def _normalized_voice_options(
    voice_options: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    source = ALIYUN_TTS_VOICES if voice_options is None else voice_options
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or item.get("id") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        options.append({
            "value": value,
            "label": str(item.get("label") or item.get("name") or value).strip() or value,
        })
    return options


def _ranked_voice_ids(voice_ids: list[str], *, rng: random.Random) -> list[str]:
    by_score: dict[int, list[str]] = {}
    for voice_id in voice_ids:
        by_score.setdefault(voice_score(voice_id), []).append(voice_id)
    ranked: list[str] = []
    for score in sorted(by_score.keys(), reverse=True):
        group = list(by_score[score])
        rng.shuffle(group)
        ranked.extend(group)
    return ranked


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
        model = f"，模型：{agent.model_label or agent.model_ref}" if agent.model_ref else ""
        lines.append(f"- {agent.role}（{agent.voice_label} / {agent.voice_id}{model}）")
    return "\n".join(lines)


def build_discussion_context(
    *,
    topic: str,
    entries: list[str],
    anchor_entries: int = PODCAST_CONTEXT_ANCHOR_ENTRIES,
    recent_entries: int = PODCAST_CONTEXT_RECENT_ENTRIES,
    max_chars: int = PODCAST_CONTEXT_MAX_CHARS,
) -> str:
    """Build a bounded podcast context without dropping the original anchor.

    Speaker prompts need enough recent detail to avoid repetition, but long
    group chats also need a stable reminder of what the conversation started
    from. Keep the opening entries and the latest entries, and make any middle
    truncation explicit.
    """
    cleaned = [_compact_context_line(item) for item in entries if str(item or "").strip()]
    lines = [f"讨论主题锚点：{topic.strip() or '自由讨论'}"]
    if not cleaned:
        return "\n".join(lines)

    anchor_count = max(0, min(anchor_entries, len(cleaned)))
    recent_count = max(0, min(recent_entries, len(cleaned) - anchor_count))
    anchors = cleaned[:anchor_count]
    recents = cleaned[len(cleaned) - recent_count:] if recent_count else []
    omitted = cleaned[anchor_count:len(cleaned) - recent_count if recent_count else len(cleaned)]

    if anchors:
        lines.append("最初讨论锚点：")
        lines.extend(f"- {line}" for line in anchors)
    if omitted:
        speaker_names = _context_speaker_names(omitted)
        suffix = f"；涉及：{'、'.join(speaker_names)}" if speaker_names else ""
        lines.append(f"中间讨论已压缩：省略 {len(omitted)} 条发言{suffix}。")
    if recents:
        lines.append("最近讨论：")
        lines.extend(f"- {line}" for line in recents)
    return _fit_context_chars(lines, max_chars)


def _compact_context_line(value: str) -> str:
    line = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(line) <= PODCAST_CONTEXT_LINE_MAX_CHARS:
        return line
    return line[:PODCAST_CONTEXT_LINE_MAX_CHARS - 1].rstrip() + "…"


def _context_speaker_names(lines: list[str]) -> list[str]:
    names: list[str] = []
    for line in lines:
        name = line.split(":", 1)[0].strip()
        if name and len(name) <= 20 and name not in names:
            names.append(name)
        if len(names) >= 6:
            break
    return names


def _fit_context_chars(lines: list[str], max_chars: int) -> str:
    text = "\n".join(lines)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    kept: list[str] = []
    remaining = max_chars
    for line in lines:
        if remaining <= 1:
            break
        if len(line) + 1 <= remaining:
            kept.append(line)
            remaining -= len(line) + 1
            continue
        if remaining > 20:
            kept.append(line[:remaining - 1].rstrip() + "…")
        break
    return "\n".join(kept)


def reference_document_names(value: str) -> list[str]:
    return list(dict.fromkeys(
        name.strip()
        for name in re.findall(r"\[参考文档：([^\]]+)\]", str(value or ""))
        if name.strip()
    ))


def has_document_reference(value: str) -> bool:
    return bool(reference_document_names(value))


def discussion_mode_for_attachments(topic: str, attachments: list[dict[str, Any]] | None) -> str:
    topic_text = str(topic or "").lower()
    paper_topic = any(keyword in topic_text for keyword in ("论文", "paper", "研究报告", "学术"))
    for item in attachments or []:
        name = str(item.get("name") or "").lower()
        mime = str(item.get("mime") or "").lower()
        if mime == "application/pdf" or name.endswith(".pdf"):
            return "paper"
        if paper_topic and name.endswith((".docx", ".md", ".txt")):
            return "paper"
    return "group"


def paper_round_focus(topic: str, round_index: int, total_rounds: int | None = None) -> str:
    ordinal_map = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    focuses: dict[int, str] = {}
    matches = list(re.finditer(r"第([一二三四五六七八九十]|\d+)轮\s*", str(topic or "")))
    for index, match in enumerate(matches):
        raw = match.group(1)
        number = int(raw) if raw.isdigit() else ordinal_map.get(raw, 0)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(str(topic or ""))
        value = str(topic or "")[match.end():end].strip(" ：:；;，,。")
        if number and value:
            focuses[number] = value
    target = max(1, int(round_index or 1))
    if target in focuses:
        return focuses[target]
    total = max(1, int(total_rounds or target or 1))
    if target == 1:
        return "研究问题、核心主张、论文贡献与总体结构"
    if target >= total:
        return "评测证据、局限、威胁有效性的因素与仍未回答的问题"
    if target == 2:
        return "核心方法、模型架构、关键公式与设计取舍"
    if target == 3:
        return "实验设置、训练与推理基础设施、数据和评价方法"
    return "实验结果、消融分析、与基线比较及结果解释"


def build_paper_reference_query(
    *,
    topic: str,
    context: str,
    round_index: int,
    total_rounds: int | None = None,
) -> str:
    focus = paper_round_focus(topic, round_index, total_rounds)
    # The bounded discussion context deliberately repeats the complete topic
    # anchor.  In paper mode that topic often contains every round's agenda;
    # feeding it back into retrieval makes round 1 select evidence for rounds
    # 2-4 as well.  Only explicit user interjections may refine the current
    # round's evidence query.  Agent/host summaries remain discussion context,
    # but cannot steer document retrieval away from the active agenda.
    followups: list[str] = []
    for line in str(context or "").splitlines():
        cleaned = line.strip().lstrip("- ").strip()
        if re.match(r"用户(?:插话)?\s*[:：]", cleaned):
            followups.append(cleaned)
    recent = "\n".join(followups[-3:])[-600:]
    if recent:
        return f"本轮议程：{focus}\n用户本轮追问：{recent}"
    return f"本轮议程：{focus}"


def select_reference_context(
    reference_context: str,
    *,
    query: str,
    round_index: int,
    max_chunks: int = PODCAST_REFERENCE_CHUNKS_PER_ROUND,
    max_chars: int = PODCAST_REFERENCE_MAX_CHARS,
) -> str:
    """Select relevant and progressively covered excerpts from attached documents."""
    source = str(reference_context or "").strip()
    if not source or not has_document_reference(source):
        return source

    blocks = list(re.finditer(
        r"\[参考文档：([^\]]+)\]\n(.*?)(?=\n\n\[参考(?:文档|图片)：|\Z)",
        source,
        flags=re.DOTALL,
    ))
    chunks: list[str] = []
    for block in blocks:
        name = block.group(1).strip()
        body = block.group(2).strip()
        page_parts = list(re.finditer(
            r"\[第\s+(\d+)\s+页\]\n(.*?)(?=\n\n\[第\s+\d+\s+页\]|\Z)",
            body,
            flags=re.DOTALL,
        ))
        if page_parts:
            for page in page_parts:
                page_number = page.group(1)
                page_text = page.group(2).strip()
                chunks.extend(_reference_text_chunks(
                    page_text,
                    label=f"参考文档：{name}｜第 {page_number} 页",
                ))
        else:
            chunks.extend(_reference_text_chunks(body, label=f"参考文档：{name}"))

    if not chunks:
        return source[:max_chars]

    query_tokens = _reference_tokens(query)
    explicit_page_hints = sorted(_reference_page_hints(query))
    page_hints = explicit_page_hints or _toc_reference_page_hints(chunks, query, limit=max_chunks)
    available_pages = {
        page for chunk in chunks for page in [_reference_chunk_page(chunk)] if page is not None
    }
    resolved_page_hints = [page for page in page_hints if page in available_pages]
    usable_indices = [
        index for index, chunk in enumerate(chunks)
        if not _is_table_of_contents_chunk(chunk)
    ] or list(range(len(chunks)))
    scored = [
        (len(query_tokens.intersection(_reference_tokens(chunk))), index)
        for index, chunk in enumerate(chunks)
        if index in usable_indices
    ]
    best_index = max(scored, key=lambda item: (item[0], -item[1]))[1]
    wanted = max(1, min(max_chunks, len(chunks)))
    selected_indices: list[int] = []

    def is_within_active_sections(index: int) -> bool:
        if not resolved_page_hints:
            return True
        page = _reference_chunk_page(chunks[index])
        if page is None:
            return False
        # Page 1 commonly contains the abstract/summary metrics.  Otherwise,
        # relevance fill must stay close to a TOC-located active section so a
        # repeated keyword in the conclusion cannot leak page 44 into round 1.
        return page == 1 or any(abs(page - hint) <= 2 for hint in resolved_page_hints)

    for page in page_hints:
        page_candidates = [
            index for index, chunk in enumerate(chunks)
            if _reference_chunk_page(chunk) == page
        ]
        if not page_candidates:
            continue
        selected_indices.append(max(
            page_candidates,
            key=lambda index: len(query_tokens.intersection(_reference_tokens(chunks[index]))),
        ))
        if len(selected_indices) >= wanted:
            break
    if (
        best_index not in selected_indices
        and len(selected_indices) < wanted
        and is_within_active_sections(best_index)
    ):
        selected_indices.append(best_index)
    if len(selected_indices) < wanted:
        for score, candidate in sorted(scored, key=lambda item: (-item[0], item[1])):
            if score <= 0:
                break
            if not is_within_active_sections(candidate):
                continue
            if candidate not in selected_indices:
                selected_indices.append(candidate)
            if len(selected_indices) >= wanted:
                break
    coverage_start = max(0, round_index - 1) * max(1, wanted - 1)
    if len(selected_indices) < wanted:
        for offset in range(len(usable_indices)):
            candidate = usable_indices[(coverage_start + offset) % len(usable_indices)]
            if candidate not in selected_indices:
                selected_indices.append(candidate)
            if len(selected_indices) >= wanted:
                break

    spans = [(match.start(), match.end()) for match in blocks]
    remainder_parts: list[str] = []
    cursor = 0
    for start, end in spans:
        remainder_parts.append(source[cursor:start])
        cursor = end
    remainder_parts.append(source[cursor:])
    remainder = "\n\n".join(part.strip() for part in remainder_parts if part.strip())

    parts = [
        f"[本轮论文依据：从 {len(chunks)} 个片段中选取；第 {max(1, round_index)} 轮]",
    ]
    if remainder:
        parts.append(remainder[:800])
    parts.extend(chunks[index] for index in selected_indices)
    fitted: list[str] = []
    used = 0
    for part in parts:
        remaining = max_chars - used
        if remaining <= 0:
            break
        value = part if len(part) <= remaining else part[:remaining].rstrip() + "…"
        fitted.append(value)
        used += len(value) + 2
    return "\n\n".join(fitted)


def _reference_text_chunks(text: str, *, label: str) -> list[str]:
    compact = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not compact:
        return []
    step = max(1, PODCAST_REFERENCE_CHUNK_CHARS - PODCAST_REFERENCE_CHUNK_OVERLAP)
    values: list[str] = []
    start = 0
    fragment = 1
    while start < len(compact):
        body = compact[start:start + PODCAST_REFERENCE_CHUNK_CHARS].strip()
        if not body:
            break
        suffix = f"｜片段 {fragment}" if len(compact) > PODCAST_REFERENCE_CHUNK_CHARS else ""
        values.append(f"[{label}{suffix}]\n{body}")
        if start + PODCAST_REFERENCE_CHUNK_CHARS >= len(compact):
            break
        start += step
        fragment += 1
    return values


def _reference_tokens(value: str) -> set[str]:
    text = str(value or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]{3,}", text))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    aliases = {
        "架构": {"architecture", "architectural"},
        "注意力": {"attention"},
        "压缩": {"compressed", "compression"},
        "训练": {"training", "optimizer"},
        "推理": {"inference", "serving"},
        "基础设施": {"infrastructure", "framework", "kernel", "cache"},
        "评测": {"evaluation", "benchmark"},
        "实验": {"experiment", "evaluation"},
        "结果": {"results", "performance"},
        "局限": {"limitations", "limitation", "future", "conclusion"},
        "方法": {"method", "architecture", "design"},
        "数据": {"data", "dataset"},
        "稳定": {"stability", "instability"},
        "缓存": {"cache", "caching"},
    }
    for keyword, expanded in aliases.items():
        if keyword in text:
            tokens.update(expanded)
    if "csa" in text:
        tokens.update({"compressed", "sparse", "attention"})
    if "hca" in text:
        tokens.update({"heavily", "compressed", "attention"})
    return tokens


def _reference_page_hints(value: str) -> set[int]:
    text = str(value or "")
    pages = {int(item) for item in re.findall(r"第\s*(\d+)\s*页", text)}
    for start, end in re.findall(r"第\s*(\d+)\s*(?:到|至|[-–—])\s*(\d+)\s*页", text):
        low, high = sorted((int(start), int(end)))
        pages.update(range(low, min(high, low + 8) + 1))
    return pages


def _reference_chunk_page(value: str) -> int | None:
    match = re.search(r"\[参考文档：[^\]]+｜第\s*(\d+)\s*页", str(value or ""))
    return int(match.group(1)) if match else None


def _is_table_of_contents_chunk(value: str) -> bool:
    text = str(value or "")
    return "\nContents\n" in text or text.count(". . .") >= 3


def _toc_reference_page_hints(chunks: list[str], query: str, *, limit: int) -> list[int]:
    query_tokens = _reference_tokens(query)
    query_acronyms = set(re.findall(r"\b(?:csa|hca)\b", str(query or "").lower()))
    candidates: list[tuple[int, int]] = []
    toc_source_pages = {
        page
        for chunk in chunks
        if _is_table_of_contents_chunk(chunk)
        for page in [_reference_chunk_page(chunk)]
        if page is not None
    }
    for chunk in chunks:
        # A long TOC page may be split so that its final chunk contains only a
        # top-level entry and no longer crosses the dot-leader threshold.
        # Once one chunk identifies the source page as a TOC, scan every chunk
        # from that same page.
        if (
            not _is_table_of_contents_chunk(chunk)
            and _reference_chunk_page(chunk) not in toc_source_pages
        ):
            continue
        for line in chunk.splitlines():
            # Some PDF extractors preserve dot leaders while others collapse a
            # top-level TOC entry to ordinary spaces (for example,
            # ``6 Conclusion, Limitations, and Future Directions 44``).  This
            # parser only runs inside chunks already classified as a table of
            # contents, so accepting a final page number is both safe and
            # necessary for those top-level entries.
            match = re.match(
                r"^(.+?)(?:(?:\s*\.\s*){2,}|\s+)(\d+)\s*$",
                line.strip(),
            )
            if not match:
                continue
            title, page = match.group(1).strip(), int(match.group(2))
            title_tokens = _reference_tokens(title)
            title_lower = title.lower()
            title_acronyms = set(re.findall(r"\b(?:csa|hca)\b", title_lower))
            if "compressed sparse attention" in title_lower:
                title_acronyms.add("csa")
            if "heavily compressed attention" in title_lower:
                title_acronyms.add("hca")
            if query_acronyms and not query_acronyms.intersection(title_acronyms):
                continue
            score = len(query_tokens.intersection(title_tokens))
            if score:
                candidates.append((score, page))
    ordered: list[int] = []
    for _score, page in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if page not in ordered:
            ordered.append(page)
        if len(ordered) >= max(1, limit):
            break
    return ordered


def validate_paper_utterance(text: str, reference_context: str) -> tuple[bool, str]:
    utterance = str(text or "").strip()
    reference = str(reference_context or "")
    allowed_pages = {
        int(page) for page in re.findall(r"\[参考文档：[^\]]+｜第\s*(\d+)\s*页", reference)
    }
    cited_pages = {int(page) for page in re.findall(r"第\s*(\d+)\s*页", utterance)}
    if allowed_pages and not cited_pages:
        return False, "没有引用本轮提供的论文页码"
    invalid_pages = cited_pages.difference(allowed_pages)
    if invalid_pages:
        return False, f"引用了本轮未提供的页码：{sorted(invalid_pages)}"
    if re.search(
        r"(?:查无出处|论文没[\u4e00-\u9fff]{1,8}|原文没[\u4e00-\u9fff]{1,8}|"
        r"论文(?:没有|没给|没写|没解释|没说明|没拆开|没做|没测|没验证|"
        r"未写|未提供|未解释|未说明|未验证|未报告|未披露)|"
        r"原文(?:没有|没给|没写|没解释|没说明|没拆开|没做|没测|没验证|"
        r"未写|未提供|未解释|未说明|未验证|未报告|未披露))",
        utterance,
    ):
        return False, "不能根据局部检索片段断言整篇论文没有相关内容"
    reference_numbers = set(re.findall(r"\d+(?:\.\d+)?\s*(?:%|×|x|T|B|K|GB|TB)", reference, flags=re.I))
    utterance_numbers = set(re.findall(r"\d+(?:\.\d+)?\s*(?:%|×|x|T|B|K|GB|TB)", utterance, flags=re.I))
    unsupported = utterance_numbers.difference(reference_numbers)
    if unsupported:
        return False, f"包含本轮依据中不存在的量化值：{sorted(unsupported)}"
    approximate_quantities = set(re.findall(
        r"(?:几百|数百|上千|数千|数万)\s*(?:GB|TB|MB|倍|卡|台)",
        utterance,
        flags=re.I,
    ))
    unsupported_approximate = {
        value for value in approximate_quantities if value.lower() not in reference.lower()
    }
    if unsupported_approximate:
        return False, f"包含原文没有的估算量级：{sorted(unsupported_approximate)}"
    inference_markers = re.compile(
        r"推断|假设|待验证|尚不能|不能据此|并非论文结论|不是论文结论"
    )
    engineering_terms = re.compile(
        r"单机|多机|多卡|部署拓扑|预分配|显存|成本|计费|延迟|调度器|静默|生产|"
        r"多\s*Agent|Agent\s*系统|checkpoint\s*恢复|CPU\s*开销|buffer",
        flags=re.I,
    )
    strong_inference = re.compile(
        r"意味着|等于|说明|必然|必须|根本|就是|直接|轻松|完全|温床|雪崩|定时炸弹|硬约束"
    )
    weak_inference = re.compile(r"可能|风险|担心|隐患")
    analogy_inference = re.compile(
        r"相当于|说白了|就像|好比|等同于|等于(?:一个|连|把|给|让|像)|"
        r"(?<!不)像(?:给|把|一个|一套|是)"
    )
    for sentence in re.split(r"[。！？\n]+", utterance):
        if analogy_inference.search(sentence) and not inference_markers.search(sentence):
            return False, "角色类比没有明确标注为非论文结论"
        if (
            engineering_terms.search(sentence)
            and (strong_inference.search(sentence) or weak_inference.search(sentence))
            and not inference_markers.search(sentence)
        ):
            return False, "工程外推没有明确标注为待验证推断"
    return True, ""


def normalize_paper_scope_claims(text: str) -> str:
    """Downgrade whole-paper absence claims to the supplied evidence window."""
    value = str(text or "")
    subject = r"(?:论文|原文)(?:本身)?"
    value = re.sub(
        subject + r"(?:没有|没给(?:出)?|未提供|未披露|未报告)",
        "本轮页面未给出",
        value,
    )
    value = re.sub(
        subject + r"(?:没解释|没说明|没交代|未解释|未说明)",
        "本轮页面未解释",
        value,
    )
    value = re.sub(
        subject + r"(?:没拆开测|没做|没测|没验证|未验证)",
        "本轮页面未显示相关验证",
        value,
    )
    return value


def build_paper_fallback_utterance(
    *,
    topic: str,
    role: str,
    round_index: int,
    reference_context: str,
) -> str:
    """Build a useful, validation-safe minimum response after failed rewrites."""
    pages = sorted({
        int(page)
        for page in re.findall(
            r"\[参考文档：[^\]]+｜第\s*(\d+)\s*页",
            str(reference_context or ""),
        )
    })
    page_text = "、".join(f"第{page}页" for page in pages) or "本轮论文片段"
    focus = paper_round_focus(topic, round_index)
    if "云计算" in str(role or ""):
        perspective = "规模化运行中的资源占用、并行效率与故障恢复稳定性"
    elif "Agent" in str(role or ""):
        perspective = "长任务链中的状态保持、检索准确性与工具调用一致性"
    else:
        perspective = "该设计在不同实验和使用条件下的稳定性"
    return (
        f"原文事实：{page_text}是本轮“{focus}”的直接依据，页面展示了相关机制与工程设计。"
        f"待验证的工程推断：从{role}视角，仍需验证{perspective}，这不是论文结论。"
        "本轮只确认页面直接呈现的内容，不补写未给出的量化结果。"
    )


def build_host_prompt(
    *,
    topic: str,
    round_index: int,
    speakers: list[PodcastAgent],
    total_rounds: int | None = None,
    user_input: str = "",
    discussion_mode: str = "group",
) -> str:
    speaker_names = "、".join(a.role for a in speakers) or "一位主讲人"
    input_clause = f"\n用户刚刚插话：{user_input.strip()}" if user_input.strip() else ""
    total_clause = f"\n总轮数：{total_rounds}" if total_rounds else ""
    if total_rounds and round_index >= total_rounds:
        ending_rule = "当前是最后一轮，可以用一句话做最终收束，但不要冗长告别。"
    else:
        ending_rule = "当前不是结束环节，禁止说“本期结束”“今天就到这里”“感谢收听”等收尾话；必须继续引导讨论。"
    paper_clause = ""
    if discussion_mode == "paper":
        current_focus = paper_round_focus(topic, max(1, round_index), total_rounds)
        next_focus = paper_round_focus(topic, round_index + 1, total_rounds) if total_rounds and round_index < total_rounds else ""
        paper_clause = f"""
论文讨论模式：
- 当前轮次议程：{current_focus}
{f'- 下一轮议程：{next_focus}' if next_focus else ''}
- 必须按轮次议程推进，不得因为上一位发言不完整而让后续所有轮次停留在旧议程；简短纠偏后进入下一轮议程。
- 不要认可主讲人声称的“纠错”或“论文未提供”，除非其引用的本轮原文直接支持该判断。
- 串讲不得把主讲人的工程猜测升级成论文事实；凡是显存容量、单机/多机拓扑、生产成本、延迟、恢复行为等外推，只能称为“待验证的推断”，不能在总结中当作已证实结论复述。
""".rstrip()
    return f"""\
你是 AI 播客的女主持人，负责串讲、承接、引导，不做长篇分析。
当前主题：{topic}
当前轮次：{round_index}{total_clause}
本轮将由这些主讲人发言：{speaker_names}{input_clause}
{paper_clause}

要求：
- 只输出你要说的话，不要 Markdown、标题、列表、括号舞台说明。
- 如果有用户插话，先自然回应用户的问题或观点，再把话题交给主讲人。
- 串讲只能承接和引导，不要替主讲人展开长篇分析。
- 始终围绕“当前主题”里的原始问题和用户明确指定的重点推进；角色身份和职业类比只能辅助解释，不能替代讨论主题。
- 如果上一位主讲人主要在谈自己的职业、系统类比或旁支问题，只能简短承接，下一问必须直接拉回原始问题；不要继续追问该类比。
- {ending_rule}
- 语气亲切、简洁，最多 120 个中文字符。
- 必须用完整句子收尾，宁可少讲一点，也不要让最后一句断在半句。
"""


def build_speaker_prompt(*, topic: str, agent: PodcastAgent, round_index: int, context: str, research: str = "") -> str:
    role_prompt = ROLE_PROMPTS.get(agent.role, ROLE_PROMPTS["AI Agent研发工程师"])
    research_block = research.strip() or "暂无可用研究结果；请降低断言强度，只基于已知主流共识发言。"
    document_rule = ""
    if has_document_reference(context) or "[本轮论文依据：" in context:
        current_focus = paper_round_focus(topic, round_index)
        document_rule = f"""
- 这是论文研讨：优先分析“本轮论文依据”，不要只复述摘要或泛泛谈背景。
- 当前轮次唯一议程：{current_focus}。即使本轮页面还出现评测、局限或其他轮次的信息，也不得提前讨论；只回答当前议程。
- 涉及论文中的方法、数据、结论或局限时，必须自然说出依据位置（文档名及页码/片段）；没有页码时只能引用片段号，不得编造页码。
- 只能依据本轮实际提供的页面作事实判断；不得把目录标题当作正文证据，也不得声称整篇论文“没有写”或“未提供”。
- 推断必须明确说“这是基于原文的推断，不是论文结论”，不得把相关性改写成因果关系。
- 身份只用于选择要检查的论文证据：不得把模型训练 checkpoint 改写成多 Agent checkpoint，也不得把论文问题替换成 Agent 系统或云平台自身的问题。
- 不得仅凭压缩倍数或架构图推出具体显存容量、单机/多机部署拓扑、buffer 分配、调度器行为、云成本、生产就绪度或故障恢复结论；如要提出，只能明确标为“待验证的工程推断”，并说明原文没有直接验证。
- “相当于、说白了、像、就像、好比”等角色类比也属于推断，使用时必须紧接着说明“这里只是类比，不是论文结论”；否则不要使用类比。
""".rstrip()
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
- 观点必须靠谱、能解释清楚；不要输出未经证实的小众判断。
- 如果资料不足，要明确降低断言强度。
- “当前主题”是讨论对象，身份只是观察角度。必须直接回答原始问题并优先覆盖用户点名的重点和限制，不能把话题改写成你的职业问题。
- 保持身份视角，但职业术语和职业类比不能成为发言主体；最多使用一个简短类比，类比后必须立刻回到主题本身。
- 优先给出一个与主题直接相关的新角度、具体例子、反例、风险或落地建议。
- 不要复述主持人或其他 Agent 已经说过的内容；如果上下文已有类似观点，直接推进到差异化判断。
{document_rule}

输出要求：
- 只输出最终发言，不要展示研究过程、引用列表、Markdown、标题或项目符号。
- 像饭桌上给聪明朋友解释：口语化、说人话，少用黑话；必须生动一点，可以用短比喻、小例子或轻微幽默。
- 不超过 200 个中文字符。
- 必须用完整句子收尾；宁可少讲一个观点，也不要让最后一句断在半句。
- 不要自称“作为某某”，直接发表观点。
"""


def build_research_prompt(*, topic: str, agent: PodcastAgent, round_index: int, context: str) -> str:
    document_rules = ""
    source_rule = "- 优先使用 web_search / web_fetch 等工具查找主流、被认可、可验证的信息。"
    if has_document_reference(context) or "[本轮论文依据：" in context:
        current_focus = paper_round_focus(topic, round_index)
        source_rule = "- 只分析本轮提供的论文原文；论文讨论模式禁止使用 Web 或其他外部资料。"
        document_rules = f"""
- 这是论文研讨，必须优先逐段分析“本轮论文依据”，提取本轮尚未讨论的论点、证据、方法或局限。
- 当前轮次唯一议程：{current_focus}。忽略页面中属于其他轮次的评测、局限或方法内容，不得提前跨轮研究。
- 每条关键结论都要保留文档名及页码/片段位置；不得编造原文没有的页码、数据或结论。
- 目录只能用于定位章节，不能作为事实结论的证据。
- 不得根据局部片段声称整篇论文没有某项内容；推断必须与论文原始结论明确区分。
- 不要把角色行业中的部署拓扑、显存容量、成本、延迟、调度、故障恢复或多 Agent 行为补写成论文事实；这些只能作为明确标注、可被证伪的待验证问题。
""".rstrip()
    return f"""\
你是 AI 播客主讲人「{agent.role}」的 research 子 Agent。

播客主题：{topic}
当前轮次：{round_index}
近期讨论上下文：
{context or "暂无。"}

请做深入但聚焦的 research：
- 播客主题是研究对象，主讲人身份只用于选择观察角度；不得把研究对象替换成该身份所在行业的问题。
- 优先回答主题中的原始问题，并覆盖用户明确指定的重点和限制。
{source_rule}
- 只总结与播客主题直接相关的关键事实、共识观点、重要争议边界。
- 不要为了贴合身份而堆职业术语或寻找牵强类比。
{document_rules}
- 不要写播客发言稿；只输出供主讲人使用的研究摘要。
- 输出控制在 500 中文字以内。
"""


async def generate_utterance(
    *,
    runtime: Any,
    registry: Any,
    system_prompt: str,
    user_text: str,
    cancellation_token: CancellationToken,
    on_delta: Any | None = None,
    client: Any | None = None,
    cfg: Any | None = None,
) -> str:
    from dataclasses import replace

    from nano_openclaw.core.loop import AgentSession, Message

    temp_history: list[Message] = []

    def handle_event(event: Any) -> None:
        if on_delta is not None and type(event).__name__ == "TextDelta":
            on_delta(getattr(event, "text", ""))

    cfg = cfg or runtime.cfg

    agent_session = AgentSession(
        history=temp_history,
        registry=registry,
        on_event=handle_event,
        client=client or runtime.client,
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
    return value.strip("`#*- \n\t")
