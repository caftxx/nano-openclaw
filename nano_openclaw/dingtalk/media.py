"""DingTalk inbound media download.

The Stream protocol delivers media attachments as a ``downloadCode`` — a
short-lived handle the robot exchanges for a presigned download URL via
``/v1.0/robot/messageFiles/download``. The presigned URL then serves the
raw bytes over plain HTTPS.

PR4 only does **downloads**: the agent loop occasionally receives images
and files from users via DingTalk, and we want them treated as
``PromptAttachment``s the same way WeChat handles them. Outbound media
upload (chunked transfer to DingTalk's media service) is out of scope —
it's a separate, much larger surface that the dws-cli skill already
covers for now.
"""

from __future__ import annotations

from typing import Optional

import httpx

from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger


log = get_logger(__name__)


DINGTALK_API = "https://api.dingtalk.com"
DOWNLOAD_URL_ENDPOINT = "/v1.0/robot/messageFiles/download"


async def fetch_download_url(
    client: httpx.AsyncClient,
    *,
    download_code: str,
    robot_code: str,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
) -> Optional[str]:
    """Exchange a ``downloadCode`` for a one-shot presigned ``downloadUrl``.

    Returns ``None`` on any failure — the caller decides whether to skip
    the attachment, retry, or surface the error.
    """
    token = await token_mgr.get_access_token(client_id, client_secret)
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }
    payload = {"downloadCode": download_code, "robotCode": robot_code}
    try:
        resp = await client.post(
            f"{DINGTALK_API}{DOWNLOAD_URL_ENDPOINT}",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dingtalk.media.url.error",
            f"downloadCode={download_code[:12]}… {type(exc).__name__}: {exc}",
        )
        return None
    body = resp.json() if resp.content else {}
    url = body.get("downloadUrl") if isinstance(body, dict) else None
    return str(url) if url else None


async def download_media(
    client: httpx.AsyncClient,
    *,
    download_code: str,
    robot_code: str,
    token_mgr: DingtalkTokenManager,
    client_id: str,
    client_secret: str,
) -> Optional[bytes]:
    """Two-step download: ``downloadCode`` → URL → bytes.

    Returns ``None`` on failure. We don't cache; the presigned URL is
    short-lived and tying agent attachments to a re-usable URL would
    complicate sub-process workflows for tiny gain.
    """
    url = await fetch_download_url(
        client,
        download_code=download_code,
        robot_code=robot_code,
        token_mgr=token_mgr,
        client_id=client_id,
        client_secret=client_secret,
    )
    if not url:
        return None
    try:
        resp = await client.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dingtalk.media.fetch.error",
            f"url={url[:60]}… {type(exc).__name__}: {exc}",
        )
        return None
