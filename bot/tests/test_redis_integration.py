"""Redis wired into the service and the bot, with no server and no network.

test_redis_cache.py drives the cache primitive on its own; these tests drive the
places the bot actually uses it -- the cached display count, the list key, the
tick's sweep lease -- against a fake async client injected through the cache's
own `connect` seam, plus the "Redis is not there" cases that must behave exactly
like the pre-Redis bot.

Every case here is one of the failures the integration has to survive: a
duplicate click, a Redis outage mid-join, a stale key that a committed write just
made wrong, and a lease that is held, refused, or unreachable. Turso stays the
only source of truth in all of them.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import unittest
import uuid

from giveaway_bot import views
from giveaway_bot.bot import GiveawayBot, wire_commands
from giveaway_bot.config import Settings
from giveaway_bot.db import Database
from giveaway_bot.redis_cache import RedisCache
from giveaway_bot.service import GiveawayService
from tests.test_bot import FakeGuild, FakeInteraction, FakeMember, FakeTextChannel


class FakeRedis:
    """Async stand-in for the client the cache would otherwise build itself."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail = fail

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def get(self, key: str):
        self.calls.append(("get", (key,), {}))
        self._maybe_fail()
        return self.data.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None):
        self.calls.append(("set", (key, value), {"nx": nx, "px": px}))
        self._maybe_fail()
        if nx and key in self.data:
            return None
        self.data[key] = value
        if px is not None:
            self.ttls[key] = px
        return True

    async def delete(self, *keys: str) -> int:
        self.calls.append(("delete", keys, {}))
        self._maybe_fail()
        removed = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                removed += 1
        return removed

    async def eval(self, script: str, numkeys: int, *args):
        self.calls.append(("eval", (script, numkeys, *args), {}))
        self._maybe_fail()
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


def cache_for(fake: FakeRedis) -> RedisCache:
    """A cache whose only client is `fake`: no socket, no URL, no network."""
    return RedisCache(url="redis://example.invalid:6379/0", connect=lambda: fake)


def failing_cache(message: str = "connection refused") -> RedisCache:
    """A cache wired to a client object that cannot answer anything.

    Not a degraded stub of the cache: the real RedisCache, the real breaker, the
    real op budget. Only the client is hostile, which is what an unreachable or
    over-quota server looks like from in here.
    """
    return RedisCache(
        url="redis://example.invalid:6379/0",
        connect=lambda: (_ for _ in ()).throw(ConnectionError(message)),
    )


class ServiceTestCase(unittest.TestCase):
    """A real SQLite database plus a real service holding a fake-backed cache."""

    def setUp(self) -> None:
        self.uri = f"file:gwcache_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(self.uri, uri=True)
        self.db = Database(
            Settings(turso_url=""),
            connect=lambda: sqlite3.connect(self.uri, uri=True, check_same_thread=False),
        )
        self.db.init_schema()
        self.fake = FakeRedis()
        self.cache = cache_for(self.fake)
        self.svc = GiveawayService(self.db, self.cache)
        self.guild = "111111111111111111"
        self.reads: list[str] = []
        self._watch_reads()

    def _watch_reads(self) -> None:
        """Count the SQL this service sends, so a cache hit is provable.

        query_one() delegates to query(), so wrapping the one entry point is
        enough to see every read the service makes.
        """
        original = self.db.query

        def counting(sql, params=()):
            self.reads.append(sql)
            return original(sql, params)

        self.db.query = counting  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.db.close_all()
        self.keeper.close()

    def make(self, **overrides):
        kwargs = {
            "guild_id": self.guild,
            "channel_id": "2" * 18,
            "prize": "Steam key",
            "winner_count": 1,
            "duration_seconds": 600,
            "created_by": "9" * 18,
        }
        kwargs.update(overrides)
        return self.svc.create(**kwargs)

    def join(self, gw, user_id: str) -> int:
        return self.svc.join(
            gw,
            user_id=user_id,
            username="member",
            member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
        )

    def count_reads(self) -> int:
        return sum(1 for sql in self.reads if "entrant_count" in sql)


class CachedCountTests(ServiceTestCase):
    """The display count: cached while it is right, dropped when it is not."""

    def test_cached_count_heals_after_a_committed_join(self) -> None:
        gw = self.make()
        self.assertEqual(asyncio.run(self.svc.entry_count_cached(gw.id)), 0)
        self.assertEqual(self.fake.data, {}, "a zero count is not worth a cached key")
        self.join(gw, "5" * 18)
        asyncio.run(self.svc.flush_invalidations())
        self.assertEqual(
            asyncio.run(self.svc.entry_count_cached(gw.id)), 1, "the stale key must not survive"
        )
        reads = self.count_reads()
        self.assertEqual(asyncio.run(self.svc.entry_count_cached(gw.id)), 1)
        self.assertEqual(self.count_reads(), reads, "the healed count is served from the cache")

    def test_a_committed_join_marks_the_count_key(self) -> None:
        gw = self.make()
        self.join(gw, "6" * 18)
        self.assertIn(f"count:{gw.id}", self.svc.drain_invalidations())

    def test_a_leave_marks_the_count_key(self) -> None:
        gw = self.make()
        self.join(gw, "7" * 18)
        self.svc.drain_invalidations()
        self.assertTrue(self.svc.leave(gw.id, "7" * 18))
        self.assertIn(f"count:{gw.id}", self.svc.drain_invalidations())

    def test_a_bulk_drop_marks_the_count_key(self) -> None:
        gw = self.make()
        self.join(gw, "8" * 18)
        self.svc.drain_invalidations()
        self.svc.blacklist_add(self.guild, "8" * 18, hours=0, reason="test")
        self.assertIn(f"count:{gw.id}", self.svc.drain_invalidations())

    def test_a_discarded_giveaway_leaves_the_active_list(self) -> None:
        gw = self.make()
        self.svc.discard(gw.id)
        spilled = self.svc.drain_invalidations()
        self.assertIn("all:active", spilled)
        self.assertIn(f"count:{gw.id}", spilled)
        self.assertEqual(asyncio.run(self.svc.flush_invalidations()), 0, "nothing cached yet")

    def test_flush_sends_every_stale_key_in_one_call(self) -> None:
        gw = self.make()
        self.join(gw, "9" * 18)
        asyncio.run(self.svc.flush_invalidations())
        deletes = [c for c in self.fake.calls if c[0] == "delete"]
        self.assertEqual(len(deletes), 1, "one DEL for the whole batch")
        self.assertIn(f"gwbot:v1:count:{gw.id}", deletes[0][1])
        self.assertEqual(self.svc.drain_invalidations(), [], "the queue is emptied")


class CachedSettingsTests(ServiceTestCase):
    """Reads that are not a count still get exactly one cached answer."""

    def test_a_notify_role_is_marked_for_guilds_that_never_set_one(self) -> None:
        self.assertIsNone(asyncio.run(self.svc.get_notify_role_cached(self.guild)))
        reads = len(self.reads)
        self.assertIsNone(asyncio.run(self.svc.get_notify_role_cached(self.guild)))
        self.assertEqual(len(self.reads), reads, "the 'none' marker must be a hit")
        self.svc.set_notify_role(self.guild, "4242")
        asyncio.run(self.svc.invalidate_notify_role(self.guild))
        self.assertEqual(asyncio.run(self.svc.get_notify_role_cached(self.guild)), "4242")


class RedisOutageTests(unittest.IsolatedAsyncioTestCase):
    """Redis being wrong, slow, or absent never changes a decision."""

    async def test_every_read_of_a_dead_server_degrades_to_no_value(self) -> None:
        cache = failing_cache()
        self.assertIsNone(await cache.get("count:gw_1"))
        self.assertFalse(await cache.set("count:gw_1", "3", 4.0))
        self.assertEqual(await cache.delete("count:gw_1"), 0)
        self.assertIsNone(await cache.incr_with_ttl("count:gw_1", 4.0))
        self.assertIsNone(await cache.ping_ms())
        token, answered = await cache.acquire_checked("sweep:live", 5.0)
        self.assertIsNone(token)
        self.assertFalse(answered, "no answer is not the same as somebody else holding it")
        self.assertFalse(await cache.release("sweep:live", "0" * 32))

    async def test_the_breaker_stops_hammering_a_dead_server(self) -> None:
        cache = failing_cache()
        for _ in range(6):
            await cache.get("count:gw_1")
        skipped = cache.stats()["counters"]["skipped"]
        for _ in range(20):
            await cache.get("count:gw_1")
        self.assertEqual(cache.stats()["counters"]["skipped"], skipped + 20)
        self.assertFalse(cache.available)

    async def test_a_dead_cache_leaves_the_service_working(self) -> None:
        uri = f"file:gwdown_{uuid.uuid4().hex}?mode=memory&cache=shared"
        keeper = sqlite3.connect(uri, uri=True)
        db = Database(
            Settings(turso_url=""),
            connect=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
        )
        db.init_schema()
        try:
            svc = GiveawayService(db, failing_cache())
            gw = svc.create(
                guild_id="111111111111111111",
                channel_id="2" * 18,
                prize="Nitro",
                winner_count=1,
                duration_seconds=600,
                created_by="9" * 18,
            )
            self.assertEqual(svc.entry_count(gw.id), 0)
            svc.join(
                gw,
                user_id="5" * 18,
                username="member",
                member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
            self.assertEqual(svc.entry_count(gw.id), 1)
            self.assertEqual(
                await svc.flush_invalidations(), 0, "a failed DEL is reported, not raised"
            )
            active = await svc.list_active_cached(gw.guild_id)
            self.assertEqual([g.id for g in active], [gw.id])
        finally:
            db.close_all()
            keeper.close()

    async def test_a_raising_client_never_breaks_a_write_path(self) -> None:
        uri = f"file:gwdel_{uuid.uuid4().hex}?mode=memory&cache=shared"
        keeper = sqlite3.connect(uri, uri=True)
        db = Database(
            Settings(turso_url=""),
            connect=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
        )
        db.init_schema()
        try:
            svc = GiveawayService(db, cache_for(FakeRedis(fail=ConnectionError("gone"))))
            gw = svc.create(
                guild_id="111111111111111111",
                channel_id="2" * 18,
                prize="Nitro",
                winner_count=1,
                duration_seconds=600,
                created_by="9" * 18,
            )
            count = svc.join(
                gw,
                user_id="6" * 18,
                username="member",
                member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
            self.assertEqual(count, 1, "the row landed even though the cache was broken")
            self.assertEqual(svc.entry_count(gw.id), 1)
            self.assertEqual(await svc.flush_invalidations(), 0)
        finally:
            db.close_all()
            keeper.close()


class CachedBotTestCase(unittest.TestCase):
    """A bot whose service holds a cache backed by a fake client."""

    def setUp(self) -> None:
        self.uri = f"file:gwbotcache_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(self.uri, uri=True)
        self.db = Database(
            Settings(turso_url=""),
            connect=lambda: sqlite3.connect(self.uri, uri=True, check_same_thread=False),
        )
        self.db.init_schema()
        self.settings = Settings(turso_url="", dashboard_url="https://dash.test/")
        self.fake = FakeRedis()
        self.cache = cache_for(self.fake)
        self.bot = GiveawayBot(self.settings, self.db, self.cache)
        self.svc = GiveawayService(self.db, self.cache)
        self.guild = "111111111111111111"
        wire_commands(self.bot)
        self.bot.register_components()

    def tearDown(self) -> None:
        views._HANDLERS.clear()
        self.db.close_all()
        self.keeper.close()

    def make(self, **overrides):
        kwargs = {
            "guild_id": self.guild,
            "channel_id": "2" * 18,
            "prize": "Nitro",
            "winner_count": 1,
            "duration_seconds": 600,
            "created_by": "9" * 18,
        }
        kwargs.update(overrides)
        return self.svc.create(**kwargs)

    def press_join(self, giveaway_id: str, member) -> FakeInteraction:
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_join(interaction, giveaway_id))
        return interaction


class CachedEmbedTests(CachedBotTestCase):
    """The number the embed shows must be a number, not an awaited coroutine."""

    def test_a_warm_count_is_rendered_as_a_number(self) -> None:
        gw = self.make()
        member = FakeMember(777777777777777777)
        self.press_join(gw.id, member)
        channel = FakeTextChannel()
        self.bot.get_channel = lambda channel_id: channel  # type: ignore[assignment]
        self.svc.set_message(gw.id, str(channel.message.id))
        # Warm the display count the way a repeat refresh would find it.
        self.assertEqual(asyncio.run(self.bot.service.entry_count_cached(gw.id)), 1)
        asyncio.run(self.bot._refresh_embed(self.svc.get(gw.id)))
        self.assertEqual(len(channel.message.edits), 1, "the embed must be edited once")
        rendered = str(channel.message.edits[0]["embed"].to_dict())
        self.assertNotIn("coroutine", rendered.lower())
        self.assertNotIn("object at 0x", rendered)


class DuplicateClickTests(CachedBotTestCase):
    """A second tap of the same button must not reach the database at all."""

    def test_the_second_tap_is_refused_and_the_row_stays_single(self) -> None:
        gw = self.make()
        member = FakeMember(555555555555555555)
        first = self.press_join(gw.id, member)
        self.assertTrue(any("You're in!" in text for text in first.texts))
        self.assertEqual(self.svc.entry_count(gw.id), 1)
        calls = len(self.fake.calls)
        second = self.press_join(gw.id, member)
        self.assertTrue(any("already entered" in text for text in second.texts))
        self.assertEqual(self.svc.entry_count(gw.id), 1)
        self.assertEqual(len(self.fake.calls), calls, "a refused tap must not touch the cache")

    def test_a_committed_join_is_acked_when_redis_is_down(self) -> None:
        self.bot.service._redis = failing_cache()
        gw = self.make()
        member = FakeMember(666666666666666666)
        interaction = self.press_join(gw.id, member)
        self.assertTrue(any("You're in!" in text for text in interaction.texts))
        self.assertTrue(self.svc.has_entry(gw.id, str(member.id)))


class SweepLeaseTests(CachedBotTestCase):
    """One poll body per window: a lease may only narrow who sweeps."""

    def run_tick(self, reads: list[str]) -> None:
        def spy(limit=200):
            reads.append("list")
            return []

        self.bot.service.list_all_active = spy  # type: ignore[method-assign]
        asyncio.run(self.bot.tick.coro(self.bot))

    def test_a_held_lease_skips_the_duplicate_sweep(self) -> None:
        self.make()
        lease = asyncio.run(self.cache.acquire("sweep:live", 90.0))
        self.assertIsNotNone(lease, "the lease must be taken first")
        reads: list[str] = []
        self.run_tick(reads)
        self.assertEqual(reads, [], "another instance is already sweeping this window")
        self.assertEqual(self.bot._last_list_refresh, 0.0)

    def test_an_unreachable_redis_still_runs_the_sweep(self) -> None:
        self.bot.redis = failing_cache()
        self.make()
        reads: list[str] = []
        self.run_tick(reads)
        self.assertEqual(reads, ["list"], "an unreachable Redis must not skip the sweep")
        self.assertGreater(self.bot._last_list_refresh, 0.0)

    def test_the_lease_is_released_after_the_sweep(self) -> None:
        self.make()
        reads: list[str] = []
        self.run_tick(reads)
        self.assertEqual(reads, ["list"])
        self.assertEqual(
            [key for key in self.fake.data if key.endswith("sweep:live")],
            [],
            "a finished sweep must free the lease",
        )


if __name__ == "__main__":
    unittest.main()
