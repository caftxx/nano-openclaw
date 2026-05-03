"""Web search tool for nano-openclaw.

Uses DuckDuckGo search (free, no API key required).
Mirrors openclaw's web search provider pattern.
"""

from __future__ import annotations

import time
from typing import Any

from ddgs import DDGS

from nano_openclaw.external_content import wrap_external_content


_SEARCH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 600  # 10 minutes
_DEFAULT_MAX_RESULTS = 10
_DEFAULT_REGION = "wt-wt"


def _cache_key(query: str, max_results: int, region: str) -> str:
    return f"search:{query}:{max_results}:{region}"


def _read_cache(key: str) -> dict[str, Any] | None:
    if key not in _SEARCH_CACHE:
        return None
    ts, result = _SEARCH_CACHE[key]
    if time.time() - ts > _CACHE_TTL_SECONDS:
        del _SEARCH_CACHE[key]
        return None
    return {**result, "cached": True}


def _write_cache(key: str, result: dict[str, Any]) -> None:
    _SEARCH_CACHE[key] = (time.time(), result)


def web_search(
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    region: str = _DEFAULT_REGION,
) -> dict[str, Any]:
    """Search the web using DuckDuckGo.
    
    Args:
        query: Search query string
        max_results: Maximum number of results (default 10, max 50)
        region: DuckDuckGo region code (default wt-wt = worldwide)
            Examples: us-en, uk-en, de-de, fr-fr, zh-cn, ja-jp
    
    Returns:
        Dict with query, results[], count, provider, text summary
    """
    if not query or not query.strip():
        return {
            "query": query,
            "error": "Empty query",
            "results": [],
            "count": 0,
            "provider": "duckduckgo",
            "text": "[Search failed: empty query]",
        }
    
    # Check cache
    key = _cache_key(query, max_results, region)
    cached = _read_cache(key)
    if cached:
        return cached
    
    start = time.time()
    
    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, region=region, max_results=max_results))
    except Exception as e:
        return {
            "query": query,
            "error": str(e),
            "results": [],
            "count": 0,
            "provider": "duckduckgo",
            "text": f"[Search failed: {e}]",
        }
    
    # Format results
    results = []
    for r in raw_results:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
        })
    
    # Build summary text for model
    text_lines = [f"Search results for: {query}", ""]
    for i, r in enumerate(results, 1):
        text_lines.append(f"{i}. {r['title']}")
        text_lines.append(f"   URL: {r['url']}")
        snippet = r['snippet']
        text_lines.append(f"   {snippet[:200]}{'...' if len(snippet) > 200 else ''}")
        text_lines.append("")
    
    if not results:
        text_lines.append("No results found.")
    
    text = wrap_external_content("\n".join(text_lines), "web_search")
    
    result = {
        "query": query,
        "results": results,
        "count": len(results),
        "provider": "duckduckgo",
        "region": region,
        "tookMs": int((time.time() - start) * 1000),
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "text": text,
    }
    
    _write_cache(key, result)
    return result
