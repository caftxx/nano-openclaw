"""ChannelAdapter lifecycle and per-device tool decoration."""

from __future__ import annotations

import time
from typing import Any

from nano_openclaw.adapters.channels.base import ChannelAdapter
from nano_openclaw.adapters.xiaozhi.codec import OpusCodec
from nano_openclaw.adapters.xiaozhi.mcp import XiaozhiHub
from nano_openclaw.adapters.xiaozhi.sessions import DeviceSessionStore
from nano_openclaw.adapters.xiaozhi.stream_player import materialize_stream_tools
from nano_openclaw.config.env_substitution import contains_env_var_reference
from nano_openclaw.features.voice import AliyunTokenProvider
from nano_openclaw.services.channels import get_channel_manager


class XiaozhiChannel(ChannelAdapter):
    id = "xiaozhi"

    async def start(self, ctx) -> None:
        self.runtime = ctx.runtime
        self.backend = ctx.backend
        self.gateway = ctx.gateway
        self.config = ctx.runtime.config.xiaozhi
        self.hub = XiaozhiHub()
        self._state = "starting"
        self._error = None
        try:
            if not self.config.enabled:
                raise RuntimeError("xiaozhi adapter is disabled")
            if not self.config.token.strip() or contains_env_var_reference(self.config.token):
                raise RuntimeError("xiaozhi.token is required when xiaozhi.enabled=true")
            voice = ctx.runtime.config.voice
            if voice.provider == "openai-compatible":
                local_values = (voice.baseUrl, voice.realtimeUrl, voice.apiKey)
                if (
                    not voice.baseUrl.strip()
                    or not voice.realtimeUrl.strip()
                    or not voice.ttsEnabled
                    or any(contains_env_var_reference(value) for value in local_values)
                ):
                    raise RuntimeError("xiaozhi requires configured local ASR and TTS")
            else:
                voice_secrets = (voice.appkey, voice.accessKeyId, voice.accessKeySecret)
                if (
                    not voice.available
                    or not voice.ttsEnabled
                    or any(contains_env_var_reference(value) for value in voice_secrets)
                ):
                    raise RuntimeError("xiaozhi requires configured Aliyun ASR and TTS")
            if not ctx.runtime.cfg.image_model:
                raise RuntimeError("xiaozhi photo support requires a configured imageModel")
            OpusCodec(
                encode_sample_rate=self.config.ttsSampleRate,
                encode_bitrate=self.config.opusBitrate,
            )  # fail early with an actionable optional-dependency/configuration error
            try:
                import python_multipart  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "xiaozhi photo upload requires: pip install 'nano-openclaw[xiaozhi]'"
                ) from exc

            self.sessions = DeviceSessionStore(
                ctx.runtime.state_dir / "xiaozhi-sessions.json", ctx.backend
            )
            self.token_provider = None
            if voice.provider == "aliyun":
                self.token_provider = AliyunTokenProvider(
                    access_key_id=voice.accessKeyId,
                    access_key_secret=voice.accessKeySecret,
                    region_id=voice.region,
                )
        except Exception as exc:  # keep an inspectable error channel without blocking the gateway
            self._state = "error"
            self._error = str(exc) or f"{type(exc).__name__}: initialization failed"
            self._started_at = None
            return

        self._state = "running"
        self._started_at = time.time()

    async def stop(self) -> None:
        connections = self.hub.all() if hasattr(self, "hub") else []
        for connection in connections:
            await connection.close()
        self._state = "stopped"
        self._error = None
        self._started_at = None

    def decorate_tools(self, base, sender_key: str):
        if self._state != "running":
            return base.clone()
        connection = self.hub.get(sender_key)
        if connection is None:
            return base.clone()
        registry = base.clone()
        for tool in materialize_stream_tools(connection):
            registry.register(tool)
        for tool in connection.mcp.materialize_tools():
            registry.register(tool)
        return registry

    async def exit_interaction(self, *, sender_key: str, reason: str = "") -> None:
        connection = self.hub.get(sender_key)
        if connection is not None:
            await connection.request_idle_after_turn(reason=reason)


get_channel_manager().register(XiaozhiChannel)
