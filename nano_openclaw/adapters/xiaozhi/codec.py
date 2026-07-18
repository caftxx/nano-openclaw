"""Raw Opus codec used by xiaozhi protocol v1.

PyAV wheels carry FFmpeg's Opus implementation, unlike ctypes wrappers that
still require users to install a platform-specific libopus shared library.
"""

from __future__ import annotations

from fractions import Fraction

from nano_openclaw.adapters.xiaozhi.protocol import (
    FRAME_DURATION_MS,
    INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
)


class CodecUnavailable(RuntimeError):
    pass


def _av_module():
    try:
        import av  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CodecUnavailable(
            "xiaozhi audio requires the optional dependency: pip install 'nano-openclaw[xiaozhi]'"
        ) from exc
    return av


class OpusCodec:
    def __init__(
        self,
        *,
        decode_sample_rate: int = INPUT_SAMPLE_RATE,
        encode_sample_rate: int = OUTPUT_SAMPLE_RATE,
        encode_bitrate: int = 64000,
    ) -> None:
        av = _av_module()
        self._av = av
        self.decode_sample_rate = decode_sample_rate
        self.encode_sample_rate = encode_sample_rate
        self._decode_frame_samples = decode_sample_rate * FRAME_DURATION_MS // 1000
        self._encode_frame_samples = encode_sample_rate * FRAME_DURATION_MS // 1000
        self._decoder = av.CodecContext.create("opus", "r")
        self._decoder.open()
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=decode_sample_rate
        )
        self._encoder = av.CodecContext.create("libopus", "w")
        self._encoder.sample_rate = encode_sample_rate
        self._encoder.layout = "mono"
        self._encoder.format = "s16"
        self._encoder.time_base = Fraction(1, encode_sample_rate)
        self._encoder.bit_rate = encode_bitrate
        self._encoder.options = {
            "frame_duration": str(FRAME_DURATION_MS),
            "application": "audio",
        }
        self._encoder.open()
        self._encoded_samples = 0

    @staticmethod
    def normalize_pcm_frame(pcm: bytes, frame_samples: int) -> bytes:
        frame_bytes = frame_samples * 2
        if len(pcm) < frame_bytes:
            return pcm + b"\x00" * (frame_bytes - len(pcm))
        return pcm[:frame_bytes]

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            raise ValueError("empty Opus packet")
        pcm_parts: list[bytes] = []
        for decoded in self._decoder.decode(self._av.Packet(packet)):
            for frame in self._resampler.resample(decoded):
                pcm_parts.append(bytes(frame.planes[0])[: frame.samples * 2])
        if not pcm_parts:
            raise ValueError("Opus decoder produced no PCM")
        # FFmpeg's resampler has a one-time startup delay. Protocol v1 still
        # requires exactly one 60 ms / 960-sample PCM frame per binary packet,
        # so pad that first frame (and defensively trim malformed overlong output).
        return self.normalize_pcm_frame(b"".join(pcm_parts), self._decode_frame_samples)

    def encode(self, pcm: bytes) -> bytes:
        frame = self._av.AudioFrame(
            format="s16", layout="mono", samples=self._encode_frame_samples
        )
        frame.sample_rate = self.encode_sample_rate
        frame.time_base = Fraction(1, self.encode_sample_rate)
        frame.pts = self._encoded_samples
        frame.planes[0].update(
            self.normalize_pcm_frame(pcm, self._encode_frame_samples)
        )
        self._encoded_samples += self._encode_frame_samples
        packets = list(self._encoder.encode(frame))
        if len(packets) != 1:
            raise ValueError(f"Opus encoder produced {len(packets)} packets for one 60ms frame")
        return bytes(packets[0])

    def encode_stream(self, pcm: bytes) -> list[bytes]:
        frame_bytes = self._encode_frame_samples * 2
        return [
            self.encode(pcm[offset : offset + frame_bytes])
            for offset in range(0, len(pcm), frame_bytes)
        ]
