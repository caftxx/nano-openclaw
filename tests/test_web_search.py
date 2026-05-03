"""Tests for web_search tool."""

from nano_openclaw.web_search import web_search, _SEARCH_CACHE


def test_web_search_empty_query():
    result = web_search("")
    assert result["count"] == 0
    assert "error" in result
    assert "Empty query" in result["error"]


def test_web_search_cache():
    """Repeated query uses cache."""
    _SEARCH_CACHE.clear()
    
    r1 = web_search("test query", max_results=3)
    assert r1.get("cached") is None
    
    r2 = web_search("test query", max_results=3)
    assert r2.get("cached") is True


def test_web_search_returns_expected_fields():
    result = web_search("Python programming", max_results=5)
    assert "query" in result
    assert "results" in result
    assert "count" in result
    assert "provider" in result
    assert result["provider"] == "duckduckgo"
    assert result["query"] == "Python programming"
    assert result["count"] <= 5
    assert "<EXTERNAL_UNTRUSTED_CONTENT source=web_search>" in result["text"]
