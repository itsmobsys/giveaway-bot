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
        self.assertIn("idx_simple_gw_status_ends", names, "due() scans on this every tick")
        self.assertIn("idx_simple_entries_gw_entered", names, "entries reads sort by entered_at")
        self.assertIn("idx_simple_entries_user", names, "blacklist purge needs this")
        self.assertIn("idx_simple_claims_pending_due", names, "due_claims scans every 30s")
        # Superseded by idx_simple_entries_gw_entered and dropped on migration;
        # it only repeated the simple_entries primary-key prefix.
        self.assertNotIn("idx_simple_entries_giveaway", names)

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

    def test_a_pragma_that_assigns_is_a_write(self) -> None:
        # "PRAGMA user_version = 2" changes the database, so it needs the same
        # commit as any other write; ignoring it would leave the schema stamp
        # unapplied and every boot would re-run the whole DDL block.
        self.assertTrue(_is_write("PRAGMA user_version = 2"))
        self.assertTrue(_is_write("  pragma journal_mode=wal"))
        self.assertFalse(_is_write("PRAGMA user_version"), "a bare pragma only reads")


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
        # The status flip is the claim, so a second ender cannot draw again â€”
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


class ClaimTests(ServiceTestCase):
    def _three_entrant_ended(self, **kw):
        gw = self.make(winner_count=1, claim_timeout_seconds=3600, **kw)
        users = [str(10 ** 17 + i) for i in range(3)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0], users)
        return ended, winners[0], users

    def test_create_validates_claim_window(self) -> None:
        for bad in (-1, 60 * 86400 + 1):
            with self.subTest(bad=bad):
                with self.assertRaises(ServiceError) as ctx:
                    self.make(claim_timeout_seconds=bad)
                self.assertIn("claim window", str(ctx.exception.message).lower())
        gw = self.make(claim_timeout_seconds=60)
        self.assertTrue(gw.claim_enabled)
        self.assertEqual(gw.claim_timeout_seconds, 60)
        legacy = self.make()
        self.assertFalse(legacy.claim_enabled)

    def test_end_opens_claims_when_enabled(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        pending = self.svc.pending_claims(ended.id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(str(pending[0]["user_id"]), winner)
        self.assertEqual(str(pending[0]["status"]), "pending")
        self.assertGreater(int(pending[0]["deadline_ms"]), 0)
        # Restart recovery: a fresh service on the same DB sees the same rows.
        svc2 = GiveawayService(self.db)
        again = svc2.pending_claims(ended.id)
        self.assertEqual(len(again), 1)
        self.assertEqual(str(again[0]["user_id"]), winner)

    def test_end_creates_no_claims_when_disabled(self) -> None:
        gw = self.make(winner_count=1)
        self.join(gw)
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.svc.pending_claims(ended.id), [])
        self.assertEqual(self.svc.due_claims(now=now_ms() + 10 * DAY_MS), [])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.claim(ended.id, winners[0])
        self.assertIn("drawn winner", str(ctx.exception.message))

    def test_claim_happy_path_and_double_claim_refused(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        row = self.svc.claim(ended.id, winner)
        self.assertEqual(str(row["user_id"]), winner)
        self.assertEqual(self.svc.pending_claims(ended.id), [])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.claim(ended.id, winner)
        self.assertIn("already claimed", str(ctx.exception.message))

    def test_claim_by_non_winner_refused(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        loser = next(u for u in users if u != winner)
        # Loser never drew, so there is no row at all for them.
        with self.assertRaises(ServiceError) as ctx:
            self.svc.claim(ended.id, loser)
        self.assertIn("drawn winner", str(ctx.exception.message))
        with self.assertRaises(ServiceError):
            self.svc.claim("gw_nope", winner)

    def test_skip_claim_needs_staff_and_no_replacement(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.skip_claim(ended.id, winner, "")
        self.assertIn("staff", str(ctx.exception.message).lower())
        row = self.svc.skip_claim(ended.id, winner, "999999999999999999")
        self.assertEqual(str(row["status"]), "skipped")
        self.assertEqual(self.svc.pending_claims(ended.id), [])
        # No replacement is drawn: the winners list is untouched.
        self.assertEqual(self.svc.get(ended.id).winners, [winner])
        with self.assertRaises(ServiceError) as ctx2:
            self.svc.skip_claim(ended.id, winner, "999999999999999999")
        self.assertIn("already verified", str(ctx2.exception.message))
        with self.assertRaises(ServiceError) as ctx3:
            self.svc.claim(ended.id, winner)
        self.assertIn("verified", str(ctx3.exception.message))

    def test_expire_draws_replacement_never_repeats(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        # Too early: the guarded UPDATE matches nothing.
        with self.assertRaises(ServiceError):
            self.svc.expire_claim(ended.id, winner, now=deadline - 1)
        redrawn, fresh = self.svc.expire_claim(ended.id, winner, now=deadline + 1)
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(fresh[0], winner)
        self.assertIn(fresh[0], users)
        self.assertIn(fresh[0], redrawn.winners)
        self.assertIn(winner, redrawn.winners, "expired winner stays named")
        # The replacement gets its own pending window.
        pending = self.svc.pending_claims(ended.id)
        self.assertEqual([str(r["user_id"]) for r in pending], [fresh[0]])
        # The expired slot can no longer be claimed.
        with self.assertRaises(ServiceError) as ctx:
            self.svc.claim(ended.id, winner)
        self.assertIn("closed", str(ctx.exception.message))
        # due_claims only fires past the deadline.
        self.assertEqual(self.svc.due_claims(now=deadline - 1), [])
        # Claiming the replacement works.
        self.svc.claim(ended.id, fresh[0])
        self.assertEqual(self.svc.pending_claims(ended.id), [])

    def test_expire_with_empty_pool_returns_no_replacement(self) -> None:
        gw = self.make(winner_count=1, claim_timeout_seconds=3600)
        self.join(gw)
        ended, winners = self.svc.end(gw.id)
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        redrawn, fresh = self.svc.expire_claim(ended.id, winners[0], now=deadline + 1)
        self.assertEqual(fresh, [])
        self.assertEqual(redrawn.winners, winners)
        self.assertEqual(self.svc.pending_claims(ended.id), [])

    def test_multi_winner_claims_are_independent(self) -> None:
        gw = self.make(winner_count=2, claim_timeout_seconds=3600)
        users = [str(10 ** 17 + i) for i in range(4)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 2)
        self.svc.claim(ended.id, winners[0])
        still = [str(r["user_id"]) for r in self.svc.pending_claims(ended.id)]
        self.assertEqual(still, [winners[1]])
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        redrawn, fresh = self.svc.expire_claim(ended.id, winners[1], now=deadline + 1)
        self.assertEqual(len(fresh), 1)
        self.assertNotIn(fresh[0], winners)
        self.assertIn(fresh[0], users)
        self.assertEqual(len(redrawn.winners), 3)

    def test_start_claims_is_idempotent(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        before = self.svc.pending_claims(ended.id)
        self.svc.start_claims(self.svc.get(ended.id), [winner])
        after = self.svc.pending_claims(ended.id)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)

    def test_manual_reroll_opens_claims_and_keeps_old_pending(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        redrawn, fresh = self.svc.reroll(ended.id, 1)
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(fresh[0], winner)
        self.assertIn(fresh[0], users)
        # Additive: both the old winner and the reroll winner stay named.
        self.assertIn(winner, redrawn.winners)
        self.assertIn(fresh[0], redrawn.winners)
        # Both hold a pending claim window; the old deadline still stands.
        pending = {str(r["user_id"]) for r in self.svc.pending_claims(ended.id)}
        self.assertEqual(pending, {winner, fresh[0]})
        # The fresh winner can actually press Claim Prize.
        self.svc.claim(ended.id, fresh[0])
        still = [str(r["user_id"]) for r in self.svc.pending_claims(ended.id)]
        self.assertEqual(still, [winner])

    def test_manual_reroll_never_repeats_claim_draws(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        _redrawn, auto = self.svc.expire_claim(ended.id, winner, now=deadline + 1)
        self.assertEqual(len(auto), 1)
        _rerolled, fresh = self.svc.reroll(ended.id, 1)
        self.assertEqual(len(fresh), 1)
        # Neither the expired winner nor the auto replacement may reappear.
        self.assertNotIn(fresh[0], [winner, auto[0]])
        self.assertIn(fresh[0], users)
        third = next(u for u in users if u not in (winner, auto[0]))
        self.assertEqual(fresh[0], third)
        # The pool is now exhausted: one more reroll must refuse.
        with self.assertRaises(ServiceError):
            self.svc.reroll(ended.id, 1)

    def _four_entrant_three_winner_ended(self, **kw):
        gw = self.make(winner_count=3, claim_timeout_seconds=3600, **kw)
        users = [str(10 ** 17 + i) for i in range(4)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 3)
        return ended, winners, users

    def test_targeted_reroll_replaces_one_slot_in_place(self) -> None:
        ended, winners, users = self._four_entrant_three_winner_ended()
        old = winners[0]
        expected = next(u for u in users if u not in winners)
        redrawn, fresh = self.svc.reroll(ended.id, 1, old)
        self.assertEqual(fresh, [expected])
        # Same length, order preserved: only the picked slot swaps.
        self.assertEqual(len(redrawn.winners), 3)
        self.assertEqual(redrawn.winners[0], fresh[0])
        self.assertEqual(redrawn.winners[1:], winners[1:])
        self.assertNotIn(old, redrawn.winners)
        # The old pending is closed as replaced; the fresh winner gets one.
        statuses = {str(r["user_id"]): str(r["status"]) for r in self.svc.claim_status(ended.id)}
        self.assertEqual(statuses[old], "replaced")
        self.assertEqual(statuses[fresh[0]], "pending")
        pending = {str(r["user_id"]) for r in self.svc.pending_claims(ended.id)}
        self.assertEqual(pending, set(redrawn.winners))
        # The fresh winner can press Claim Prize; the old one gets a clear no.
        self.svc.claim(ended.id, fresh[0])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.claim(ended.id, old)
        self.assertIn("replaced", str(ctx.exception.message).lower())

    def test_targeted_reroll_rejects_non_winner_and_exhausted_pool(self) -> None:
        ended, winners, _users = self._four_entrant_three_winner_ended()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(ended.id, 1, "999999999999999999")
        self.assertIn("not a winner", str(ctx.exception.message).lower())
        # One swap exhausts the 4-entrant pool: every user is now ever-drawn.
        _redrawn, _fresh = self.svc.reroll(ended.id, 1, winners[0])
        with self.assertRaises(ServiceError) as ctx2:
            self.svc.reroll(ended.id, 1, winners[1])
        self.assertIn("no other entrants", str(ctx2.exception.message).lower())
        with self.assertRaises(ServiceError):
            self.svc.reroll(ended.id, 1)

    def test_targeted_reroll_without_claims_still_excludes(self) -> None:
        gw = self.make(winner_count=1)
        users = [str(10 ** 17 + i) for i in range(2)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        old = winners[0]
        redrawn, fresh = self.svc.reroll(ended.id, 1, old)
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(fresh[0], old)
        self.assertEqual(redrawn.winners, fresh)
        # A replaced marker keeps the old winner out of every future draw.
        rows = self.svc.claim_status(ended.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["user_id"]), old)
        self.assertEqual(str(rows[0]["status"]), "replaced")
        with self.assertRaises(ServiceError):
            self.svc.reroll(ended.id, 1)


class ResolveEndedTests(ServiceTestCase):
    def _ended(self, **kw):
        gw = self.make(**kw)
        self.join(gw)
        ended, _winners = self.svc.end(gw.id)
        return ended

    def test_resolve_ended_returns_ended_by_id(self) -> None:
        ended = self._ended()
        self.assertEqual(self.svc.resolve_ended(self.guild, ended.id).id, ended.id)

    def test_resolve_ended_refuses_live(self) -> None:
        live = self.make()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve_ended(self.guild, live.id)
        self.assertIn("not ended", str(ctx.exception.message).lower())

    def test_resolve_ended_never_falls_back_to_live(self) -> None:
        live = self.make(prize="live")
        ended = self._ended(prize="ended")
        # Typo id with a single ended row falls back to that ended row --
        # same shape as resolve() -- and never to the running giveaway.
        self.assertEqual(self.svc.resolve_ended(self.guild, "gw_typo").id, ended.id)
        self.assertEqual(self.svc.resolve_ended(self.guild, "").id, ended.id)
        self.assertNotEqual(self.svc.resolve_ended(self.guild, "gw_typo").id, live.id)
        # Two ended rows -> needs an id, even though only one live exists.
        other = self.make(prize="ended2")
        self.join(other)
        ended2, _w = self.svc.end(other.id)
        with self.assertRaises(ServiceError) as ctx2:
            self.svc.resolve_ended(self.guild, "")
        self.assertIn("did not match", str(ctx2.exception.message))
        self.assertEqual(self.svc.resolve_ended(self.guild, ended.id).id, ended.id)
        self.assertEqual(self.svc.resolve_ended(self.guild, ended2.id).id, ended2.id)
        self.assertEqual(live.id is not None, True)

    def test_resolve_ended_single_fallback(self) -> None:
        ended = self._ended()
        self.assertEqual(self.svc.resolve_ended(self.guild, "").id, ended.id)
        self.assertEqual(self.svc.resolve_ended(self.guild, "  ").id, ended.id)

    def test_resolve_ended_refuses_another_guild(self) -> None:
        other = self.make(guild_id="555")
        self.join(other)
        ended, _w = self.svc.end(other.id)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve_ended(self.guild, ended.id)
        self.assertIn("another server", str(ctx.exception.message))

    def test_resolve_ended_with_nothing_ended(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.svc.resolve_ended(self.guild, "gw_x")
        self.assertIn("no ended", str(ctx.exception.message).lower())

    def test_list_ended_is_guild_scoped(self) -> None:
        mine = self._ended(prize="mine")
        other = self.make(guild_id="555", prize="theirs")
        self.join(other)
        self.svc.end(other.id)
        ids = [g.id for g in self.svc.list_ended(self.guild)]
        self.assertEqual(ids, [mine.id])


class RerollCasTests(ServiceTestCase):
    def _three_entrant_ended(self, **kw):
        gw = self.make(winner_count=1, **kw)
        users = [str(10 ** 17 + i) for i in range(3)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        return ended, winners[0], users

    def test_reroll_preserves_racing_append(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        external = "999999999999999999"  # not an entrant: _pick can never draw it
        cur = self.svc.get(ended.id)
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?",
            (json.dumps(cur.winners + [external]), ended.id),
        )
        _redrawn, fresh = self.svc.reroll(ended.id, 1)
        final = self.svc.get(ended.id).winners
        self.assertIn(external, final, "racing append must survive the CAS rebase")
        self.assertIn(fresh[0], final)
        self.assertEqual(len(final), 3)

    def test_expire_preserves_racing_append(self) -> None:
        ended, winner, users = self._three_entrant_ended(claim_timeout_seconds=3600)
        external = "999999999999999999"
        cur = self.svc.get(ended.id)
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?",
            (json.dumps(cur.winners + [external]), ended.id),
        )
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        redrawn, fresh = self.svc.expire_claim(ended.id, winner, now=deadline + 1)
        self.assertIn(external, redrawn.winners)
        self.assertIn(winner, redrawn.winners, "expired winner stays named")
        self.assertEqual(len(fresh), 1)

    def test_read_winners_raw_round_trip(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        winners, raw = self.svc._read_winners_raw(ended.id)
        self.assertEqual(winners, [winner])
        self.assertEqual(json.loads(raw), [winner])


class ClaimAtomicityTests(ServiceTestCase):
    """B01: a crash between a stored draw and its claim inserts heals."""

    def _three_entrant_ended(self, **kw):
        kw.setdefault("claim_timeout_seconds", 3600)
        kw.setdefault("winner_count", 1)
        gw = self.make(**kw)
        users = [str(10 ** 17 + i) for i in range(3)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        return ended, winners[0], users

    def _strand(self, giveaway_id: str) -> None:
        self.db.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", (giveaway_id,))
        self.assertEqual(
            self.db.query(
                "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (giveaway_id,)
            ),
            [],
        )

    def test_end_insert_crash_still_announces_and_heals(self) -> None:
        gw = self.make(winner_count=1, claim_timeout_seconds=3600)
        users = [str(10 ** 17 + i) for i in range(3)]
        for u in users:
            self.join(gw, user_id=u)
        with unittest.mock.patch.object(
            self.svc, "start_claims", side_effect=RuntimeError("boom")
        ):
            ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0], users)
        # Post-flip failure returns the stored draw; the repair healed the window.
        pending = self.svc.pending_claims(ended.id)
        self.assertEqual([str(r["user_id"]) for r in pending], winners)

    def test_stranded_draw_healed_by_pending_and_claim_retry(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        self._strand(ended.id)
        pending = self.svc.pending_claims(ended.id)
        self.assertEqual([str(r["user_id"]) for r in pending], [winner])
        self._strand(ended.id)
        row = self.svc.claim(ended.id, winner)
        self.assertEqual(str(row["user_id"]), winner)
        self.assertEqual(self.svc.pending_claims(ended.id), [])

    def test_stranded_draw_healed_by_tick_sweep(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        self._strand(ended.id)
        due = self.svc.due_claims(now=deadline + 1)
        self.assertEqual([str(r["user_id"]) for r in due], [winner])

    def test_repair_is_idempotent_and_settles_stale_pending(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        # A stale pending row for a non-winner (targeted reroll that died
        # before settling) flips to replaced; existing winners keep windows.
        loser = next(u for u in users if u != winner)
        self.db.execute(
            "INSERT OR IGNORE INTO simple_claims"
            " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
            " VALUES (?, ?, 0, 'pending', ?, ?)",
            (ended.id, loser, now_ms() + 3600 * 1000, now_ms()),
        )
        written = self.svc.repair_missing_claims(ended.id)
        self.assertGreaterEqual(written, 1)
        statuses = {
            str(r["user_id"]): str(r["status"]) for r in self.svc.claim_status(ended.id)
        }
        self.assertEqual(statuses[winner], "pending")
        self.assertEqual(statuses[loser], "replaced")
        again = self.svc.repair_missing_claims(ended.id)
        self.assertEqual(again, 0, "second sweep must be a no-op")

    def test_healthy_claim_sweep_is_three_reads_and_no_writes(self) -> None:
        ended_ids: list[tuple[str, list[str]]] = []
        for i in range(25):
            gw = self.make(winner_count=2, claim_timeout_seconds=3600)
            for j in range(4):
                self.join(gw, user_id=str(10 ** 17 + i * 10 + j))
            ended, winners = self.svc.end(gw.id)
            self.assertEqual(len(winners), 2)
            ended_ids.append((ended.id, winners))
        self.db.reset_stats()
        written = self.svc.repair_all_missing_claims()
        stats = self.db.get_stats()
        self.assertEqual(written, 0)
        self.assertEqual(int(stats["reads"]), 3)
        self.assertEqual(int(stats["writes"]), 0)
        # Positive control: a sweep that bailed after one read would also show
        # zero writes, so strand a winner of the newest giveaway (last id the
        # bounded id query returns) and prove the sweep walked the whole set.
        deepest, deepest_winners = ended_ids[-1]
        self.db.execute(
            "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
            (now_ms() + 60_000, deepest),
        )
        self.db.execute(
            "DELETE FROM simple_claims WHERE giveaway_id = ? AND user_id = ?",
            (deepest, deepest_winners[0]),
        )
        self.db.reset_stats()
        self.assertEqual(self.svc.repair_all_missing_claims(), 1)
        stats = self.db.get_stats()
        self.assertEqual(int(stats["reads"]), 3)
        self.assertEqual(int(stats["writes"]), 1)
        rows = {str(r["user_id"]) for r in self.svc.claim_status(deepest)}
        self.assertEqual(rows, set(deepest_winners))

    def test_claim_sweep_recreates_a_stranded_window_in_one_batch(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        row = self.db.query_one(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?", (ended.id,)
        )
        ended_at = int(row["ended_at"])
        self.db.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", (ended.id,))
        # Second stranded giveaway with no ended_at: its fallback base is now.
        gw2 = self.make(winner_count=1, claim_timeout_seconds=3600)
        self.join(gw2, user_id="555555555555555555")
        ended2, _winners2 = self.svc.end(gw2.id)
        self.db.execute(
            "UPDATE simple_giveaways SET ended_at = NULL WHERE id = ?", (ended2.id,)
        )
        self.db.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", (ended2.id,))
        ts = ended_at + 5000
        self.db.reset_stats()
        written = self.svc.repair_all_missing_claims(now=ts)
        self.assertEqual(written, 2)
        stats = self.db.get_stats()
        self.assertEqual(int(stats["reads"]), 3, "the batch must not loop per giveaway")
        self.assertEqual(int(stats["writes"]), 1, "both rows belong to one INSERT")
        rows = {str(r["user_id"]): r for r in self.svc.claim_status(ended.id)}
        self.assertEqual(list(rows), [winner])
        self.assertEqual(str(rows[winner]["status"]), "pending")
        self.assertEqual(int(rows[winner]["deadline_ms"]), ended_at + 3600 * 1000)
        rows2 = self.svc.claim_status(ended2.id)
        self.assertEqual(len(rows2), 1)
        self.assertEqual(str(rows2[0]["status"]), "pending")
        self.assertEqual(int(rows2[0]["deadline_ms"]), ts + 3600 * 1000)
        self.assertEqual(self.svc.repair_all_missing_claims(now=ts), 0)

    def test_claim_sweep_settles_a_stale_pending_row(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        loser = next(u for u in users if u != winner)
        self.db.execute(
            "INSERT OR IGNORE INTO simple_claims"
            " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
            " VALUES (?, ?, 0, 'pending', ?, ?)",
            (ended.id, loser, now_ms() + 3600 * 1000, now_ms()),
        )
        self.assertEqual(self.svc.repair_all_missing_claims(), 1)
        statuses = {
            str(r["user_id"]): str(r["status"]) for r in self.svc.claim_status(ended.id)
        }
        self.assertEqual(statuses[winner], "pending")
        self.assertEqual(statuses[loser], "replaced")
        self.assertEqual(self.svc.repair_all_missing_claims(), 0)

    def test_claim_sweep_survives_one_bad_row(self) -> None:
        bad, _bad_winner, _users = self._three_entrant_ended()
        good = self.make(winner_count=1, claim_timeout_seconds=3600)
        self.join(good, user_id="444444444444444444")
        good_ended, good_winners = self.svc.end(good.id)
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = 'not json' WHERE id = ?",
            (bad.id,),
        )
        before = sorted(
            (str(r["user_id"]), str(r["status"]), int(r["deadline_ms"]))
            for r in self.svc.claim_status(bad.id)
        )
        self.db.execute(
            "DELETE FROM simple_claims WHERE giveaway_id = ?", (good_ended.id,)
        )
        self.assertEqual(self.svc.repair_all_missing_claims(), 1)
        after = sorted(
            (str(r["user_id"]), str(r["status"]), int(r["deadline_ms"]))
            for r in self.svc.claim_status(bad.id)
        )
        self.assertEqual(
            after, before, "the malformed giveaway's own rows must be left intact"
        )
        rows = self.svc.claim_status(good_ended.id)
        self.assertEqual([str(r["user_id"]) for r in rows], good_winners)
        self.assertEqual({str(r["status"]) for r in rows}, {"pending"})

    def test_claim_sweep_accounting_error_still_settles(self) -> None:
        """A cursor without rowcount must not skip the settle phase."""
        ended, winner, users = self._three_entrant_ended()
        loser = next(u for u in users if u != winner)
        self.db.execute(
            "INSERT OR IGNORE INTO simple_claims"
            " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
            " VALUES (?, ?, 0, 'pending', ?, ?)",
            (ended.id, loser, now_ms() + 3600 * 1000, now_ms()),
        )
        good = self.make(winner_count=1, claim_timeout_seconds=3600)
        self.join(good, user_id="444444444444444444")
        good_ended, good_winners = self.svc.end(good.id)
        self.db.execute(
            "DELETE FROM simple_claims WHERE giveaway_id = ?", (good_ended.id,)
        )

        real_execute = self.db.execute

        class _NoRowcount:
            def __init__(self, inner):
                self._inner = inner

            @property
            def rowcount(self):
                raise AttributeError("driver cursor has no rowcount")

        def flaky(sql, params=()):
            cur = real_execute(sql, params)
            return _NoRowcount(cur) if "INSERT" in sql else cur

        with unittest.mock.patch.object(self.db, "execute", flaky):
            written = self.svc.repair_all_missing_claims()

        self.assertEqual(written, 2, "one settled row plus the len(chunk) fallback")

        statuses = {
            str(r["user_id"]): str(r["status"]) for r in self.svc.claim_status(ended.id)
        }
        self.assertEqual(
            statuses[loser],
            "replaced",
            "the stale row must settle even when insert accounting fails",
        )
        self.assertEqual(
            [str(r["user_id"]) for r in self.svc.claim_status(good_ended.id)],
            good_winners,
        )

    def test_reroll_insert_crash_heals_fresh_window(self) -> None:
        ended, winner, users = self._three_entrant_ended()
        with unittest.mock.patch.object(
            self.svc, "start_claims", side_effect=RuntimeError("boom")
        ):
            redrawn, fresh = self.svc.reroll(ended.id, 1)
        self.assertEqual(len(fresh), 1)
        pending = {str(r["user_id"]) for r in self.svc.pending_claims(ended.id)}
        self.assertIn(fresh[0], pending)
        self.assertIn(winner, pending)
        self.assertIn(fresh[0], redrawn.winners)

    def test_expire_insert_crash_heals_replacement(self) -> None:
        ended, winner, _users = self._three_entrant_ended()
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        with unittest.mock.patch.object(
            self.svc, "start_claims", side_effect=RuntimeError("boom")
        ):
            redrawn, fresh = self.svc.expire_claim(ended.id, winner, now=deadline + 1)
        self.assertEqual(len(fresh), 1)
        pending = {str(r["user_id"]) for r in self.svc.pending_claims(ended.id)}
        self.assertIn(fresh[0], pending)


class TargetedSettledGuardTests(ServiceTestCase):
    def _four_entrant_three_winner_ended(self, **kw):
        gw = self.make(winner_count=3, claim_timeout_seconds=3600, **kw)
        users = [str(10 ** 17 + i) for i in range(4)]
        for u in users:
            self.join(gw, user_id=u)
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 3)
        return ended, winners, users

    def test_targeted_replace_refuses_claimed(self) -> None:
        ended, winners, _users = self._four_entrant_three_winner_ended()
        old = winners[0]
        self.svc.claim(ended.id, old)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(ended.id, 1, old)
        self.assertIn("already claimed", str(ctx.exception.message).lower())

    def test_targeted_replace_refuses_skipped(self) -> None:
        ended, winners, _users = self._four_entrant_three_winner_ended()
        old = winners[0]
        self.svc.skip_claim(ended.id, old, "999999999999999999")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(ended.id, 1, old)
        self.assertIn("already verified", str(ctx.exception.message).lower())

    def test_targeted_replace_refuses_expired(self) -> None:
        ended, winners, _users = self._four_entrant_three_winner_ended()
        old = winners[0]
        deadline = int(self.svc.pending_claims(ended.id)[0]["deadline_ms"])
        self.svc.expire_claim(ended.id, old, now=deadline + 1)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reroll(ended.id, 1, old)
        self.assertIn("already replaced", str(ctx.exception.message).lower())

    def test_second_replace_of_same_slot_refused(self) -> None:
        ended, winners, _users = self._four_entrant_three_winner_ended()
        old = winners[0]
        _redrawn, fresh = self.svc.reroll(ended.id, 1, old)
        self.assertNotIn(old, self.svc.get(ended.id).winners)
        with self.assertRaises(ServiceError):
            self.svc.reroll(ended.id, 1, old)


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


class EligibilitySnapshotTests(ServiceTestCase):
    """One SELECT for blacklist + ban + messages + hit (latency fast-path)."""

    def test_snapshot_returns_all_four_fields(self) -> None:
        gw = self.make()
        self.svc.record_message(self.guild, "5" * 18)
        self.svc.record_message(self.guild, "5" * 18)
        snap = self.svc.eligibility_snapshot(self.guild, "5" * 18, gw.id)
        self.assertIsNotNone(snap)
        assert snap is not None
        bl, ban, msgs, hit = snap
        self.assertFalse(bl)
        self.assertEqual(ban, 0)
        self.assertEqual(msgs, 2)
        self.assertFalse(hit)

    def test_snapshot_sees_blacklist_ban_and_hit(self) -> None:
        gw = self.make()
        uid = "6" * 18
        self.svc.blacklist_add(self.guild, uid)
        self.svc.apply_timeout_penalty(self.guild, uid, giveaway_id=gw.id)
        snap = self.svc.eligibility_snapshot(self.guild, uid, gw.id)
        assert snap is not None
        bl, ban, msgs, hit = snap
        self.assertTrue(bl)
        self.assertEqual(ban, 3)
        self.assertTrue(hit)

    def test_snapshot_empty_ids_fail_closed(self) -> None:
        self.assertEqual(self.svc.eligibility_snapshot("", ""), (False, 0, 0, False))

    def test_snapshot_none_on_db_failure_forces_fallback(self) -> None:
        gw = self.make()
        with unittest.mock.patch.object(self.db, "query_one", side_effect=OSError("turso down")):
            self.assertIsNone(self.svc.eligibility_snapshot(self.guild, "5" * 18, gw.id))
        # None forces the individual checks: the join still works.
        self.assertEqual(self.join(gw, user_id="5" * 18), 1)

    def test_join_with_snapshot_honors_live_block(self) -> None:
        gw = self.make()
        uid = "7" * 18
        self.svc.blacklist_add(self.guild, uid)
        with self.assertRaises(ServiceError):
            self.svc.join(
                gw, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                snapshot=(True, 0, 0, False),
            )
        self.assertEqual(self.svc.entry_count(gw.id), 0)

    def test_stale_snapshot_block_does_not_falsely_refuse(self) -> None:
        # Snapshot says blocked but the row is gone (blacklist_remove raced
        # the snapshot): live-confirm lets the join through. The INSERT
        # guard still blocks a racing blacklist_add, so safety is kept.
        gw = self.make()
        count = self.svc.join(
            gw, user_id="7" * 18, username="u", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
            snapshot=(True, 0, 0, False),
        )
        self.assertEqual(count, 1)

    def test_join_with_snapshot_counts_buffered_messages(self) -> None:
        gw = self.make(min_messages=3)
        self.svc.record_message(self.guild, "8" * 18)
        count = self.svc.join(
            gw, user_id="8" * 18, username="u", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
            snapshot=(False, 0, 1, False), pending_messages=2,
        )
        self.assertEqual(count, 1)

    def test_get_status_is_status_only(self) -> None:
        gw = self.make()
        self.assertEqual(self.svc.get_status(gw.id), "active")
        self.svc.end(gw.id)
        self.assertEqual(self.svc.get_status(gw.id), "ended")
        self.assertIsNone(self.svc.get_status("gw_missing"))


class DbStatsTests(ServiceTestCase):
    def test_stats_count_read_trips(self) -> None:
        gw = self.make()
        self.db.reset_stats()
        self.svc.eligibility_snapshot(self.guild, "5" * 18, gw.id)
        self.svc.get_status(gw.id)
        stats = self.db.get_stats()
        self.assertGreaterEqual(stats["reads"], 2)
        self.assertEqual(stats["writes"], 0)

    def test_stats_counts_failed_attempts(self) -> None:
        gw = self.make()
        self.db.reset_stats()
        orig_conn = self.db._conn
        calls = {"n": 0}
        def flaky_conn():
            if calls["n"] == 0:
                calls["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            return orig_conn()
        self.db._conn = flaky_conn  # type: ignore[method-assign]
        try:
            with unittest.mock.patch("time.sleep", return_value=None):
                self.svc.get_status(gw.id)
        finally:
            self.db._conn = orig_conn  # type: ignore[method-assign]
        stats = self.db.get_stats()
        self.assertGreaterEqual(stats["reads"], 2, "failed retry must count, not hide")

    def test_stats_separates_reads_and_writes(self) -> None:
        self.db.reset_stats()
        self.svc.record_message(self.guild, "9" * 18)
        stats = self.db.get_stats()
        self.assertGreaterEqual(stats["writes"], 1)
        self.assertEqual(stats["reads"], 0)


class RoundTripBudgetTests(ServiceTestCase):
    """Statement budgets for the hot DB paths, measured against real SQLite."""

    def test_message_flush_reads_once_per_chunk(self) -> None:
        rows = [(self.guild, f"u{i:05d}", 1) for i in range(300)]
        self.db.reset_stats()
        self.assertEqual(self.svc.add_message_counts(rows), 300)
        stats = self.db.get_stats()
        self.assertEqual(stats["reads"], 1, "one batched snapshot read per chunk")
        self.assertEqual(stats["writes"], 1, "one multi-row insert per chunk")

    def test_ambiguous_flush_still_requeues_only_missing_keys(self) -> None:
        # The write landed for one key but its response was lost: the retry
        # must add only the other key, preserving the arithmetic the old
        # per-key snapshot/reconcile pair used.
        original = self.db.execute
        rows = [(self.guild, "a" * 18, 2), (self.guild, "b" * 18, 3)]

        def partial(sql, params=()):
            if sql.startswith("INSERT INTO simple_message_counts"):
                original(
                    "INSERT INTO simple_message_counts (guild_id, user_id, count)"
                    " VALUES (?, ?, ?)",
                    (self.guild, "a" * 18, 2),
                )
                raise OSError("connection reset by peer")
            return original(sql, params)

        with self.assertRaises(PartialFlush), unittest.mock.patch.object(
            self.db, "execute", partial
        ):
            self.svc.add_message_counts(rows)
        # The landed key is not requeued at all; the missing key is requeued
        # whole and the list keeps its length (the padding adds delta 0).
        self.assertEqual([uid for _g, uid, _d in rows if uid == "a" * 18], [])
        self.assertEqual(
            sum(delta for _g, uid, delta in rows if uid == "b" * 18), 3
        )
        self.assertEqual(self.svc.add_message_counts(rows), len(rows))
        self.assertEqual(self.svc.message_count(self.guild, "a" * 18), 2)
        self.assertEqual(self.svc.message_count(self.guild, "b" * 18), 3)

    def test_snapshot_read_failure_requeues_whole_chunk(self) -> None:
        # When the batched snapshot read itself fails, no key has a baseline:
        # an ambiguous write must requeue every delta whole rather than risk
        # dropping a message, and the failure logs once per chunk (not per key).
        rows = [(self.guild, "c" * 18, 4), (self.guild, "d" * 18, 5)]
        original = self.db.execute

        def partial(sql, params=()):
            if sql.startswith("INSERT INTO simple_message_counts"):
                raise OSError("connection reset by peer")
            return original(sql, params)

        with self.assertRaises(PartialFlush), unittest.mock.patch.object(
            self.svc, "_count_rows", return_value=None
        ), unittest.mock.patch.object(
            self.db, "execute", partial
        ), unittest.mock.patch("giveaway_bot.service.log.warning") as warn:
            self.svc.add_message_counts(rows)
        warn.assert_called_once_with("flush snapshot batch read failed for %d keys", 2)
        self.assertEqual(
            rows, [(self.guild, "c" * 18, 4), (self.guild, "d" * 18, 5)]
        )
        # The unmodified deltas retry cleanly, so each message lands once.
        self.assertEqual(self.svc.add_message_counts(rows), len(rows))
        self.assertEqual(self.svc.message_count(self.guild, "c" * 18), 4)
        self.assertEqual(self.svc.message_count(self.guild, "d" * 18), 5)

    def test_start_claims_is_one_write(self) -> None:
        gw = self.make(winner_count=10, claim_timeout_seconds=3600)
        winners = [str(500000 + i) for i in range(10)]
        self.db.reset_stats()
        self.svc.start_claims(gw, winners, now=now_ms())
        stats = self.db.get_stats()
        self.assertEqual(stats["writes"], 1, "one multi-row claim insert")
        self.assertEqual(stats["reads"], 0)
        rows = self.db.query(
            "SELECT user_id, status, deadline_ms FROM simple_claims"
            " WHERE giveaway_id = ?",
            (gw.id,),
        )
        self.assertEqual({str(r["user_id"]) for r in rows}, set(winners))
        self.assertTrue(all(str(r["status"]) == "pending" for r in rows))
        first_deadlines = {
            str(r["user_id"]): int(r["deadline_ms"]) for r in rows
        }
        # Retried announce: INSERT OR IGNORE keeps the rows and their original
        # deadlines, so a restarted announce cannot extend a claim window.
        self.svc.start_claims(gw, winners, now=now_ms() + 5000)
        again = self.db.query(
            "SELECT user_id, status, deadline_ms FROM simple_claims"
            " WHERE giveaway_id = ?",
            (gw.id,),
        )
        self.assertEqual(len(again), 10)
        self.assertEqual(
            {str(r["user_id"]): int(r["deadline_ms"]) for r in again},
            first_deadlines,
        )
        self.assertTrue(all(str(r["status"]) == "pending" for r in again))

    def test_empty_giveaway_count_is_known_zero(self) -> None:
        gw = self.make()
        self.db.reset_stats()
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertEqual(self.db.get_stats()["reads"], 1, "0 is an answer, not a miss")
        self.db.reset_stats()
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertEqual(self.db.get_stats()["reads"], 1, "and it stays one read")

    def test_failed_counter_bump_resets_known_zero_to_unknown(self) -> None:
        # A committed entry whose counter bump failed must not leave the new
        # known-zero behind: the next read has to fall back to COUNT(*) and
        # heal the row, exactly as a NULL-on-create row did before this task.
        gw = self.make()
        original = self.db.execute

        def fail_bump(sql, params=()):
            if sql.startswith("UPDATE simple_giveaways SET entrant_count = COALESCE"):
                raise OSError("connection reset by peer")
            return original(sql, params)

        with unittest.mock.patch.object(self.db, "execute", fail_bump):
            self.assertEqual(
                self.join(gw, user_id="444444444444444444", fetch_count=False), -1
            )
        row = self.db.query_one(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", (gw.id,)
        )
        self.assertIsNone(row.get("entrant_count"), "unknown, not a stale known 0")
        self.db.reset_stats()
        self.assertEqual(self.svc.entry_count(gw.id), 1)
        stats = self.db.get_stats()
        self.assertEqual(stats["reads"], 2, "counter column + COUNT(*) fallback")
        self.assertEqual(stats["writes"], 1, "the heal write")
        healed = self.db.query_one(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", (gw.id,)
        )
        self.assertEqual(int(healed["entrant_count"]), 1)

    def test_draw_reads_only_user_ids(self) -> None:
        gw = self.make(winner_count=2)
        users = [str(10 ** 17 + i) for i in range(5)]
        for uid in users:
            self.join(gw, user_id=uid)
        original = self.svc.entry_user_ids
        calls: list[str] = []

        def spy(giveaway_id: str) -> list[str]:
            calls.append(giveaway_id)
            return original(giveaway_id)

        with unittest.mock.patch.object(
            self.svc, "entry_user_ids", side_effect=spy
        ), unittest.mock.patch.object(
            self.svc,
            "entries",
            side_effect=AssertionError("the draw must not read display columns"),
        ):
            ended, winners = self.svc.end(gw.id)
        self.assertEqual(calls, [gw.id], "exactly one user-id-only pool read")
        self.assertEqual(len(winners), 2)
        self.assertEqual(ended.entrant_count, len(users))

    def test_pick_reads_only_user_ids(self) -> None:
        gw = self.make(winner_count=2)
        for i in range(5):
            self.join(gw, user_id=str(10 ** 17 + i))
        original = self.svc.entry_user_ids
        calls: list[str] = []

        def spy(giveaway_id: str) -> list[str]:
            calls.append(giveaway_id)
            return original(giveaway_id)

        with unittest.mock.patch.object(
            self.svc, "entry_user_ids", side_effect=spy
        ), unittest.mock.patch.object(
            self.svc,
            "entries",
            side_effect=AssertionError("_pick must not read display columns"),
        ):
            picked = self.svc._pick(gw.id, 2)
        self.assertEqual(calls, [gw.id], "one user-id-only pool read")
        self.assertEqual(len(picked), 2)

    def test_end_reads_the_row_once_after_the_flip(self) -> None:
        gw = self.make(winner_count=2)
        for i in range(5):
            self.join(gw, user_id=str(10 ** 17 + i))
        self.db.reset_stats()
        ended, winners = self.svc.end(gw.id)
        stats = self.db.get_stats()
        self.assertLessEqual(stats["reads"], 2, "pre-flip get + one pool read")
        self.assertEqual(stats["writes"], 2, "the flip + the message-count wipe")
        self.assertEqual(ended.status, "ended")
        self.assertEqual(sorted(ended.winners), sorted(winners))
        self.assertEqual(ended.entrant_count, 5)

    def test_healthy_claim_repair_does_not_write(self) -> None:
        gw = self.make(winner_count=2, claim_timeout_seconds=3600)
        for i in range(4):
            self.join(gw, user_id=str(10 ** 17 + i))
        ended, winners = self.svc.end(gw.id)
        self.assertEqual(len(winners), 2)
        self.db.reset_stats()
        self.assertEqual(self.svc.repair_missing_claims(ended.id), 0)
        stats = self.db.get_stats()
        self.assertEqual(stats["writes"], 0, "every claim row belongs to a current winner")
        self.assertEqual(stats["reads"], 2, "the giveaway row + its claim rows")

    def test_statuses_reads_the_whole_window_in_one_statement(self) -> None:
        first = self.make(prize="first")
        second = self.make(prize="second")
        self.svc.end(second.id)
        self.db.reset_stats()
        statuses = self.svc.statuses([first.id, second.id])
        stats = self.db.get_stats()
        self.assertEqual(statuses, {first.id: "active", second.id: "ended"})
        self.assertEqual(stats["reads"], 1, "one IN(...) statement for the refresh window")

    def test_statuses_failure_is_an_empty_map_not_an_exception(self) -> None:
        self.assertEqual(self.svc.statuses([]), {})
        with unittest.mock.patch.object(
            self.db, "query", side_effect=RuntimeError("turso down")
        ):
            self.assertEqual(self.svc.statuses(["missing"]), {})

    def test_due_claims_repair_is_gated_by_the_caller(self) -> None:
        with unittest.mock.patch.object(
            self.svc, "repair_all_missing_claims"
        ) as repair:
            self.assertEqual(self.svc.due_claims(repair=False), [])
            repair.assert_not_called()
            self.svc.due_claims(repair=True)
            repair.assert_called_once()


class AuditRaceTests(ServiceTestCase):
    """Production-safety races for the join fast-path (audit fixes)."""

    def test_insert_guard_blocks_racing_blacklist(self) -> None:
        gw = self.make()
        fresh_gw = self.svc.get(gw.id)
        uid = "9" * 18
        self.svc.blacklist_add(self.guild, uid)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.join(
                fresh_gw, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                snapshot=(False, 0, 0, False),
            )
        self.assertIn("blocked", str(ctx.exception.message))
        self.assertEqual(self.svc.entry_count(gw.id), 0)

    def test_insert_guard_blocks_racing_deadline(self) -> None:
        gw = self.make()
        stale = self.svc.get(gw.id)
        self.db.execute(
            "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
            (now_ms() - 1, gw.id),
        )
        with self.assertRaises(ServiceError) as ctx:
            self.svc.join(
                stale, user_id="9" * 18, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                snapshot=(False, 0, 0, False),
            )
        self.assertIn("ended", str(ctx.exception.message))
        self.assertEqual(self.svc.entry_count(gw.id), 0)

    def test_insert_guard_blocks_penalty_race_and_consumes(self) -> None:
        gw = self.make()
        uid = "8" * 18
        stale_gw = self.svc.get(gw.id)
        self.svc.apply_timeout_penalty(self.guild, uid, giveaway_id="other_gw")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.join(
                stale_gw, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                snapshot=(False, 0, 0, False),
            )
        self.assertEqual(ctx.exception.kind, TIMEOUT_BAN_KIND)
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, uid), 2)

    def test_insert_guard_blocks_ban_hit_race(self) -> None:
        gw = self.make()
        uid = "8" * 18
        self.svc._record_ban_hit(self.guild, uid, gw.id)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.join(
                gw, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                snapshot=(False, 0, 0, False),
            )
        self.assertEqual(ctx.exception.kind, TIMEOUT_BAN_KIND)
        self.assertEqual(self.svc.entry_count(gw.id), 0)

    def test_stale_ban_snapshot_does_not_falsely_penalize(self) -> None:
        gw = self.make()
        count = self.svc.join(
            gw, user_id="7" * 18, username="u", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
            snapshot=(False, 3, 0, False),
        )
        self.assertEqual(count, 1)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, "7" * 18), 0)

    def test_stale_hit_snapshot_does_not_falsely_refuse(self) -> None:
        gw = self.make()
        count = self.svc.join(
            gw, user_id="7" * 18, username="u", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
            snapshot=(False, 0, 0, True),
        )
        self.assertEqual(count, 1)

    def test_stale_message_snapshot_live_confirm_allows(self) -> None:
        gw = self.make(min_messages=3)
        uid = "8" * 18
        for _ in range(3):
            self.svc.record_message(self.guild, uid)
        count = self.svc.join(
            gw, user_id=uid, username="u", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
            snapshot=(False, 0, 1, False),
        )
        self.assertEqual(count, 1)

    def test_stale_ended_never_penalizes_timed_out(self) -> None:
        gw = self.make()
        stale = self.svc.get(gw.id)
        self.svc.end(gw.id)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.join(
                stale, user_id="7" * 18, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                timed_out=True, snapshot=(False, 0, 0, False),
            )
        self.assertIn("ended", str(ctx.exception.message))
        self.assertNotEqual(ctx.exception.kind, TIMEOUT_BAN_KIND)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, "7" * 18), 0)

    def test_ambiguous_insert_fresh_age_succeeds(self) -> None:
        gw = self.make()
        uid = "9" * 18
        self.join(gw, user_id=uid)
        original = self.db.execute
        def flaky(sql, params=()):
            if sql.lstrip().startswith("INSERT INTO simple_entries"):
                raise OSError("connection reset")
            return original(sql, params)
        self.db.execute = flaky  # type: ignore[method-assign]
        try:
            count = self.svc.join(
                gw, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        finally:
            self.db.execute = original  # type: ignore[method-assign]
        self.assertGreaterEqual(count, 1)

    def test_ambiguous_insert_old_age_reports_duplicate(self) -> None:
        gw = self.make()
        uid = "9" * 18
        self.join(gw, user_id=uid)
        self.db.execute(
            "UPDATE simple_entries SET entered_at = ? WHERE giveaway_id = ? AND user_id = ?",
            (now_ms() - 60000, gw.id, uid),
        )
        original = self.db.execute
        def flaky(sql, params=()):
            if sql.lstrip().startswith("INSERT INTO simple_entries"):
                raise OSError("connection reset")
            return original(sql, params)
        self.db.execute = flaky  # type: ignore[method-assign]
        try:
            with self.assertRaises(ServiceError) as ctx:
                self.svc.join(
                    gw, user_id=uid, username="u", member_roles=[],
                    account_created_ts=time.time() - 400 * 86400,
                )
        finally:
            self.db.execute = original  # type: ignore[method-assign]
        self.assertIn("already entered", str(ctx.exception.message))

    def test_rowcount_negative_with_entry_succeeds(self) -> None:
        gw = self.make()
        uid = "9" * 18
        original = self.db.execute
        def fake(sql, params=()):
            cur = original(sql, params)
            if sql.lstrip().startswith("INSERT INTO simple_entries"):
                class Neg:
                    rowcount = -1
                return Neg()
            return cur
        self.db.execute = fake  # type: ignore[method-assign]
        try:
            count = self.svc.join(
                gw, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        finally:
            self.db.execute = original  # type: ignore[method-assign]
        self.assertEqual(count, 1)
        self.assertTrue(self.svc.has_entry(gw.id, uid))


class TimedBanTests(ServiceTestCase):
    """Hour bans: 0 = permanent, 1-720 timed, reason in both places."""

    def test_default_add_is_permanent(self) -> None:
        self.svc.blacklist_add(self.guild, "1" * 18)
        info = self.svc.ban_info(self.guild, "1" * 18)
        assert info is not None
        self.assertTrue(info.get("permanent"))
        self.assertIsNone(info.get("expires_at"))
        self.assertTrue(self.svc.is_blacklisted(self.guild, "1" * 18))

    def test_timed_ban_blocks_then_expires(self) -> None:
        gw = self.make()
        uid = "2" * 18
        base = now_ms()
        self.svc.blacklist_add(self.guild, uid, hours=5, reason="spam", now=base)
        info = self.svc.ban_info(self.guild, uid, now=base)
        assert info is not None
        self.assertFalse(info.get("permanent"))
        self.assertEqual(info.get("reason"), "spam")
        self.assertTrue(self.svc.is_blacklisted(self.guild, uid, now=base))
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, user_id=uid)
        self.assertIn("blocked", str(ctx.exception.message))
        # After expiry the same row reads as absent (lazy expiry, no write).
        later = base + 5 * 3600 * 1000 + 1
        self.assertFalse(self.svc.is_blacklisted(self.guild, uid, now=later))
        self.assertIsNone(self.svc.ban_info(self.guild, uid, now=later))
        # join() uses real now, so expire the row for real before re-joining.
        self.db.execute(
            "UPDATE simple_blacklist SET expires_at = ? WHERE guild_id = ? AND user_id = ?",
            (now_ms() - 1, self.guild, uid),
        )
        self.assertEqual(self.join(gw, user_id=uid), 1)

    def test_hours_validation(self) -> None:
        for bad in (-1, 721, "x"):
            with self.assertRaises(ServiceError):
                self.svc.blacklist_add(self.guild, "3" * 18, hours=bad)
        # Boundary values are accepted.
        self.svc.blacklist_add(self.guild, "3" * 18, hours=720)
        self.assertTrue(self.svc.is_blacklisted(self.guild, "3" * 18))
        self.svc.blacklist_remove(self.guild, "3" * 18)
        self.svc.blacklist_add(self.guild, "3" * 18, hours=0)
        self.assertTrue(self.svc.is_blacklisted(self.guild, "3" * 18))

    def test_reban_overwrites_expiry_and_reason(self) -> None:
        uid = "4" * 18
        base = now_ms()
        self.svc.blacklist_add(self.guild, uid, hours=5, reason="first", now=base)
        first = self.svc.ban_info(self.guild, uid, now=base)
        assert first is not None
        self.svc.blacklist_add(
            self.guild, uid, hours=2, reason="second", now=base + 1000
        )
        second = self.svc.ban_info(self.guild, uid, now=base + 1000)
        assert second is not None
        self.assertEqual(second.get("reason"), "second")
        self.assertLess(int(second.get("expires_at") or 0), int(first.get("expires_at") or 0))

    def test_reason_trimmed_to_256(self) -> None:
        self.svc.blacklist_add(self.guild, "5" * 18, hours=1, reason="x" * 300)
        info = self.svc.ban_info(self.guild, "5" * 18)
        assert info is not None
        self.assertEqual(len(str(info.get("reason"))), 256)

    def test_ban_message_shows_reason_and_expiry(self) -> None:
        uid = "6" * 18
        base = now_ms()
        self.svc.blacklist_add(self.guild, uid, hours=5, reason="spam", now=base)
        msg = self.svc.ban_message(self.guild, uid, now=base)
        self.assertIn("blocked", msg)
        self.assertIn("spam", msg)
        self.assertIn("Ban expires <t:", msg)
        # Permanent variant.
        self.svc.blacklist_add(self.guild, "7" * 18, hours=0, reason="bad")
        pmsg = self.svc.ban_message(self.guild, "7" * 18)
        self.assertIn("blocked", pmsg)
        self.assertIn("permanent", pmsg.lower())

    def test_timed_ban_purges_running_entries(self) -> None:
        gw = self.make()
        uid = "8" * 18
        self.join(gw, user_id=uid)
        self.svc.blacklist_add(self.guild, uid, hours=3, reason="r")
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        with self.assertRaises(ServiceError) as ctx:
            self.join(gw, user_id=uid)
        self.assertIn("blocked", str(ctx.exception.message))

    def test_count_and_list_hide_expired(self) -> None:
        base = now_ms()
        self.svc.blacklist_add(self.guild, "1" * 18, hours=0)
        self.svc.blacklist_add(self.guild, "2" * 18, hours=1, now=base)
        self.assertEqual(self.svc.count_blacklisted(self.guild, now=base), 2)
        later = base + 3600 * 1000 + 1
        self.assertEqual(self.svc.count_blacklisted(self.guild, now=later), 1)
        self.assertEqual(self.svc.blacklist_list(self.guild, now=later), ["1" * 18])
        detailed = self.svc.blacklist_list_detailed(self.guild, now=base)
        by_id = {r["user_id"]: r for r in detailed}
        self.assertTrue(by_id["1" * 18]["permanent"])
        self.assertFalse(by_id["2" * 18]["permanent"])
        self.assertIn("expires_at", by_id["2" * 18])

    def test_purge_expired_bans(self) -> None:
        base = now_ms()
        self.svc.blacklist_add(self.guild, "1" * 18, hours=1, now=base)
        self.svc.blacklist_add(self.guild, "2" * 18, hours=0)
        removed = self.svc.purge_expired_bans(now=base + 3600 * 1000 + 1)
        self.assertEqual(removed, 1)
        self.assertFalse(self.svc.is_blacklisted(self.guild, "1" * 18, now=base + 3600 * 1000 + 1))
        self.assertTrue(self.svc.is_blacklisted(self.guild, "2" * 18))

    def test_snapshot_is_expiry_aware(self) -> None:
        gw = self.make()
        uid = "9" * 18
        base = now_ms()
        self.svc.blacklist_add(self.guild, uid, hours=1, now=base)
        snap = self.svc.eligibility_snapshot(self.guild, uid, gw.id)
        assert snap is not None
        self.assertTrue(snap[0])

    def test_insert_guard_blocks_racing_timed_ban(self) -> None:
        gw = self.make()
        fresh = self.svc.get(gw.id)
        uid = "9" * 18
        self.svc.blacklist_add(self.guild, uid, hours=4, reason="race")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.join(
                fresh, user_id=uid, username="u", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
                snapshot=(False, 0, 0, False),
            )
        self.assertIn("blocked", str(ctx.exception.message))
        self.assertIn("race", str(ctx.exception.message))

    def test_zero_insert_reports_ban_with_reason(self) -> None:
        gw = self.make()
        uid = "9" * 18
        self.svc.blacklist_add(self.guild, uid, hours=4, reason="late race")
        original = self.svc.is_blacklisted
        calls = {"n": 0}
        def stale_first(guild_id: str, user_id: str) -> bool:
            calls["n"] += 1
            return False if calls["n"] == 1 else original(guild_id, user_id)
        self.svc.is_blacklisted = stale_first  # type: ignore[method-assign]
        try:
            with self.assertRaises(ServiceError) as ctx:
                self.join(gw, user_id=uid)
        finally:
            self.svc.is_blacklisted = original  # type: ignore[method-assign]
        self.assertIn("blocked", str(ctx.exception.message))


class DueExtendRaceTests(ServiceTestCase):
    """B04: a stale due() snapshot must not beat a concurrent extend.

    due() is a snapshot; the tick used to end whatever it listed with a
    status-only claim, so due-listed -> extended -> tick-end still ended the
    giveaway. The flip now re-checks ends_at itself (require_due=True), and
    manual /giveaway_end keeps the early-end behavior (require_due=False).
    """

    def _due_gw(self, base: int, extra_ms: int = 1000):
        gw = self.make(duration_seconds=60)
        self.join(gw)
        self.db.execute(
            "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
            (base + extra_ms, gw.id),
        )
        return self.svc.get(gw.id)

    def test_stale_due_loses_to_extend(self) -> None:
        base = now_ms()
        gw = self._due_gw(base)
        tick_ts = base + 2000
        # Tick snapshots while still due.
        self.assertIn(gw.id, [g.id for g in self.svc.due(now=tick_ts)])
        # Operator extends before the tick acts on its snapshot.
        self.svc.extend(gw.id, 3600)
        self.assertGreater(self.svc.get(gw.id).ends_at, tick_ts)
        # The stale tick end must refuse and leave the giveaway running.
        with self.assertRaises(ServiceError) as ctx:
            self.svc.end(gw.id, now=tick_ts, require_due=True)
        self.assertIn("not due", str(ctx.exception.message).lower())
        self.assertTrue(self.svc.get(gw.id).active)
        # Once the new deadline passes the tick ends it for real.
        later = self.svc.get(gw.id).ends_at + 1
        self.assertIn(gw.id, [g.id for g in self.svc.due(now=later)])
        ended, _winners = self.svc.end(gw.id, now=later, require_due=True)
        self.assertEqual(ended.status, "ended")

    def test_atomic_flip_rechecks_deadline(self) -> None:
        base = now_ms()
        gw = self._due_gw(base)
        tick_ts = base + 2000
        stale = self.svc.get(gw.id)
        # Extend behind the stale read: pre-check would pass on stale data,
        # so the UPDATE predicate itself must refuse.
        self.db.execute(
            "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
            (base + 5000000, gw.id),
        )
        original_get = self.svc.get
        calls = {"n": 0}

        def stale_first(giveaway_id: str):
            calls["n"] += 1
            if calls["n"] == 1:
                return stale
            return original_get(giveaway_id)

        self.svc.get = stale_first  # type: ignore[method-assign]
        try:
            with self.assertRaises(ServiceError) as ctx:
                self.svc.end(gw.id, now=tick_ts, require_due=True)
        finally:
            self.svc.get = original_get  # type: ignore[method-assign]
        self.assertIn("not due", str(ctx.exception.message).lower())
        self.assertTrue(self.svc.get(gw.id).active)

    def test_tick_end_still_wins_when_still_due(self) -> None:
        base = now_ms()
        gw = self._due_gw(base)
        tick_ts = base + 2000
        ended, winners = self.svc.end(gw.id, now=tick_ts, require_due=True)
        self.assertEqual(ended.status, "ended")
        self.assertEqual(sorted(ended.winners), sorted(winners))

    def test_manual_end_keeps_early_end(self) -> None:
        gw = self.make(duration_seconds=3600)
        self.join(gw)
        self.svc.extend(gw.id, 3600)
        # Manual /giveaway_end (no require_due) still ends before the deadline.
        ended, _winners = self.svc.end(gw.id)
        self.assertEqual(ended.status, "ended")


class JoinBurstTests(ServiceTestCase):
    """High-concurrency join proof: 100 simultaneous clicks, one row each."""

    def _burst_join(self, gw, user_ids, **kw):
        errors: list = []
        barrier = threading.Barrier(len(user_ids), timeout=30)

        def one(uid: str) -> None:
            try:
                barrier.wait()
                self.svc.join(
                    gw, user_id=uid, username="u" + uid[-4:],
                    member_roles=[],
                    account_created_ts=time.time() - 400 * 86400, **kw,
                )
            except ServiceError:
                pass
            except Exception as exc:  # noqa: BLE001 -- must surface as failure
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(uid,)) for uid in user_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [], f"burst raised: {errors!r}")

    def test_100_distinct_users_each_get_exactly_one_entry(self) -> None:
        gw = self.make()
        user_ids = [str(10 ** 17 + i) for i in range(100)]
        self._burst_join(gw, user_ids)
        self.assertEqual(self.svc.entry_count(gw.id), 100)
        for uid in user_ids:
            self.assertTrue(self.svc.has_entry(gw.id, uid), f"{uid} lost its entry")

    def test_simultaneous_duplicate_clicks_keep_one_row(self) -> None:
        gw = self.make()
        uid = "9" * 18
        barrier = threading.Barrier(10, timeout=30)
        outcomes: list = []

        def click() -> None:
            try:
                barrier.wait()
                self.svc.join(
                    gw, user_id=uid, username="dup", member_roles=[],
                    account_created_ts=time.time() - 400 * 86400,
                )
                outcomes.append("ok")
            except ServiceError as exc:
                outcomes.append(str(exc.message))

        threads = [threading.Thread(target=click) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(self.svc.entry_count(gw.id), 1)
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertTrue(all("already entered" in o for o in outcomes if o != "ok"))

    def test_join_while_closing_never_accepts_late_entries(self) -> None:
        gw = self.make()
        user_ids = [str(2 * 10 ** 17 + i) for i in range(40)]
        barrier = threading.Barrier(len(user_ids) + 1, timeout=30)
        ended: list = []

        def closer() -> None:
            barrier.wait()
            try:
                self.svc.end(gw.id)
                ended.append(True)
            except ServiceError:
                ended.append(False)

        def joiner(uid: str) -> None:
            try:
                barrier.wait()
                self.svc.join(
                    gw, user_id=uid, username="u" + uid[-4:],
                    member_roles=[],
                    account_created_ts=time.time() - 400 * 86400,
                )
            except ServiceError:
                pass

        threads = [threading.Thread(target=closer)]
        threads += [threading.Thread(target=joiner, args=(uid,)) for uid in user_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(ended), 1)
        # Whatever landed before the flip is the pool; the draw can only
        # pick from committed rows, never from refused late clicks.
        if self.svc.get(gw.id).active:
            _, winners = self.svc.end(gw.id)
        else:
            winners = self.svc.get(gw.id).winners
        rows = {r["user_id"] for r in self.svc.entries(gw.id)}
        for winner in winners:
            self.assertIn(winner, rows)

    def test_banned_user_gets_zero_entries_under_burst(self) -> None:
        gw = self.make()
        banned = str(10 ** 17 + 999)
        self.svc.blacklist_add(self.guild, banned, hours=5, reason="spam")
        user_ids = [str(10 ** 17 + i) for i in range(20)] + [banned]
        self._burst_join(gw, user_ids)
        self.assertFalse(self.svc.has_entry(gw.id, banned))
        self.assertEqual(self.svc.entry_count(gw.id), 20)

    def test_fetch_count_false_skips_count_but_commits(self) -> None:
        gw = self.make()
        before = self.db.get_stats()
        marker = self.svc.join(
            gw, user_id="1" * 18, username="u1", member_roles=[],
            account_created_ts=time.time() - 400 * 86400, fetch_count=False,
        )
        after = self.db.get_stats()
        self.assertEqual(marker, -1, "-1 means committed, count unknown")
        self.assertTrue(self.svc.has_entry(gw.id, "1" * 18))
        # One fewer read wave than the counted path: no COUNT(*) trip.
        self.assertLessEqual(after["reads"], before["reads"] + 4, after)

    def test_join_snapshot_matches_split_reads(self) -> None:
        gw = self.make(min_messages=2)
        uid = "3" * 18
        self.svc.record_message(self.guild, uid)
        self.svc.record_message(self.guild, uid)
        merged_gw, merged_snap = self.svc.join_snapshot(gw.id, self.guild, uid)
        split_gw = self.svc.get(gw.id)
        split_snap = self.svc.eligibility_snapshot(self.guild, uid, gw.id)
        self.assertEqual(merged_gw.id, split_gw.id)
        self.assertEqual(merged_snap, split_snap)

    def test_refresh_snapshot_returns_status_and_count(self) -> None:
        gw = self.make()
        self.join(gw, user_id="1" * 18)
        self.join(gw, user_id="2" * 18)
        status, count = self.svc.refresh_snapshot(gw.id)
        self.assertEqual(status, "active")
        self.assertEqual(count, 2)
        self.svc.end(gw.id)
        status, _count = self.svc.refresh_snapshot(gw.id)
        self.assertEqual(status, "ended")


if __name__ == "__main__":
    unittest.main()
