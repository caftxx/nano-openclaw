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
import io
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image

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

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB — API limit is 6MB, leave margin
_COMPRESSED_TARGET_BYTES = 4 * 1024 * 1024  # 4 MB target after compression
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB — max download size before compression

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


async def describe_image(b64: str, mime: str, *, client: Any, model: str, api: str) -> str:
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
        response = await client.messages.create(
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
        response = await client.chat.completions.create(
            model=model,
            max_tokens=512,
            messages=oai_messages,
        )
        return response.choices[0].message.content or ""

    raise ValueError(f"unsupported api for image description: {api!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compress_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Compress image to fit within _COMPRESSED_TARGET_BYTES.

    Strategy:
    - JPEG/WebP: reduce quality progressively
    - PNG: convert to JPEG (better compression for photos)
    - Resize if quality reduction isn't enough
    """
    img = Image.open(io.BytesIO(data))

    # Determine output format - convert PNG to JPEG for better compression
    output_mime = mime
    if mime == "image/png":
        output_mime = "image/jpeg"
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

    # Quality levels to try (progressively lower)
    quality_levels = [85, 70, 55, 40]

    for quality in quality_levels:
        buffer = io.BytesIO()
        if output_mime == "image/jpeg":
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
        elif output_mime == "image/webp":
            img.save(buffer, format="WEBP", quality=quality)
        else:
            img.save(buffer, format=img.format or "JPEG", quality=quality)

        if buffer.tell() <= _COMPRESSED_TARGET_BYTES:
            return buffer.getvalue(), output_mime

    # If still too large, resize progressively
    current_img = img.copy()
    scale = 0.5

    while scale >= 0.1:
        new_width = int(img.width * scale)
        new_height = int(img.height * scale)
        resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        if output_mime == "image/jpeg":
            resized.save(buffer, format="JPEG", quality=70, optimize=True)
        elif output_mime == "image/webp":
            resized.save(buffer, format="WEBP", quality=70)
        else:
            resized.save(buffer, format="JPEG", quality=70, optimize=True)
            output_mime = "image/jpeg"

        if buffer.tell() <= _COMPRESSED_TARGET_BYTES:
            return buffer.getvalue(), output_mime

        scale -= 0.1

    raise ValueError(
        f"无法压缩图片到 {_COMPRESSED_TARGET_BYTES // 1024 // 1024}MB 以内。"
        f"请手动压缩图片或使用更小的图片。"
    )


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
    data = path.read_bytes()
    mime = _MIME_FROM_EXT.get(path.suffix.lower(), "image/jpeg")

    if size > _MAX_IMAGE_BYTES:
        try:
            data, mime = _compress_image(data, mime)
        except Exception as e:
            raise ValueError(
                f"图片过大 ({size // 1024 // 1024}MB > {_MAX_IMAGE_BYTES // 1024 // 1024}MB) 且自动压缩失败: {e}"
            ) from e

    return base64.standard_b64encode(data).decode(), mime


def _load_remote(url: str) -> tuple[str, str]:
    _assert_safe_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "nano-openclaw/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — SSRF guard above
        content_length = resp.headers.get("Content-Length")

        # Reject obviously huge files
        if content_length and int(content_length) > _MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"远程图片过大 ({int(content_length) // 1024 // 1024}MB > {_MAX_DOWNLOAD_BYTES // 1024 // 1024}MB)"
            )

        data = resp.read(_MAX_DOWNLOAD_BYTES + 1)
        if len(data) > _MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"远程图片超过 {_MAX_DOWNLOAD_BYTES // 1024 // 1024}MB，无法处理"
            )

        mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()

        # Compress if needed
        if len(data) > _MAX_IMAGE_BYTES:
            try:
                data, mime = _compress_image(data, mime)
            except Exception as e:
                raise ValueError(
                    f"远程图片过大且自动压缩失败: {e}"
                ) from e

    return base64.standard_b64encode(data).decode(), mime


def _assert_safe_url(url: str) -> None:
    """SSRF guard — mirrors openclaw isBlockedRemoteMediaHostname.

    Uses explicit network ranges instead of addr.is_private to avoid
    Python 3.11+'s overly broad definition (which incorrectly blocks CDN
    addresses in 198.18.0.0/15 and 100.64.0.0/10).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Resolve to IP for network-range checks
    try:
        addr_str = socket.getaddrinfo(hostname, None)[0][4][0]
        addr = ipaddress.ip_address(addr_str)
    except Exception:
        raise ValueError(f"cannot resolve host: {hostname!r}")

    if _is_blocked_addr(addr):
        raise ValueError(f"SSRF: blocked address {addr} for host {hostname!r}")


# Explicit SSRF blocklist — mirrors openclaw isBlockedRemoteMediaHostname.
# We do NOT use addr.is_private because Python 3.11 expanded that to include
# benchmarking (198.18.0.0/15) and shared CGN (100.64.0.0/10) ranges that
# are legitimately used by CDNs such as Cloudflare and Fastly.
_BLOCKED_NETWORKS_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),        # "this" network
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918 private
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local (APIPA)
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918 private
    ipaddress.ip_network("192.0.0.0/29"),      # IETF protocol assignments
    ipaddress.ip_network("240.0.0.0/4"),       # reserved / future use
    ipaddress.ip_network("255.255.255.255/32"),# broadcast
]
_BLOCKED_NETWORKS_V6 = [
    ipaddress.ip_network("::1/128"),           # loopback
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local
]


def _is_blocked_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    nets = _BLOCKED_NETWORKS_V4 if addr.version == 4 else _BLOCKED_NETWORKS_V6
    return any(addr in net for net in nets)
