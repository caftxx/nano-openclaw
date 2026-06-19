"""talk.* RPC handlers."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext
from nano_openclaw.services.backend import VoiceError


async def talk_config(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"config": await ctx.backend.voice_config()}


async def talk_speak(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = await ctx.backend.talk_speak(
            text=str(params.get("text") or ""),
            voice_id=_optional_str(params.get("voiceId") or params.get("voice")),
            sample_rate=_optional_int(params.get("sampleRate") or params.get("sample_rate")),
            speed=_optional_float(params.get("speed")),
            rate_wpm=_optional_int(params.get("rateWpm") or params.get("rate_wpm")),
        )
    except VoiceError as exc:
        # Let ws_route map this to INTERNAL for now would be too vague; return a
        # structured error payload so HTTP wrappers and simple clients can decide
        # whether to fall back locally.
        return {
            "ok": False,
            "error": str(exc),
            "reason": exc.reason,
            "fallbackEligible": exc.fallback_eligible,
        }
    return {"ok": True, **payload}


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
