"""Text-to-speech provider layer for nano-openclaw."""

from nano_openclaw.tts.talk import (
    TalkSpeakError,
    TalkSpeakResult,
    build_talk_config,
    synthesize_talk_speech,
)

__all__ = [
    "TalkSpeakError",
    "TalkSpeakResult",
    "build_talk_config",
    "synthesize_talk_speech",
]
