"""Text-to-speech provider layer for nano-openclaw."""

from nano_openclaw.features.voice.talk import (
    TalkSpeakError,
    TalkSpeakResult,
    build_talk_config,
    synthesize_talk_speech,
)
from nano_openclaw.features.voice.aliyun_token import AliyunTokenProvider, TokenError

__all__ = [
    "TalkSpeakError",
    "TalkSpeakResult",
    "AliyunTokenProvider",
    "TokenError",
    "build_talk_config",
    "synthesize_talk_speech",
]
