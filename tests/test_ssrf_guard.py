"""Tests for SSRF guard."""

import pytest
from nano_openclaw.ssrf_guard import assert_public_url, SsrfBlockedError


def test_ssrf_localhost_blocked():
    with pytest.raises(SsrfBlockedError):
        assert_public_url("http://localhost/test")


def test_ssrf_metadata_blocked():
    with pytest.raises(SsrfBlockedError):
        assert_public_url("http://metadata.google.internal/computeMetadata/v1/")


def test_ssrf_private_ip_blocked():
    with pytest.raises(SsrfBlockedError):
        assert_public_url("http://127.0.0.1/admin")
    with pytest.raises(SsrfBlockedError):
        assert_public_url("http://10.0.0.1/internal")
    with pytest.raises(SsrfBlockedError):
        assert_public_url("http://192.168.1.1/config")
    with pytest.raises(SsrfBlockedError):
        assert_public_url("http://172.16.0.1/admin")


def test_ssrf_invalid_scheme():
    with pytest.raises(ValueError, match="Invalid URL scheme"):
        assert_public_url("ftp://example.com/file")
    with pytest.raises(ValueError, match="Invalid URL scheme"):
        assert_public_url("file:///etc/passwd")


def test_ssrf_no_hostname():
    with pytest.raises(ValueError, match="Invalid URL: no hostname"):
        assert_public_url("http:///path")


def test_ssrf_public_url_allowed():
    url = assert_public_url("https://example.com")
    assert url == "https://example.com"


def test_ssrf_public_url_with_path():
    url = assert_public_url("https://docs.python.org/3/library/")
    assert url == "https://docs.python.org/3/library/"
