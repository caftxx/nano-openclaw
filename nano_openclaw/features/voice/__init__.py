"""Text-to-speech provider layer for nano-openclaw."""

from nano_openclaw.features.voice.talk import (
    TalkSpeakError,
    TalkSpeakResult,
    build_talk_config,
    synthesize_talk_speech,
)
from nano_openclaw.features.voice.aliyun_token import AliyunTokenProvider, TokenError
from nano_openclaw.features.voice.podcast import (
    AGENT_ROLES,
    HOST_ROLE,
    HOST_VOICE_ID,
    HOST_VOICE_LABEL,
    PodcastAgent,
    assign_agents,
    build_start_summary,
    normalize_rounds,
)

__all__ = [
    "TalkSpeakError",
    "TalkSpeakResult",
    "AliyunTokenProvider",
    "TokenError",
    "build_talk_config",
    "synthesize_talk_speech",
    "AGENT_ROLES",
    "HOST_ROLE",
    "HOST_VOICE_ID",
    "HOST_VOICE_LABEL",
    "PodcastAgent",
    "assign_agents",
    "build_start_summary",
    "normalize_rounds",
]
