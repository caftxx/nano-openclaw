from __future__ import annotations

import asyncio

import httpx

from nano_openclaw.config.types import VoiceConfig
from nano_openclaw.api.methods.talk import talk_config, talk_speak
from nano_openclaw.features.voice.talk import (
    build_talk_config,
    discover_openai_compatible_voices,
    synthesize_openai_compatible_speech,
)
from nano_openclaw.services.backend import VoiceError


class _Runtime:
    def __init__(self, config):
        self.config = config


class _Ctx:
    def __init__(self, config):
        self.runtime = _Runtime(config)
        self.backend = _Backend(config)


class _Config:
    def __init__(self, voice):
        self.voice = voice


class _Backend:
    def __init__(self, config):
        self._config = config

    async def voice_config(self):
        return build_talk_config(self._config)

    async def talk_speak(self, **params):
        raise VoiceError(
            "talk.speak unavailable: Aliyun voice is not configured",
            reason="talk_unconfigured",
            fallback_eligible=True,
            status_code=503,
        )


def test_build_talk_config_exposes_non_secret_voice_surface():
    cfg = _Config(VoiceConfig(
        appkey="app",
        accessKeyId="id",
        accessKeySecret="secret",
        wakeWord="小克",
    ))
    payload = build_talk_config(cfg)
    assert payload["available"] is True
    assert payload["appkey"] == "app"
    assert payload["wake_word"] == "小克"
    assert payload["tts"]["enabled"] is True
    assert payload["talk"]["provider"] == "aliyun"
    assert "accessKeySecret" not in str(payload)


def test_talk_config_rpc_wraps_config_payload():
    cfg = _Config(VoiceConfig())
    payload = asyncio.run(talk_config(_Ctx(cfg), {}))
    assert payload["config"]["available"] is False
    assert payload["config"]["talk"]["provider"] == ""


def test_talk_speak_unconfigured_returns_fallback_eligible_error():
    cfg = _Config(VoiceConfig())
    payload = asyncio.run(talk_speak(_Ctx(cfg), {"text": "hi"}))
    assert payload["ok"] is False
    assert payload["reason"] == "talk_unconfigured"
    assert payload["fallbackEligible"] is True


def test_openai_compatible_talk_config_and_pcm_synthesis():
    voice = VoiceConfig(
        provider="openai-compatible",
        baseUrl="http://speech.local/v1",
        realtimeUrl="ws://speech.local/v1/realtime",
        apiKey="secret",
        ttsVoice="nano",
        ttsSampleRate=24000,
    )
    payload = build_talk_config(_Config(voice))
    assert payload["available"] is True
    assert payload["talk"]["provider"] == "openai-compatible"
    assert payload["talk"]["resolved"]["config"]["tts_model"] == "fun-cosyvoice3-0.5b"
    assert "secret" not in str(payload)

    def post(url, **kwargs):
        assert url == "http://speech.local/v1/audio/speech"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            content=b"pcm",
            headers={"x-audio-sample-rate": "24000"},
            request=httpx.Request("POST", url),
        )

    assert synthesize_openai_compatible_speech(
        base_url=voice.baseUrl,
        api_key=voice.apiKey,
        text="你好",
        model=voice.ttsModel,
        voice=voice.ttsVoice,
        sample_rate=24000,
        http_post=post,
    ) == b"pcm"


def test_openai_compatible_talk_config_defaults_to_gateway_24khz():
    voice = VoiceConfig(
        provider="openai-compatible",
        baseUrl="http://speech.local/v1",
        realtimeUrl="ws://speech.local/v1/realtime",
    )
    payload = build_talk_config(_Config(voice))
    assert payload["tts"]["sample_rate"] == 24000
    assert payload["talk"]["resolved"]["config"]["sample_rate"] == 24000


def test_openai_compatible_voice_discovery_and_config_catalog():
    voice = VoiceConfig(
        provider="openai-compatible",
        baseUrl="http://speech.local/v1",
        realtimeUrl="ws://speech.local/v1/realtime",
        apiKey="secret",
        ttsVoice="nano",
    )

    def get(url, **kwargs):
        assert url == "http://speech.local/v1/audio/voices"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={"data": [
                {"id": "female", "name": "女声"},
                {"id": "nano", "name": "默认音色"},
                {"id": "female", "name": "重复项"},
            ]},
            request=httpx.Request("GET", url),
        )

    catalog = discover_openai_compatible_voices(
        base_url=voice.baseUrl,
        api_key=voice.apiKey,
        default_voice=voice.ttsVoice,
        http_get=get,
    )
    assert catalog == [
        {"value": "nano", "label": "默认音色"},
        {"value": "female", "label": "女声"},
    ]
    payload = build_talk_config(_Config(voice), voice_catalog=catalog)
    assert payload["tts"]["voices"] == catalog
    assert payload["talk"]["resolved"]["config"]["voices"] == catalog
