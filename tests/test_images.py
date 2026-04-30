"""Tests for image parsing, loading, and description.

Tests the security guards (SSRF, path traversal, size limits),
image reference extraction, and compression logic.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from nano_openclaw.images import (
    _compress_image,
    _is_safe_ref,
    _load_local,
    _load_remote,
    _assert_safe_url,
    _is_blocked_addr,
    parse_image_refs,
    load_image,
    to_anthropic_image_block,
    describe_image,
    _MIME_FROM_EXT,
    _MAX_IMAGE_BYTES,
    _COMPRESSED_TARGET_BYTES,
    _MAX_DOWNLOAD_BYTES,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_image_bytes():
    """Create a small sample PNG image in memory."""
    img = Image.new("RGB", (100, 100), color="red")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def large_image_bytes():
    """Create a large JPEG image that exceeds size limits."""
    img = Image.new("RGB", (3000, 3000), color="blue")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


@pytest.fixture
def sample_png(tmp_path):
    """Create a sample PNG file."""
    path = tmp_path / "test.png"
    img = Image.new("RGB", (100, 100), color="green")
    img.save(path, format="PNG")
    return path


@pytest.fixture
def sample_jpg(tmp_path):
    """Create a sample JPEG file."""
    path = tmp_path / "test.jpg"
    img = Image.new("RGB", (100, 100), color="blue")
    img.save(path, format="JPEG", quality=90)
    return path


# =============================================================================
# parse_image_refs Tests
# =============================================================================


def test_parse_at_prefixed_absolute_path(sample_png, tmp_path):
    """Test @-prefixed absolute path extraction."""
    text = f"@{sample_png} 请分析这张图片"
    cleaned, refs = parse_image_refs(text)
    assert str(sample_png) in refs
    assert "请分析这张图片" in cleaned
    assert "@" not in cleaned


def test_parse_at_prefixed_relative_path(sample_png, tmp_path):
    """Test @-prefixed relative path resolution to CWD."""
    rel_path = "test_relative.png"
    full_path = tmp_path / rel_path
    img = Image.new("RGB", (50, 50), color="yellow")
    img.save(full_path, format="PNG")
    
    with patch("nano_openclaw.images.os.getcwd", return_value=str(tmp_path)):
        text = f"@{rel_path} relative image"
        cleaned, refs = parse_image_refs(text)
        assert str(full_path) in refs


def test_parse_markdown_image():
    """Test Markdown image extraction."""
    text = "![alt text](https://example.com/image.png) description"
    cleaned, refs = parse_image_refs(text)
    assert "https://example.com/image.png" in refs
    assert "description" in cleaned
    assert "![" not in cleaned


def test_parse_https_url():
    """Test bare HTTPS URL extraction."""
    text = "See https://example.com/photo.jpg for details"
    cleaned, refs = parse_image_refs(text)
    assert "https://example.com/photo.jpg" in refs


def test_parse_local_absolute_path_unix():
    """Test Unix-style absolute path extraction."""
    text = "/tmp/image.png is a local file"
    cleaned, refs = parse_image_refs(text)
    assert "/tmp/image.png" in refs


def test_parse_local_absolute_path_windows():
    """Test Windows-style absolute path extraction."""
    text = "C:\\Users\\test\\image.jpg is a local file"
    cleaned, refs = parse_image_refs(text)
    assert "C:\\Users\\test\\image.jpg" in refs


def test_parse_rejects_path_traversal():
    """Test that path traversal is rejected."""
    text = "@/etc/passwd/../../../etc/shadow.png"
    cleaned, refs = parse_image_refs(text)
    assert refs == []
    assert "../" in cleaned or cleaned == ""


def test_parse_rejects_home_dir():
    """Test that home directory prefix is rejected."""
    text = "@~/secret/image.png private image"
    cleaned, refs = parse_image_refs(text)
    assert refs == []
    assert "~" not in cleaned


def test_parse_mixed_refs():
    """Test extraction of multiple image reference types."""
    text = """Here are several images:
    @/abs/path.png
    ![md](https://example.com/md.jpg)
    https://example.com/bare.jpg
    /local/file.gif
    """
    cleaned, refs = parse_image_refs(text)
    assert len(refs) >= 3
    assert any("/abs/path.png" in ref or "abs" in ref for ref in refs)
    assert "https://example.com/md.jpg" in refs
    assert "https://example.com/bare.jpg" in refs


def test_parse_removes_duplicates():
    """Test that duplicate references are removed."""
    text = "@/img.png @/img.png @/img.png"
    cleaned, refs = parse_image_refs(text)
    assert len(refs) == 1
    assert any("img.png" in ref for ref in refs)


def test_parse_empty_text():
    """Test parsing empty text."""
    cleaned, refs = parse_image_refs("")
    assert cleaned == ""
    assert refs == []


# =============================================================================
# load_image Tests
# =============================================================================


def test_load_local_image(sample_png):
    """Test loading a local PNG file."""
    b64, mime = load_image(str(sample_png))
    assert mime == "image/png"
    assert isinstance(b64, str)
    assert len(base64.b64decode(b64)) > 0


def test_load_local_jpeg(sample_jpg):
    """Test loading a local JPEG file."""
    b64, mime = load_image(str(sample_jpg))
    assert mime == "image/jpeg"
    assert isinstance(b64, str)


def test_load_local_nonexistent():
    """Test loading nonexistent local file raises error."""
    with pytest.raises(FileNotFoundError):
        load_image("/nonexistent/path/image.png")


def test_load_local_large_image(tmp_path):
    """Test that large local images are compressed."""
    large_path = tmp_path / "large.jpg"
    img = Image.new("RGB", (3000, 3000), color="blue")
    img.save(large_path, format="JPEG", quality=95)
    
    b64, mime = load_image(str(large_path))
    data = base64.b64decode(b64)
    assert len(data) <= _MAX_IMAGE_BYTES


def test_load_remote_image(sample_image_bytes):
    """Test loading a remote image via HTTPS."""
    url = "https://example.com/image.png"
    mock_response = MagicMock()
    mock_response.headers.get.side_effect = lambda key: {
        "Content-Length": str(len(sample_image_bytes)),
        "Content-Type": "image/png"
    }.get(key)
    mock_response.read.return_value = sample_image_bytes
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    
    with patch("nano_openclaw.images.urllib.request.urlopen", return_value=mock_response):
        with patch("nano_openclaw.images._assert_safe_url"):
            b64, mime = load_image(url)
            assert isinstance(b64, str)
            assert mime == "image/png"


def test_load_remote_too_large():
    """Test that oversized remote images are rejected."""
    url = "https://example.com/huge.jpg"
    with patch("nano_openclaw.images._assert_safe_url"):
        with patch("nano_openclaw.images.urllib.request.Request"):
            mock_response = MagicMock()
            mock_response.headers.get.return_value = str(_MAX_DOWNLOAD_BYTES + 1000)
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            
            with patch("nano_openclaw.images.urllib.request.urlopen", return_value=mock_response):
                with pytest.raises(ValueError, match="远程图片过大"):
                    load_image(url)


# =============================================================================
# to_anthropic_image_block Tests
# =============================================================================


def test_to_anthropic_image_block():
    """Test Anthropic image block format."""
    b64 = "abc123"
    mime = "image/png"
    block = to_anthropic_image_block(b64, mime)
    
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == mime
    assert block["source"]["data"] == b64


# =============================================================================
# describe_image Tests
# =============================================================================


def test_describe_image_anthropic():
    """Test image description with Anthropic API."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "A beautiful sunset"
    mock_response.content = [mock_text_block]
    mock_client.messages.create.return_value = mock_response
    
    b64 = base64.b64encode(b"fake_image_data").decode()
    result = describe_image(b64, "image/png", client=mock_client, model="claude-3-sonnet", api="anthropic")
    
    assert result == "A beautiful sunset"
    mock_client.messages.create.assert_called_once()


def test_describe_image_openai():
    """Test image description with OpenAI API."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_message = MagicMock()
    mock_message.content = "A mountain landscape"
    mock_response.choices = [MagicMock(message=mock_message)]
    mock_client.chat.completions.create.return_value = mock_response
    
    b64 = base64.b64encode(b"fake_image_data").decode()
    result = describe_image(b64, "image/jpeg", client=mock_client, model="gpt-4-vision", api="openai")
    
    assert result == "A mountain landscape"
    mock_client.chat.completions.create.assert_called_once()


def test_describe_image_unsupported_api():
    """Test that unsupported API raises ValueError."""
    mock_client = MagicMock()
    b64 = base64.b64encode(b"fake_image_data").decode()
    
    with pytest.raises(ValueError, match="unsupported api"):
        describe_image(b64, "image/png", client=mock_client, model="model", api="unsupported")


# =============================================================================
# _compress_image Tests
# =============================================================================


def test_compress_png_to_jpeg(sample_image_bytes):
    """Test PNG compression converts to JPEG."""
    data, mime = _compress_image(sample_image_bytes, "image/png")
    assert mime == "image/jpeg"
    assert len(data) <= _COMPRESSED_TARGET_BYTES


def test_compress_jpeg_quality_reduction(large_image_bytes):
    """Test JPEG compression via quality reduction."""
    data, mime = _compress_image(large_image_bytes, "image/jpeg")
    assert mime == "image/jpeg"
    assert len(data) <= _COMPRESSED_TARGET_BYTES


def test_compress_webp_quality_reduction():
    """Test WebP compression via quality reduction."""
    img = Image.new("RGB", (2000, 2000), color="red")
    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=95)
    
    data, mime = _compress_image(buffer.getvalue(), "image/webp")
    assert mime == "image/webp"
    assert len(data) <= _COMPRESSED_TARGET_BYTES


def test_compress_extreme_size():
    """Test compression of extremely large image with resizing."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        img = Image.new("RGB", (10000, 10000), color="blue")
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=95)
        
        data, mime = _compress_image(buffer.getvalue(), "image/jpeg")
        assert len(data) <= _COMPRESSED_TARGET_BYTES


def test_compress_rgba_png():
    """Test that RGBA PNG is converted to RGB JPEG."""
    img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    
    data, mime = _compress_image(buffer.getvalue(), "image/png")
    assert mime == "image/jpeg"
    assert len(data) <= _COMPRESSED_TARGET_BYTES


# =============================================================================
# _is_safe_ref Tests
# =============================================================================


def test_is_safe_ref_valid_path():
    """Test that valid paths are accepted."""
    assert _is_safe_ref("/absolute/path.png") is True
    assert _is_safe_ref("C:\\Users\\test.jpg") is True
    assert _is_safe_ref("https://example.com/img.png") is True


def test_is_safe_ref_rejects_home():
    """Test that home directory prefix is rejected."""
    assert _is_safe_ref("~/secret.png") is False
    assert _is_safe_ref("~user/file.jpg") is False


def test_is_safe_ref_rejects_traversal():
    """Test that path traversal is rejected."""
    assert _is_safe_ref("../../../etc/passwd.png") is False
    assert _is_safe_ref("..\\..\\..\\windows\\system.jpg") is False
    assert _is_safe_ref("../config.png") is False
    assert _is_safe_ref("..") is False


# =============================================================================
# _assert_safe_url / SSRF Tests
# =============================================================================


def test_assert_safe_url_blocks_localhost():
    """Test that localhost URLs are blocked."""
    with patch("nano_openclaw.images.socket.getaddrinfo") as mock_getaddr:
        mock_getaddr.return_value = [(None, None, None, None, ("127.0.0.1", None))]
        
        with pytest.raises(ValueError, match="SSRF"):
            _assert_safe_url("https://localhost/image.png")


def test_assert_safe_url_blocks_private_network():
    """Test that private network addresses are blocked."""
    with patch("nano_openclaw.images.socket.getaddrinfo") as mock_getaddr:
        mock_getaddr.return_value = [(None, None, None, None, ("192.168.1.1", None))]
        
        with pytest.raises(ValueError, match="SSRF"):
            _assert_safe_url("https://internal.local/image.png")


def test_assert_safe_url_allows_public():
    """Test that public URLs are allowed."""
    with patch("nano_openclaw.images.socket.getaddrinfo") as mock_getaddr:
        mock_getaddr.return_value = [(None, None, None, None, ("8.8.8.8", None))]
        
        _assert_safe_url("https://example.com/image.png")


def test_assert_safe_url_unresolvable():
    """Test that unresolvable host raises ValueError."""
    with patch("nano_openclaw.images.socket.getaddrinfo", side_effect=Exception("DNS fail")):
        with pytest.raises(ValueError, match="cannot resolve"):
            _assert_safe_url("https://nonexistent.invalid/img.png")


# =============================================================================
# _is_blocked_addr Tests
# =============================================================================


def test_is_blocked_addr_loopback():
    """Test that loopback addresses are blocked."""
    import ipaddress
    assert _is_blocked_addr(ipaddress.ip_address("127.0.0.1")) is True
    assert _is_blocked_addr(ipaddress.ip_address("::1")) is True


def test_is_blocked_addr_private():
    """Test that private network addresses are blocked."""
    import ipaddress
    assert _is_blocked_addr(ipaddress.ip_address("10.0.0.1")) is True
    assert _is_blocked_addr(ipaddress.ip_address("172.16.0.1")) is True
    assert _is_blocked_addr(ipaddress.ip_address("192.168.0.1")) is True


def test_is_blocked_addr_public():
    """Test that public addresses are not blocked."""
    import ipaddress
    assert _is_blocked_addr(ipaddress.ip_address("8.8.8.8")) is False
    assert _is_blocked_addr(ipaddress.ip_address("1.1.1.1")) is False


def test_is_blocked_addr_link_local():
    """Test that link-local addresses are blocked."""
    import ipaddress
    assert _is_blocked_addr(ipaddress.ip_address("169.254.1.1")) is True
    assert _is_blocked_addr(ipaddress.ip_address("fe80::1")) is True


# =============================================================================
# Integration Tests
# =============================================================================


def test_full_workflow_local_image(sample_png):
    """Test complete workflow: parse, load, convert to block."""
    text = f"@{sample_png} analyze this"
    cleaned, refs = parse_image_refs(text)
    assert len(refs) == 1
    
    b64, mime = load_image(refs[0])
    block = to_anthropic_image_block(b64, mime)
    
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"


def test_mime_type_detection():
    """Test MIME type detection from file extensions."""
    assert _MIME_FROM_EXT[".png"] == "image/png"
    assert _MIME_FROM_EXT[".jpg"] == "image/jpeg"
    assert _MIME_FROM_EXT[".jpeg"] == "image/jpeg"
    assert _MIME_FROM_EXT[".gif"] == "image/gif"
    assert _MIME_FROM_EXT[".webp"] == "image/webp"