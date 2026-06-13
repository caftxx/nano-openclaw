"""talk.* RPC handlers."""

from __future__ import annotations

import base64
from typing import Any

from nano_openclaw.gateway.context import GatewayContext
from nano_openclaw.tts import TalkSpeakError, build_talk_config, synthesize_talk_speech


async def talk_config(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"config": build_talk_config(ctx.runtime.config)}


async def talk_speak(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text") or "")
    try:
        result = synthesize_talk_speech(
            ctx.runtime.config,
            text=text,
            voice_id=_optional_str(params.get("voiceId") or params.get("voice")),
            sample_rate=_optional_int(params.get("sampleRate") or params.get("sample_rate")),
            speed=_optional_float(params.get("speed")),
            rate_wpm=_optional_int(params.get("rateWpm") or params.get("rate_wpm")),
        )
    except TalkSpeakError as exc:
        # Let ws_route map this to INTERNAL for now would be too vague; return a
        # structured error payload so HTTP wrappers and simple clients can decide
        # whether to fall back locally.
        return {
            "ok": False,
            "error": str(exc),
            "reason": exc.reason,
            "fallbackEligible": exc.fallback_eligible,
        }
    return {
        "ok": True,
        "audioBase64": base64.b64encode(result.audio).decode("ascii"),
        "provider": result.provider,
        "outputFormat": result.output_format,
        "voiceCompatible": result.voice_compatible,
        "mimeType": result.mime_type,
        "fileExtension": result.file_extension,
    }


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


HANDLERS = {
    "talk.config": talk_config,
    "talk.speak": talk_speak,
}
