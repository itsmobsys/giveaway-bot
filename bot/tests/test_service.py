"""Rules-level tests, run against a real SQLite database.

The bot talks to Turso through libsql, but libsql speaks the sqlite3 API, so the
whole rules layer (service.py) and the schema layer (db.py) can be exercised for
real -- real SQL, real constraints, real upserts -- by handing Database a
sqlite3 connection. No Turso account, no network, no mocks of the SQL itself.

Run from the bot/ directory:

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
import uuid

from giveaway_bot.config import Settings
from giveaway_bot.db import Database, _is_write
from giveaway_bot.service import GiveawayService, ServiceError, now_ms

DAY_MS = 86_400_000


class ServiceTestCase(unittest.TestCase):
    """Base: a fresh SQLite database per test.

    A shared-cache in-memory URI rather than a file, so the tests touch no disk
    at all. The plain ":memory:" spelling cannot be used: every new connection
    gets its own private database, so a connection recycled by the dead-stream
    retry would silently come back with an empty schema.
    """

    def setUp(self) -> None:
        self.uri = f"file:gwtest_{uuid.uuid4().hex}?mode=memory&cache=shared"
        #: Holds the in-memory database open; it disappears with its last
        #: connection, so this one lives for the whole test.
        self.keeper = sqlite3.connect(self.uri, uri=True)
        self.db = Database(Settings(turso_url=""), connect=self._connect)
        self.db.init_schema()
        self.svc = GiveawayService(self.db)
        self.guild = "111111111111111111"

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.uri, uri=True, check_same_thread=False)

    def tearDown(self) -> None:
        self.db.close_all()
        self.keeper.close()

    # -- helpers --------------------------------------------------------
    def make(self, **overrides):
        kwargs = {
            "guild_id": self.guild,
            "channel_id": "222222222222222222",
            "prize": "Steam key",
            "winner_count": 1,
            "duration_seconds": 60,
            "created_by": "999999999999999999",
        }
        kwargs.update(overrides)
        return self.svc.create(**kwargs)

    #: Sentinel meaning "pass None through as the account age".
    UNKNOWN_AGE = "unknown"

    def join(self, gw, user_id="333333333333333333", roles=(), created=None, **kw):
        if created == self.UNKNOWN_AGE:
            age = None
        elif created is None:
            age = time.time() - 400 * 86400
        else:
            age = created
        return self.svc.join(
            gw,
            user_id=user_id,
            username="user" + user_id[-3:],
            member_roles=list(roles),
            account_created_ts=age,
            **kw,
        )


class SchemaTests(ServiceTestCase):
    def test_init_schema_is_idempotent(self) -> None:
        self.db.init_schema()
        self.db.init_schema()
        cols = {str(r["name"]) for r in self.db.query("PRAGMA table_info(simple_giveaways)")}
        for name in (
            "id", "prize", "winner_count", "ends_at", "status", "required_role_id",
            "required_role_ids", "blocked_role_id", "entrants_role_id", "winners_json",
        ):
            self.assertIn(name, cols)
        entry_cols = {str(r["name"]) for r in self.db.query("PRAGMA table_info(simple_entries)")}
        self.assertEqual(entry_cols, {"giveaway_id", "user_id", "username", "entered_at"})

    def test_indexes_exist_including_user_lookup(self) -> None:
        names = {str(r["name"]) for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        self.assertIn("idx_simple_gw_status_ends", names)
        self.assertIn("idx_simple_entries_giveaway", names)
        self.assertIn("idx_simple_entries_user", names, "blacklist purge needs this")

    def test_engine_agnostic_defaults(self) -> None:
        self.assertEqual(self.db.backend, "injected")
        with self.assertRaises(RuntimeError) as ctx:
            Database(Settings(turso_url="", turso_token=""))
        self.assertIn("TURSO_DATABASE_URL", str(ctx.exception))


class WriteClassificationTests(unittest.TestCase):
    def test_writes_need_a_commit(self) -> None:
        for sql in (
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "  insert into t values (1)",
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
            "REPLACE INTO t VALUES (1)",
        ):
            self.assertTrue(_is_write(sql), sql)

    def test_reads_do_not(self) -> None:
        for sql in (
            "SELECT 1",
            "  select * from t",
            "PRAGMA table_info(t)",
            "EXPLAIN SELECT 1",
            "VALUES (1)",
            "",
        ):
            self.assertFalse(_is_write(sql), sql)


class CreateTests(ServiceTestCase):
    def test_defaults_and_round_trip(self) -> None:
        gw = self.make(prize="  Nitro  ", required_role_ids=["7", "8"], host_name="Mod")
        self.assertTrue(gw.id.startswith("gw_"))
        self.assertEqual(gw.prize, "Nitro", "prize is stripped")
        self.assertEqual(gw.status, "active")
        self.assertTrue(gw.active)
        self.assertEqual(gw.required_role_ids, ["7", "8"])
        self.assertEqual(gw.required_role_id, "7", "first role mirrors the legacy column")
        self.assertEqual(gw.host_id, "999999999999999999", "host defaults to the creator")
        self.assertEqual(gw.winner_count, 1)
        self.assertEqual(gw.winners, [])
        self.assertAlmostEqual(gw.ends_at, now_ms() + 60_000, delta=5000)

    def test_validation(self) -> None:
        cases = [
            ({"prize": ""}, "prize"),
            ({"prize": "x" * 257}, "prize"),
            ({"winner_count": 0}, "winner"),
            ({"winner_count": 26}, "winner"),
            ({"duration_seconds": 10}, "duration"),
            ({"duration_seconds": 61 * 86400}, "duration"),
            ({"min_messages": -1}, "messages"),
            ({"min_messages": 100_001}, "messages"),
            ({"min_account_age_days": -1}, "account age"),
            ({"min_account_age_days": 4000}, "account age"),
            ({"image_url": "ftp://x/y.png"}, "http"),
            ({"image_url": "https://x/" + "a" * 600}, "http"),
        ]
        for overrides, needle in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ServiceError) as ctx:
                    self.make(**overrides)
                self.assertIn(needle, str(ctx.exception.message).lower())

    def test_role_list_is_capped_and_deduplicated(self) -> None:
        gw = self.make(required_role_ids=["1", "2", "3", "4", "5", "6", "7"])
        self.assertEqual(gw.required_role_ids, ["1", "2", "3", "4", "5"])
        gw2 = self.make(required_role_id="9", required_role_ids=["9", "8"])
        self.assertEqual(gw2.required_role_ids, ["9", "8"])

    def test_legacy_required_role_column_is_merged(self) -> None:
        gw = self.make()
        self.db.execute(
            "UPDATE simple_giveaways SET required_role_id = ?, required_role_ids = ? WHERE id = ?",
            ("42", json.dumps([]), gw.id),
        )
        self.assertEqual(self.svc.get(gw.id).required_role_ids, ["42"])

    def test_get_missing_raises(self) -> None:
        with self.assertRaises(ServiceError):
            self.svc.get("gw_nope")


class LookupTests(ServiceTestCase):
    def test_list_active_is_guild_scoped_and_ordered(self) -> None:
        first = self.make(prize="first", duration_seconds=3600)
        second = self.make(prize="second", duration_seconds=60)
        third = self.make(prize="other guild", guild_id="555", duration_seconds=30)
        ids = [g.id for g in self.svc.list_active(self.guild)]
        self.assertEqual(ids, [second.id, first.id])
        self.assertEqual(len(self.svc.list_all_active(200)), 3)
        # due() is deliberately global: it is the auto-draw sweep, so it sees
        # every guild, soonest deadline first.
        self.assertEqual(
            [g.id for g in self.svc.due(now_ms() + 10 * DAY_MS)],
            [third.id, second.id, first.id],
        )

    def test_resolve_refuses_another_guild(self) -> None:
        other = self.make(guild_id="555")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve(self.guild, other.id)
        self.assertIn("another server", str(ctx.exception.message))

    def test_resolve_falls_back_to_the_only_live_one(self) -> None:
        only = self.make()
        self.assertEqual(self.svc.resolve(self.guild, "gw_typo").id, only.id)
        self.assertEqual(self.svc.resolve(self.guild, "").id, only.id)

    def test_resolve_needs_an_id_when_several_are_live(self) -> None:
        a = self.make(prize="a")
        self.make(prize="b")
        self.assertEqual(self.svc.resolve(self.guild, a.id).id, a.id)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve(self.guild, "gw_typo")
        self.assertIn("did not match", str(ctx.exception.message))

    def test_resolve_with_nothing_live(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve(self.guild, "gw_x")
        self.assertIn("No active giveaway", str(ctx.exception.message))

    def test_get_by_message(self) -> None:
        gw = self.make()
        self.svc.set_message(gw.id, "12345")
        found = self.svc.get_by_message("12345")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, gw.id)
        self.assertIsNone(self.svc.get_by_message("999"))


class JoinTests(ServiceTestCase):
    def test_join_then_leave(self) -> None:
        gw = self.make()
        self.assertEqual(self.join(gw), 1)
        self.assertEqual(self.svc.entry_count(gw.id), 1)
        self.assertEqual(self.join(gw, user_id="444444444444444444"), 2)
        self.assertTrue(self.svc.leave(gw.id, "333333333333333333"))
        self.assertFalse(self.svc.leave(gw.id, "333333333333333333"))
        self.assertEqual(self.svc.entry_count(gw.id), 1)

    def test_double_join_is_a_service_error(self) -> None:
        gw = self.make()
        self.join(gw)
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw)
        self.assertIn("already entered", str(ctx.exception.message))

    def test_required_roles_need_one_match(self) -> None:
        gw = self.make(required_role_ids=["10", "11"])
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, roles=["99"])
        self.assertIn("required roles", str(ctx.exception.message))
        self.assertEqual(self.join(gw, user_id="1" * 18, roles=["11"]), 1)

    def test_blocked_role_cannot_enter(self) -> None:
        gw = self.make(blocked_role_id="77")
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, roles=["77"])
        self.assertIn("not allowed", str(ctx.exception.message))

    def test_account_age_is_enforced_and_fails_closed(self) -> None:
        gw = self.make(min_account_age_days=30)
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, created=time.time() - 5 * 86400)
        self.assertIn("30+ days", str(ctx.exception.message))
        self.assertEqual(self.join(gw, user_id="2" * 18), 1)
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, user_id="3" * 18, created=self.UNKNOWN_AGE)
        self.assertIn("account age", str(ctx.exception.message))

    def test_min_messages_uses_the_database_count(self) -> None:
        gw = self.make(min_messages=3)
        for _ in range(2):
            self.svc.record_message(self.guild, "5" * 18)
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, user_id="5" * 18)
        self.assertIn("(2 counted)", str(ctx.exception.message))
        self.svc.record_message(self.guild, "5" * 18)
        self.assertEqual(self.join(gw, user_id="5" * 18), 1)

    def test_min_messages_counts_the_unflushed_buffer(self) -> None:
        gw = self.make(min_messages=3)
        self.svc.record_message(self.guild, "6" * 18)
        with self.assertRaises(ServiceError):
            self.join(gw, user_id="6" * 18)
        # Two messages are still buffered in memory by the bot.
        self.assertEqual(self.join(gw, user_id="6" * 18, pending_messages=2), 1)

    def test_ended_giveaway_refuses_joins(self) -> None:
        gw = self.make()
        self.svc.end(gw.id)
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw)
        self.assertIn("ended", str(ctx.exception.message))

    def test_expired_deadline_refuses_joins(self) -> None:
        gw = self.make()
        self.db.execute("UPDATE simple_giveaways SET ends_at = ? WHERE id = ?", (now_ms() - 1, gw.id))
        with self.assertRaises(ServiceError) as ctx:
            self.join(self.svc.get(gw.id))
        self.assertIn("ended", str(ctx.exception.message))


class MessageCountTests(ServiceTestCase):
    def test_record_message_increments(self) -> None:
        self.svc.record_message(self.guild, "7" * 18)
        self.svc.record_message(self.guild, "7" * 18)
        self.assertEqual(self.svc.message_count(self.guild, "7" * 18), 2)
        self.assertEqual(self.svc.message_count(self.guild, "8" * 18), 0)
        self.assertEqual(self.svc.message_count("999", "7" * 18), 0)

    def test_batch_flush_adds_deltas(self) -> None:
        self.svc.record_message(self.guild, "a" * 18)
        self.svc.add_message_counts([(self.guild, "a" * 18, 5), (self.guild, "b" * 18, 3)])
        self.assertEqual(self.svc.message_count(self.guild, "a" * 18), 6)
        self.assertEqual(self.svc.message_count(self.guild, "b" * 18), 3)

    def test_batch_flush_chunks_large_payloads(self) -> None:
        rows = [(self.guild, f"u{i:05d}", 1) for i in range(700)]
        self.svc.add_message_counts(rows)
        self.assertEqual(self.svc.message_count(self.guild, "u00000"), 1)
        self.assertEqual(self.svc.message_count(self.guild, "u00699"), 1)
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_message_counts")["n"], 700)

    def test_end_resets_counts_for_the_next_grind(self) -> None:
        gw = self.make()
        self.svc.record_message(self.guild, "c" * 18)
        self.svc.end(gw.id)
        self.assertEqual(self.svc.message_count(self.guild, "c" * 18), 0)


class DrawTests(ServiceTestCase):
    def test_end_draws_and_is_idempotent(self) -> None:
        gw = self.make(winner_count=2)
        for i in range(5):
            self.join(gw, user_id=str(10 ** 17 + i))
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 2)
        self.assertEqual(sorted(ended.winners), sorted(winners))
        self.assertEqual(ended.status, "ended")
        again, winners2 = self.svc.end(gw.id)
        self.assertEqual(winners2, winners, "re-ending must not redraw")
        self.assertEqual(again.status, "ended")

    def test_end_without_entries_is_safe(self) -> None:
        ended, winners = self.svc.end(self.make().id)
        self.assertEqual(winners, [])
        self.assertEqual(ended.winners, [])

    def test_winners_never_exceed_the_pool(self) -> None:
        gw = self.make(winner_count=25)
        self.join(gw, user_id="1" * 18)
        self.join(gw, user_id="2" * 18)
        _, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 2)

    def test_reroll_excludes_previous_winners(self) -> None:
        gw = self.make()
        for i in range(4):
            self.join(gw, user_id=str(10 ** 17 + i))
        _, first = self.svc.end(gw.id)
        after, fresh = self.svc.reroll(gw.id, 1)
        self.assertEqual(len(fresh), 1)
        self.assertNotIn(fresh[0], first)
        self.assertEqual(sorted(after.winners), sorted(first + fresh))

    def test_reroll_falls_back_when_the_pool_is_exhausted(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        _, first = self.svc.end(gw.id)
        _, fresh = self.svc.reroll(gw.id, 1)
        self.assertEqual(fresh, first, "only entrant is redrawn rather than failing")

    def test_reroll_refuses_a_running_giveaway(self) -> None:
        gw = self.make()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(gw.id, 1)
        self.assertIn("End the giveaway", str(ctx.exception.message))

    def test_extend_and_cancel(self) -> None:
        gw = self.make(duration_seconds=600)
        extended = self.svc.extend(gw.id, 300)
        self.assertEqual(extended.ends_at, gw.ends_at + 300_000)
        for bad in (0, 59, 61 * 86400):
            with self.assertRaises(ServiceError):
                self.svc.extend(gw.id, bad)
        cancelled = self.svc.cancel(gw.id)
        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(ServiceError):
            self.svc.cancel(gw.id)
        with self.assertRaises(ServiceError):
            self.svc.extend(gw.id, 600)

    def test_discard_removes_a_never_posted_giveaway(self) -> None:
        gw = self.make()
        self.join(gw)
        self.svc.discard(gw.id)
        with self.assertRaises(ServiceError):
            self.svc.get(gw.id)
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertEqual(self.svc.list_active(self.guild), [])


class RetentionTests(ServiceTestCase):
    def test_leftover_entrants_roles_are_reported(self) -> None:
        gw = self.make()
        self.svc.set_entrants_role(gw.id, "4242")
        self.assertEqual(self.svc.ended_with_roles(), [], "still running")
        self.svc.end(gw.id)
        leftovers = self.svc.ended_with_roles()
        self.assertEqual([g.id for g in leftovers], [gw.id])
        self.svc.set_entrants_role(gw.id, None)
        self.assertEqual(self.svc.ended_with_roles(), [], "cleaned up")

    def test_entry_rows_are_wiped_after_the_retention_window(self) -> None:
        old = self.make()
        self.join(old)
        self.svc.end(old.id)
        fresh = self.make()
        self.join(fresh, user_id="9" * 18)
        self.svc.end(fresh.id)

        # Push the old one past the retention window; the fresh one stays inside
        # it because it ended moments ago.
        self.db.execute(
            "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
            (now_ms() - self.svc.ENTRY_RETENTION_MS - 60_000, old.id),
        )
        self.svc.wipe_stale_entries()
        self.assertEqual(self.svc.entry_count(old.id), 0, "old entry rows are gone")
        self.assertEqual(self.svc.entry_count(fresh.id), 1, "recent ones are kept")
        self.assertEqual(len(self.svc.get(old.id).winners), 1, "the record and winners stay")


class BlacklistTests(ServiceTestCase):
    def test_add_blocks_and_purges_only_running_giveaways(self) -> None:
        running = self.make()
        ended = self.make()
        self.join(running, user_id="1" * 18)
        self.join(ended, user_id="1" * 18)
        self.svc.end(ended.id)

        self.svc.blacklist_add(self.guild, "1" * 18)
        self.assertTrue(self.svc.is_blacklisted(self.guild, "1" * 18))
        self.assertFalse(self.svc.is_blacklisted("999", "1" * 18))
        self.assertFalse(self.svc.is_blacklisted(self.guild, ""))
        self.assertEqual(self.svc.entry_count(running.id), 0)
        self.assertEqual(self.svc.entry_count(ended.id), 1, "history is untouched")
        with self.assertRaises(ServiceError) as ctx:
            self.join(running, user_id="1" * 18)
        self.assertIn("blocked", str(ctx.exception.message))

    def test_remove_and_list(self) -> None:
        for user_id in ("1" * 18, "2" * 18):
            self.svc.blacklist_add(self.guild, user_id)
        self.svc.blacklist_add(self.guild, "2" * 18)
        self.assertEqual(self.svc.blacklist_list(self.guild), ["1" * 18, "2" * 18])
        self.assertTrue(self.svc.blacklist_remove(self.guild, "1" * 18))
        self.assertFalse(self.svc.blacklist_remove(self.guild, "1" * 18))
        self.assertEqual(self.svc.blacklist_list(self.guild), ["2" * 18])
        self.assertFalse(self.svc.is_blacklisted(self.guild, "1" * 18))


class GuildSettingTests(ServiceTestCase):
    def test_notify_role_upsert(self) -> None:
        self.assertIsNone(self.svc.get_notify_role(self.guild))
        self.svc.set_notify_role(self.guild, "123")
        self.assertEqual(self.svc.get_notify_role(self.guild), "123")
        self.svc.set_notify_role(self.guild, "456")
        self.assertEqual(self.svc.get_notify_role(self.guild), "456", "upsert, not insert")
        self.svc.set_notify_role(self.guild, None)
        self.assertIsNone(self.svc.get_notify_role(self.guild))
        self.assertIsNone(self.svc.get_notify_role("999"))


if __name__ == "__main__":
    unittest.main()
