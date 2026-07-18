"""Talk-mode TTS configuration and provider dispatch.

This module is intentionally small: nano supports Aliyun plus an
OpenAI-compatible local speech gateway, with browser-local fallback on the
client. Keeping the provider boundary here prevents WebUI endpoints from
owning speech synthesis details and gives gateway RPC clients the same
``talk.config`` / ``talk.speak`` surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx

from nano_openclaw.features.voice.aliyun_token import AliyunTokenProvider, TokenError
from nano_openclaw.features.voice.aliyun_tts import TtsError, synthesize_tts
from nano_openclaw.features.voice.voice_catalog import ALIYUN_TTS_VOICES


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


def build_talk_config(
    config: Any,
    *,
    voice_catalog: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return non-secret talk configuration for WebUI/RPC clients."""

    voice_cfg = config.voice
    if voice_cfg.provider == "aliyun":
        voices = ALIYUN_TTS_VOICES
    else:
        voices = voice_catalog or [{"value": voice_cfg.ttsVoice, "label": voice_cfg.ttsVoice}]
    provider_config: dict[str, Any] = {
        "voice": voice_cfg.ttsVoice,
        "sample_rate": voice_cfg.ttsSampleRate,
        "voices": voices,
        "streaming": True,
        "rest": True,
    }
    if voice_cfg.provider == "openai-compatible":
        provider_config["base_url"] = voice_cfg.baseUrl
        provider_config["realtime_url"] = voice_cfg.realtimeUrl
        provider_config["asr_model"] = voice_cfg.asrModel
        provider_config["tts_model"] = voice_cfg.ttsModel
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
            "voices": voices,
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


def discover_openai_compatible_voices(
    *,
    base_url: str,
    api_key: str,
    default_voice: str,
    http_get: Callable[..., httpx.Response] | None = None,
) -> list[dict[str, str]]:
    """Discover an OpenAI-compatible TTS voice catalog without exposing its key."""
    url = f"{base_url.rstrip('/')}/audio/voices"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        if http_get is None:
            with httpx.Client(timeout=3) as client:
                response = client.get(url, headers=headers)
        else:
            response = http_get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise TalkSpeakError(
            f"local speech voice discovery failed: {exc}",
            reason="voice_discovery_failed",
            fallback_eligible=True,
        ) from exc

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise TalkSpeakError(
            "local speech voice discovery returned an invalid catalog",
            reason="voice_discovery_failed",
            fallback_eligible=True,
        )
    voices: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        voice_id = str(entry.get("id") or "").strip()
        if not voice_id or voice_id in seen:
            continue
        seen.add(voice_id)
        voices.append({
            "value": voice_id,
            "label": str(entry.get("name") or voice_id).strip() or voice_id,
        })
    if not voices:
        raise TalkSpeakError(
            "local speech voice discovery returned no voices",
            reason="voice_discovery_failed",
            fallback_eligible=True,
        )
    voices.sort(key=lambda item: item["value"] != default_voice)
    return voices


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
            "talk.speak unavailable: voice synthesis is not configured",
            reason="talk_unconfigured",
            fallback_eligible=True,
        )
    if voice_cfg.provider == "openai-compatible":
        audio = synthesize_openai_compatible_speech(
            base_url=voice_cfg.baseUrl,
            api_key=voice_cfg.apiKey,
            text=text,
            model=voice_cfg.ttsModel,
            voice=voice_id or voice_cfg.ttsVoice,
            sample_rate=sample_rate or voice_cfg.ttsSampleRate,
            speed=resolved_speed or 1.0,
        )
        return TalkSpeakResult(
            audio=audio,
            provider="openai-compatible",
            output_format="pcm",
            mime_type="application/octet-stream",
            voice_compatible=True,
            file_extension=".pcm",
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


def synthesize_openai_compatible_speech(
    *,
    base_url: str,
    api_key: str,
    text: str,
    model: str,
    voice: str,
    sample_rate: int,
    speed: float = 1.0,
    http_post: Callable[..., httpx.Response] | None = None,
) -> bytes:
    """Call an OpenAI-compatible local speech endpoint and return PCM16LE."""
    url = f"{base_url.rstrip('/')}/audio/speech"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "pcm",
        "stream_format": "audio",
        "speed": speed,
    }
    try:
        if http_post is None:
            with httpx.Client(timeout=60) as client:
                response = client.post(url, headers=headers, json=payload)
        else:
            response = http_post(url, headers=headers, json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TalkSpeakError(
            f"local speech synthesis failed: {exc}", reason="synthesis_failed"
        ) from exc
    actual_rate = int(response.headers.get("x-audio-sample-rate") or sample_rate)
    if actual_rate != sample_rate:
        raise TalkSpeakError(
            f"local speech sample rate mismatch: expected {sample_rate}, got {actual_rate}",
            reason="invalid_audio_result",
        )
    if not response.content:
        raise TalkSpeakError(
            "local speech synthesis returned empty audio", reason="invalid_audio_result"
        )
    return response.content


def _resolve_speed(*, speed: float | None, rate_wpm: int | None) -> float | None:
    if rate_wpm is not None:
        if rate_wpm <= 0:
            return None
        speed = rate_wpm / 175
    if speed is None:
        return None
    return speed if 0.5 < speed < 2.0 else None
