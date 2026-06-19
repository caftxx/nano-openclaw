"""阿里云 CreateToken 签名 + 缓存 provider 单测（纯逻辑，不打真网络）。"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from nano_openclaw.adapters.webui.aliyun_token import (
    AliyunTokenProvider,
    TokenError,
    build_canonicalized_query,
    build_signed_query,
    build_string_to_sign,
    percent_encode,
    request_token,
    sign,
)


# ── percent_encode 边界 ───────────────────────────────────────────────────────
def test_percent_encode_space_plus_star_tilde():
    # 空格→%20、`*`→%2A、`~` 不编码、字面 `+`→%2B（阿里云 RFC3986 规范）
    assert percent_encode("a b") == "a%20b"
    assert percent_encode("a*b") == "a%2Ab"
    assert percent_encode("a~b") == "a~b"
    assert percent_encode("a+b") == "a%2Bb"


def test_percent_encode_reserved_chars():
    assert percent_encode("/") == "%2F"
    assert percent_encode("=") == "%3D"
    assert percent_encode("&") == "%26"


# ── canonicalized query + stringToSign 对已知输入产出确定串 ────────────────────
def test_build_canonicalized_query_sorted_and_encoded():
    params = {"B": "2", "A": "1 x", "C": "a*b"}
    # 按 key 排序，value 编码
    assert build_canonicalized_query(params) == "A=1%20x&B=2&C=a%2Ab"


def test_build_string_to_sign_known_input():
    params = {
        "Action": "CreateToken",
        "Version": "2019-02-28",
        "AccessKeyId": "AK",
    }
    canonical = build_canonicalized_query(params)
    expected = "GET&" + percent_encode("/") + "&" + percent_encode(canonical)
    assert build_string_to_sign(params) == expected
    # %2F 必然出现（"/" 被编码）
    assert build_string_to_sign(params).startswith("GET&%2F&")


# ── sign 的 HMAC 结果与手算一致 ───────────────────────────────────────────────
def test_sign_matches_manual_hmac_sha1():
    secret = "mysecret"
    sts = "GET&%2F&Action%3DCreateToken"
    manual = base64.b64encode(
        hmac.new((secret + "&").encode(), sts.encode(), hashlib.sha1).digest()
    ).decode()
    assert sign(secret, sts) == manual


def test_build_signed_query_deterministic_with_injected_nonce_timestamp():
    q1 = build_signed_query(
        access_key_id="AK", access_key_secret="SK",
        nonce="fixed-nonce", timestamp="2026-06-02T08:00:00Z",
    )
    q2 = build_signed_query(
        access_key_id="AK", access_key_secret="SK",
        nonce="fixed-nonce", timestamp="2026-06-02T08:00:00Z",
    )
    assert q1 == q2                       # 注入固定 nonce/timestamp → 确定串
    assert "Signature=" in q1
    assert "Action=CreateToken" in q1
    assert "SignatureNonce=fixed-nonce" in q1


# ── request_token：用假 http 验证解析与错误处理 ───────────────────────────────
class _FakeResp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_request_token_parses_token_id_and_expire():
    def fake_get(url: str) -> _FakeResp:
        assert url.startswith("https://nls-meta.cn-shanghai.aliyuncs.com/?")
        return _FakeResp(200, {"Token": {"Id": "tok-123", "ExpireTime": 1893456000}})

    token, expire = request_token(access_key_id="AK", access_key_secret="SK", http_get=fake_get)
    assert token == "tok-123"
    assert expire == 1893456000


def test_request_token_raises_on_non_200():
    def fake_get(url: str) -> _FakeResp:
        return _FakeResp(403, text="Forbidden")

    with pytest.raises(TokenError):
        request_token(access_key_id="AK", access_key_secret="SK", http_get=fake_get)


def test_request_token_raises_on_missing_token_field():
    def fake_get(url: str) -> _FakeResp:
        return _FakeResp(200, {"Message": "no token"})

    with pytest.raises(TokenError):
        request_token(access_key_id="AK", access_key_secret="SK", http_get=fake_get)


# ── 缓存 provider：未过期不重签 / 过期重签（假 clock + 假 requester）──────────
def test_provider_caches_until_safety_margin():
    now = {"t": 1000.0}
    calls = {"n": 0}

    def fake_requester(*, access_key_id, access_key_secret, region_id):
        calls["n"] += 1
        return f"tok-{calls['n']}", 1000 + 3600   # expire at 4600

    provider = AliyunTokenProvider(
        access_key_id="AK", access_key_secret="SK",
        clock=lambda: now["t"], requester=fake_requester, safety_margin_sec=120,
    )

    # 首次签发
    assert provider.get_token() == ("tok-1", 4600)
    assert calls["n"] == 1

    # 远未到期 → 复用缓存，不重签
    now["t"] = 4000.0
    assert provider.get_token() == ("tok-1", 4600)
    assert calls["n"] == 1

    # 进入安全余量窗口内（expire - 120 = 4480）→ 重签
    now["t"] = 4500.0
    assert provider.get_token() == ("tok-2", 4600)
    assert calls["n"] == 2


def test_provider_matches_distinguishes_credentials():
    provider = AliyunTokenProvider(access_key_id="AK", access_key_secret="SK", region_id="cn-shanghai")
    assert provider.matches(access_key_id="AK", access_key_secret="SK", region_id="cn-shanghai")
    assert not provider.matches(access_key_id="AK2", access_key_secret="SK", region_id="cn-shanghai")
    assert not provider.matches(access_key_id="AK", access_key_secret="SK", region_id="cn-beijing")
