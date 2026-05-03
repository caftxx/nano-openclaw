"""Tests for web_fetch tool."""

from nano_openclaw.web_fetch import web_fetch, _FETCH_CACHE, _truncate, _normalize_whitespace


def test_web_fetch_ssrf_blocked():
    result = web_fetch("http://localhost/admin")
    assert result["status"] == 403
    assert "SSRF blocked" in result["text"]


def test_web_fetch_private_ip_blocked():
    result = web_fetch("http://127.0.0.1/admin")
    assert result["status"] == 403


def test_web_fetch_invalid_url():
    result = web_fetch("not-a-url")
    assert result["status"] == 400
    assert "Invalid URL" in result["text"]


def test_web_fetch_invalid_scheme():
    result = web_fetch("ftp://example.com/file")
    assert result["status"] == 400


def test_web_fetch_truncation():
    """Truncation logic works correctly."""
    text = "a" * 100
    truncated, was_truncated = _truncate(text, 50)
    assert len(truncated) == 50
    assert was_truncated is True
    
    short_text, was_short = _truncate("hello", 100)
    assert short_text == "hello"
    assert was_short is False


def test_web_fetch_normalize_whitespace():
    assert _normalize_whitespace("hello\n\n\nworld") == "hello\n\nworld"
    assert _normalize_whitespace("  hello   world  ") == "hello world"
    assert _normalize_whitespace("hello\rworld") == "helloworld"


def test_web_fetch_cache():
    """Cache key logic works for valid URLs."""
    from nano_openclaw.web_fetch import _write_cache, _read_cache, _cache_key
    
    _FETCH_CACHE.clear()
    
    key = _cache_key("http://test.example.com/page", "markdown", 5000)
    _write_cache(key, {"url": "http://test.example.com/page", "text": "cached"})
    
    result = _read_cache(key)
    assert result is not None
    assert result.get("cached") is True
    assert result["text"] == "cached"
