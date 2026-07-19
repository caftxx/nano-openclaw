"""xiaozhi WebSocket v1 protocol validation and message helpers."""

from __future__ import annotations

import json
import re
from typing import Any


INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHANNELS = 1
FRAME_DURATION_MS = 60
INPUT_FRAME_SAMPLES = INPUT_SAMPLE_RATE * FRAME_DURATION_MS // 1000
OUTPUT_FRAME_SAMPLES = OUTPUT_SAMPLE_RATE * FRAME_DURATION_MS // 1000
SUPPORTED_VERSION = 1


class ProtocolError(ValueError):
    pass


def parse_text_message(data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid JSON message") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ProtocolError("message must be an object with string type")
    return payload


def validate_hello(payload: dict[str, Any]) -> int:
    if payload.get("type") != "hello":
        raise ProtocolError("first message must be hello")
    version = payload.get("version")
    if version != SUPPORTED_VERSION:
        raise ProtocolError(f"unsupported protocol version: {version}")
    if payload.get("transport") != "websocket":
        raise ProtocolError("transport must be websocket")
    audio = payload.get("audio_params")
    if not isinstance(audio, dict):
        raise ProtocolError("hello.audio_params is required")
    expected = {
        "format": "opus",
        "sample_rate": INPUT_SAMPLE_RATE,
        "channels": CHANNELS,
        "frame_duration": FRAME_DURATION_MS,
    }
    for key, value in expected.items():
        if audio.get(key) != value:
            raise ProtocolError(f"unsupported audio_params.{key}: {audio.get(key)!r}")
    return version


def server_hello(
    session_id: str,
    *,
    output_sample_rate: int = OUTPUT_SAMPLE_RATE,
) -> dict[str, Any]:
    return {
        "type": "hello",
        "transport": "websocket",
        "session_id": session_id,
        "audio_params": {
            "format": "opus",
            "sample_rate": output_sample_rate,
            "channels": CHANNELS,
            "frame_duration": FRAME_DURATION_MS,
        },
    }


def envelope(session_id: str, kind: str, **fields: Any) -> dict[str, Any]:
    return {"session_id": session_id, "type": kind, **fields}


def mcp_envelope(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return envelope(session_id, "mcp", payload=payload)


def sanitize_tool_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_")
    return f"xiaozhi__{safe or 'tool'}"


_SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")
_STRONG_SPEECH_END = frozenset("。！？!?；;\n")
_SOFT_SPEECH_END = frozenset("，,、：:")


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text.strip()) if part.strip()]


class SpeechTextChunker:
    """Turn streaming model deltas into stable, TTS-friendly text chunks."""

    def __init__(self, *, min_chars: int = 12, max_chars: int = 30) -> None:
        if min_chars < 1 or max_chars < min_chars:
            raise ValueError("speech chunk bounds must satisfy 1 <= min_chars <= max_chars")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        return self._drain(final=False)

    def finish(self) -> list[str]:
        chunks = self._drain(final=True)
        self._buffer = ""
        return chunks

    def _drain(self, *, final: bool) -> list[str]:
        chunks: list[str] = []
        while self._buffer:
            boundary = self._next_boundary(final=final)
            if boundary is None:
                break
            chunk = self._buffer[:boundary].strip()
            self._buffer = self._buffer[boundary:]
            if chunk:
                chunks.append(chunk)
        return chunks

    def _next_boundary(self, *, final: bool) -> int | None:
        for index, char in enumerate(self._buffer):
            if char in _STRONG_SPEECH_END:
                return index + 1
            if char in _SOFT_SPEECH_END and index + 1 >= self.min_chars:
                return index + 1

        if len(self._buffer) >= self.max_chars:
            for index in range(self.max_chars - 1, self.min_chars - 2, -1):
                if self._buffer[index] in _SOFT_SPEECH_END or self._buffer[index].isspace():
                    return index + 1
            return self.max_chars
        if final:
            return len(self._buffer)
        return None
