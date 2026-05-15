"""Token-bucket limiter: concurrency, backoff."""

from __future__ import annotations

import asyncio
import time

from nano_openclaw.dingtalk.ai_card import TokenBucket


def test_bucket_acquires_burst_immediately_then_paces():
    """A fresh bucket should grant ``max_qps`` calls instantly, then pace."""
    async def run() -> tuple[int, float]:
        bucket = TokenBucket(max_qps=5, backoff=0)
        start = time.monotonic()
        for _ in range(7):
            await bucket.acquire()
        return 7, time.monotonic() - start

    n, elapsed = asyncio.run(run())
    assert n == 7
    # After 5 instant tokens, the 6th + 7th each wait ~1/max_qps seconds.
    assert elapsed >= 0.3, f"expected pacing after burst, got {elapsed:.3f}s"
    assert elapsed < 1.0, "shouldn't pace much beyond the deficit"


def test_concurrent_acquires_serialize_under_lock():
    """Concurrent callers must not double-spend the same token."""
    async def run() -> int:
        bucket = TokenBucket(max_qps=3, backoff=0)
        # Drain instantaneously: 3 tokens are immediately granted, 7 more
        # must each wait ~333ms before the bucket has refilled enough.
        before = time.monotonic()
        await asyncio.gather(*[bucket.acquire() for _ in range(10)])
        elapsed = time.monotonic() - before
        # 7 paced tokens / 3 QPS ≈ 2.3s lower bound
        assert elapsed >= 2.0, f"concurrency should serialize; elapsed={elapsed:.3f}s"
        return 10

    n = asyncio.run(run())
    assert n == 10


def test_trigger_backoff_pauses_for_backoff_duration():
    async def run() -> float:
        bucket = TokenBucket(max_qps=100, backoff=0.3)
        await bucket.acquire()  # warm
        bucket.trigger_backoff()
        start = time.monotonic()
        await bucket.acquire()
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed >= 0.3, f"expected ≥0.3s backoff, got {elapsed:.3f}s"
    assert elapsed < 0.6, f"backoff should not overshoot, got {elapsed:.3f}s"
