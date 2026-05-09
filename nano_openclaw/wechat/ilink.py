"""Async iLink WeChat API client.

Mirrors hermesclaw's iLink functions but uses httpx.AsyncClient for asyncio
compatibility. Supports long-polling, text send, typing indicators, and image
download.
"""

from __future__ import annotations

import base64
import secrets
from typing import Any

import httpx

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

ILINK_VER = "2.1.7"
ILINK_CV = "65547"
T, IMG, VO, VIDEO, FILE = 1, 2, 3, 4, 5   # item type: text, image, voice, video, file
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # 文件上限更大


def _headers(token: str, body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body)),
        "iLink-App-Id": "",
        "iLink-App-ClientVersion": ILINK_CV,
        "Authorization": f"Bearer {token}",
    }


async def _post(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    body: dict[str, Any],
    token: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    import json as _json
    raw = _json.dumps(body).encode()
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    resp = await client.post(url, content=raw, headers=_headers(token, raw), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


async def get_updates(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    buf: str = "",
    timeout: int = 35,
) -> dict[str, Any]:
    """Long-poll iLink for incoming messages."""
    try:
        return await _post(
            client,
            base_url,
            "ilink/bot/getupdates",
            {"get_updates_buf": buf, "base_info": {"channel_version": ILINK_VER}},
            token,
            timeout=timeout + 5,
        )
    except httpx.TimeoutException:
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}


async def send_text(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    to_user: str,
    text: str,
    ctx: str | None = None,
) -> None:
    """Send a text reply to a WeChat user via iLink."""
    msg: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user,
        "client_id": "nano-" + secrets.token_hex(8),
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": T, "text_item": {"text": text}}],
    }
    if ctx:
        msg["context_token"] = ctx
    await _post(
        client,
        base_url,
        "ilink/bot/sendmessage",
        {"msg": msg, "base_info": {"channel_version": ILINK_VER}},
        token,
    )


async def get_typing_ticket(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    to_user: str,
    ctx: str | None = None,
) -> str:
    """Fetch a typing ticket for the given user."""
    body: dict[str, Any] = {
        "ilink_user_id": to_user,
        "base_info": {"channel_version": ILINK_VER},
    }
    if ctx:
        body["context_token"] = ctx
    resp = await _post(client, base_url, "ilink/bot/getconfig", body, token)
    return resp.get("typing_ticket", "")


async def send_typing(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    to_user: str,
    ticket: str,
    status: int = 1,
) -> None:
    """Send typing status: 1=active, 2=done."""
    await _post(
        client,
        base_url,
        "ilink/bot/sendtyping",
        {
            "ilink_user_id": to_user,
            "typing_ticket": ticket,
            "status": status,
            "base_info": {"channel_version": ILINK_VER},
        },
        token,
    )


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    token: str,
) -> tuple[bytes, str]:
    """Download an image from iLink CDN; return (bytes, mime_type)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": "",
        "iLink-App-ClientVersion": ILINK_CV,
    }
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url, headers=headers, timeout=30.0) as resp:
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
        async for chunk in resp.aiter_bytes(65536):
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
            chunks.append(chunk)
    return b"".join(chunks), mime or "image/jpeg"


async def download_wechat_image(
    client: httpx.AsyncClient,
    image_item: dict[str, Any],
    token: str,
) -> tuple[bytes, str]:
    """Download and decrypt a WeChat image.

    image_item contains:
      - aeskey: hex string (16 bytes key)
      - media.full_url: download URL with encrypted_query_param

    Returns (decrypted_bytes, mime_type).
    """
    aeskey = image_item.get("aeskey", "")
    media = image_item.get("media", {})
    url = media.get("full_url", "")

    if not url:
        raise ValueError("no download URL in image_item")
    if not aeskey:
        # No encryption, download directly
        return await download_image(client, url, token)

    # Download encrypted data
    encrypted_data, mime = await download_image(client, url, token)

    # Decrypt
    try:
        decrypted = _decrypt_wechat_media(encrypted_data, aeskey)
        return decrypted, mime
    except Exception:
        # If decryption fails, return raw data (might already be unencrypted)
        return encrypted_data, mime


async def download_wechat_file(
    client: httpx.AsyncClient,
    file_item: dict[str, Any],
    token: str,
) -> tuple[bytes, str, str]:
    """Download and decrypt a WeChat file (PDF, doc, etc).

    file_item contains:
      - aes_key: base64-encoded AES key (optional)
      - file_url or media.full_url: download URL
      - file_name: original filename

    Returns (decrypted_bytes, mime_type, filename).
    """
    # aes_key is base64-encoded hex string (32 chars), can be at top level or in media
    aes_key_b64 = file_item.get("aes_key") or file_item.get("media", {}).get("aes_key", "")
    aeskey_hex = ""
    if aes_key_b64:
        try:
            # Decode base64 -> hex string like "f25e0b8d..."
            aeskey_hex = base64.b64decode(aes_key_b64).decode()
        except Exception:
            pass

    url = file_item.get("file_url") or file_item.get("media", {}).get("full_url", "")
    filename = file_item.get("file_name", "wechat-file")

    if not url:
        raise ValueError("no download URL in file_item")

    headers = {
        "Authorization": f"Bearer {token}",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": "",
        "iLink-App-ClientVersion": ILINK_CV,
    }

    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url, headers=headers, timeout=60.0) as resp:
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip()
        async for chunk in resp.aiter_bytes(65536):
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
            chunks.append(chunk)

    data = b"".join(chunks)

    # Decrypt if aes_key present
    if aeskey_hex:
        try:
            data = _decrypt_wechat_media(data, aeskey_hex)
        except Exception:
            pass  # Keep raw data if decryption fails

    # Detect PDF from content (fix mime if needed)
    if data[:4] == b"%PDF":
        mime = "application/pdf"

    return data, mime or "application/octet-stream", filename


def extract_text(item_list: list[dict[str, Any]]) -> str:
    """Extract combined text from iLink item_list (text + voice transcription)."""
    parts: list[str] = []
    for it in item_list:
        tp = it.get("type", 0)
        if tp == T:
            x = it.get("text_item", {}).get("text", "")
            if x:
                parts.append(x)
        elif tp == VO:
            x = it.get("voice_item", {}).get("text", "")
            if x:
                parts.append(f'[用户发送了语音消息，内容："{x}"]')
    return "\n".join(parts).strip()


def _first_url(obj: Any, _depth: int = 0) -> str:
    """Recursively find the first http(s) URL in a nested dict/list."""
    if _depth > 6:
        return ""
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
            found = _first_url(v, _depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_url(item, _depth + 1)
            if found:
                return found
    return ""


def extract_image_urls(item_list: list[dict[str, Any]]) -> list[str]:
    """Extract image URLs from iLink item_list (type=2 image items)."""
    urls: list[str] = []
    for it in item_list:
        if it.get("type") == IMG or "image_item" in it:
            url = _first_url(it.get("image_item", it))
            if url:
                urls.append(url)
    return urls


def extract_image_items(item_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract full image_item dicts with aeskey for decryption."""
    items: list[dict[str, Any]] = []
    for it in item_list:
        if it.get("type") == IMG or "image_item" in it:
            img_item = it.get("image_item", it)
            if img_item:
                items.append(img_item)
    return items


def extract_file_items(item_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract full file_item dicts (type=5 or type=4 with file_item key).

    Note: Some iLink clients send document files with type=4 (video),
    so we look for 'file_item' key regardless of type number.
    """
    items: list[dict[str, Any]] = []
    for it in item_list:
        if "file_item" in it:
            file_item = it.get("file_item")
            if file_item:
                items.append(file_item)
        elif it.get("type") == FILE:
            # type=5 is explicitly FILE
            items.append(it)
    return items


def _decrypt_wechat_media(data: bytes, aeskey_hex: str) -> bytes:
    """Decrypt WeChat AES-encrypted media (image or file).

    aeskey_hex is a 32-char hex string, i.e. 16 bytes AES-128 key.
    WeChat C2C CDN media uses AES-128-ECB with PKCS7 padding.
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required for WeChat image decryption")

    key = bytes.fromhex(aeskey_hex)

    if len(key) != 16:
        raise ValueError(f"invalid WeChat image AES key length: {len(key)}")

    if len(data) % 16 != 0:
        raise ValueError(f"invalid encrypted image length: {len(data)}, not multiple of 16")

    cipher = Cipher(
        algorithms.AES(key),
        modes.ECB(),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    decrypted_padded = decryptor.update(data) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    decrypted = unpadder.update(decrypted_padded) + unpadder.finalize()

    return decrypted
