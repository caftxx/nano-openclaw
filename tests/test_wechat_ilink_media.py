from __future__ import annotations

import asyncio
import base64

import httpx

from nano_openclaw.wechat.ilink import FILE, VIDEO, download_wechat_file


def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def test_item_type_constants_match_openilink_sdk():
    assert FILE == 4
    assert VIDEO == 5


def test_download_wechat_file_supports_sdk_cdn_media_fields():
    key = b"0123456789abcdef"
    plaintext = b"%PDF fake document"
    ciphertext = _encrypt_aes_ecb(plaintext, key)
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert request.url.path == "/c2c/download"
        assert request.url.params["encrypted_query_param"] == "a/b?c=d"
        return httpx.Response(200, content=ciphertext, headers={"content-type": "application/octet-stream"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_wechat_file(
                client,
                {
                    "file_name": "doc.pdf",
                    "media": {
                        "encrypt_query_param": "a/b?c=d",
                        "aes_key": base64.b64encode(key).decode(),
                    },
                },
                "token",
            )

    data, mime, filename = asyncio.run(run())

    assert data == plaintext
    assert mime == "application/pdf"
    assert filename == "doc.pdf"
    assert seen_urls == [
        "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=a%2Fb%3Fc%3Dd"
    ]
