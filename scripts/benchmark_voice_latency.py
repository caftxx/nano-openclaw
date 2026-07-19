"""Compare legacy collect-then-speak with the streaming voice pipeline.

The benchmark uses the real speech-gateway TTS model while replaying the same
deterministic LLM delta schedule for both clients. It isolates orchestration
latency from model-provider variance and reports stop-to-first-audio estimates
using the old/new server-VAD thresholds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import AsyncIterator

from nano_openclaw.adapters.xiaozhi.local_speech import stream_local_speech
from nano_openclaw.adapters.xiaozhi.protocol import SpeechTextChunker


DELTAS = [
    "这是一个",
    "用于比较",
    "优化前后",
    "语音实时性的开头，",
    "当模型还在生成后续内容时，",
    "前面的稳定文本",
    "已经可以送去合成，",
    "因此用户不必等待",
    "整段回答全部完成，",
    "就能够听到",
    "第一段语音。",
]


async def _consume_audio(
    text_chunks: AsyncIterator[str],
    *,
    realtime_url: str,
    voice: str,
    started_at: float,
) -> dict[str, float | int]:
    first_audio_at = 0.0
    total_bytes = 0
    async for pcm in stream_local_speech(
        realtime_url=realtime_url,
        api_key="",
        model="fun-cosyvoice3-0.5b",
        voice=voice,
        text_chunks=text_chunks,
        sample_rate=24000,
    ):
        first_audio_at = first_audio_at or time.perf_counter()
        total_bytes += len(pcm)
    completed_at = time.perf_counter()
    return {
        "first_audio_ms": round((first_audio_at - started_at) * 1000, 1),
        "total_ms": round((completed_at - started_at) * 1000, 1),
        "audio_bytes": total_bytes,
    }


async def legacy_run(*, realtime_url: str, voice: str, delta_ms: int) -> dict:
    started_at = time.perf_counter()
    text = ""
    for delta in DELTAS:
        await asyncio.sleep(delta_ms / 1000)
        text += delta

    async def complete_text() -> AsyncIterator[str]:
        yield text

    result = await _consume_audio(
        complete_text(), realtime_url=realtime_url, voice=voice, started_at=started_at
    )
    result["first_tts_text_ms"] = round(len(DELTAS) * delta_ms, 1)
    return result


async def streaming_run(*, realtime_url: str, voice: str, delta_ms: int) -> dict:
    started_at = time.perf_counter()
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=32)
    first_text_ready = asyncio.Event()
    first_text_at = 0.0

    async def produce() -> None:
        nonlocal first_text_at
        chunker = SpeechTextChunker()
        for delta in DELTAS:
            await asyncio.sleep(delta_ms / 1000)
            for chunk in chunker.feed(delta):
                if not first_text_at:
                    first_text_at = time.perf_counter()
                    first_text_ready.set()
                await queue.put(chunk)
        for chunk in chunker.finish():
            if not first_text_at:
                first_text_at = time.perf_counter()
                first_text_ready.set()
            await queue.put(chunk)
        await queue.put(None)

    async def live_text() -> AsyncIterator[str]:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    producer = asyncio.create_task(produce())
    await first_text_ready.wait()
    try:
        result = await _consume_audio(
            live_text(), realtime_url=realtime_url, voice=voice, started_at=started_at
        )
    finally:
        await producer
    result["first_tts_text_ms"] = round((first_text_at - started_at) * 1000, 1)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--realtime-url", default="ws://127.0.0.1:5100/v1/realtime")
    parser.add_argument("--voice", default="nano")
    parser.add_argument("--delta-ms", type=int, default=180)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    results: list[dict] = []
    for run in range(1, args.repeat + 1):
        legacy = await legacy_run(
            realtime_url=args.realtime_url, voice=args.voice, delta_ms=args.delta_ms
        )
        streaming = await streaming_run(
            realtime_url=args.realtime_url, voice=args.voice, delta_ms=args.delta_ms
        )
        item = {"run": run, "legacy": legacy, "streaming": streaming}
        results.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    legacy_first = statistics.median(item["legacy"]["first_audio_ms"] for item in results)
    streaming_first = statistics.median(
        item["streaming"]["first_audio_ms"] for item in results
    )
    before_stop_to_audio = 600 + legacy_first
    after_stop_to_audio = 400 + streaming_first
    summary = {
        "repeat": args.repeat,
        "delta_ms": args.delta_ms,
        "legacy_first_audio_median_ms": round(legacy_first, 1),
        "streaming_first_audio_median_ms": round(streaming_first, 1),
        "pipeline_first_audio_saved_ms": round(legacy_first - streaming_first, 1),
        "pipeline_first_audio_reduction_pct": round(
            (legacy_first - streaming_first) * 100 / legacy_first, 1
        ),
        "estimated_stop_to_first_audio_before_ms": round(before_stop_to_audio, 1),
        "estimated_stop_to_first_audio_after_ms": round(after_stop_to_audio, 1),
        "estimated_end_to_end_reduction_pct": round(
            (before_stop_to_audio - after_stop_to_audio) * 100 / before_stop_to_audio,
            1,
        ),
    }
    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
