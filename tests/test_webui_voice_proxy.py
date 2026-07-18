from types import SimpleNamespace

from nano_openclaw.adapters.webui.server import _openai_voice_settings
from nano_openclaw.config.types import VoiceConfig


def _backend(voice: VoiceConfig):
    return SimpleNamespace(runtime=SimpleNamespace(config=SimpleNamespace(voice=voice)))


def test_openai_voice_settings_keeps_gateway_secret_server_side():
    voice = VoiceConfig(
        provider="openai-compatible",
        baseUrl="http://127.0.0.1:5100/v1",
        realtimeUrl="ws://127.0.0.1:5100/v1/realtime",
        apiKey="secret",
        asrModel="paraformer-zh-streaming",
    )

    assert _openai_voice_settings(_backend(voice)) == {
        "url": "ws://127.0.0.1:5100/v1/realtime",
        "model": "paraformer-zh-streaming",
        "api_key": "secret",
    }


def test_openai_voice_settings_rejects_other_or_incomplete_providers():
    assert _openai_voice_settings(_backend(VoiceConfig())) is None
    assert _openai_voice_settings(_backend(VoiceConfig(
        provider="openai-compatible",
        baseUrl="http://127.0.0.1:5100/v1",
    ))) is None
