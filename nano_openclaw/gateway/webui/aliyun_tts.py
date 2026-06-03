"""阿里云智能语音交互 —— RESTful 语音合成（POST /stream/v1/tts，纯 stdlib + httpx）。

为什么自己实现而不引阿里云 SDK：与 aliyun_token.py 同理，本项目约束「不引入新的
第三方依赖」，RESTful 合成只是一次普通 POST JSON 请求，httpx 足以覆盖，无需 SDK。

为什么走后端代理而非浏览器直连：阿里云 RESTful 文档明确「不支持纯 JavaScript 直接
调用」——会遇到 CORS 跨域，且把 appkey 暴露在前端有泄露风险。故由后端代理：浏览器
永不接触 AK/SK，appkey 也不下发；后端用临时 Token + appkey 调阿里云，把音频字节回流。

这条 RESTful 路径是流式合成（FlowingSpeechSynthesizer）的自动备选：流式仅商用版可用、
不支持试用版，未开通账号每轮首句会 TaskFailed；RESTful 是标准「语音合成」产品，试用版
亦可用，作为「流式 → RESTful → 浏览器本地」回退链的中间一环。

协议（详见阿里云「语音合成 RESTful API」）：
  - POST <url>，Content-Type: application/json
  - body: {"appkey","text","token","format","sample_rate","voice"}
  - 成功：响应 Headers 的 Content-Type 为 audio/mpeg（我们请 pcm，仍以 audio/ 前缀判定），
    body 为合成音频二进制。
  - 失败：无 Content-Type 或为 application/json，body 为 JSON 错误体
    {"task_id","result","status","message"}。
"""

from __future__ import annotations

from typing import Callable, Optional

import httpx

# 合成可能比 Token 签发稍慢（长文本 + 算法复杂度），给较宽裕的超时。
DEFAULT_TIMEOUT_SEC = 30.0


class TtsError(Exception):
    """RESTful 语音合成失败（网络错误 / 阿里云返回非音频 / 错误 JSON 体）。"""


def synthesize_tts(
    *,
    url: str,
    appkey: str,
    token: str,
    text: str,
    voice: str,
    sample_rate: int,
    http_post: Optional[Callable[[str, dict], httpx.Response]] = None,
) -> bytes:
    """POST 合成请求，成功返回音频字节（pcm），失败抛 TtsError。

    http_post 可注入（签名 (url, json_body_dict) -> httpx.Response），便于单测不打真网络。
    默认实现用 httpx 同步 client POST JSON。
    """
    body = {
        "appkey": appkey,
        "token": token,
        "text": text,
        "voice": voice,
        "format": "pcm",
        "sample_rate": sample_rate,
    }
    if http_post is None:
        def _default_post(u: str, json_body: dict) -> httpx.Response:
            with httpx.Client(timeout=DEFAULT_TIMEOUT_SEC) as client:
                return client.post(u, json=json_body, headers={"Content-Type": "application/json"})
        http_post = _default_post

    try:
        resp = http_post(url, body)
    except httpx.HTTPError as exc:  # 网络层错误统一包成 TtsError
        raise TtsError(f"语音合成请求失败: {exc}") from exc

    content_type = resp.headers.get("content-type", "")
    if "audio/" in content_type:
        return resp.content

    # 失败：尝试从 JSON 错误体取 message/status 拼原因，解析不出用文本前缀兜底。
    try:
        data = resp.json()
    except ValueError:
        raise TtsError(f"语音合成失败: {resp.text[:200]}")
    if isinstance(data, dict):
        message = data.get("message")
        status = data.get("status")
        reason = message or (f"status={status}" if status is not None else None)
        raise TtsError(f"语音合成失败: {reason or str(data)[:200]}")
    raise TtsError(f"语音合成失败: {str(data)[:200]}")
