"""阿里云 RESTful 语音合成单测：URL 派生 + synthesize_tts（注入 http_post，不打真网络）。"""

from __future__ import annotations

import httpx
import pytest

from nano_openclaw.config.types import VoiceConfig
from nano_openclaw.gateway.webui.aliyun_tts import TtsError, synthesize_tts


# ── resolved_rest_tts_url 派生 ────────────────────────────────────────────────
def test_resolved_rest_tts_url_default_region():
    cfg = VoiceConfig(appkey="ak", accessKeyId="id", accessKeySecret="sec")  # region 默认 cn-shanghai
    assert cfg.resolved_rest_tts_url() == "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/tts"


def test_resolved_rest_tts_url_other_region():
    cfg = VoiceConfig(appkey="ak", accessKeyId="id", accessKeySecret="sec", region="cn-beijing")
    assert cfg.resolved_rest_tts_url() == "https://nls-gateway-cn-beijing.aliyuncs.com/stream/v1/tts"


def test_resolved_rest_tts_url_explicit_wss_endpoint():
    # 显式 wss endpoint 覆盖：scheme wss→https，host 保留，path 换成 /stream/v1/tts。
    cfg = VoiceConfig(
        appkey="ak", accessKeyId="id", accessKeySecret="sec",
        endpoint="wss://custom.example.com/ws/v1",
    )
    assert cfg.resolved_rest_tts_url() == "https://custom.example.com/stream/v1/tts"


def test_resolved_rest_tts_url_internal_ws_endpoint():
    # 内网 ws（ECS internal 只支持 http）→ scheme ws→http。
    cfg = VoiceConfig(
        appkey="ak", accessKeyId="id", accessKeySecret="sec",
        endpoint="ws://nls-gateway-cn-shanghai-internal.aliyuncs.com/ws/v1",
    )
    assert cfg.resolved_rest_tts_url() == "http://nls-gateway-cn-shanghai-internal.aliyuncs.com/stream/v1/tts"


# ── synthesize_tts 成功 ───────────────────────────────────────────────────────
def test_synthesize_tts_success_returns_audio_bytes():
    captured = {}

    def fake_post(url, json_body):
        captured["url"] = url
        captured["body"] = json_body
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"PCMDATA")

    audio = synthesize_tts(
        url="https://gw/stream/v1/tts",
        appkey="ak",
        token="tok",
        text="你好",
        voice="xiaoyun",
        sample_rate=16000,
        http_post=fake_post,
    )
    assert audio == b"PCMDATA"
    # 请求体含所有约定字段。
    body = captured["body"]
    assert body["format"] == "pcm"
    assert body["voice"] == "xiaoyun"
    assert body["sample_rate"] == 16000
    assert body["text"] == "你好"
    assert body["appkey"] == "ak"
    assert body["token"] == "tok"
    assert captured["url"] == "https://gw/stream/v1/tts"


# ── synthesize_tts 失败：JSON 错误体 ─────────────────────────────────────────
def test_synthesize_tts_json_error_raises_with_message():
    def fake_post(url, json_body):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"task_id": "t", "result": "", "status": 40000001, "message": "token invalid"},
        )

    with pytest.raises(TtsError) as exc:
        synthesize_tts(
            url="https://gw/stream/v1/tts", appkey="ak", token="bad",
            text="x", voice="xiaoyun", sample_rate=16000, http_post=fake_post,
        )
    assert "token invalid" in str(exc.value)


def test_synthesize_tts_json_error_without_message_uses_status():
    def fake_post(url, json_body):
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"status": 40000010})

    with pytest.raises(TtsError) as exc:
        synthesize_tts(
            url="https://gw/stream/v1/tts", appkey="ak", token="t",
            text="x", voice="xiaoyun", sample_rate=16000, http_post=fake_post,
        )
    assert "40000010" in str(exc.value)


# ── synthesize_tts 失败：网络错误 ────────────────────────────────────────────
def test_synthesize_tts_network_error_raises_ttserror():
    def fake_post(url, json_body):
        raise httpx.ConnectError("boom")

    with pytest.raises(TtsError):
        synthesize_tts(
            url="https://gw/stream/v1/tts", appkey="ak", token="t",
            text="x", voice="xiaoyun", sample_rate=16000, http_post=fake_post,
        )


# ── synthesize_tts 失败：非 JSON 文本体 ──────────────────────────────────────
def test_synthesize_tts_non_json_text_body_raises():
    def fake_post(url, json_body):
        return httpx.Response(500, headers={"content-type": "text/plain"}, content=b"server boom")

    with pytest.raises(TtsError) as exc:
        synthesize_tts(
            url="https://gw/stream/v1/tts", appkey="ak", token="t",
            text="x", voice="xiaoyun", sample_rate=16000, http_post=fake_post,
        )
    assert "server boom" in str(exc.value)
