"""Tests for web_search tool."""

from unittest.mock import MagicMock, patch

from nano_openclaw.web_search import web_search, _SEARCH_CACHE

_FAKE_RESULTS = [
    {"title": "Result 1", "href": "https://example.com/1", "body": "First result snippet"},
    {"title": "Result 2", "href": "https://example.com/2", "body": "Second result snippet"},
    {"title": "Result 3", "href": "https://example.com/3", "body": "Third result snippet"},
]


def _mock_ddgs():
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.text.return_value = _FAKE_RESULTS
    return mock


def test_web_search_empty_query():
    result = web_search("")
    assert result["count"] == 0
    assert "error" in result
    assert "Empty query" in result["error"]


@patch("nano_openclaw.web_search.DDGS")
def test_web_search_cache(MockDDGS):
    """Repeated query uses cache."""
    MockDDGS.return_value = _mock_ddgs()
    _SEARCH_CACHE.clear()

    r1 = web_search("test query", max_results=3)
    assert r1.get("cached") is None

    r2 = web_search("test query", max_results=3)
    assert r2.get("cached") is True


@patch("nano_openclaw.web_search.DDGS")
def test_web_search_returns_expected_fields(MockDDGS):
    MockDDGS.return_value = _mock_ddgs()
    _SEARCH_CACHE.clear()

    result = web_search("Python programming", max_results=5)
    assert "query" in result
    assert "results" in result
    assert "count" in result
    assert "provider" in result
    assert result["provider"] == "duckduckgo"
    assert result["query"] == "Python programming"
    assert result["count"] <= 5
    assert "<EXTERNAL_UNTRUSTED_CONTENT source=web_search>" in result["text"]
