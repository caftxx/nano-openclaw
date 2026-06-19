"""Talk-mode TTS configuration and provider dispatch.

This module is intentionally small: nano currently ships one cloud speech
provider (Aliyun) plus browser-local fallback on the client. Keeping the
provider boundary here prevents WebUI endpoints from owning speech synthesis
details and gives gateway RPC clients the same ``talk.config`` / ``talk.speak``
surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nano_openclaw.features.voice.aliyun_token import AliyunTokenProvider, TokenError
from nano_openclaw.adapters.webui.aliyun_tts import TtsError, synthesize_tts
from nano_openclaw.adapters.webui.voice_catalog import ALIYUN_TTS_VOICES


class TalkSpeakError(Exception):
    """TTS request failed in a way the caller should surface."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "synthesis_failed",
        fallback_eligible: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.fallback_eligible = fallback_eligible


@dataclass(frozen=True)
class TalkSpeakResult:
    audio: bytes
    provider: str
    output_format: str
    mime_type: str
    voice_compatible: bool = True
    file_extension: str = ".pcm"


def build_talk_config(config: Any) -> dict[str, Any]:
    """Return non-secret talk configuration for WebUI/RPC clients."""

    voice_cfg = config.voice
    provider_config: dict[str, Any] = {
        "voice": voice_cfg.ttsVoice,
        "sample_rate": voice_cfg.ttsSampleRate,
        "voices": ALIYUN_TTS_VOICES,
        "streaming": True,
        "rest": True,
    }
    return {
        "available": voice_cfg.available,
        "provider": voice_cfg.provider,
        "appkey": voice_cfg.appkey,
        "endpoint": voice_cfg.resolved_endpoint(),
        "wake_word": voice_cfg.wakeWord,
        "tts": {
            "enabled": voice_cfg.available and voice_cfg.ttsEnabled,
            "provider": voice_cfg.provider,
            "voice": voice_cfg.ttsVoice,
            "sample_rate": voice_cfg.ttsSampleRate,
            "voices": ALIYUN_TTS_VOICES,
        },
        "talk": {
            "provider": voice_cfg.provider if voice_cfg.available and voice_cfg.ttsEnabled else "",
            "providers": {
                voice_cfg.provider: provider_config,
            } if voice_cfg.available else {},
            "resolved": {
                "provider": voice_cfg.provider,
                "config": provider_config,
            } if voice_cfg.available else None,
        },
    }


def synthesize_talk_speech(
    config: Any,
    *,
    text: str,
    voice_id: str | None = None,
    sample_rate: int | None = None,
    speed: float | None = None,
    rate_wpm: int | None = None,
    token_provider: AliyunTokenProvider | None = None,
) -> TalkSpeakResult:
    """Synthesize speech through the active talk provider.

    ``speed``/``rate_wpm`` are accepted for API parity with OpenClaw Talk
    directives. Aliyun REST in this code path does not support them yet, so
    they are validated and otherwise ignored.
    """

    text = text.strip()
    if not text:
        raise TalkSpeakError("talk.speak requires text", reason="invalid_request")
    resolved_speed = _resolve_speed(speed=speed, rate_wpm=rate_wpm)
    if (speed is not None or rate_wpm is not None) and resolved_speed is None:
        raise TalkSpeakError(
            "rateWpm/speed must resolve to a speed between 0.5 and 2.0",
            reason="invalid_request",
        )

    voice_cfg = config.voice
    if not voice_cfg.available or not voice_cfg.ttsEnabled:
        raise TalkSpeakError(
            "talk.speak unavailable: Aliyun voice is not configured",
            reason="talk_unconfigured",
            fallback_eligible=True,
        )
    if voice_cfg.provider != "aliyun":
        raise TalkSpeakError(
            f"talk.speak unavailable: unsupported provider {voice_cfg.provider!r}",
            reason="talk_provider_unsupported",
            fallback_eligible=True,
        )

    provider = token_provider or AliyunTokenProvider(
        access_key_id=voice_cfg.accessKeyId,
        access_key_secret=voice_cfg.accessKeySecret,
        region_id=voice_cfg.region,
    )
    try:
        token_id, _ = provider.get_token()
    except TokenError as exc:
        raise TalkSpeakError(
            f"sign Aliyun token failed: {exc}",
            reason="synthesis_failed",
        ) from exc

    voice = voice_id or voice_cfg.ttsVoice
    sr = sample_rate or voice_cfg.ttsSampleRate
    try:
        audio = synthesize_tts(
            url=voice_cfg.resolved_rest_tts_url(),
            appkey=voice_cfg.appkey,
            token=token_id,
            text=text,
            voice=voice,
            sample_rate=sr,
        )
    except TtsError as exc:
        raise TalkSpeakError(
            f"talk synthesis failed: {exc}",
            reason="synthesis_failed",
        ) from exc

    if not audio:
        raise TalkSpeakError(
            "talk synthesis returned empty audio",
            reason="invalid_audio_result",
        )
    return TalkSpeakResult(
        audio=audio,
        provider="aliyun",
        output_format="pcm",
        mime_type="application/octet-stream",
        voice_compatible=True,
        file_extension=".pcm",
    )


def _resolve_speed(*, speed: float | None, rate_wpm: int | None) -> float | None:
    if rate_wpm is not None:
        if rate_wpm <= 0:
            return None
        speed = rate_wpm / 175
    if speed is None:
        return None
    return speed if 0.5 < speed < 2.0 else None
