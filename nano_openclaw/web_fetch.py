"""Web fetch tool for nano-openclaw.

Mirrors openclaw's src/agents/tools/web-fetch.ts.
Fetches URL and extracts readable content using readability-lxml.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

import httpx
from readability import Document

from nano_openclaw.external_content import wrap_external_content
from nano_openclaw.ssrf_guard import assert_public_url, SsrfBlockedError


ExtractMode = Literal["markdown", "text"]

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_FETCH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 600  # 10 minutes
_DEFAULT_MAX_CHARS = 20_000
_DEFAULT_MAX_REDIRECTS = 3
_DEFAULT_TIMEOUT_SECONDS = 30


def _cache_key(url: str, extract_mode: ExtractMode, max_chars: int) -> str:
    return f"fetch:{url}:{extract_mode}:{max_chars}"


def _read_cache(key: str) -> dict[str, Any] | None:
    if key not in _FETCH_CACHE:
        return None
    ts, result = _FETCH_CACHE[key]
    if time.time() - ts > _CACHE_TTL_SECONDS:
        del _FETCH_CACHE[key]
        return None
    return {**result, "cached": True}


def _write_cache(key: str, result: dict[str, Any]) -> None:
    _FETCH_CACHE[key] = (time.time(), result)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _normalize_whitespace(text: str) -> str:
    """Normalize whitespace in extracted text."""
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _html_to_markdown(html: str) -> str:
    """Convert HTML to simple markdown-like format."""
    # Remove script, style, noscript
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.I)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html, flags=re.I)
    html = re.sub(r"<noscript[^>]*>[\s\S]*?</noscript>", "", html, flags=re.I)
    
    # Extract title
    title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.I)
    title = _normalize_whitespace(re.sub(r"<[^>]+>", "", title_match.group(1))) if title_match else None
    
    # Convert links: <a href="...">text</a> -> [text](url)
    html = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        lambda m: f"[{_normalize_whitespace(re.sub(r'<[^>]+>', '', m.group(2)))}]({m.group(1)})",
        html,
        flags=re.I,
    )
    
    # Convert headers
    html = re.sub(
        r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
        lambda m: f"\n{'#' * int(m.group(1))} {_normalize_whitespace(re.sub(r'<[^>]+>', '', m.group(2)))}\n",
        html,
        flags=re.I,
    )
    
    # Convert list items
    html = re.sub(
        r"<li[^>]*>([\s\S]*?)</li>",
        lambda m: f"\n- {_normalize_whitespace(re.sub(r'<[^>]+>', '', m.group(1)))}",
        html,
        flags=re.I,
    )
    
    # Convert block-level closings to newlines
    html = re.sub(r"</(p|div|section|article|header|footer|table|tr|ul|ol)>", "\n", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    
    return _normalize_whitespace(html), title


def _extract_html(html: str, extract_mode: ExtractMode) -> tuple[str, str | None, str]:
    """Extract readable content from HTML using readability-lxml.
    
    Returns:
        (text, title, extractor_name)
    """
    doc = Document(html)
    title = doc.title()
    summary_html = doc.summary()
    
    if extract_mode == "text":
        text = re.sub(r"<[^>]+>", "", summary_html)
        text = _normalize_whitespace(text)
    else:
        text, md_title = _html_to_markdown(summary_html)
        if not title and md_title:
            title = md_title
    
    return text, title, "readability"


def web_fetch(
    url: str,
    extract_mode: ExtractMode = "markdown",
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch URL and extract readable content.
    
    Args:
        url: HTTP/HTTPS URL to fetch
        extract_mode: "markdown" or "text"
        max_chars: Maximum characters to return
        max_redirects: Maximum redirect hops
        timeout_seconds: Request timeout
    
    Returns:
        Dict with url, status, contentType, title, text, extractor, truncated, etc.
    """
    # Check cache
    key = _cache_key(url, extract_mode, max_chars)
    cached = _read_cache(key)
    if cached:
        return cached
    
    # SSRF check
    try:
        assert_public_url(url)
    except SsrfBlockedError as e:
        return {
            "url": url,
            "status": 403,
            "error": str(e),
            "text": f"[SSRF blocked: {e}]",
        }
    except ValueError as e:
        return {
            "url": url,
            "status": 400,
            "error": str(e),
            "text": f"[Invalid URL: {e}]",
        }
    
    start = time.time()
    
    # Fetch with httpx
    try:
        with httpx.Client(
            follow_redirects=True,
            max_redirects=max_redirects,
            timeout=timeout_seconds,
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept": "text/html, application/json, text/markdown, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        return {
            "url": url,
            "status": 408,
            "error": "Request timeout",
            "text": "[Request timed out]",
        }
    except httpx.RequestError as e:
        return {
            "url": url,
            "status": 503,
            "error": str(e),
            "text": f"[Network error: {e}]",
        }
    
    final_url = str(resp.url)
    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_type_clean = content_type.split(";")[0].strip().lower()
    
    if not resp.is_success:
        error_text = resp.text[:4000] if resp.text else resp.status_text
        return {
            "url": url,
            "finalUrl": final_url,
            "status": resp.status_code,
            "error": f"HTTP {resp.status_code}: {resp.status_text}",
            "text": f"[HTTP {resp.status_code}: {resp.status_text}]\n{error_text[:1000]}",
        }
    
    # Extract content based on type
    title: str | None = None
    extractor = "raw"
    
    try:
        body = resp.text
    except UnicodeDecodeError:
        return {
            "url": url,
            "finalUrl": final_url,
            "status": 200,
            "contentType": content_type_clean,
            "error": "Unable to decode response as text",
            "text": "[Binary or unsupported encoding]",
        }
    
    if "text/markdown" in content_type_clean:
        extractor = "cf-markdown"
        text = body
        if extract_mode == "text":
            text = _normalize_whitespace(text)
    
    elif "text/html" in content_type_clean:
        try:
            text, title, extractor = _extract_html(body, extract_mode)
        except Exception:
            text, raw_title = _html_to_markdown(body)
            title = title or raw_title
            extractor = "regex-fallback"
    
    elif "application/json" in content_type_clean:
        try:
            text = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
            extractor = "json"
        except json.JSONDecodeError:
            text = body
    
    else:
        text = body
    
    # Truncate
    text, truncated = _truncate(text, max_chars)
    
    # Wrap for security
    wrapped = wrap_external_content(text, "web_fetch")
    
    result = {
        "url": url,
        "finalUrl": final_url,
        "status": resp.status_code,
        "contentType": content_type_clean,
        "title": title,
        "extractMode": extract_mode,
        "extractor": extractor,
        "truncated": truncated,
        "length": len(wrapped),
        "rawLength": len(text),
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tookMs": int((time.time() - start) * 1000),
        "text": wrapped,
    }
    
    _write_cache(key, result)
    return result
