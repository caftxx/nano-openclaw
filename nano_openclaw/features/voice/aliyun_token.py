"""阿里云智能语音交互 —— CreateToken 临时 Token 签发（纯 stdlib 实现）。

为什么自己实现而不引阿里云 SDK：本项目约束「不引入新的第三方依赖」，且 CreateToken
只是一个 RPC 风格的 GET 请求加 HMAC-SHA1 签名，stdlib（hmac/hashlib/base64/
urllib.parse）足以覆盖。HTTP 调用复用项目已有的 httpx。

阿里云实时语音识别走浏览器直连 wss 网关，鉴权靠 URL query 上的临时 Token；Token 由
AccessKeyId/AccessKeySecret 通过 NLS Meta 的 CreateToken 接口换取，有效期约 24h。
后端在此签发并缓存 Token，绝不把 AK/SK 暴露给浏览器。

签名算法（RPC 风格 v1.0）：
  1. 公共参数排序后做 RFC3986 percent-encode，拼成 k=v&k=v 的 canonicalizedQueryString；
  2. stringToSign = "GET" + "&" + enc("/") + "&" + enc(canonicalizedQueryString)；
  3. signature = base64(HMAC-SHA1(key=AccessKeySecret+"&", msg=stringToSign))；
  4. 把 Signature（同样 percent-encode）并入最终 query 发请求。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

# NLS Meta 的 CreateToken 端点固定在 cn-shanghai（与实际识别 region 无关）。
NLS_META_ENDPOINT = "https://nls-meta.cn-shanghai.aliyuncs.com/"
# 临时 Token 过期前的安全余量：在 expire_time - SAFETY_MARGIN_SEC 之前复用缓存。
SAFETY_MARGIN_SEC = 120


def percent_encode(s: str) -> str:
    """RFC3986 percent-encode，符合阿里云 RPC 签名要求。

    阿里云要求：空格→%20、`*`→%2A、`~` 不编码、其余保留字按 RFC3986 编码。
    urllib.parse.quote(safe="~") 恰好满足：默认空格→%20（quote 不会输出 `+`，那是
    quote_plus 的行为）、`*`→%2A、显式保留 `~` 不编码。所以一次 quote 即合规，无需
    任何额外替换（输入里出现字面 `+` 时应编成 %2B，quote 已正确处理）。
    """
    from urllib.parse import quote
    return quote(s, safe="~")


def build_canonicalized_query(params: dict[str, str]) -> str:
    """按 key 排序、对 key/value 分别 percent-encode，拼成 k=v&k=v。"""
    items = sorted(params.items(), key=lambda kv: kv[0])
    return "&".join(f"{percent_encode(k)}={percent_encode(v)}" for k, v in items)


def build_string_to_sign(params: dict[str, str]) -> str:
    """构造待签名串 stringToSign（不含 Signature 参数本身）。"""
    canonicalized = build_canonicalized_query(params)
    return "GET&" + percent_encode("/") + "&" + percent_encode(canonicalized)


def sign(secret: str, string_to_sign: str) -> str:
    """signature = base64(HMAC-SHA1(key=secret+"&", msg=stringToSign))。

    阿里云 RPC v1.0 要求密钥是 AccessKeySecret 后再追加一个 "&"。
    """
    key = (secret + "&").encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _build_request_params(
    *,
    access_key_id: str,
    region_id: str,
    nonce: str,
    timestamp: str,
) -> dict[str, str]:
    """CreateToken 的公共参数（不含 Signature）。"""
    return {
        "Action": "CreateToken",
        "Version": "2019-02-28",
        "RegionId": region_id,
        "Format": "JSON",
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": nonce,
        "Timestamp": timestamp,
    }


def build_signed_query(
    *,
    access_key_id: str,
    access_key_secret: str,
    region_id: str = "cn-shanghai",
    nonce: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> str:
    """构造带 Signature 的完整 query string（可直接拼到 endpoint 后发 GET）。

    nonce/timestamp 可注入，便于单测对已知输入产出确定串。
    """
    if nonce is None:
        nonce = uuid.uuid4().hex
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = _build_request_params(
        access_key_id=access_key_id,
        region_id=region_id,
        nonce=nonce,
        timestamp=timestamp,
    )
    string_to_sign = build_string_to_sign(params)
    signature = sign(access_key_secret, string_to_sign)
    # 最终 query：原始参数 + Signature，两者都按 RFC3986 编码。顺序无所谓（服务端按 key 取）。
    parts = [f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(params.items())]
    parts.append(f"Signature={percent_encode(signature)}")
    return "&".join(parts)


class TokenError(Exception):
    """CreateToken 失败（网络错误 / 阿里云返回非 200 / 响应缺字段）。"""


def request_token(
    *,
    access_key_id: str,
    access_key_secret: str,
    region_id: str = "cn-shanghai",
    http_get: Optional[Callable[[str], httpx.Response]] = None,
) -> tuple[str, int]:
    """调用 CreateToken，返回 (token_id, expire_time)。

    http_get 可注入（接收完整 URL、返回 httpx.Response），便于单测不打真网络。
    默认用 httpx 同步 client 发 GET（端点是固定外部域名）。
    """
    query = build_signed_query(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        region_id=region_id,
    )
    url = f"{NLS_META_ENDPOINT}?{query}"
    if http_get is None:
        def _default_get(u: str) -> httpx.Response:
            with httpx.Client(timeout=10.0) as client:
                return client.get(u)
        http_get = _default_get

    try:
        resp = http_get(url)
    except httpx.HTTPError as exc:  # 网络层错误统一包成 TokenError
        raise TokenError(f"CreateToken 请求失败: {exc}") from exc

    if resp.status_code != 200:
        raise TokenError(f"CreateToken 返回 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise TokenError("CreateToken 返回非 JSON") from exc
    token = data.get("Token") if isinstance(data, dict) else None
    if not isinstance(token, dict) or not token.get("Id") or token.get("ExpireTime") is None:
        raise TokenError(f"CreateToken 响应缺少 Token.Id/ExpireTime: {str(data)[:200]}")
    return str(token["Id"]), int(token["ExpireTime"])


class AliyunTokenProvider:
    """带缓存的临时 Token 提供者。

    缓存 (token, expire_time)，在 expire_time - SAFETY_MARGIN_SEC 之前复用，过期才重签。
    AK/SK/region 任一变化（config 热重载）则缓存失效——以构造时的凭据为缓存 key。
    clock / requester 可注入，便于单测用假时钟 + 假 http 验证「未过期不重签 / 过期重签」。
    """

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        region_id: str = "cn-shanghai",
        clock: Callable[[], float] = time.time,
        requester: Optional[Callable[..., tuple[str, int]]] = None,
        safety_margin_sec: int = SAFETY_MARGIN_SEC,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._region_id = region_id
        self._clock = clock
        self._requester = requester or request_token
        self._safety_margin_sec = safety_margin_sec
        self._cached_token: Optional[str] = None
        self._cached_expire: int = 0

    def matches(self, *, access_key_id: str, access_key_secret: str, region_id: str) -> bool:
        """凭据是否与本 provider 一致——endpoint 据此判断要不要重建 provider。"""
        return (
            self._access_key_id == access_key_id
            and self._access_key_secret == access_key_secret
            and self._region_id == region_id
        )

    def get_token(self) -> tuple[str, int]:
        """返回 (token, expire_time)，命中缓存则不重签。"""
        now = self._clock()
        if self._cached_token and now < (self._cached_expire - self._safety_margin_sec):
            return self._cached_token, self._cached_expire
        token, expire = self._requester(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region_id=self._region_id,
        )
        self._cached_token = token
        self._cached_expire = expire
        return token, expire
