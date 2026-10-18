"""Tests for the optional Redis cache. No network, no credentials."""

from __future__ import annotations

import asyncio
import json
import unittest

from giveaway_bot.redis_cache import RedisCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedis:
    """Minimal async stand-in that records every call it is asked to make."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail = fail
        self.closed = False
        #: Set to raise only from EVAL, to exercise the incr fallback.
        self.eval_fails = False

    def _maybe_fail(self, name: str) -> None:
        if self.fail is not None:
            raise self.fail

    async def ping(self) -> bool:
        self.calls.append(("ping", (), {}))
        self._maybe_fail("ping")
        return True

    async def get(self, key: str):
        self.calls.append(("get", (key,), {}))
        self._maybe_fail("get")
        return self.data.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None):
        self.calls.append(("set", (key, value), {"nx": nx, "px": px}))
        self._maybe_fail("set")
        if nx and key in self.data:
            return None
        self.data[key] = value
        if px is not None:
            self.ttls[key] = px
        return True

    async def delete(self, *keys: str) -> int:
        self.calls.append(("delete", keys, {}))
        self._maybe_fail("delete")
        removed = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                removed += 1
        return removed

    async def incr(self, key: str) -> int:
        self.calls.append(("incr", (key,), {}))
        self._maybe_fail("incr")
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    async def pexpire(self, key: str, millis: int) -> bool:
        self.calls.append(("pexpire", (key, millis), {}))
        self._maybe_fail("pexpire")
        self.ttls[key] = millis
        return True

    async def eval(self, script: str, numkeys: int, *args):
        self.calls.append(("eval", (script, numkeys, *args), {}))
        if self.eval_fails:
            raise RuntimeError("scripting disabled")
        self._maybe_fail("eval")
        key, payload = args[0], args[1]
        if "pexpire" in script:
            value = int(self.data.get(key, "0")) + 1
            self.data[key] = str(value)
            if key not in self.ttls:
                self.ttls[key] = int(payload)
            return value
        if self.data.get(key) == payload:
            self.data.pop(key, None)
            return 1
        return 0

    async def aclose(self) -> None:
        self.calls.append(("aclose", (), {}))
        self.closed = True


def cache_for(fake: FakeRedis, **kwargs) -> RedisCache:
    kwargs.setdefault("url", "redis://example.invalid:6379/0")
    kwargs.setdefault("connect", lambda: fake)
    return RedisCache(**kwargs)


class DegradedModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_url_is_inert_and_never_raises(self) -> None:
        cache = RedisCache("")
        self.assertFalse(cache.configured)
        self.assertFalse(cache.available)
        self.assertEqual(await cache.get("k"), None)
        self.assertEqual(await cache.set("k", "v", 5), False)
        self.assertEqual(await cache.delete("k"), 0)
        self.assertEqual(await cache.get_json("k"), None)
        self.assertEqual(await cache.set_json("k", {"a": 1}, 5), False)
        self.assertEqual(await cache.incr_with_ttl("k", 5), None)
        self.assertEqual(await cache.acquire("k", 5), None)
        self.assertEqual(await cache.release("k", "tok"), False)
        self.assertEqual(await cache.start(), False)
        self.assertEqual(await cache.ping_ms(), None)
        await cache.aclose()

    async def test_disabled_switch_beats_a_valid_url(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake, enabled=False)
        self.assertFalse(cache.configured)
        self.assertEqual(await cache.get("k"), None)
        self.assertEqual(fake.calls, [])

    async def test_dead_server_degrades_then_opens_the_breaker(self) -> None:
        fake = FakeRedis(fail=ConnectionError("host 1.2.3.4:19722 refused"))
        clock = FakeClock()
        cache = cache_for(fake, breaker_threshold=3, breaker_cooldown=30.0, clock=clock)

        for _ in range(3):
            self.assertEqual(await cache.get("k"), None)
        self.assertFalse(cache.available)
        attempts = len(fake.calls)
        # While the breaker is open nothing reaches the network at all.
        for _ in range(5):
            self.assertEqual(await cache.get("k"), None)
        self.assertEqual(len(fake.calls), attempts)
        self.assertGreaterEqual(cache.stats()["counters"]["skipped"], 5)
        self.assertTrue(cache.stats()["breaker_open"])

        # After the cooldown it tries again and a success closes the breaker.
        clock.advance(31.0)
        fake.fail = None
        fake.data["gwbot:v1:k"] = "v"
        self.assertEqual(await cache.get("k"), "v")
        self.assertTrue(cache.available)
        self.assertFalse(cache.stats()["breaker_open"])

    async def test_malformed_json_is_a_miss(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake)
        fake.data["gwbot:v1:gw:gw_1"] = "{not json"
        self.assertIsNone(await cache.get_json("gw:gw_1"))

    async def test_missing_driver_is_a_miss_not_a_crash(self) -> None:
        def broken() -> FakeRedis:
            raise ModuleNotFoundError("No module named 'redis'")

        cache = RedisCache("redis://example.invalid:6379/0", connect=broken)
        self.assertTrue(cache.configured)
        self.assertIsNone(await cache.get("k"))
        self.assertFalse(await cache.start())
        self.assertGreaterEqual(cache.stats()["counters"]["skipped"], 1)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_over_budget_ops_are_skipped_without_touching_redis(self) -> None:
        fake = FakeRedis()
        clock = FakeClock()
        cache = cache_for(fake, op_budget_per_sec=2.0, clock=clock)
        fake.data["gwbot:v1:k"] = "v"

        self.assertEqual(await cache.get("k"), "v")
        self.assertEqual(await cache.get("k"), "v")
        # Budget spent: the next calls must not reach the client at all.
        self.assertIsNone(await cache.get("k"))
        self.assertIsNone(await cache.get("k"))
        self.assertEqual(len([c for c in fake.calls if c[0] == "get"]), 2)
        self.assertEqual(cache.stats()["counters"]["skipped"], 2)

        # The bucket refills over time.
        clock.advance(1.0)
        self.assertEqual(await cache.get("k"), "v")


class OperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_get_delete_round_trip_and_millis_ttl(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake)
        self.assertTrue(await cache.set("count:gw_1", "7", 2.5))
        set_call = [c for c in fake.calls if c[0] == "set"][0]
        self.assertEqual(set_call[1], ("gwbot:v1:count:gw_1", "7"))
        self.assertEqual(set_call[2]["px"], 2500)
        self.assertEqual(await cache.get("count:gw_1"), "7")
        self.assertTrue(fake.calls[-1][1][0].startswith("gwbot:v1:"))
        self.assertTrue(await cache.set_json("gw:gw_1", {"id": "gw_1"}, 2.0))
        self.assertEqual(await cache.get_json("gw:gw_1"), {"id": "gw_1"})
        self.assertEqual(await cache.delete("count:gw_1", "gw:gw_1"), 2)

    async def test_incr_with_ttl_uses_one_round_trip_and_sets_the_ttl(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake)
        self.assertEqual(await cache.incr_with_ttl("count:gw_1", 30), 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "eval")
        self.assertEqual(fake.ttls["gwbot:v1:count:gw_1"], 30000)
        self.assertEqual(await cache.incr_with_ttl("count:gw_1", 30), 2)
        self.assertEqual(fake.ttls["gwbot:v1:count:gw_1"], 30000)

    async def test_incr_falls_back_when_scripting_is_unavailable(self) -> None:
        fake = FakeRedis()
        fake.eval_fails = True
        cache = cache_for(fake)
        self.assertEqual(await cache.incr_with_ttl("count:gw_1", 30), 1)
        names = [c[0] for c in fake.calls]
        self.assertIn("incr", names)
        self.assertIn("pexpire", names)
        self.assertEqual(fake.ttls["gwbot:v1:count:gw_1"], 30000)

    async def test_lock_is_only_released_by_its_owner(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake)
        token = await cache.acquire("lock:join:gw_1:42", 10)
        self.assertIsNotNone(token)
        acquire_call = [c for c in fake.calls if c[0] == "set"][0]
        self.assertEqual(acquire_call[2], {"nx": True, "px": 10000})
        # A second holder gets nothing while the lock is held.
        self.assertIsNone(await cache.acquire("lock:join:gw_1:42", 10))
        # A stale/wrong token must not delete the live lock.
        self.assertFalse(await cache.release("lock:join:gw_1:42", "someone-elses"))
        self.assertIn("gwbot:v1:lock:join:gw_1:42", fake.data)
        self.assertTrue(await cache.release("lock:join:gw_1:42", token or ""))
        self.assertNotIn("gwbot:v1:lock:join:gw_1:42", fake.data)

    async def test_json_payload_is_compact_and_unicode_safe(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake)
        await cache.set_json("gw:gw_1", {"prize": "café ☕", "n": 2}, 5)
        stored = fake.data["gwbot:v1:gw:gw_1"]
        # Compact separators: no space after ':' or ',' outside the values.
        self.assertNotIn('": ', stored)
        self.assertNotIn(", ", stored)
        self.assertEqual(json.loads(stored), {"prize": "café ☕", "n": 2})

    async def test_a_stalled_call_times_out_instead_of_parking_the_caller(self) -> None:
        class Hanging(FakeRedis):
            async def get(self, key: str):
                await asyncio.sleep(5)

        cache = cache_for(Hanging(), socket_timeout=0.01, socket_connect_timeout=0.01)
        self.assertIsNone(await asyncio.wait_for(cache.get("k"), timeout=1.0))


class HygieneTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_reveal_no_endpoint_or_secret(self) -> None:
        fake = FakeRedis(fail=ConnectionError("auth failed for user default at redis-1.example:19722"))
        cache = cache_for(fake, url="redis://default:supersecretpassword@redis-1.example:19722/0")
        for _ in range(6):
            await cache.get("k")
        blob = json.dumps(cache.stats())
        for secret in ("supersecretpassword", "redis-1.example", "19722", "default"):
            self.assertNotIn(secret, blob)

    async def test_close_is_idempotent_and_ends_the_cache(self) -> None:
        fake = FakeRedis()
        cache = cache_for(fake)
        self.assertEqual(await cache.get("k"), None)
        await cache.aclose()
        await cache.aclose()
        self.assertTrue(fake.closed)
        self.assertFalse(cache.available)
        self.assertEqual(await cache.get("k"), None)


if __name__ == "__main__":
    unittest.main()
