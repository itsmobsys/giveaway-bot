"""Rule-level tests, ported from ``bot/tests/test_service.py``.

Every test drives the real Rust service through the harness. Nothing here
re-implements a rule in Python: assertions are about what the engine stored,
returned or refused.

Conventions that keep the port honest:

* the fake clock is the only clock (``advance``/``set_time``), never ``sleep``;
* a draw is never asserted to pick a *particular* member, only invariants;
* cross-process races live in ``test_concurrency.py`` and restarts in
  ``test_recovery.py``, because one process cannot produce either.
"""

from __future__ import annotations

import json
import unittest

from tests.harness import HarnessError, HarnessTestCase, ServiceError

USER = "100000000000000010"
OTHER = "100000000000000011"
STAFF = "100000000000000012"


class CreateTests(HarnessTestCase):
    def test_defaults_and_round_trip(self) -> None:
        gw = self.gw(prize="  Steam key  ", host_name="Host", required_role_id="42")

        self.assertEqual("Steam key", gw["prize"])
        self.assertEqual("42", gw["required_role_id"])
        self.assertEqual(["42"], gw["required_role_ids"])
        self.assertEqual("active", gw["status"])
        self.assertEqual(1, gw["winner_count"])
        self.assertEqual(0, gw["entrant_count"])
        self.assertEqual(0, gw["claim_timeout_seconds"])
        self.assertEqual("Host", gw["host_name"])
        self.assertIsNone(gw["message_id"])
        self.assertEqual([], gw["winners"])

        row = self.h.one("SELECT * FROM simple_giveaways WHERE id = ?", [gw["id"]])
        assert row is not None
        self.assertEqual("[]", row["winners_json"])
        self.assertEqual(gw["ends_at"], row["ends_at"])
        self.assertEqual(3600 * 1000, row["ends_at"] - row["created_at"])

    def test_validation(self) -> None:
        cases = [
            ({"prize": ""}, "Prize must be 1-256 characters."),
            ({"prize": "x" * 257}, "Prize must be 1-256 characters."),
            ({"winner_count": 0}, "Winner count must be 1-25."),
            ({"winner_count": 26}, "Winner count must be 1-25."),
            ({"duration_seconds": 29}, "Duration must be 30 seconds to 60 days."),
            ({"duration_seconds": 60 * 86400 + 1}, "Duration must be 30 seconds to 60 days."),
            ({"min_messages": -1}, "Minimum messages must be 0-100000."),
            ({"min_messages": 100001}, "Minimum messages must be 0-100000."),
            ({"min_account_age_days": -1}, "Minimum account age must be 0-3650 days."),
            ({"min_account_age_days": 3651}, "Minimum account age must be 0-3650 days."),
            ({"image_url": "ftp://example.test/a.png"}, "Image must be an http(s) URL."),
            ({"image_url": "https://example.test/" + "a" * 500}, "Image must be an http(s) URL."),
            ({"claim_timeout_seconds": -1}, "Claim window must be 0 (off) or up to 60 days in seconds."),
            (
                {"claim_timeout_seconds": 60 * 86400 + 1},
                "Claim window must be 0 (off) or up to 60 days in seconds.",
            ),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ServiceError) as caught:
                    self.gw(**overrides)
                self.assertEqual(message, str(caught.exception))

    def test_role_list_is_capped_and_deduplicated(self) -> None:
        gw = self.gw(required_role_ids=["1", "2", "3", "4", "5", "6", "7"])
        # Trimmed and capped at five, in the order the caller gave them.
        self.assertEqual(["1", "2", "3", "4", "5"], gw["required_role_ids"])

        # The legacy single role is only appended when the list does not
        # already name it: "9" leads here, so it is not added twice.
        gw2 = self.gw(required_role_id="9", required_role_ids=["9", "8"])
        self.assertEqual(["9", "8"], gw2["required_role_ids"])
        self.assertEqual("9", gw2["required_role_id"])

    def test_legacy_required_role_column_is_merged(self) -> None:
        gw = self.gw(required_role_id="legacy", required_role_ids=["a", "b"])
        self.assertEqual(["a", "b", "legacy"], gw["required_role_ids"])
        row = self.h.one("SELECT required_role_ids FROM simple_giveaways WHERE id = ?", [gw["id"]])
        assert row is not None
        self.assertEqual(["a", "b", "legacy"], json.loads(row["required_role_ids"]))

    def test_get_missing_raises(self) -> None:
        with self.assertRaises(ServiceError) as caught:
            self.h.call("get", giveaway_id="does-not-exist")
        self.assertEqual("Giveaway not found.", str(caught.exception))


class LookupTests(HarnessTestCase):
    def test_list_active_is_guild_scoped_and_ordered(self) -> None:
        later = self.gw(duration_seconds=1200)
        sooner = self.gw(duration_seconds=600)
        self.gw(guild_id=self.other_guild, duration_seconds=300)
        finished = self.gw(duration_seconds=300)
        self.end(finished)

        ids = [gw["id"] for gw in self.h.call("list_active", guild_id=self.guild)]
        self.assertEqual([sooner["id"], later["id"]], ids)

    def test_resolve_refuses_another_guild(self) -> None:
        foreign = self.gw(guild_id=self.other_guild)
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve", guild_id=self.guild, raw_id=foreign["id"])
        self.assertEqual("That giveaway belongs to another server.", str(caught.exception))

    def test_resolve_falls_back_to_the_only_live_one(self) -> None:
        gw = self.gw()
        self.assertEqual(gw["id"], self.h.call("resolve", guild_id=self.guild, raw_id="typo")["id"])

    def test_resolve_needs_an_id_when_several_are_live(self) -> None:
        self.gw()
        self.gw()
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve", guild_id=self.guild, raw_id="")
        self.assertIn("That ID did not match.", str(caught.exception))

    def test_resolve_with_nothing_live(self) -> None:
        self.end(self.gw())
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve", guild_id=self.guild, raw_id="")
        self.assertEqual("No active giveaway in this server.", str(caught.exception))

    def test_get_by_message(self) -> None:
        gw = self.gw()
        self.h.call("set_message", giveaway_id=gw["id"], message_id="m-1")
        found = self.h.call("get_by_message", message_id="m-1")
        assert found is not None
        self.assertEqual(gw["id"], found["id"])
        self.assertIsNone(self.h.call("get_by_message", message_id="m-2"))


class JoinTests(HarnessTestCase):
    def test_join_then_leave(self) -> None:
        gw = self.gw()
        self.assertEqual(1, self.enter(gw, USER))
        self.assertTrue(self.h.call("has_entry", giveaway_id=gw["id"], user_id=USER))
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"]))

        self.assertEqual([True, 0, "active"], self.h.call(
            "leave_with_count", giveaway_id=gw["id"], user_id=USER
        ))
        self.assertFalse(self.h.call("has_entry", giveaway_id=gw["id"], user_id=USER))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))
        # A leave must undo the join bump, not leave the live counter behind.
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"]))

    def test_leave_after_the_end_keeps_the_entry(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.end(gw)

        self.assertEqual([False, 0, ""], self.h.call(
            "leave_with_count", giveaway_id=gw["id"], user_id=USER
        ))
        self.assertTrue(self.h.call("has_entry", giveaway_id=gw["id"], user_id=USER))

    def test_a_non_duplicate_constraint_error_is_not_called_already_entered(self) -> None:
        gw = self.gw()
        self.trigger(
            "CREATE TRIGGER bad_entry BEFORE INSERT ON simple_entries "
            "BEGIN SELECT RAISE(ABORT, 'NOT NULL constraint failed: simple_entries.username'); END"
        )
        # Any constraint failure other than the duplicate-entry one is a real
        # bug and must surface, not be disguised as "you are already entered".
        with self.assertRaises(HarnessError) as caught:
            self.enter(gw, USER)
        self.assertNotIn("already entered", str(caught.exception))

    def test_double_join_is_a_service_error(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.assertEqual("You are already entered.", str(self.join_error(gw, USER)))
        self.assertEqual(1, self.entry_rows(gw["id"]))

    def test_required_roles_need_one_match(self) -> None:
        gw = self.gw(required_role_ids=["r1", "r2"])
        self.assertEqual(
            "You need one of the required roles to enter.",
            str(self.join_error(gw, USER, member_roles=["other"])),
        )
        self.assertEqual(1, self.enter(gw, USER, member_roles=["r2"]))

    def test_blocked_role_cannot_enter(self) -> None:
        gw = self.gw(blocked_role_id="bad")
        self.assertEqual(
            "Your role is not allowed to enter.",
            str(self.join_error(gw, USER, member_roles=["bad"])),
        )
        self.assertEqual(1, self.enter(gw, USER, member_roles=["good"]))

    def test_account_age_is_enforced_and_fails_closed(self) -> None:
        gw = self.gw(min_account_age_days=30)

        # An unreadable age must refuse: waving it through silently disables the
        # alt-account filter.
        self.assertEqual(
            "Could not check your account age — try again in a moment.",
            str(self.join_error(gw, USER, account_created_ts=None)),
        )
        self.assertEqual(
            "Account must be 30+ days old.",
            str(self.join_error(gw, USER, account_created_ts=self.account_age(10))),
        )
        self.assertEqual(1, self.enter(gw, USER, account_created_ts=self.account_age(40)))

    def test_min_messages_uses_the_database_count(self) -> None:
        gw = self.gw(min_messages=5)
        for _ in range(4):
            self.h.call("record_message", guild_id=self.guild, user_id=USER)
        self.assertEqual(
            "You need 5+ messages in this server (4 counted).",
            str(self.join_error(gw, USER)),
        )
        self.h.call("record_message", guild_id=self.guild, user_id=USER)
        self.assertEqual(1, self.enter(gw, USER))

    def test_min_messages_counts_the_unflushed_buffer(self) -> None:
        gw = self.gw(min_messages=5)
        self.h.call("add_message_counts", rows=[[self.guild, USER, 3]])
        # Three flushed + two still in the in-memory buffer must clear a 5 gate.
        self.assertEqual(1, self.enter(gw, USER, pending_messages=2))

    def test_has_entry_is_a_targeted_lookup(self) -> None:
        gw = self.gw()
        self.assertFalse(self.h.call("has_entry", giveaway_id=gw["id"], user_id=""))
        self.enter(gw, USER)
        self.assertTrue(self.h.call("has_entry", giveaway_id=gw["id"], user_id=USER))
        self.assertFalse(self.h.call("has_entry", giveaway_id=gw["id"], user_id=OTHER))

    def test_entries_can_be_limited_for_a_display_path(self) -> None:
        gw = self.gw()
        members = [f"30000000000000{index:04d}" for index in range(3)]
        for member in members:
            self.enter(gw, member)
            self.h.advance(1)  # entered_at is the only ordering key

        rows = self.h.call("entries", giveaway_id=gw["id"], limit=2)
        self.assertEqual([member["user_id"] for member in rows], members[:2])
        # limit.max(1): a display path never asks for zero rows by accident.
        self.assertEqual(1, len(self.h.call("entries", giveaway_id=gw["id"], limit=0)))

    def test_ended_giveaway_refuses_joins(self) -> None:
        gw = self.gw()
        self.end(gw)
        self.assertEqual("This giveaway has ended.", str(self.join_error(gw, USER)))

    def test_expired_deadline_refuses_joins(self) -> None:
        gw = self.gw(duration_seconds=60)
        self.h.advance(61_000)
        self.assertEqual("This giveaway has ended.", str(self.join_error(gw, USER)))


class MessageCountTests(HarnessTestCase):
    def test_record_message_increments(self) -> None:
        for _ in range(3):
            self.h.call("record_message", guild_id=self.guild, user_id=USER)
        self.assertEqual(3, self.h.call("message_count", guild_id=self.guild, user_id=USER))
        self.assertEqual(0, self.h.call("message_count", guild_id=self.guild, user_id=OTHER))

    def test_batch_flush_adds_deltas(self) -> None:
        result = self.h.call(
            "add_message_counts",
            rows=[[self.guild, USER, 2], [self.guild, USER, 3], [self.other_guild, USER, 4]],
        )
        # "before" counts the distinct keys snapshotted (two), not the rows sent.
        self.assertEqual({"applied": 3, "before": 2}, result)
        self.assertEqual(5, self.h.call("message_count", guild_id=self.guild, user_id=USER))
        self.assertEqual(4, self.h.call("message_count", guild_id=self.other_guild, user_id=USER))

    def test_batch_flush_chunks_large_payloads(self) -> None:
        rows = [[self.guild, f"40000000000000{index:04d}", 1] for index in range(301)]
        result = self.h.call("add_message_counts", rows=rows)
        # 301 rows is two statements: the chunk size is 300.
        self.assertEqual({"applied": 301, "before": 301}, result)
        self.assertEqual(1, self.h.call("message_count", guild_id=self.guild, user_id=rows[0][1]))
        self.assertEqual(1, self.h.call("message_count", guild_id=self.guild, user_id=rows[-1][1]))

    def test_ambiguous_chunk_failure_requeues_only_what_is_missing(self) -> None:
        # Before the chunk ran the stored count was 3; the chunk carried +5; a
        # partial write landed +2 (stored 5). Only 3 are still missing.
        self.h.call("add_message_counts", rows=[[self.guild, USER, 3]])
        self.h.call("add_message_counts", rows=[[self.guild, USER, 2]])
        shortfall = self.h.call(
            "reconcile_message_counts",
            rows=[[self.guild, USER, 5]],
            before=[[self.guild, USER, 3]],
        )
        self.assertEqual([[self.guild, USER, 3]], shortfall)
        self.h.call("add_message_counts", rows=shortfall)
        self.assertEqual(8, self.h.call("message_count", guild_id=self.guild, user_id=USER))

    def test_deterministic_chunk_failure_requeues_the_whole_chunk(self) -> None:
        # No pre-write snapshot means the stored value cannot be compared, so the
        # whole delta is requeued: over-counting beats losing messages.
        shortfall = self.h.call(
            "reconcile_message_counts",
            rows=[[self.guild, USER, 4]],
            before=[],
        )
        self.assertEqual([[self.guild, USER, 4]], shortfall)

    def test_end_resets_counts_for_the_next_grind(self) -> None:
        for _ in range(4):
            self.h.call("record_message", guild_id=self.guild, user_id=USER)
        self.end(self.gw())

        self.assertEqual(0, self.h.call("message_count", guild_id=self.guild, user_id=USER))

    def test_end_keeps_counts_while_another_giveaway_needs_them(self) -> None:
        first = self.gw(min_messages=5)
        self.gw(min_messages=5)
        for _ in range(4):
            self.h.call("record_message", guild_id=self.guild, user_id=USER)

        self.end(first)

        # Zeroing now would silently reset the grind the other giveaway measures.
        self.assertEqual(4, self.h.call("message_count", guild_id=self.guild, user_id=USER))

    def test_end_resets_only_the_guild_whose_giveaway_ended(self) -> None:
        self.h.call("record_message", guild_id=self.guild, user_id=USER)
        self.h.call("record_message", guild_id=self.other_guild, user_id=USER)
        self.end(self.gw())

        self.assertEqual(0, self.h.call("message_count", guild_id=self.guild, user_id=USER))
        self.assertEqual(1, self.h.call("message_count", guild_id=self.other_guild, user_id=USER))


class DrawTests(HarnessTestCase):
    def test_end_draws_once_and_refuses_a_second_call(self) -> None:
        gw = self.gw(winner_count=1)
        entrants = {f"50000000000000{index:04d}" for index in range(3)}
        for member in entrants:
            self.enter(gw, member)

        ended, winners = self.end(gw)

        self.assertEqual("ended", ended["status"])
        self.assertEqual(1, len(winners))
        self.assertIn(winners[0], entrants)
        # entrant_count records the pool the draw saw, not the winner count.
        self.assertEqual(3, ended["entrant_count"])
        self.assertEqual("This giveaway has already ended.", str(self.end_error(gw)))

    def end_error(self, gw: dict) -> ServiceError:
        with self.assertRaises(ServiceError) as caught:
            self.end(gw)
        return caught.exception

    def test_end_without_entries_is_safe(self) -> None:
        ended, winners = self.end(self.gw())
        self.assertEqual([], winners)
        self.assertEqual(0, ended["entrant_count"])
        self.assertEqual([], ended["winners"])

    def test_winners_never_exceed_the_pool(self) -> None:
        gw = self.gw(winner_count=5)
        entrants = {f"50000000000000{index:04d}" for index in range(3)}
        for member in entrants:
            self.enter(gw, member)

        _, winners = self.end(gw)

        self.assertEqual(3, len(winners))
        self.assertEqual(3, len(set(winners)))
        self.assertTrue(set(winners) <= entrants)

    def test_reroll_excludes_previous_winners(self) -> None:
        gw = self.gw(winner_count=1)
        entrants = {f"50000000000000{index:04d}" for index in range(4)}
        for member in entrants:
            self.enter(gw, member)
        _, winners = self.end(gw)

        _, fresh = self.h.call("reroll", giveaway_id=gw["id"], count=1)

        self.assertEqual(1, len(fresh))
        self.assertNotIn(fresh[0], winners)
        stored = self.h.call("get", giveaway_id=gw["id"])
        self.assertEqual(2, len(stored["winners"]))
        self.assertEqual(len(stored["winners"]), len(set(stored["winners"])))

    def test_reroll_refuses_when_the_pool_is_exhausted(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.end(gw)

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("No other entrants left to reroll", str(caught.exception))

    def test_reroll_refuses_after_the_entries_were_wiped(self) -> None:
        gw = self.gw()
        for index in range(3):
            self.enter(gw, f"50000000000000{index:04d}")
        self.end(gw)
        self.execute("DELETE FROM simple_entries WHERE giveaway_id = ?", [gw["id"]])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("No other entrants left to reroll", str(caught.exception))

    def test_end_stores_the_entrant_count(self) -> None:
        gw = self.gw()
        for index in range(3):
            self.enter(gw, f"50000000000000{index:04d}")

        ended, _ = self.end(gw)

        self.assertEqual(3, ended["entrant_count"])
        self.assertEqual(3, self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))

    def test_a_failed_end_write_leaves_the_giveaway_active(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.trigger(
            "CREATE TRIGGER fail_end BEFORE UPDATE ON simple_giveaways WHEN NEW.status = 'ended' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )

        with self.assertRaises(HarnessError):
            self.end(gw)

        stored = self.h.call("get", giveaway_id=gw["id"])
        self.assertEqual("active", stored["status"])
        self.assertEqual([], stored["winners"])
        self.assertEqual("[]", self.h.scalar(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))

        # The guarded flip is the only step allowed to raise; once the failure
        # is gone the same call must still be able to end it.
        self.drop_trigger("fail_end")
        ended, winners = self.end(gw)
        self.assertEqual("ended", ended["status"])
        self.assertEqual(1, len(winners))

    def test_cancel_stores_the_entrant_count_and_loses_a_race_to_end(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.enter(gw, OTHER)

        cancelled = self.h.call("cancel", giveaway_id=gw["id"])

        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual(2, cancelled["entrant_count"])
        self.assertEqual("This giveaway has already ended.", str(self.end_error(gw)))

    def test_reroll_refuses_a_running_giveaway(self) -> None:
        gw = self.gw()
        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("End the giveaway before rerolling.", str(caught.exception))

    def test_reroll_never_exceeds_the_winner_cap(self) -> None:
        gw = self.gw(winner_count=25)
        for index in range(30):
            self.enter(gw, f"50000000000000{index:04d}")
        self.end(gw)

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=5)
        self.assertIn("25 winners", str(caught.exception))

    def test_a_cancelled_giveaway_cannot_be_rerolled(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.h.call("cancel", giveaway_id=gw["id"])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("A cancelled giveaway cannot be rerolled.", str(caught.exception))

    def test_extend_and_cancel(self) -> None:
        gw = self.gw(duration_seconds=600)

        extended = self.h.call("extend", giveaway_id=gw["id"], extra_seconds=60)
        self.assertEqual(gw["ends_at"] + 60_000, extended["ends_at"])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("extend", giveaway_id=gw["id"], extra_seconds=59)
        self.assertEqual("Extend by 1 minute to 60 days at a time.", str(caught.exception))

        cancelled = self.h.call("cancel", giveaway_id=gw["id"])
        self.assertEqual("cancelled", cancelled["status"])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("cancel", giveaway_id=gw["id"])
        self.assertEqual("Only an active giveaway can be cancelled.", str(caught.exception))

        with self.assertRaises(ServiceError) as caught:
            self.h.call("extend", giveaway_id=gw["id"], extra_seconds=60)
        self.assertEqual("Only a running giveaway can be extended.", str(caught.exception))

    def test_discard_removes_a_never_posted_giveaway(self) -> None:
        gw = self.gw()
        self.h.call("set_message", giveaway_id=gw["id"], message_id="m-1")
        self.enter(gw, USER)

        self.h.call("discard", giveaway_id=gw["id"])

        with self.assertRaises(ServiceError):
            self.h.call("get", giveaway_id=gw["id"])
        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, int(self.h.scalar("SELECT COUNT(*) FROM simple_claims")))


class RetentionTests(HarnessTestCase):
    def test_leftover_entrants_roles_are_reported(self) -> None:
        ended = self.gw()
        self.h.call("set_entrants_role", giveaway_id=ended["id"], role_id="r-ended")
        self.end(ended)

        running = self.gw()
        self.h.call("set_entrants_role", giveaway_id=running["id"], role_id="r-running")

        leftovers = [gw["id"] for gw in self.h.call("ended_with_roles", limit=50)]
        self.assertEqual([ended["id"]], leftovers)

    def test_entry_rows_are_wiped_after_the_retention_window(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        self.end(gw)

        self.h.advance(5 * 60 * 60 * 1000 + 1)
        deleted = self.h.call("wipe_stale_entries")

        self.assertEqual(2, deleted)
        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"]))
        self.assertEqual(0, self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))

    def test_rows_without_an_ended_at_still_expire(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        # Giveaways that finished before ended_at existed carry NULL forever.
        self.execute(
            "UPDATE simple_giveaways SET status = 'ended', ended_at = NULL, created_at = ? WHERE id = ?",
            [self.h.now() - 6 * 60 * 60 * 1000, gw["id"]],
        )

        self.assertEqual(2, self.h.call("wipe_stale_entries"))
        self.assertEqual(0, self.entry_rows(gw["id"]))


class BlacklistTests(HarnessTestCase):
    def test_add_blocks_and_purges_only_running_giveaways(self) -> None:
        running = self.gw()
        finished = self.gw()
        self.enter(running, USER)
        self.enter(finished, USER)
        self.end(finished)

        info = self.h.call(
            "blacklist_add",
            guild_id=self.guild,
            user_id=USER,
            hours=0,
            reason="spam",
            banned_by=STAFF,
        )

        self.assertTrue(info["permanent"])
        self.assertIsNone(info["expires_at"])
        self.assertIn("blocked", self.h.call("ban_message", guild_id=self.guild, user_id=USER))
        self.assertEqual(0, self.entry_rows(running["id"]))
        self.assertEqual(1, self.entry_rows(finished["id"]))
        self.assertIn("blocked", str(self.join_error(running, USER)))

    def test_count_matches_the_list(self) -> None:
        members = [f"60000000000000{index:04d}" for index in range(3)]
        for member in members:
            self.h.call("blacklist_add", guild_id=self.guild, user_id=member)

        self.assertEqual(3, self.h.call("count_blacklisted", guild_id=self.guild))
        self.assertEqual(members, self.h.call("blacklist_list", guild_id=self.guild, limit=50))

    def test_the_purge_leaves_other_guilds_alone(self) -> None:
        here = self.gw()
        there = self.gw(guild_id=self.other_guild)
        self.enter(here, USER)
        self.enter(there, USER)

        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)

        self.assertEqual(0, self.entry_rows(here["id"]))
        self.assertEqual(1, self.entry_rows(there["id"]))

    def test_a_blacklist_landing_mid_join_still_refuses(self) -> None:
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        # The snapshot says "clean": only the INSERT guard can catch the ban.
        self.assertIn("blocked", str(self.join_error(gw, USER, snapshot=[False, 0, 0, False])))

    def test_remove_and_list(self) -> None:
        self.h.call(
            "blacklist_add",
            guild_id=self.guild,
            user_id=USER,
            hours=1,
            reason="  spam  ",
            banned_by=STAFF,
        )
        detailed = self.h.call("blacklist_list_detailed", guild_id=self.guild)
        self.assertEqual(1, len(detailed))
        self.assertEqual("spam", detailed[0]["reason"])
        self.assertEqual(STAFF, detailed[0]["banned_by"])
        self.assertFalse(detailed[0]["permanent"])

        self.assertTrue(self.h.call("blacklist_remove", guild_id=self.guild, user_id=USER))
        self.assertFalse(self.h.call("blacklist_remove", guild_id=self.guild, user_id=USER))
        self.assertEqual([], self.h.call("blacklist_list", guild_id=self.guild))
        self.assertEqual(0, self.h.call("count_blacklisted", guild_id=self.guild))


class TimeoutBanTests(HarnessTestCase):
    def _start_penalty(self, user_id: str = USER) -> dict:
        gw = self.gw()
        error = self.timed_join_error(gw, user_id)
        self.assertEqual("timeout_ban", error.kind)
        return gw

    def _remaining(self, user_id: str = USER) -> int:
        return int(self.h.call("timeout_ban_remaining", guild_id=self.guild, user_id=user_id))

    def test_a_timed_out_member_is_refused_and_starts_the_penalty(self) -> None:
        gw = self.gw()
        error = self.timed_join_error(gw, USER)

        self.assertEqual("timeout_ban", error.kind)
        self.assertIn("sit out the next 3", str(error))
        self.assertEqual(3, self._remaining())
        self.assertEqual(0, self.entry_rows(gw["id"]))

    def test_a_timeout_refusal_is_tagged_for_the_caller(self) -> None:
        # The bot branches on this tag to take back the entrant role it granted.
        self.assertEqual("timeout_ban", self.timed_join_error(self.gw(), USER).kind)

    def test_an_untimed_out_member_is_unaffected(self) -> None:
        self.assertEqual(1, self.enter(self.gw(), USER))
        self.assertEqual(0, self._remaining())

    def test_a_role_is_never_mistaken_for_a_timeout(self) -> None:
        gw = self.gw()
        self.assertEqual(1, self.enter(gw, USER, member_roles=["some-role"]))
        self.assertEqual(0, self._remaining())

    def test_three_blocked_giveaways_spend_the_penalty_and_lift_it(self) -> None:
        self._start_penalty()
        left: list[int] = []
        for _ in range(3):
            blocked = self.gw()
            # No timed_out flag here: the penalty alone must keep refusing.
            self.assertIn("penalty", str(self.join_error(blocked, USER)))
            self.assertEqual(0, self.entry_rows(blocked["id"]))
            left.append(self._remaining())

        self.assertEqual([2, 1, 0], left, "one giveaway per blocked attempt")
        # The zero row is deleted, not left behind to show up in a listing.
        self.assertEqual([], self.h.call("list_timeout_bans", guild_id=self.guild))
        self.assertEqual(1, self.enter(self.gw(), USER))

    def test_repeated_attempts_never_stack_or_respend_the_penalty(self) -> None:
        gw = self._start_penalty()
        for _ in range(5):
            # Still timed out, same giveaway: neither a second penalty nor a
            # second giveaway spent on the one they were already caught in.
            self.assertIn("3 giveaway(s) left", str(self.timed_join_error(gw, USER)))
            self.assertEqual(3, self._remaining())
        self.assertEqual(1, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?",
            [self.guild, USER],
        )))

        # A different giveaway while still timed out spends one and reports the
        # running penalty, never a fresh three.
        other = self.gw()
        self.assertIn("2 giveaway(s) left", str(self.timed_join_error(other, USER)))
        self.assertEqual(2, self._remaining())

    def test_alternating_giveaways_cannot_burn_the_penalty(self) -> None:
        first = self._start_penalty()
        second = self.gw()

        self.timed_join_error(second, USER)
        self.assertEqual(2, self._remaining())
        # Both giveaways already count as sat out, so neither may spend again.
        self.timed_join_error(first, USER)
        self.timed_join_error(second, USER)
        self.assertEqual(2, self._remaining())

    def test_the_giveaway_that_spent_the_last_unit_still_refuses(self) -> None:
        self._start_penalty()
        blocked = [self.gw() for _ in range(3)]
        for gw in blocked:
            self.join_error(gw, USER)
        self.assertEqual(0, self._remaining())

        # The penalty is gone, but this giveaway is one they already sat out:
        # rejoining it would hand the penalty straight back, so it refuses.
        error = self.join_error(blocked[-1], USER)
        self.assertEqual("timeout_ban", error.kind)
        self.assertIn("sat out this giveaway", str(error))
        self.assertEqual(0, self.entry_rows(blocked[-1]["id"]))
        self.assertEqual(1, self.enter(self.gw(), USER))

    def test_an_entry_made_before_the_timeout_is_dropped(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.timed_join_error(gw, USER)

        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"]))

    def test_a_penalty_clears_entries_in_every_running_giveaway(self) -> None:
        first = self.gw()
        second = self.gw()
        self.enter(first, USER)
        self.enter(second, USER)

        self.timed_join_error(self.gw(), USER)

        self.assertEqual(0, self.entry_rows(first["id"]))
        self.assertEqual(0, self.entry_rows(second["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=first["id"]))

    def test_a_blacklisted_member_is_refused_without_a_penalty(self) -> None:
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)

        self.assertIn("blocked", str(self.timed_join_error(gw, USER)))
        self.assertEqual(0, self._remaining())

    def test_an_ended_giveaway_never_hands_out_a_penalty(self) -> None:
        gw = self.gw()
        self.end(gw)

        self.assertEqual("This giveaway has ended.", str(self.timed_join_error(gw, USER)))
        self.assertEqual(0, self._remaining())

    def test_an_expired_deadline_never_hands_out_a_penalty(self) -> None:
        gw = self.gw(duration_seconds=60)
        self.h.advance(61_000)

        self.assertEqual("This giveaway has ended.", str(self.timed_join_error(gw, USER)))
        self.assertEqual(0, self._remaining())

    def test_the_penalty_is_per_guild_and_per_user(self) -> None:
        self._start_penalty()

        self.assertEqual(1, self.enter(self.gw(guild_id=self.other_guild), USER))
        self.assertEqual(1, self.enter(self.gw(), OTHER))
        self.assertEqual(3, self._remaining())

    def test_a_second_penalty_never_shrinks_the_first(self) -> None:
        args = {"guild_id": self.guild, "user_id": USER, "giveaway_id": self.gw()["id"]}
        self.assertEqual(5, self.h.call("apply_timeout_penalty", giveaways=5, **args))
        self.assertEqual(5, self.h.call("apply_timeout_penalty", giveaways=3, **args))
        self.assertEqual(5, self.h.call("apply_timeout_penalty", giveaways=1, **args))

    def test_one_penalty_does_not_block_anybody_else(self) -> None:
        self._start_penalty()
        self.assertEqual(1, self.enter(self.gw(), OTHER))

    def test_the_entry_sweep_never_touches_a_penalty(self) -> None:
        self._start_penalty()
        gw = self.gw()
        self.enter(gw, OTHER)
        self.end(gw)
        self.execute(
            "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
            [self.h.now() - 6 * 60 * 60 * 1000, gw["id"]],
        )

        self.assertEqual(1, self.h.call("wipe_stale_entries"))

        self.assertEqual(3, self._remaining())
        self.assertEqual(1, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ?",
            [self.guild, USER],
        )))

    def test_counting_matches_the_listing(self) -> None:
        users = {f"70000000000000{index:04d}": left for index, left in enumerate((2, 5, 3))}
        for user_id, left in users.items():
            self.h.call(
                "apply_timeout_penalty",
                guild_id=self.guild,
                user_id=user_id,
                giveaways=left,
                giveaway_id=self.gw()["id"],
            )

        self.assertEqual(3, self.h.call("count_timeout_bans", guild_id=self.guild))
        listed = self.h.call("list_timeout_bans", guild_id=self.guild, limit=50)
        self.assertEqual([5, 3, 2], [row["giveaways_remaining"] for row in listed])

    def test_list_is_guild_scoped_ordered_and_skips_zero_rows(self) -> None:
        self.execute(
            "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) VALUES (?, ?, 0)",
            [self.guild, "700000000000000099"],
        )
        self.execute(
            "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) VALUES (?, ?, 9)",
            [self.other_guild, "700000000000000098"],
        )
        self.h.call("apply_timeout_penalty", guild_id=self.guild, user_id="a", giveaways=1)
        self.h.call("apply_timeout_penalty", guild_id=self.guild, user_id="b", giveaways=4)

        listed = self.h.call("list_timeout_bans", guild_id=self.guild, limit=50)

        self.assertEqual(["b", "a"], [row["user_id"] for row in listed])
        # limit is clamped to at least one and applied by the query.
        self.assertEqual(1, len(self.h.call("list_timeout_bans", guild_id=self.guild, limit=1)))


class ClaimTests(HarnessTestCase):
    def _ended_with_claims(
        self,
        entrants: int = 3,
        *,
        winner_count: int = 1,
        claim_timeout_seconds: int = 300,
    ) -> tuple[dict, list[str], list[str]]:
        gw = self.gw(
            winner_count=winner_count, claim_timeout_seconds=claim_timeout_seconds
        )
        users = [f"80000000000000{index:04d}" for index in range(entrants)]
        for member in users:
            self.enter(gw, member)
        _, winners = self.end(gw)
        self.assertEqual(winner_count, len(winners))
        self.assertTrue(set(winners) <= set(users))
        return gw, list(winners), users

    def test_create_validates_claim_window(self) -> None:
        for seconds in (-1, 60 * 86400 + 1):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ServiceError):
                    self.gw(claim_timeout_seconds=seconds)
        # 0 is off, and a positive window round-trips.
        self.assertEqual(0, self.gw(claim_timeout_seconds=0)["claim_timeout_seconds"])
        self.assertEqual(60, self.gw(claim_timeout_seconds=60)["claim_timeout_seconds"])

    def test_end_opens_claims_when_enabled(self) -> None:
        gw, winners, _ = self._ended_with_claims()

        pending = self.h.call("pending_claims", giveaway_id=gw["id"])
        ended_at = self.h.scalar("SELECT ended_at FROM simple_giveaways WHERE id = ?", [gw["id"]])

        self.assertEqual([winners[0]], [row["user_id"] for row in pending])
        self.assertEqual("pending", pending[0]["status"])
        # The deadline is anchored to ended_at, so a restart can rebuild it.
        self.assertEqual(ended_at + 300_000, pending[0]["deadline_ms"])

        # Restart recovery: a second process on the same file sees the same rows.
        again = self.sibling().call("pending_claims", giveaway_id=gw["id"])
        self.assertEqual([winners[0]], [row["user_id"] for row in again])

    def test_end_creates_no_claims_when_disabled(self) -> None:
        gw, winners, _ = self._ended_with_claims(claim_timeout_seconds=0)

        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))
        self.assertEqual([], self.h.call("due_claims", now=self.h.now() + 10 * 86400 * 1000))
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])
        self.assertEqual("Only the drawn winner can claim this prize.", str(caught.exception))

    def test_claim_happy_path_and_double_claim_refused(self) -> None:
        gw, winners, _ = self._ended_with_claims()

        row = self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])

        self.assertEqual(winners[0], row["user_id"])
        self.assertEqual("claimed", row["status"])
        self.assertIsNotNone(row["claimed_at"])
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])
        self.assertEqual("This prize was already claimed.", str(caught.exception))

    def test_claim_by_non_winner_refused(self) -> None:
        gw, winners, users = self._ended_with_claims()
        loser = next(user for user in users if user != winners[0])

        # The loser never drew, so there is no claim row for them at all.
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=loser)
        self.assertEqual("Only the drawn winner can claim this prize.", str(caught.exception))
        with self.assertRaises(ServiceError):
            self.h.call("claim", giveaway_id="missing", user_id=winners[0])

    def test_skip_claim_needs_staff_and_no_replacement(self) -> None:
        gw, winners, _ = self._ended_with_claims()

        with self.assertRaises(ServiceError) as caught:
            self.h.call("skip_claim", giveaway_id=gw["id"], user_id=winners[0], staff_id="")
        self.assertEqual("A staff member must verify the claim.", str(caught.exception))

        row = self.h.call("skip_claim", giveaway_id=gw["id"], user_id=winners[0], staff_id=STAFF)
        self.assertEqual("skipped", row["status"])
        self.assertEqual(STAFF, row["skipped_by"])
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))
        # No replacement is drawn: the winners list is untouched.
        self.assertEqual(winners, self.h.call("get", giveaway_id=gw["id"])["winners"])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("skip_claim", giveaway_id=gw["id"], user_id=winners[0], staff_id=STAFF)
        self.assertEqual("That claim was already verified by staff.", str(caught.exception))
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])
        self.assertEqual("This prize was already verified by staff.", str(caught.exception))

    def test_expire_draws_replacement_never_repeats(self) -> None:
        gw, winners, users = self._ended_with_claims()
        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])

        # Too early: the guarded UPDATE matches nothing.
        with self.assertRaises(ServiceError):
            self.h.call(
                "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline - 1
            )

        ended, replacements = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline + 1
        )

        self.assertEqual(1, len(replacements))
        self.assertIn(replacements[0], users)
        self.assertNotIn(replacements[0], winners)
        # The expired winner stays named; the replacement is added alongside.
        self.assertIn(winners[0], ended["winners"])
        self.assertIn(replacements[0], ended["winners"])

        statuses = {row["user_id"]: row["status"] for row in self.h.call(
            "claim_status", giveaway_id=gw["id"]
        )}
        self.assertEqual("expired", statuses[winners[0]])
        self.assertEqual("pending", statuses[replacements[0]])
        self.assertEqual([replacements[0]], [
            row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])
        ])

        # The expired slot can no longer be claimed, but its replacement can.
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])
        self.assertEqual("This claim window has closed.", str(caught.exception))
        self.h.call("claim", giveaway_id=gw["id"], user_id=replacements[0])
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))

    def test_expire_with_empty_pool_returns_no_replacement(self) -> None:
        gw, winners, _ = self._ended_with_claims(entrants=1)
        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])

        ended, replacements = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline + 1
        )

        self.assertEqual([], replacements)
        self.assertEqual(winners, ended["winners"])
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))

    def test_multi_winner_claims_are_independent(self) -> None:
        gw, winners, _ = self._ended_with_claims(entrants=4, winner_count=2)
        self.assertEqual(2, len(winners))

        self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])
        still = [row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])]
        self.assertEqual([winners[1]], still)

        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])
        ended, replacements = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[1], now=deadline + 1
        )
        self.assertEqual(1, len(replacements))
        self.assertNotIn(replacements[0], winners)
        self.assertEqual(3, len(ended["winners"]))

    def test_start_claims_is_idempotent(self) -> None:
        gw, winners, _ = self._ended_with_claims()
        before = self.h.call("pending_claims", giveaway_id=gw["id"])

        self.h.call("start_claims", gw=gw, winners=winners, now=self.h.now())

        after = self.h.call("pending_claims", giveaway_id=gw["id"])
        self.assertEqual(len(before), len(after))
        self.assertEqual(1, len(after))

    def test_manual_reroll_opens_claims_and_keeps_old_pending(self) -> None:
        gw, winners, _ = self._ended_with_claims()

        ended, fresh = self.h.call("reroll", giveaway_id=gw["id"], count=1)

        self.assertEqual(1, len(fresh))
        self.assertNotIn(fresh[0], winners)
        # Additive: both the old winner and the reroll winner stay named.
        self.assertIn(winners[0], ended["winners"])
        self.assertIn(fresh[0], ended["winners"])

        pending = {row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])}
        self.assertEqual({winners[0], fresh[0]}, pending)

        self.h.call("claim", giveaway_id=gw["id"], user_id=fresh[0])
        still = [row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])]
        self.assertEqual([winners[0]], still)

    def test_manual_reroll_never_repeats_claim_draws(self) -> None:
        gw, winners, users = self._ended_with_claims()
        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])

        _redrawn, auto = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline + 1
        )
        self.assertEqual(1, len(auto))

        _rerolled, fresh = self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual(1, len(fresh))
        # Neither the expired winner nor the auto replacement may reappear.
        self.assertNotIn(fresh[0], [winners[0], auto[0]])
        self.assertIn(fresh[0], users)
        third = next(user for user in users if user not in (winners[0], auto[0]))
        self.assertEqual(third, fresh[0])

        # The pool is now exhausted: one more reroll must refuse.
        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("No other entrants left to reroll", str(caught.exception))

    def test_targeted_reroll_replaces_one_slot_in_place(self) -> None:
        gw, winners, users = self._ended_with_claims(entrants=4, winner_count=3)
        target = winners[0]
        expected = next(user for user in users if user not in winners)

        ended, replacements = self.h.call(
            "reroll", giveaway_id=gw["id"], count=1, replace_user_id=target
        )

        self.assertEqual([expected], replacements)
        # Same length, order preserved: only the picked slot swaps.
        self.assertEqual(3, len(ended["winners"]))
        self.assertEqual(replacements[0], ended["winners"][0])
        self.assertEqual(winners[1:], ended["winners"][1:])
        self.assertNotIn(target, ended["winners"])

        statuses = {row["user_id"]: row["status"] for row in self.h.call(
            "claim_status", giveaway_id=gw["id"]
        )}
        self.assertEqual("replaced", statuses[target])
        self.assertEqual("pending", statuses[replacements[0]])
        pending = {row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])}
        self.assertEqual(set(ended["winners"]), pending)

        # The fresh winner can press Claim Prize; the old one gets a clear no.
        self.h.call("claim", giveaway_id=gw["id"], user_id=replacements[0])
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=target)
        self.assertEqual("This winner was replaced by a reroll.", str(caught.exception))

    def test_targeted_reroll_rejects_non_winner_and_exhausted_pool(self) -> None:
        gw, winners, _ = self._ended_with_claims(entrants=4, winner_count=3)

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], replace_user_id="899999999999999999")
        self.assertEqual("That member is not a winner of this giveaway.", str(caught.exception))

        # One swap exhausts the 4-entrant pool: every user is now ever-drawn.
        self.h.call("reroll", giveaway_id=gw["id"], count=1, replace_user_id=winners[0])
        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1, replace_user_id=winners[1])
        self.assertEqual("No other entrants left to reroll", str(caught.exception))
        with self.assertRaises(ServiceError):
            self.h.call("reroll", giveaway_id=gw["id"], count=1)

    def test_targeted_reroll_without_claims_still_excludes(self) -> None:
        gw = self.gw(claim_timeout_seconds=0)
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        _, winners = self.end(gw)

        ended, replacements = self.h.call(
            "reroll", giveaway_id=gw["id"], replace_user_id=winners[0]
        )
        self.assertEqual([replacements[0]], ended["winners"])

        # The swapped-out winner has no claim row, so a marker must keep them
        # out of every later draw.
        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("No other entrants left to reroll", str(caught.exception))


class ResolveEndedTests(HarnessTestCase):
    def test_resolve_ended_returns_ended_by_id(self) -> None:
        gw = self.gw()
        self.end(gw)
        self.assertEqual(gw["id"], self.h.call(
            "resolve_ended", guild_id=self.guild, raw_id=gw["id"]
        )["id"])

    def test_resolve_ended_refuses_live(self) -> None:
        gw = self.gw()
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve_ended", guild_id=self.guild, raw_id=gw["id"])
        self.assertEqual("That giveaway has not ended yet.", str(caught.exception))

    def test_resolve_ended_never_falls_back_to_live(self) -> None:
        self.gw()
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve_ended", guild_id=self.guild, raw_id="")
        self.assertEqual("No ended giveaway in this server.", str(caught.exception))

    def test_resolve_ended_single_fallback(self) -> None:
        gw = self.gw()
        self.end(gw)
        self.assertEqual(gw["id"], self.h.call(
            "resolve_ended", guild_id=self.guild, raw_id="typo"
        )["id"])

    def test_resolve_ended_refuses_another_guild(self) -> None:
        gw = self.gw(guild_id=self.other_guild)
        self.end(gw)
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve_ended", guild_id=self.guild, raw_id=gw["id"])
        self.assertEqual("That giveaway belongs to another server.", str(caught.exception))

    def test_resolve_ended_with_nothing_ended(self) -> None:
        gw = self.gw()
        self.h.call("cancel", giveaway_id=gw["id"])
        with self.assertRaises(ServiceError) as caught:
            self.h.call("resolve_ended", guild_id=self.guild, raw_id=gw["id"])
        self.assertEqual("That giveaway has not ended yet.", str(caught.exception))

    def test_list_ended_is_guild_scoped(self) -> None:
        first = self.gw()
        self.end(first)
        self.h.advance(10)
        second = self.gw()
        self.end(second)
        foreign = self.gw(guild_id=self.other_guild)
        self.end(foreign)

        ids = [gw["id"] for gw in self.h.call("list_ended", guild_id=self.guild, limit=25)]
        self.assertEqual([second["id"], first["id"]], ids)
        self.assertEqual([foreign["id"]], [gw["id"] for gw in self.h.call(
            "list_ended", guild_id=self.other_guild, limit=25
        )])


class RerollCasTests(HarnessTestCase):
    def test_read_winners_raw_round_trip(self) -> None:
        gw = self.gw(winner_count=2)
        for index in range(3):
            self.enter(gw, f"80000000000000{index:04d}")
        _, winners = self.end(gw)

        stored = self.h.scalar("SELECT winners_json FROM simple_giveaways WHERE id = ?", [gw["id"]])
        # The draw is persisted as JSON; reroll reads it back to exclude winners.
        self.assertEqual(winners, json.loads(stored))


class ClaimAtomicityTests(HarnessTestCase):
    def _block_claim_inserts(self) -> None:
        self.trigger(
            "CREATE TRIGGER no_claims BEFORE INSERT ON simple_claims "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )

    def test_end_insert_crash_still_announces_and_heals(self) -> None:
        gw = self.gw(claim_timeout_seconds=300)
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        self._block_claim_inserts()

        ended, winners = self.end(gw)

        # The draw is stored before the inserts, so the announcement still works.
        self.assertEqual("ended", ended["status"])
        self.assertEqual(1, len(winners))
        self.assertIn(winners[0], {USER, OTHER})
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))

        # The next read of the claim window repairs the stranded draw.
        self.drop_trigger("no_claims")
        self.assertEqual([winners[0]], [
            row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])
        ])

    def test_stranded_draw_healed_by_pending_and_claim_retry(self) -> None:
        gw = self.gw(claim_timeout_seconds=300)
        self.enter(gw, USER)
        _, winners = self.end(gw)
        self.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", [gw["id"]])

        row = self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])

        self.assertEqual("claimed", row["status"])

    def test_stranded_draw_healed_by_tick_sweep(self) -> None:
        gw = self.gw(claim_timeout_seconds=300)
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        _, winners = self.end(gw)
        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])
        self.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", [gw["id"]])

        # due_claims heals stranded windows before it answers "nothing due".
        due = self.h.call("due_claims", now=deadline + 1)

        self.assertEqual(winners, [row["user_id"] for row in due])
        self.assertEqual(winners, [
            row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])
        ])

    def test_repair_is_idempotent_and_settles_stale_pending(self) -> None:
        gw = self.gw(claim_timeout_seconds=300)
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        self.end(gw)
        self.execute(
            "INSERT INTO simple_claims (giveaway_id, user_id, round, status, deadline_ms, created_at)"
            " VALUES (?, ?, 0, 'pending', 0, 0)",
            [gw["id"], "899999999999999999"],
        )

        self.assertEqual(1, self.h.call("repair_missing_claims", giveaway_id=gw["id"]))
        self.assertEqual(0, self.h.call("repair_missing_claims", giveaway_id=gw["id"]))
        statuses = {row["user_id"]: row["status"] for row in self.h.call(
            "claim_status", giveaway_id=gw["id"]
        )}
        self.assertEqual("replaced", statuses["899999999999999999"])

    def test_reroll_insert_crash_heals_fresh_window(self) -> None:
        gw = self.gw(claim_timeout_seconds=300)
        for index in range(3):
            self.enter(gw, f"80000000000000{index:04d}")
        self._block_claim_inserts()
        self.end(gw)

        self.drop_trigger("no_claims")
        ended, fresh = self.h.call("reroll", giveaway_id=gw["id"], count=1)

        self.assertEqual(1, len(fresh))
        self.assertEqual(2, len(ended["winners"]))
        pending = self.h.call("pending_claims", giveaway_id=gw["id"])
        self.assertEqual(2, len(pending))
        self.assertEqual(set(ended["winners"]), {row["user_id"] for row in pending})

    def test_expire_insert_crash_heals_replacement(self) -> None:
        gw = self.gw(claim_timeout_seconds=60)
        for index in range(3):
            self.enter(gw, f"80000000000000{index:04d}")
        _, winners = self.end(gw)
        self.h.advance(61_000)
        self._block_claim_inserts()

        _, replacements = self.h.call("expire_claim", giveaway_id=gw["id"], user_id=winners[0])
        self.assertEqual(1, len(replacements))

        self.drop_trigger("no_claims")
        pending = self.h.call("pending_claims", giveaway_id=gw["id"])
        self.assertEqual([replacements[0]], [row["user_id"] for row in pending])


class TargetedSettledGuardTests(HarnessTestCase):
    def _draw(self, entrants: int = 3, **overrides: Any) -> tuple[dict, list[str]]:
        gw = self.gw(claim_timeout_seconds=300, **overrides)
        for index in range(entrants):
            self.enter(gw, f"90000000000000{index:04d}")
        return gw, list(self.end(gw)[1])

    def test_targeted_replace_refuses_claimed(self) -> None:
        gw, winners = self._draw()
        self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], replace_user_id=winners[0])
        self.assertEqual("That prize was already claimed.", str(caught.exception))

    def test_targeted_replace_refuses_skipped(self) -> None:
        gw, winners = self._draw()
        self.h.call("skip_claim", giveaway_id=gw["id"], user_id=winners[0], staff_id=STAFF)

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], replace_user_id=winners[0])
        self.assertEqual("That claim was already verified by staff.", str(caught.exception))

    def test_targeted_replace_refuses_expired(self) -> None:
        gw = self.gw(claim_timeout_seconds=60)
        for index in range(3):
            self.enter(gw, f"90000000000000{index:04d}")
        _, winners = self.end(gw)
        self.h.advance(61_000)
        self.h.call("expire_claim", giveaway_id=gw["id"], user_id=winners[0])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], replace_user_id=winners[0])
        self.assertEqual("That winner was already replaced.", str(caught.exception))

    def test_second_replace_of_same_slot_refused(self) -> None:
        gw, winners = self._draw()
        self.h.call("reroll", giveaway_id=gw["id"], replace_user_id=winners[0])

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], replace_user_id=winners[0])
        self.assertIn(
            str(caught.exception),
            {
                "That member is not a winner of this giveaway.",
                "That winner was already replaced.",
            },
        )


class GuildSettingTests(HarnessTestCase):
    def test_notify_role_upsert(self) -> None:
        self.assertIsNone(self.h.call("get_notify_role", guild_id=self.guild))

        self.h.call("set_notify_role", guild_id=self.guild, role_id="r1")
        self.assertEqual("r1", self.h.call("get_notify_role", guild_id=self.guild))

        self.h.call("set_notify_role", guild_id=self.guild, role_id="r2")
        self.assertEqual("r2", self.h.call("get_notify_role", guild_id=self.guild))

        self.h.call("set_notify_role", guild_id=self.guild, role_id=None)
        self.assertIsNone(self.h.call("get_notify_role", guild_id=self.guild))


class EligibilitySnapshotTests(HarnessTestCase):
    def test_snapshot_returns_all_four_fields(self) -> None:
        gw = self.gw()
        for _ in range(4):
            self.h.call("record_message", guild_id=self.guild, user_id=USER)
        self.h.call(
            "apply_timeout_penalty",
            guild_id=self.guild,
            user_id=USER,
            giveaways=2,
            giveaway_id=gw["id"],
        )

        self.assertEqual([False, 2, 4, True], self.h.call(
            "eligibility_snapshot", guild_id=self.guild, user_id=USER, giveaway_id=gw["id"]
        ))

    def test_snapshot_sees_blacklist_ban_and_hit(self) -> None:
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        self.h.call(
            "apply_timeout_penalty",
            guild_id=self.guild,
            user_id=USER,
            giveaways=1,
            giveaway_id=gw["id"],
        )

        banned, remaining, messages, hit = self.h.call(
            "eligibility_snapshot", guild_id=self.guild, user_id=USER, giveaway_id=gw["id"]
        )
        self.assertTrue(banned)
        self.assertEqual(1, remaining)
        self.assertEqual(0, messages)
        self.assertTrue(hit)

    def test_snapshot_empty_ids_fail_closed(self) -> None:
        self.assertEqual([False, 0, 0, False], self.h.call(
            "eligibility_snapshot", guild_id="", user_id="", giveaway_id=None
        ))

    def test_snapshot_none_on_db_failure_forces_fallback(self) -> None:
        # A missing snapshot is how a failed snapshot read is reported, and the
        # live checks must still refuse. Omitting the argument is the same path.
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        self.assertIn("blocked", str(self.join_error(gw, USER)))

    def test_join_with_snapshot_honors_live_block(self) -> None:
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        self.assertIn("blocked", str(self.join_error(gw, USER, snapshot=[True, 0, 0, False])))

    def test_stale_snapshot_block_does_not_falsely_refuse(self) -> None:
        gw = self.gw()
        # The snapshot predates a blacklist_remove: confirming live is what stops
        # an unbanned member from being refused for another minute.
        self.assertEqual(1, self.enter(gw, USER, snapshot=[True, 0, 0, False]))

    def test_join_with_snapshot_counts_buffered_messages(self) -> None:
        gw = self.gw(min_messages=5)
        self.assertEqual(1, self.enter(gw, USER, snapshot=[False, 0, 3, False], pending_messages=2))

    def test_get_status_is_status_only(self) -> None:
        gw = self.gw()
        self.assertEqual("active", self.h.call("get_status", giveaway_id=gw["id"]))
        self.end(gw)
        self.assertEqual("ended", self.h.call("get_status", giveaway_id=gw["id"]))
        self.assertIsNone(self.h.call("get_status", giveaway_id="missing"))


class AuditRaceTests(HarnessTestCase):
    def test_insert_guard_blocks_racing_blacklist(self) -> None:
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        self.assertIn("blocked", str(self.join_error(gw, USER, snapshot=[False, 0, 0, False])))

    def test_insert_guard_blocks_racing_deadline(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.end(gw)
        # ``gw`` is the stale pre-end copy the button handler still holds. Every
        # pre-check passes; only the INSERT guard can refuse it.
        self.assertEqual("This giveaway has ended.", str(self.join_error(gw, OTHER)))

    def test_insert_guard_blocks_penalty_race_and_consumes(self) -> None:
        gw = self.gw()
        self.h.call(
            "apply_timeout_penalty",
            guild_id=self.guild,
            user_id=USER,
            giveaways=3,
            # The penalty was already spent on another giveaway, so the join
            # below must consume a unit here (3 -> 2) instead of being a no-op.
            giveaway_id="some-other-giveaway",
        )
        error = self.join_error(gw, USER, snapshot=[False, 0, 0, False])

        self.assertEqual("timeout_ban", error.kind)
        self.assertIn("2 giveaway(s) left", str(error))
        self.assertEqual(2, self.h.call("timeout_ban_remaining", guild_id=self.guild, user_id=USER))

    def test_insert_guard_blocks_ban_hit_race(self) -> None:
        trigger = self.gw()
        self.timed_join_error(trigger, USER)  # starts a 3-giveaway penalty
        for _ in range(3):
            self.timed_join_error(self.gw(), USER)
        self.assertEqual(0, self.h.call("timeout_ban_remaining", guild_id=self.guild, user_id=USER))

        error = self.join_error(trigger, USER, snapshot=[False, 0, 0, False])
        self.assertEqual("timeout_ban", error.kind)
        self.assertIn("sat out this giveaway", str(error))

    def test_stale_ban_snapshot_does_not_falsely_penalize(self) -> None:
        gw = self.gw()
        # Snapshot claims a 3-giveaway penalty that has already been lifted.
        self.assertEqual(1, self.enter(gw, USER, snapshot=[False, 3, 0, False]))

    def test_stale_hit_snapshot_does_not_falsely_refuse(self) -> None:
        gw = self.gw()
        self.assertEqual(1, self.enter(gw, USER, snapshot=[False, 0, 0, True]))

    def test_stale_message_snapshot_live_confirm_allows(self) -> None:
        gw = self.gw(min_messages=5)
        self.h.call("add_message_counts", rows=[[self.guild, USER, 6]])
        # The snapshot saw 1; a re-read before refusing sees the flush.
        self.assertEqual(1, self.enter(gw, USER, snapshot=[False, 0, 1, False]))

    def test_stale_ended_never_penalizes_timed_out(self) -> None:
        gw = self.gw()
        self.end(gw)
        self.assertEqual("This giveaway has ended.", str(self.timed_join_error(gw, USER)))
        self.assertEqual(0, self.h.call("timeout_ban_remaining", guild_id=self.guild, user_id=USER))


class TimedBanTests(HarnessTestCase):
    def test_default_add_is_permanent(self) -> None:
        info = self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        self.assertTrue(info["permanent"])
        self.assertIsNone(info["expires_at"])
        self.assertIn("This ban is permanent.", self.h.call(
            "ban_message", guild_id=self.guild, user_id=USER
        ))

    def test_timed_ban_blocks_then_expires(self) -> None:
        base = self.h.now()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER, hours=1, now=base)
        self.assertTrue(self.h.call("is_blacklisted", guild_id=self.guild, user_id=USER))

        self.h.advance(3600 * 1000 + 1)

        # Created after the jump so the giveaway itself is still running.
        gw = self.gw()
        self.assertFalse(self.h.call("is_blacklisted", guild_id=self.guild, user_id=USER))
        self.assertEqual(1, self.enter(gw, USER))

    def test_hours_validation(self) -> None:
        for hours in (-1, 721):
            with self.subTest(hours=hours):
                with self.assertRaises(ServiceError) as caught:
                    self.h.call("blacklist_add", guild_id=self.guild, user_id=USER, hours=hours)
                self.assertEqual(
                    "Ban duration must be 0-720 hours (0 = permanent).", str(caught.exception)
                )

    def test_reban_overwrites_expiry_and_reason(self) -> None:
        first = self.h.call(
            "blacklist_add", guild_id=self.guild, user_id=USER, hours=1, reason="first"
        )
        second = self.h.call(
            "blacklist_add", guild_id=self.guild, user_id=USER, hours=2, reason="second"
        )

        self.assertEqual("second", second["reason"])
        self.assertGreater(second["expires_at"], first["expires_at"])
        self.assertEqual(1, self.h.call("count_blacklisted", guild_id=self.guild))

    def test_reason_trimmed_to_256(self) -> None:
        reason = "  " + "x" * 300 + "  "
        info = self.h.call(
            "blacklist_add", guild_id=self.guild, user_id=USER, reason=reason, hours=0
        )
        self.assertEqual(256, len(info["reason"]))
        self.assertFalse(info["reason"].startswith(" "))

    def test_ban_message_shows_reason_and_expiry(self) -> None:
        self.h.call(
            "blacklist_add",
            guild_id=self.guild,
            user_id=USER,
            hours=1,
            reason="spam",
            banned_by=STAFF,
        )
        message = self.h.call("ban_message", guild_id=self.guild, user_id=USER)

        self.assertIn("blocked", message)
        self.assertIn("Reason: spam", message)
        self.assertIn("Ban expires <t:", message)
        self.assertNotIn("permanent", message)

    def test_timed_ban_purges_running_entries(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER, hours=1)

        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"]))

    def test_count_and_list_hide_expired(self) -> None:
        self.h.call("blacklist_add", guild_id=self.guild, user_id="a", hours=0)
        self.h.call("blacklist_add", guild_id=self.guild, user_id="b", hours=1)
        self.h.call("blacklist_add", guild_id=self.guild, user_id="c", hours=1)

        self.h.advance(3600 * 1000 + 1)

        self.assertEqual(1, self.h.call("count_blacklisted", guild_id=self.guild))
        self.assertEqual(["a"], self.h.call("blacklist_list", guild_id=self.guild, limit=50))
        self.assertEqual(["a"], [row["user_id"] for row in self.h.call(
            "blacklist_list_detailed", guild_id=self.guild, limit=50
        )])

    def test_purge_expired_bans(self) -> None:
        self.h.call("blacklist_add", guild_id=self.guild, user_id="a", hours=0)
        self.h.call("blacklist_add", guild_id=self.guild, user_id="b", hours=1)
        self.h.advance(3600 * 1000 + 1)

        self.assertEqual(1, self.h.call("purge_expired_bans"))
        self.assertEqual(["a"], self.h.call("blacklist_list", guild_id=self.guild, limit=50))

    def test_snapshot_is_expiry_aware(self) -> None:
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER, hours=1)
        self.assertTrue(self.h.call(
            "eligibility_snapshot", guild_id=self.guild, user_id=USER, giveaway_id=None
        )[0])

        self.h.advance(3600 * 1000 + 1)

        self.assertFalse(self.h.call(
            "eligibility_snapshot", guild_id=self.guild, user_id=USER, giveaway_id=None
        )[0])

    def test_insert_guard_blocks_racing_timed_ban(self) -> None:
        gw = self.gw()
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER, hours=1)
        self.assertIn("blocked", str(self.join_error(gw, USER, snapshot=[False, 0, 0, False])))

    def test_zero_insert_reports_ban_with_reason(self) -> None:
        gw = self.gw()
        self.h.call(
            "blacklist_add", guild_id=self.guild, user_id=USER, hours=1, reason="spam"
        )
        message = str(self.join_error(gw, USER, snapshot=[False, 0, 0, False]))
        self.assertIn("blocked", message)
        self.assertIn("Reason: spam", message)


class DueExtendRaceTests(HarnessTestCase):
    def test_stale_due_loses_to_extend(self) -> None:
        gw = self.gw(duration_seconds=60)
        now = self.h.advance(61_000)
        stale = self.h.call("due", now=now)
        self.assertEqual([gw["id"]], [row["id"] for row in stale])

        self.h.call("extend", giveaway_id=gw["id"], extra_seconds=3600)

        with self.assertRaises(ServiceError) as caught:
            self.end(stale[0], now=now, require_due=True)
        self.assertEqual("This giveaway is not due to end yet.", str(caught.exception))
        self.assertEqual("active", self.h.call("get", giveaway_id=gw["id"])["status"])

    def test_atomic_flip_rechecks_deadline(self) -> None:
        gw = self.gw(duration_seconds=3600)
        stale = dict(gw)
        stale["ends_at"] = 0  # what a very old cached row would claim
        with self.assertRaises(ServiceError) as caught:
            self.end(stale, now=self.h.now(), require_due=True)
        self.assertEqual("This giveaway is not due to end yet.", str(caught.exception))

    def test_tick_end_still_wins_when_still_due(self) -> None:
        gw = self.gw(duration_seconds=60)
        self.enter(gw, USER)
        now = self.h.advance(61_000)

        ended, winners = self.end(gw, now=now, require_due=True)

        self.assertEqual("ended", ended["status"])
        self.assertEqual([USER], winners)

    def test_manual_end_keeps_early_end(self) -> None:
        gw = self.gw(duration_seconds=3600)
        ended, _ = self.end(gw)
        self.assertEqual("ended", ended["status"])
        self.assertLess(self.h.now(), gw["ends_at"])


class JoinBurstTests(HarnessTestCase):
    def test_fetch_count_false_skips_count_but_commits(self) -> None:
        gw = self.gw()
        result = self.h.call(
            "join",
            gw=gw,
            user_id=USER,
            username="u",
            member_roles=[],
            account_created_ts=None,
            fetch_count=False,
        )
        # -1 means "committed, count unknown": callers must not print it.
        self.assertEqual(-1, result)
        self.assertTrue(self.h.call("has_entry", giveaway_id=gw["id"], user_id=USER))
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"]))

    def test_join_snapshot_matches_split_reads(self) -> None:
        gw = self.gw()
        self.h.call("record_message", guild_id=self.guild, user_id=USER)

        snapshot_gw, snapshot = self.h.call(
            "join_snapshot", giveaway_id=gw["id"], guild_id=self.guild, user_id=USER
        )

        self.assertEqual(gw["id"], snapshot_gw["id"])
        self.assertEqual(
            self.h.call(
                "eligibility_snapshot", guild_id=self.guild, user_id=USER, giveaway_id=gw["id"]
            ),
            snapshot,
        )

    def test_refresh_snapshot_returns_status_and_count(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        self.assertEqual(["active", 2], self.h.call("refresh_snapshot", giveaway_id=gw["id"]))

        self.end(gw)
        self.assertEqual(["ended", 2], self.h.call("refresh_snapshot", giveaway_id=gw["id"]))


if __name__ == "__main__":  # pragma: no cover - convenience for a single module
    unittest.main()
