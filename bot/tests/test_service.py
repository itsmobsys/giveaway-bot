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
import threading
import time
import unittest
import unittest.mock
import uuid

from giveaway_bot.config import Settings
from giveaway_bot.db import Database, _is_write
from giveaway_bot.service import (
    TIMEOUT_BAN_KIND,
    GiveawayService,
    PartialFlush,
    ServiceError,
    now_ms,
)

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
            "entrant_count",
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

    def test_leave_after_the_end_keeps_the_entry(self) -> None:
        gw = self.make()
        self.join(gw)
        self.svc.end(gw.id)
        self.assertFalse(self.svc.leave(gw.id, "333333333333333333"))
        self.assertEqual(self.svc.entry_count(gw.id), 1, "the drawn pool is not rewritten")

    def test_a_non_duplicate_constraint_error_is_not_called_already_entered(self) -> None:
        gw = self.make()
        original = self.db.execute

        def failing(sql, params=()):
            if sql.lstrip().startswith("INSERT INTO simple_entries"):
                raise sqlite3.IntegrityError("NOT NULL constraint failed: simple_entries.username")
            return original(sql, params)

        self.db.execute = failing  # type: ignore[method-assign]
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.join(gw)
        finally:
            self.db.execute = original  # type: ignore[method-assign]

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

    def test_has_entry_is_a_targeted_lookup(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        self.assertTrue(self.svc.has_entry(gw.id, "1" * 18))
        self.assertFalse(self.svc.has_entry(gw.id, "2" * 18))
        self.assertFalse(self.svc.has_entry(gw.id, ""))

    def test_entries_can_be_limited_for_a_display_path(self) -> None:
        gw = self.make()
        for index in range(5):
            self.join(gw, user_id=f"{index + 1:0>18}")
        full = self.svc.entries(gw.id)
        self.assertEqual(len(full), 5)
        self.assertEqual(self.svc.entries(gw.id, 2), full[:2], "oldest first, same order")
        self.assertEqual(self.svc.entry_count(gw.id), 5, "the count is still the whole pool")
        self.assertEqual(self.svc.entries(gw.id, 0), full[:1], "never an unbounded read")

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

    def test_ambiguous_chunk_failure_requeues_only_what_is_missing(self) -> None:
        # The write landed but its response was lost: the retry must not add
        # the chunk a second time.
        original = self.db.execute

        def flaky(sql, params=()):
            if "simple_message_counts" in sql and "INSERT" in sql:
                original(sql, params)  # applied, then the response is "lost"
                raise OSError("connection reset by peer")
            return original(sql, params)

        rows = [(self.guild, "a" * 18, 2), (self.guild, "b" * 18, 3)]
        with self.assertRaises(PartialFlush), unittest.mock.patch.object(self.db, "execute", flaky):
            self.svc.add_message_counts(rows)
        # rows was reconciled in place: the retry adds nothing twice.
        self.assertEqual(self.svc.message_count(self.guild, "a" * 18), 2)
        self.assertEqual(self.svc.message_count(self.guild, "b" * 18), 3)

    def test_deterministic_chunk_failure_requeues_the_whole_chunk(self) -> None:
        original = self.db.execute

        def boom(sql, params=()):
            if "INSERT" in sql and "simple_message_counts" in sql:
                raise OSError("disk is full")
            return original(sql, params)

        with self.assertRaises(PartialFlush) as ctx, unittest.mock.patch.object(self.db, "execute", boom):
            self.svc.add_message_counts([(self.guild, "a" * 18, 2)])
        self.assertEqual(ctx.exception.applied, 0)
        self.assertEqual(self.svc.message_count(self.guild, "a" * 18), 0)

    def test_end_resets_counts_for_the_next_grind(self) -> None:
        gw = self.make()
        self.svc.record_message(self.guild, "c" * 18)
        self.svc.end(gw.id)
        self.assertEqual(self.svc.message_count(self.guild, "c" * 18), 0)

    def test_end_keeps_counts_while_another_giveaway_needs_them(self) -> None:
        self.make(prize="grind", min_messages=5)
        self.svc.record_message(self.guild, "c" * 18)
        self.svc.end(self.make(prize="plain").id)
        self.assertEqual(
            self.svc.message_count(self.guild, "c" * 18), 1,
            "a running min_messages giveaway must not be reset under its entrants",
        )

    def test_end_resets_only_the_guild_whose_giveaway_ended(self) -> None:
        other = "555555555555555555"
        self.svc.record_message(self.guild, "c" * 18)
        self.svc.record_message(other, "c" * 18)
        self.svc.end(self.make().id)
        self.assertEqual(self.svc.message_count(self.guild, "c" * 18), 0)
        self.assertEqual(
            self.svc.message_count(other, "c" * 18), 1,
            "another server's min_messages counters must survive",
        )


class DrawTests(ServiceTestCase):
    def test_end_draws_once_and_refuses_a_second_call(self) -> None:
        gw = self.make(winner_count=2)
        for i in range(5):
            self.join(gw, user_id=str(10 ** 17 + i))
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 2)
        self.assertEqual(sorted(ended.winners), sorted(winners))
        self.assertEqual(ended.status, "ended")
        # The status flip is the claim, so a second ender cannot draw again —
        # and cannot get a different set of winners to announce either.
        with self.assertRaises(ServiceError) as ctx:
            self.svc.end(gw.id)
        self.assertIn("already ended", str(ctx.exception.message))
        self.assertEqual(sorted(self.svc.get(gw.id).winners), sorted(winners))

    def test_two_end_calls_cannot_both_draw(self) -> None:
        gw = self.make(winner_count=1)
        for i in range(30):
            self.join(gw, user_id=str(10 ** 17 + i))
        winners: list[list[str]] = []
        refusals: list[str] = []
        barrier = threading.Barrier(2, timeout=10)

        def finish() -> None:
            try:
                barrier.wait()
                _, drawn = self.svc.end(gw.id)
                winners.append(drawn)
            except ServiceError as exc:
                refusals.append(str(exc.message))
            except Exception as exc:  # a lost race must not surface as a crash
                refusals.append(f"unexpected: {exc!r}")

        threads = [threading.Thread(target=finish) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(winners), 1, f"exactly one draw, got {winners}")
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn("already ended", refusals[0])
        self.assertEqual(sorted(self.svc.get(gw.id).winners), sorted(winners[0]))

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

    def test_reroll_refuses_when_the_pool_is_exhausted(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        _, first = self.svc.end(gw.id)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(gw.id, 1)
        self.assertIn("No other entrants", str(ctx.exception.message))
        self.assertEqual(self.svc.get(gw.id).winners, first, "winners unchanged")

    def test_reroll_refuses_after_the_entries_were_wiped(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        self.svc.end(gw.id)
        self.svc.wipe_stale_entries(now=now_ms() + 10 * DAY_MS)
        with self.assertRaises(ServiceError):
            self.svc.reroll(gw.id, 1)

    def test_end_stores_the_entrant_count(self) -> None:
        gw = self.make()
        for i in range(3):
            self.join(gw, user_id=str(10 ** 17 + i))
        self.svc.end(gw.id)
        row = self.db.query_one("SELECT entrant_count FROM simple_giveaways WHERE id = ?", (gw.id,))
        self.assertEqual(row["entrant_count"], 3)

    def test_a_failed_end_write_leaves_the_giveaway_active(self) -> None:
        gw = self.make()
        self.join(gw)
        original = self.db.execute

        def failing(sql, params=()):
            if "status = 'ended'" in sql:
                raise RuntimeError("write failed")
            return original(sql, params)

        self.db.execute = failing  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.svc.end(gw.id)
        finally:
            self.db.execute = original  # type: ignore[method-assign]
        self.assertTrue(self.svc.get(gw.id).active, "not stranded as ended with no winners")
        _, winners = self.svc.end(gw.id)
        self.assertEqual(winners, ["333333333333333333"])

    def test_cancel_stores_the_entrant_count_and_loses_a_race_to_end(self) -> None:
        gw = self.make()
        self.join(gw)
        self.svc.cancel(gw.id)
        row = self.db.query_one("SELECT entrant_count FROM simple_giveaways WHERE id = ?", (gw.id,))
        self.assertEqual(row["entrant_count"], 1)
        # A stale read: get() still says active, but end() already claimed it.
        other = self.make(prize="raced")
        stale = self.svc.get(other.id)
        self.svc.end(other.id)
        original = self.svc.get
        self.svc.get = lambda _id: stale  # type: ignore[method-assign]
        try:
            with self.assertRaises(ServiceError):
                self.svc.cancel(other.id)
            with self.assertRaises(ServiceError):
                self.svc.extend(other.id, 600)
        finally:
            self.svc.get = original  # type: ignore[method-assign]
        self.assertEqual(self.svc.get(other.id).status, "ended")

    def test_reroll_refuses_a_running_giveaway(self) -> None:
        gw = self.make()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(gw.id, 1)
        self.assertIn("End the giveaway", str(ctx.exception.message))

    def test_reroll_never_exceeds_the_winner_cap(self) -> None:
        gw = self.make(winner_count=1)
        for i in range(30):
            self.join(gw, user_id=str(10 ** 17 + i))
        self.svc.end(gw.id)
        _, fresh = self.svc.reroll(gw.id, 200)
        self.assertEqual(
            len(self.svc.get(gw.id).winners), 25, "the list stays inside the cap"
        )
        self.assertEqual(len(fresh), 24, "the existing winner leaves room for 24 more")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(gw.id, 1)
        self.assertIn("most one", str(ctx.exception.message))

    def test_a_cancelled_giveaway_cannot_be_rerolled(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        self.svc.cancel(gw.id)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(gw.id, 1)
        self.assertIn("cancelled", str(ctx.exception.message))

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


    def test_rows_without_an_ended_at_still_expire(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        # ended_at arrived as a later column, so giveaways that finished before
        # it existed carry NULL and used to keep their entries forever.
        self.db.execute(
            "UPDATE simple_giveaways SET status = 'cancelled', ended_at = NULL WHERE id = ?",
            (gw.id,),
        )
        self.assertEqual(self.svc.wipe_stale_entries(now=now_ms() + 10 * DAY_MS), 1)


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

    def test_count_matches_the_list(self) -> None:
        self.assertEqual(self.svc.count_blacklisted(self.guild), 0)
        for user_id in ("1" * 18, "2" * 18):
            self.svc.blacklist_add(self.guild, user_id)
        self.assertEqual(self.svc.count_blacklisted(self.guild), 2)
        self.assertEqual(self.svc.count_blacklisted("999"), 0)

    def test_the_purge_leaves_other_guilds_alone(self) -> None:
        other = "555555555555555555"
        here = self.make()
        there = self.make(guild_id=other)
        self.join(here, user_id="1" * 18)
        self.join(there, user_id="1" * 18)
        self.svc.blacklist_add(self.guild, "1" * 18)
        self.assertEqual(self.svc.entry_count(here.id), 0)
        self.assertEqual(self.svc.entry_count(there.id), 1, "another server is untouched")

    def test_a_blacklist_landing_mid_join_still_refuses(self) -> None:
        gw = self.make()
        # The block lands after check_eligible has read the blacklist, which is
        # the window the in-statement guard exists to close.
        self.svc.blacklist_add(self.guild, "1" * 18)
        original = self.svc.is_blacklisted
        calls = {"n": 0}

        def stale_first_read(guild_id: str, user_id: str) -> bool:
            calls["n"] += 1
            return False if calls["n"] == 1 else original(guild_id, user_id)

        self.svc.is_blacklisted = stale_first_read  # type: ignore[method-assign]
        try:
            with self.assertRaises(ServiceError) as ctx:
                self.join(gw, user_id="1" * 18)
        finally:
            self.svc.is_blacklisted = original  # type: ignore[method-assign]
        self.assertIn("blocked", str(ctx.exception.message))
        self.assertEqual(self.svc.entry_count(gw.id), 0, "the guard held")

    def test_remove_and_list(self) -> None:
        for user_id in ("1" * 18, "2" * 18):
            self.svc.blacklist_add(self.guild, user_id)
        self.svc.blacklist_add(self.guild, "2" * 18)
        self.assertEqual(self.svc.blacklist_list(self.guild), ["1" * 18, "2" * 18])
        self.assertTrue(self.svc.blacklist_remove(self.guild, "1" * 18))
        self.assertFalse(self.svc.blacklist_remove(self.guild, "1" * 18))
        self.assertEqual(self.svc.blacklist_list(self.guild), ["2" * 18])
        self.assertFalse(self.svc.is_blacklisted(self.guild, "1" * 18))


class TimeoutBanTests(ServiceTestCase):
    """The penalty for joining while timed out (Discord's native /mute).

    Caught timed out, a member starts a three-giveaway penalty; every giveaway
    they are then blocked from spends one and the restriction ends with the
    third. All of it lives in the database, so a restart cannot forgive it.
    """

    USER = "7" * 18

    def refused(self, gw, **kwargs) -> str:
        """Assert the join is refused and hand back the message."""
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, user_id=self.USER, **kwargs)
        return str(ctx.exception.message)

    def test_a_timed_out_member_is_refused_and_starts_the_penalty(self) -> None:
        gw = self.make()
        message = self.refused(gw, timed_out=True)
        self.assertIn("timed out", message)
        self.assertIn("3 giveaway(s)", message)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 3)
        self.assertEqual(self.svc.entry_count(gw.id), 0, "a refused join leaves no entry")

    def test_a_timeout_refusal_is_tagged_for_the_caller(self) -> None:
        gw = self.make()
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, user_id=self.USER, timed_out=True)
        self.assertEqual(ctx.exception.kind, TIMEOUT_BAN_KIND)
        # An ordinary refusal must stay untagged, so a caller never cleans up
        # after a join that was never blocked by this rule.
        ended = self.make(prize="plain")
        self.svc.end(ended.id)
        with self.assertRaises(ServiceError) as plain:
            self.join(self.svc.get(ended.id), user_id=self.USER)
        self.assertEqual(plain.exception.kind, "")

    def test_an_untimed_out_member_is_unaffected(self) -> None:
        gw = self.make()
        self.assertEqual(self.join(gw, user_id=self.USER), 1)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 0)
        self.assertEqual(self.svc.list_timeout_bans(self.guild), [])

    def test_a_role_is_never_mistaken_for_a_timeout(self) -> None:
        # Only the timed_out flag counts. A role called "Muted" would be just
        # another id in this list, and ids are not what the rule reads.
        self.assertEqual(self.join(self.make(), user_id=self.USER, roles=["5" * 18]), 1)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 0)

    def test_three_blocked_giveaways_spend_the_penalty_and_lift_it(self) -> None:
        self.refused(self.make(prize="trigger"), timed_out=True)
        left = []
        for index in range(3):
            gw = self.make(prize=f"blocked {index}")
            self.assertIn("penalty", self.refused(gw))
            self.assertEqual(self.svc.entry_count(gw.id), 0)
            left.append(self.svc.timeout_ban_remaining(self.guild, self.USER))
        self.assertEqual(left, [2, 1, 0], "one giveaway per blocked attempt")
        self.assertEqual(self.svc.list_timeout_bans(self.guild), [], "gone, not left at zero")
        self.assertEqual(self.join(self.make(prize="free"), user_id=self.USER), 1)

    def test_repeated_attempts_never_stack_or_respend_the_penalty(self) -> None:
        gw = self.make(prize="trigger")
        self.refused(gw, timed_out=True)
        # Still timed out, same giveaway: neither a second penalty nor a second
        # giveaway spent on the one they were already caught in.
        for _ in range(3):
            self.assertIn("3 giveaway(s) left", self.refused(gw, timed_out=True))
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 3)
        # A different giveaway while still timed out spends one and is reported
        # as the running penalty, never as a fresh three.
        other = self.make(prize="other")
        self.assertIn("2 giveaway(s) left", self.refused(other, timed_out=True))

    def test_alternating_giveaways_cannot_burn_the_penalty(self) -> None:
        a = self.make(prize="a")
        b = self.make(prize="b")
        self.refused(a, timed_out=True)
        for gw in (b, a, b, a):
            self.refused(gw)
        self.assertEqual(
            self.svc.timeout_ban_remaining(self.guild, self.USER), 2,
            "only b was a new giveaway to sit out",
        )

    def test_the_giveaway_that_spent_the_last_unit_still_refuses(self) -> None:
        self.refused(self.make(prize="trigger"), timed_out=True)
        blocked = [self.make(prize=f"blocked {i}") for i in range(3)]
        for gw in blocked:
            self.refused(gw)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 0)
        with self.assertRaises(ServiceError) as ctx:
            self.join(blocked[-1], user_id=self.USER)
        self.assertEqual(ctx.exception.kind, TIMEOUT_BAN_KIND)
        self.assertEqual(self.svc.entry_count(blocked[-1].id), 0)
        self.assertEqual(self.join(self.make(prize="next"), user_id=self.USER), 1)

    def test_an_entry_made_before_the_timeout_is_dropped(self) -> None:
        gw = self.make()
        self.assertEqual(self.join(gw, user_id=self.USER), 1)
        self.refused(gw, timed_out=True)
        self.assertEqual(self.svc.entry_count(gw.id), 0, "a timed-out member cannot stay in")

    def test_a_penalty_clears_entries_in_every_running_giveaway(self) -> None:
        yesterday = self.make(prize="joined yesterday")
        today = self.make(prize="joined today")
        self.assertEqual(self.join(yesterday, user_id=self.USER), 1)
        self.assertEqual(self.join(today, user_id=self.USER), 1)
        self.refused(self.make(prize="trigger"), timed_out=True)
        # Sitting out has to mean not entered anywhere: keeping the entry in the
        # giveaway they joined before the mute would let them win it.
        self.assertEqual(self.svc.entry_count(yesterday.id), 0)
        self.assertEqual(self.svc.entry_count(today.id), 0)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 3)

    def test_a_blacklisted_member_is_refused_without_a_penalty(self) -> None:
        self.svc.blacklist_add(self.guild, self.USER)
        message = self.refused(self.make(), timed_out=True)
        self.assertIn("blocked", message)
        self.assertEqual(
            self.svc.timeout_ban_remaining(self.guild, self.USER), 0,
            "a permanent block is not a timeout penalty",
        )

    def test_an_ended_giveaway_never_hands_out_a_penalty(self) -> None:
        gw = self.make()
        self.svc.end(gw.id)
        self.assertIn("ended", self.refused(self.svc.get(gw.id), timed_out=True))
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 0)

    def test_an_expired_deadline_never_hands_out_a_penalty(self) -> None:
        gw = self.make()
        self.db.execute("UPDATE simple_giveaways SET ends_at = ? WHERE id = ?", (now_ms() - 1, gw.id))
        self.assertIn("ended", self.refused(self.svc.get(gw.id), timed_out=True))
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 0)

    def test_the_penalty_is_per_guild_and_per_user(self) -> None:
        self.refused(self.make(prize="trigger"), timed_out=True)
        self.assertEqual(self.svc.timeout_ban_remaining("555555555555555555", self.USER), 0)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, "8" * 18), 0)
        self.assertEqual(self.svc.timeout_ban_remaining("", self.USER), 0)

    def test_a_second_penalty_never_shrinks_the_first(self) -> None:
        self.svc.apply_timeout_penalty(self.guild, self.USER, giveaway_id="gw_a")
        self.assertEqual(
            self.svc.apply_timeout_penalty(self.guild, self.USER, giveaway_id="gw_b"), 3
        )
        self.svc.apply_timeout_penalty(self.guild, self.USER, giveaways=1, giveaway_id="gw_c")
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 3)

    def test_one_penalty_does_not_block_anybody_else(self) -> None:
        self.refused(self.make(prize="trigger"), timed_out=True)
        self.assertEqual(self.join(self.make(prize="someone else"), user_id="9" * 18), 1)

    def test_the_entry_sweep_never_touches_a_penalty(self) -> None:
        gw = self.make()
        self.refused(gw, timed_out=True)
        self.svc.end(gw.id)
        # The 5h entry wipe runs long after a giveaway ends; a penalty must
        # outlive it, which is exactly why it lives in its own table.
        self.assertEqual(self.svc.wipe_stale_entries(now=now_ms() + 10 * DAY_MS), 0)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 3)

    def test_two_simultaneous_clicks_cannot_stack_the_penalty(self) -> None:
        gw = self.make(prize="race")
        messages: list[str] = []
        barrier = threading.Barrier(2, timeout=10)

        def click() -> None:
            try:
                barrier.wait()
                self.svc.join(
                    gw,
                    user_id=self.USER,
                    username="racer",
                    member_roles=[],
                    account_created_ts=time.time() - 400 * 86400,
                    timed_out=True,
                )
            except ServiceError as exc:
                messages.append(str(exc.message))
            except Exception as exc:  # a lost race must not surface as a crash
                messages.append(f"unexpected: {exc!r}")

        threads = [threading.Thread(target=click) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(messages), 2, messages)
        self.assertTrue(
            all("timed out" in m or "penalty" in m for m in messages), messages
        )
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, self.USER), 3)
        self.assertEqual(self.svc.entry_count(gw.id), 0)

    def test_the_penalty_survives_a_restart(self) -> None:
        self.refused(self.make(prize="trigger"), timed_out=True)
        # A new Database on the same store is what a restart looks like: fresh
        # connections, fresh schema pass, same rows.
        restarted = Database(Settings(turso_url=""), connect=self._connect)
        try:
            restarted.init_schema()
            service = GiveawayService(restarted)
            self.assertEqual(service.timeout_ban_remaining(self.guild, self.USER), 3)
            self.assertEqual(len(service.list_timeout_bans(self.guild)), 1)
            gw = self.svc.get(self.make(prize="after the restart").id)
            with self.assertRaises(ServiceError) as ctx:
                service.join(
                    gw,
                    user_id=self.USER,
                    username="restarted",
                    member_roles=[],
                    account_created_ts=time.time() - 400 * 86400,
                )
            self.assertIn("penalty", str(ctx.exception.message))
        finally:
            restarted.close_all()

    def test_counting_matches_the_listing(self) -> None:
        self.assertEqual(self.svc.count_timeout_bans(self.guild), 0)
        for user_id in ("1" * 18, "2" * 18):
            self.svc.apply_timeout_penalty(self.guild, user_id, giveaway_id="gw_x")
        self.db.execute(
            "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining)"
            " VALUES (?, ?, 0)",
            (self.guild, "5" * 18),
        )
        self.assertEqual(self.svc.count_timeout_bans(self.guild), 2, "zero rows are not bans")
        self.assertEqual(self.svc.count_timeout_bans("555555555555555555"), 0)

    def test_list_is_guild_scoped_ordered_and_skips_zero_rows(self) -> None:
        for user_id in ("1" * 18, "2" * 18):
            self.svc.apply_timeout_penalty(self.guild, user_id, giveaway_id="gw_x")
        self.svc.apply_timeout_penalty(self.guild, "3" * 18, giveaways=1, giveaway_id="gw_x")
        # A hand-written zero row must never be reported as a ban.
        self.db.execute(
            "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining)"
            " VALUES (?, ?, 0)",
            (self.guild, "4" * 18),
        )
        rows = self.svc.list_timeout_bans(self.guild)
        self.assertEqual([r["user_id"] for r in rows], ["1" * 18, "2" * 18, "3" * 18])
        self.assertEqual([r["giveaways_remaining"] for r in rows], [3, 3, 1])
        self.assertEqual(self.svc.list_timeout_bans("555555555555555555"), [])
        self.assertEqual(len(self.svc.list_timeout_bans(self.guild, limit=1)), 1)


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
