"""Async iLink WeChat API client.

Mirrors hermesclaw's iLink functions but uses httpx.AsyncClient for asyncio
compatibility. Supports long-polling, text send, typing indicators, image
download, and QR-code login.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode

import httpx

from nano_openclaw.logger import get_logger

log = get_logger(__name__)

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

ILINK_VER = "2.1.7"
ILINK_CV = "65547"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
T, IMG, VO, FILE, VIDEO = 1, 2, 3, 4, 5   # item type: text, image, voice, file, video
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # 文件上限更大


def _headers(token: str, body: bytes) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body)),
        "iLink-App-Id": "",
        "iLink-App-ClientVersion": ILINK_CV,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


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


async def _get(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    token: str = "",
    query: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Generic GET helper used by QR endpoints.

    Mirrors ``_post`` but for the few iLink endpoints (qrcode lifecycle) that
    use GET. Token may be empty — we're often pre-login here.
    """
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    if query:
        url += "?" + urlencode(query)
    headers = _headers(token, b"")
    headers.pop("Content-Length", None)  # GET has no body
    if extra_headers:
        headers.update(extra_headers)
    resp = await client.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def is_session_expired(resp: dict[str, Any]) -> bool:
    """Detect a session-expired response from getUpdates / sendmessage etc.

    iLink returns ``ret == -14`` or ``errcode == -14`` once the bot token is
    revoked or the server-side session times out. Mirrors SDK
    ``APIError.is_session_expired()``.
    """
    return resp.get("ret") == -14 or resp.get("errcode") == -14


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
        log.warning("ilink.get_updates.timeout", "get_updates request timed out")
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


def _cdn_download_url(encrypt_query_param: str) -> str:
    return DEFAULT_CDN_BASE_URL + "/download?encrypted_query_param=" + quote(encrypt_query_param, safe="")


def _media_download_url(media: dict[str, Any]) -> str:
    url = media.get("full_url", "")
    if url:
        return url
    encrypt_query_param = media.get("encrypt_query_param", "")
    if encrypt_query_param:
        return _cdn_download_url(encrypt_query_param)
    return ""


def _is_hex_key(s: str) -> bool:
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def _parse_media_aes_key(aes_key: str) -> bytes:
    """Parse iLink media AES keys in hex, base64(raw), or base64(hex) form."""
    if len(aes_key) == 32 and _is_hex_key(aes_key):
        return bytes.fromhex(aes_key)

    raw = base64.b64decode(aes_key)
    if len(raw) == 32:
        text = raw.decode("ascii", errors="replace")
        if _is_hex_key(text):
            return bytes.fromhex(text)
    if len(raw) == 16:
        return raw
    raise ValueError(f"unexpected AES key length: {len(raw)} bytes")


def _decrypt_wechat_media_with_key(data: bytes, key: bytes) -> bytes:
    """Decrypt WeChat AES-encrypted media using a 16-byte AES-128 key."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required for WeChat media decryption")

    if len(key) != 16:
        raise ValueError(f"invalid WeChat media AES key length: {len(key)}")

    if len(data) % 16 != 0:
        raise ValueError(f"invalid encrypted media length: {len(data)}, not multiple of 16")

    cipher = Cipher(
        algorithms.AES(key),
        modes.ECB(),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    decrypted_padded = decryptor.update(data) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    return unpadder.update(decrypted_padded) + unpadder.finalize()


async def download_wechat_image(
    client: httpx.AsyncClient,
    image_item: dict[str, Any],
    token: str,
) -> tuple[bytes, str]:
    """Download and decrypt a WeChat image.

    image_item contains:
      - aeskey: hex string, or media.aes_key in SDK CDNMedia format
      - media.full_url or media.encrypt_query_param

    Returns (decrypted_bytes, mime_type).
    """
    media = image_item.get("media") or {}
    aeskey = image_item.get("aeskey", "") or media.get("aes_key", "")
    url = _media_download_url(media) or image_item.get("url", "")

    if not url:
        raise ValueError("no download URL in image_item")
    if not aeskey:
        # No encryption, download directly
        return await download_image(client, url, token)

    # Download encrypted data
    encrypted_data, mime = await download_image(client, url, token)

    # Decrypt
    try:
        decrypted = _decrypt_wechat_media_with_key(encrypted_data, _parse_media_aes_key(aeskey))
        return decrypted, mime
    except Exception as e:
        # If decryption fails, return raw data (might already be unencrypted)
        log.warning("ilink.image.decrypt.error", f"Image decryption failed: {e}")
        return encrypted_data, mime


async def download_wechat_file(
    client: httpx.AsyncClient,
    file_item: dict[str, Any],
    token: str,
) -> tuple[bytes, str, str]:
    """Download and decrypt a WeChat file (PDF, doc, etc).

    file_item contains:
      - aes_key or media.aes_key: AES key in SDK CDNMedia format (optional)
      - file_url, media.full_url, or media.encrypt_query_param: download URL
      - file_name: original filename

    Returns (decrypted_bytes, mime_type, filename).
    """
    media = file_item.get("media") or {}
    aes_key = file_item.get("aes_key") or media.get("aes_key", "")
    url = file_item.get("file_url") or _media_download_url(media)
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
    if aes_key:
        try:
            data = _decrypt_wechat_media_with_key(data, _parse_media_aes_key(aes_key))
        except Exception as e:
            log.warning("ilink.file.decrypt.error", f"File decryption failed: {e}")
            pass  # Keep raw data if decryption fails

    # Detect PDF from content (fix mime if needed)
    if data[:4] == b"%PDF":
        mime = "application/pdf"

    return data, mime or "application/octet-stream", filename


def _item_text(it: dict[str, Any]) -> str:
    """Extract text from one iLink item, excluding quoted-message metadata."""
    tp = it.get("type", 0)
    if tp == T:
        return it.get("text_item", {}).get("text", "")
    if tp == VO:
        x = it.get("voice_item", {}).get("text", "")
        if x:
            return f'[用户发送了语音消息，内容："{x}"]'
        return "[用户发送了语音消息]"
    if tp == IMG or "image_item" in it:
        return "[用户发送了图片]"
    if tp == FILE or "file_item" in it:
        filename = it.get("file_item", it).get("file_name", "")
        if filename:
            return f"[用户发送了文件：{filename}]"
        return "[用户发送了文件]"
    if tp == VIDEO or "video_item" in it:
        return "[用户发送了视频]"
    return ""


def _ref_msg_text(ref_msg: Any, depth: int) -> str:
    if depth > 3 or not isinstance(ref_msg, dict):
        return ""

    message_item = ref_msg.get("message_item")
    title = ref_msg.get("title", "")
    parts: list[str] = []
    if title:
        parts.append(f"标题: {title}")
    if isinstance(message_item, dict):
        item_text = _extract_text([message_item], depth + 1)
        if item_text:
            parts.append(item_text)
    return "\n".join(parts)


def _extract_text(item_list: list[dict[str, Any]], depth: int = 0) -> str:
    parts: list[str] = []
    for it in item_list:
        x = _item_text(it)
        if x:
            parts.append(x)

        ref_text = _ref_msg_text(it.get("ref_msg"), depth)
        if ref_text:
            parts.append(f"[引用消息]\n{ref_text}\n[/引用消息]")
    return "\n".join(parts).strip()


def extract_text(item_list: list[dict[str, Any]]) -> str:
    """Extract combined text from iLink item_list, including quoted messages."""
    return _extract_text(item_list)


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
    """Extract full file_item dicts (type=4 or any item with file_item key)."""
    items: list[dict[str, Any]] = []
    for it in item_list:
        if "file_item" in it:
            file_item = it.get("file_item")
            if file_item:
                items.append(file_item)
        elif it.get("type") == FILE:
            items.append(it)
    return items


# --- QR-code login ----------------------------------------------------------

QR_MAX_REFRESH = 3
QR_POLL_TIMEOUT = 40.0  # seconds — server long-polls roughly this long
QR_LOGIN_DEFAULT_TIMEOUT = 8 * 60.0  # 8 minutes


@dataclass
class LoginResult:
    """Outcome of :func:`login_with_qr`. ``connected`` toggles success/fail."""
    connected: bool = False
    bot_token: str = ""
    bot_id: str = ""
    base_url: str = ""
    user_id: str = ""
    message: str = ""


@dataclass
class LoginCallbacks:
    """Async/sync callbacks invoked during :func:`login_with_qr`.

    ``on_qrcode`` receives the raw ``qrcode_img_content`` payload (typically a
    URL string the QR encodes); the caller decides how to render it.
    Each callback may be a regular function or an async coroutine.
    """
    on_qrcode: Callable[[str], Any] | None = None
    on_scanned: Callable[[], Any] | None = None
    on_expired: Callable[[int, int], Any] | None = None


async def _maybe_await(fn: Callable[..., Any] | None, *args: Any) -> None:
    if fn is None:
        return
    try:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001
        log.warning("ilink.login.callback.error", f"login callback raised: {exc}")


async def fetch_qr_code(
    client: httpx.AsyncClient,
    base_url: str,
    bot_type: str = "3",
) -> dict[str, Any]:
    """Request a fresh login QR code. Returns ``{qrcode, qrcode_img_content}``."""
    return await _get(
        client, base_url, "ilink/bot/get_bot_qrcode",
        query={"bot_type": bot_type}, timeout=15.0,
    )


async def poll_qr_status(
    client: httpx.AsyncClient,
    base_url: str,
    qrcode: str,
) -> dict[str, Any]:
    """Long-poll the scan status of a QR code.

    Server hangs the request up to ~40s. Network failures degrade to ``wait``
    so the outer loop keeps polling rather than aborting.
    """
    try:
        return await _get(
            client, base_url, "ilink/bot/get_qrcode_status",
            query={"qrcode": qrcode},
            extra_headers={"iLink-App-ClientVersion": "1"},
            timeout=QR_POLL_TIMEOUT + 5.0,
        )
    except (httpx.TimeoutException, httpx.HTTPError):
        return {"status": "wait"}


async def login_with_qr(
    client: httpx.AsyncClient,
    base_url: str,
    callbacks: LoginCallbacks | None = None,
    timeout: float = QR_LOGIN_DEFAULT_TIMEOUT,
) -> LoginResult:
    """Run the full QR-code login state machine.

    States: ``wait`` -> ``scaned`` -> ``confirmed`` (success) or ``expired``
    (refresh QR up to :data:`QR_MAX_REFRESH` times). Returns once the user
    confirms on their phone, the QR-refresh budget is exhausted, or
    ``timeout`` elapses.
    """
    callbacks = callbacks or LoginCallbacks()
    deadline = time.monotonic() + timeout

    qr = await fetch_qr_code(client, base_url)
    current_qr = qr.get("qrcode", "")
    if not current_qr:
        return LoginResult(message="server returned empty qrcode")
    await _maybe_await(callbacks.on_qrcode, qr.get("qrcode_img_content", ""))

    scanned_notified = False
    refresh_count = 1

    while True:
        if time.monotonic() > deadline:
            return LoginResult(message="login timeout")

        status_resp = await poll_qr_status(client, base_url, current_qr)
        status = status_resp.get("status", "")

        if status == "wait":
            await asyncio.sleep(1.0)
            continue
        if status == "scaned":
            if not scanned_notified:
                scanned_notified = True
                await _maybe_await(callbacks.on_scanned)
            await asyncio.sleep(1.0)
            continue
        if status == "expired":
            refresh_count += 1
            if refresh_count > QR_MAX_REFRESH:
                return LoginResult(message="QR code expired too many times")
            await _maybe_await(callbacks.on_expired, refresh_count, QR_MAX_REFRESH)
            qr = await fetch_qr_code(client, base_url)
            current_qr = qr.get("qrcode", "")
            scanned_notified = False
            await _maybe_await(callbacks.on_qrcode, qr.get("qrcode_img_content", ""))
            await asyncio.sleep(1.0)
            continue
        if status == "confirmed":
            bot_id = status_resp.get("ilink_bot_id", "")
            if not bot_id:
                return LoginResult(message="server did not return bot ID")
            return LoginResult(
                connected=True,
                bot_token=status_resp.get("bot_token", ""),
                bot_id=bot_id,
                base_url=status_resp.get("baseurl", ""),
                user_id=status_resp.get("ilink_user_id", ""),
                message="connected",
            )

        # Unknown status → keep polling but don't tight-loop.
        log.debug("ilink.qr.unknown_status", f"unrecognized status={status!r}")
        await asyncio.sleep(1.0)


def print_qrcode(content: str) -> None:
    """Render a QR code as ASCII to stdout. Falls back to printing the URL.

    Lazy-imports ``qrcode`` so missing the dep degrades gracefully — caller
    can still scan by typing the URL into a phone QR generator if needed.
    """
    if not content:
        print("(empty qrcode payload)")
        return
    try:
        import qrcode  # type: ignore[import-not-found]
    except ImportError:
        print(f"qrcode package not installed; QR payload:\n  {content}")
        print("install with:  uv add qrcode  (or pip install qrcode)")
        return
    qr = qrcode.QRCode(
        box_size=1, border=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
    )
    qr.add_data(content)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except (UnicodeEncodeError, OSError):
        # Some terminals can't render box-drawing; fall back to plain blocks.
        matrix = qr.get_matrix()
        for row in matrix:
            print("".join("##" if cell else "  " for cell in row))


# --- Media decryption -------------------------------------------------------


def _decrypt_wechat_media(data: bytes, aeskey_hex: str) -> bytes:
    """Decrypt WeChat AES-encrypted media (image or file).

    aeskey_hex is a 32-char hex string, i.e. 16 bytes AES-128 key.
    WeChat C2C CDN media uses AES-128-ECB with PKCS7 padding.
    """
    return _decrypt_wechat_media_with_key(data, bytes.fromhex(aeskey_hex))
