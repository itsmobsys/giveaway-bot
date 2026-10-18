"""Latency benchmark for giveaway_bot.redis_cache.RedisCache.

Run from bot/:  ..\\.venv\\Scripts\\python.exe bench_redis.py

Timing note: this machine sits in India while the Redis server sits in
Singapore, so every number below is an UPPER BOUND on what the deployed bot --
which runs next to the cache -- actually sees. Read them as a worst case, not
as the production figure.

The benchmark drives the real server through the production code path and the
production constructor defaults, writes only throwaway keys
("bench:redis:<uuid4>"), removes everything it created in a single delete()
call, and exits 0 even when Redis is unreachable. Nothing it prints can
identify the server: no URL, host, port or password is ever formatted, and the
counters come from stats(), which redis_cache.py already scrubs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable

from dotenv import load_dotenv

from giveaway_bot.redis_cache import RedisCache

MS = 1_000_000.0  # nanoseconds per millisecond
TTL = 30.0  # seconds; long enough that nothing expires mid-run
RUNS = 30  # reads per read group
WRITES = 20  # set() calls
INCRS = 20  # incr_with_ttl() calls
LOCK_PAIRS = 10  # acquire+release pairs
BATCH = 30  # keys removed by ONE delete() call
BUDGET_READS = 5  # reads attempted with the op budget already spent
THROTTLE_OPS_PER_SEC = 1.0
WRONG_TOKEN = "0" * 32
CLEANUP_TRIES = 3
SETTLE_S = 1.0  # refills a drained bucket (capacity 80, refill 80/sec)

# redis_cache.py logs its one-per-outage warning with the driver's own
# exception text. That scrubber is good but not a proof, so this script
# silences the logger and reports stats()["errors"] (bare exception-class
# names) instead: same information, provably free of server details.
logging.getLogger("giveaway_bot.redis").setLevel(logging.CRITICAL)


def _redis_url() -> str:
    """REDIS_URL from the repo .env (this script lives in bot/). Never echoed."""
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        load_dotenv(env_file)
    return (os.getenv("REDIS_URL") or "").strip()


def _spread(samples: list[float]) -> list[float]:
    if not samples:
        return [float("nan")] * 4
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return [ordered[0], statistics.median(ordered), p95, ordered[-1]]


class _Report:
    """Greppable one-line-per-group output.

    A call the op-budget governor skips never touches the network, so it reads
    as an impossibly fast round trip. Carrying the per-group skip count on the
    same line is what keeps those numbers honest.
    """

    def __init__(self, bench: RedisCache) -> None:
        self._bench = bench
        self._skips = 0

    def group(self, label: str, samples: list[float], note: str = "") -> None:
        low, mid, high, top = _spread(samples)
        skips = self._bench.stats()["counters"]["skipped"] - self._skips
        self._skips += skips
        extra = f" {note}" if note else ""
        print(
            f"{label}: min {low:.1f} p50 {mid:.1f} p95 {high:.1f} max {top:.1f}"
            f" (n={len(samples)} skipped={skips}){extra}"
        )


async def _timed_ms(job: Callable[[], Awaitable[object]]) -> float:
    began = time.perf_counter_ns()
    await job()
    return (time.perf_counter_ns() - began) / MS


async def _bench(url: str) -> None:  # noqa: C901 - a benchmark is a linear script
    bench = RedisCache(url=url)  # production defaults on purpose
    report = _Report(bench)
    tag = f"bench:redis:{uuid.uuid4().hex}"
    hot, counter, lock = f"{tag}:hot", f"{tag}:counter", f"{tag}:lock"
    absent = [f"{tag}:miss{i}" for i in range(RUNS)]
    batch = [f"{tag}:b{i}" for i in range(BATCH)]
    live: set[str] = set()  # every key this run is still responsible for

    try:
        # (a) Cold: the first start() pays DNS + TCP + handshake + PING.
        began = time.perf_counter_ns()
        started = await bench.start()
        cold = (time.perf_counter_ns() - began) / MS
        report.group("cold start (DNS+TCP+handshake+ping)", [cold])
        if not started:
            print(f"redis unreachable from this machine: {bench.stats()['errors']}")
            return

        # (b) Warm PING round trips on the now-established connection.
        report.group("warm ping_ms", [await _timed_ms(bench.ping_ms) for _ in range(RUNS)])

        # (c) Misses: keys never written -- the real cold-cache path.
        report.group("cache MISS get()", [await _timed_ms(lambda k=key: bench.get(k)) for key in absent])

        # (d) Hits: one write, then reads of that key.
        if await bench.set(hot, "1", TTL):
            live.add(hot)
        report.group("cache HIT get()", [await _timed_ms(lambda: bench.get(hot)) for _ in range(RUNS)])

        # (e) Writes.
        writes: list[float] = []
        for key in batch[:WRITES]:
            began = time.perf_counter_ns()
            stored = await bench.set(key, "v", TTL)
            writes.append((time.perf_counter_ns() - began) / MS)
            if stored:
                live.add(key)
        report.group("set() ttl=30s", writes)

        # (f) INCR + TTL in ONE round trip. The values must climb 1..N and never
        # reset: that is the whole point of the Lua path, and a reset would mean
        # the TTL branch replaced the counter instead of just setting a TTL.
        incr_ms: list[float] = []
        seen: list[int | None] = []
        for _ in range(INCRS):
            began = time.perf_counter_ns()
            seen.append(await bench.incr_with_ttl(counter, TTL))
            incr_ms.append((time.perf_counter_ns() - began) / MS)
        if any(value is not None for value in seen):
            live.add(counter)
        report.group("incr_with_ttl()", incr_ms)
        print(f"incr monotonic 1..{INCRS}: {seen == list(range(1, INCRS + 1))} (got {seen})")

        # (g) Lock pairs, then a release that must be refused.
        pairs: list[float] = []
        honoured = 0
        for _ in range(LOCK_PAIRS):
            began = time.perf_counter_ns()
            token = await bench.acquire(lock, TTL)
            if token is not None:
                live.add(lock)
            released = bool(token) and await bench.release(lock, token)
            if released:
                live.discard(lock)
                honoured += 1
            pairs.append((time.perf_counter_ns() - began) / MS)
        report.group("acquire+release pair", pairs)
        print(f"lock pairs fully honoured: {honoured}/{LOCK_PAIRS}")

        token = await bench.acquire(lock, TTL)
        if token is not None:
            live.add(lock)
        began = time.perf_counter_ns()
        refused = not await bench.release(lock, WRONG_TOKEN)
        report.group("release(wrong token)", [(time.perf_counter_ns() - began) / MS])
        still_held = await bench.get(lock) is not None if token is not None else False
        print(f"wrong-token release refused: {refused}; lock still held: {still_held}")
        if token is not None and await bench.release(lock, token):
            live.discard(lock)

        # (h) One delete() call for a whole batch of keys. Settle first so a
        # bucket drained by the groups above cannot turn this into a local skip.
        for key in batch:
            if await bench.set(key, "v", TTL):
                live.add(key)
        await asyncio.sleep(SETTLE_S)
        began = time.perf_counter_ns()
        dropped = await bench.delete(*batch)
        one_call = (time.perf_counter_ns() - began) / MS
        report.group("delete() x30 keys, one call", [one_call], note=f"removed={dropped}")
        live.difference_update(batch)

        # (i) Governance: with the op budget spent, the op is skipped locally and
        # the network is never touched -- exactly what production does under load.
        throttled = RedisCache(url=url, op_budget_per_sec=THROTTLE_OPS_PER_SEC)
        try:
            await throttled.start()  # spends this cache's single token
            report.group(  # noqa: SIM117 - the inner try needs its own except-free body
                "budget-exhausted get() [skipped (no network)]",
                [await _timed_ms(lambda: throttled.get(hot)) for _ in range(BUDGET_READS)],
            )
            print(f"throttled counters: {throttled.stats()['counters']}")
        finally:
            with contextlib.suppress(Exception):
                await throttled.aclose()
    finally:
        await _cleanup(bench, live)


async def _cleanup(bench: RedisCache, live: set[str]) -> None:
    """Report counters, delete every remaining key, verify, close. Never raises."""
    counters = bench.stats()["counters"]
    print(f"op counters: {counters}")
    if counters["skipped"]:
        print(f"WARNING: {counters['skipped']} op(s) were skipped by the production op budget")

    # One delete() call for everything still owned. Retrying only happens if the
    # budget skipped that call; re-sending the same names is harmless.
    removed = 0
    for _ in range(CLEANUP_TRIES):
        with contextlib.suppress(Exception):
            removed += await bench.delete(*sorted(live))
        if removed >= len(live):
            break
        await asyncio.sleep(SETTLE_S)

    # Report what is really still there instead of trusting delete()'s count: a
    # key can also be gone simply because its TTL ran out.
    before = bench.stats()["counters"]["skipped"]
    leftover = 0
    with contextlib.suppress(Exception):
        for key in sorted(live):
            leftover += 1 if await bench.get(key) is not None else 0
    if bench.stats()["counters"]["skipped"] > before:
        print("note: a key-verification read was skipped by the op budget")
    print(f"leftover bench keys: {leftover}")

    with contextlib.suppress(Exception):
        await bench.aclose()


def main() -> int:
    url = _redis_url()
    if not url:
        print("REDIS_URL not set in .env -- benchmark skipped")
        return 0
    try:
        asyncio.run(_bench(url))
    except Exception as exc:
        # Type name only, no traceback and no URL: the report stays sanitised.
        print(f"benchmark aborted ({type(exc).__name__}); is the Redis server reachable from here?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
