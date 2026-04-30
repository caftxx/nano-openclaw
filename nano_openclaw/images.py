"""Image parsing, loading, and description for nano-openclaw.

Mirrors the split between:
  - src/media/parse.ts          (parse_image_refs)
  - src/media/input-files.ts    (load_image — SSRF + size guards)
  - src/media-understanding/    (describe_image — Media Understanding path)

Two runtime paths (mirrors openclaw runner.ts:819-857):
  Native Vision      — no image_model configured; images go as base64 blocks to the main model.
  Media Understanding — image_model configured; images are described to text, injected into the prompt.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

_MIME_FROM_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB — mirrors DEFAULT_INPUT_IMAGE_MAX_BYTES

_EXT_GROUP = r"(?:png|jpe?g|gif|webp)"

# ---------------------------------------------------------------------------
# Regex patterns (mirrors openclaw parse.ts / images.ts detection patterns)
# ---------------------------------------------------------------------------

# 0. @-prefixed explicit attachment: @file.png  @./rel/path.png  @/abs/path.png
#    Relative paths are resolved against CWD. Safest pattern — user opts in explicitly.
_AT_IMAGE = re.compile(rf"@([^\s]+\.{_EXT_GROUP})", re.IGNORECASE)

# 1. Markdown: ![alt](src)  — high priority
_MARKDOWN_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)", re.IGNORECASE)

# 2. Bare HTTPS URL ending in image extension
_HTTPS_URL = re.compile(
    rf"https://\S+\.{_EXT_GROUP}(?:\?[^\s]*)?",
    re.IGNORECASE,
)

# 3. Local absolute path:  /foo/bar.png  |  C:\path\img.png  |  C:/path/img.png
_LOCAL_PATH = re.compile(
    rf"(?:^|(?<=\s))(?:[A-Za-z]:[/\\][^\s]+|/[^\s]+)\.{_EXT_GROUP}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_image_refs(text: str) -> tuple[str, list[str]]:
    """Extract image references from user text.

    Returns (cleaned_text, refs) where refs are paths/URLs in detection order
    and cleaned_text has the references removed.

    Security: rejects path-traversal ("../") and home-dir ("~") prefixes,
    mirroring openclaw parse.ts hasTraversalOrHomeDirPrefix.
    """
    refs: list[str] = []

    # Pass 0: @-prefixed explicit attachments — resolve relative paths to CWD.
    for m in _AT_IMAGE.finditer(text):
        raw = m.group(1)
        if not _is_safe_ref(raw):
            continue
        path = Path(raw)
        resolved = str(path if path.is_absolute() else Path(os.getcwd()) / path)
        if resolved not in refs:
            refs.append(resolved)
    text = _AT_IMAGE.sub("", text)

    # Pass 1: Markdown images
    for m in _MARKDOWN_IMG.finditer(text):
        src = m.group(1).strip()
        if _is_safe_ref(src):
            refs.append(src)
    text = _MARKDOWN_IMG.sub("", text)

    # Pass 2: Bare HTTPS URLs
    for m in _HTTPS_URL.finditer(text):
        url = m.group(0)
        if url not in refs and _is_safe_ref(url):
            refs.append(url)
    text = _HTTPS_URL.sub("", text)

    # Pass 3: Local absolute paths
    for m in _LOCAL_PATH.finditer(text):
        path = m.group(0).strip()
        if path not in refs and _is_safe_ref(path):
            refs.append(path)
    text = _LOCAL_PATH.sub("", text)

    return text.strip(), refs


def load_image(path_or_url: str) -> tuple[str, str]:
    """Load an image and return (base64_data, mime_type).

    Supports local absolute paths and HTTPS URLs.
    Raises ValueError / OSError / urllib.error.URLError on failure.
    """
    if path_or_url.startswith("https://"):
        return _load_remote(path_or_url)
    return _load_local(path_or_url)


def to_anthropic_image_block(b64: str, mime: str) -> dict[str, Any]:
    """Build an Anthropic-format image content block (Native Vision path)."""
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime, "data": b64},
    }


def describe_image(b64: str, mime: str, *, client: Any, model: str, api: str) -> str:
    """Call the image model to describe an image (Media Understanding path).

    Mirrors openclaw runner.entries.ts:528-564 describeImage call:
    sends a single-turn message with the image block + a describe prompt,
    returns the model's text description.
    """
    image_block = to_anthropic_image_block(b64, mime)
    messages = [
        {
            "role": "user",
            "content": [
                image_block,
                {"type": "text", "text": "Describe this image concisely."},
            ],
        }
    ]

    if api == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=512,
            messages=messages,
        )
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                return block.text
        return ""

    if api == "openai":
        # Convert image block to OpenAI image_url format
        data_url = f"data:{mime};base64,{b64}"
        oai_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "Describe this image concisely."},
                ],
            }
        ]
        response = client.chat.completions.create(
            model=model,
            max_tokens=512,
            messages=oai_messages,
        )
        return response.choices[0].message.content or ""

    raise ValueError(f"unsupported api for image description: {api!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_safe_ref(ref: str) -> bool:
    """Reject path-traversal and home-dir prefixes (mirrors openclaw parse.ts)."""
    if ref.startswith("~"):
        return False
    # Normalise backslashes for the traversal check
    normalised = ref.replace("\\", "/")
    if "../" in normalised or normalised == ".." or normalised.startswith("../"):
        return False
    return True


def _load_local(path_str: str) -> tuple[str, str]:
    path = Path(path_str)
    size = path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"image too large ({size:,} bytes > {_MAX_IMAGE_BYTES:,})")
    data = path.read_bytes()
    mime = _MIME_FROM_EXT.get(path.suffix.lower(), "image/jpeg")
    return base64.standard_b64encode(data).decode(), mime


def _load_remote(url: str) -> tuple[str, str]:
    _assert_safe_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "nano-openclaw/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — SSRF guard above
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_IMAGE_BYTES:
            raise ValueError(f"remote image too large ({content_length} bytes)")
        data = resp.read(_MAX_IMAGE_BYTES + 1)
        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(f"remote image exceeds {_MAX_IMAGE_BYTES:,} bytes")
        mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    return base64.standard_b64encode(data).decode(), mime


def _assert_safe_url(url: str) -> None:
    """SSRF guard — mirrors openclaw isBlockedRemoteMediaHostname."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Resolve to IP for network-range checks
    try:
        addr_str = socket.getaddrinfo(hostname, None)[0][4][0]
        addr = ipaddress.ip_address(addr_str)
    except Exception:
        raise ValueError(f"cannot resolve host: {hostname!r}")

    if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
        raise ValueError(f"SSRF: blocked address {addr} for host {hostname!r}")
