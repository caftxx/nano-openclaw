from __future__ import annotations

import asyncio

from nano_openclaw.config.types import VoiceConfig
from nano_openclaw.api.methods.talk import talk_config, talk_speak
from nano_openclaw.features.voice.talk import build_talk_config
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
