"""Failure recovery: half-applied writes, duplicate clicks, races and penalties.

Every test starts from a fresh database and drives the real Rust service through
``gwbot-harness``. The point is narrower than "the happy path works":

* a flush that only half landed must be recoverable without double counting;
* a duplicate click (join, leave, end) must be refused, not applied twice;
* a race must leave exactly one durable outcome;
* the sweeps that run unattended must never eat the blacklist or a penalty.

Nothing here re-implements a rule in Python: assertions are about what the
engine stored, returned or refused. Time only moves through the fake clock, and
the cross-process races use the ``run_concurrently`` barrier from the base
fixture, because one process cannot lose its own write.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from tests.harness import HarnessTestCase, ServiceError

USER = "100000000000000010"
OTHER = "100000000000000011"
STAFF = "100000000000000012"

#: Mirrors ``MESSAGE_FLUSH_CHUNK`` in gwbot-core; a chunk boundary is 300 rows.
FLUSH_CHUNK = 300

#: Mirrors ``TIMEOUT_PENALTY``: giveaways a timed-out member sits out.
PENALTY = 3

#: Mirrors ``ENTRY_RETENTION_MS``: entry rows expire five hours after the end.
RETENTION_MS = 5 * 60 * 60 * 1000


def _ids(prefix: str, count: int) -> list[str]:
    """Deterministic, unique user ids that never collide across tests."""
    return [f"{prefix}{index:013d}" for index in range(count)]


class PartialFlushTests(HarnessTestCase):
    """``add_message_counts`` is chunked; the receipt must describe the chunks."""

    def test_chunk_boundaries_report_applied_and_before(self) -> None:
        # 299/300/301 straddle the chunk size: the receipt counts rows applied
        # (not statements issued) and distinct keys snapshotted, both equal here.
        for size in (FLUSH_CHUNK - 1, FLUSH_CHUNK, FLUSH_CHUNK + 1):
            with self.subTest(size=size):
                guild = f"{self.guild}-{size}"
                rows = [[guild, f"31{index:014d}", 1] for index in range(size)]

                receipt = self.h.call("add_message_counts", rows=rows)

                self.assertEqual({"applied": size, "before": size}, receipt)
                self.assertEqual(size, int(self.h.scalar(
                    "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ?", [guild]
                )))
                self.assertEqual(size, int(self.h.scalar(
                    "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?", [guild]
                )))

    def test_before_counts_distinct_keys_not_rows(self) -> None:
        # Two rows for one key and one for another: three rows, two keys. The
        # receipt says two, and the deltas add up (2 + 3 = 5, never 3).
        receipt = self.h.call(
            "add_message_counts",
            rows=[[self.guild, USER, 2], [self.guild, USER, 3], [self.other_guild, USER, 4]],
        )
        self.assertEqual({"applied": 3, "before": 2}, receipt)
        self.assertEqual(5, self.h.call("message_count", guild_id=self.guild, user_id=USER))
        self.assertEqual(4, self.h.call(
            "message_count", guild_id=self.other_guild, user_id=USER
        ))

        # A later flush only adds its own delta: counts are monotonic and are
        # never inflated by re-reading the same key twice.
        self.assertEqual(
            {"applied": 1, "before": 1},
            self.h.call("add_message_counts", rows=[[self.guild, USER, 5]]),
        )
        self.assertEqual(10, self.h.call("message_count", guild_id=self.guild, user_id=USER))

    def test_a_partial_flush_replays_only_the_missing_tail(self) -> None:
        users = _ids("41", 400)
        batch = [[self.guild, user, 1] for user in users]

        # Simulate a flush whose first chunk committed and whose second chunk
        # never ran: exactly what a PartialFlush leaves behind.
        head = self.h.call("add_message_counts", rows=batch[:FLUSH_CHUNK])
        self.assertEqual({"applied": FLUSH_CHUNK, "before": FLUSH_CHUNK}, head)

        # The caller reconciles the *whole* batch against the pre-flush
        # snapshot (every key was 0). Only the rows still missing come back.
        shortfall = self.h.call(
            "reconcile_message_counts",
            rows=batch,
            before=[[self.guild, user, 0] for user in users],
        )
        self.assertEqual([[self.guild, user, 1] for user in users[FLUSH_CHUNK:]], shortfall)

        replay = self.h.call("add_message_counts", rows=shortfall)
        self.assertEqual(
            {"applied": len(users) - FLUSH_CHUNK, "before": len(users) - FLUSH_CHUNK},
            replay,
        )

        # Every message counted exactly once: no loss, no double count.
        self.assertEqual(len(users), int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ?", [self.guild]
        )))
        self.assertEqual(len(users), int(self.h.scalar(
            "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?", [self.guild]
        )))
        self.assertEqual(0, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ? AND count > 1",
            [self.guild],
        )))

        # Reconciling the finished batch again finds nothing left to send.
        self.assertEqual([], self.h.call(
            "reconcile_message_counts",
            rows=batch,
            before=[[self.guild, user, 0] for user in users],
        ))


class MessageCountReconciliationTests(HarnessTestCase):
    """The timed-out-flush path: a worker may have committed more than it said."""

    def _snapshot(self, users: list[str]) -> list[list[Any]]:
        rows = self.h.sql(
            "SELECT user_id, count FROM simple_message_counts WHERE guild_id = ?",
            [self.guild],
        )
        by_user = {row["user_id"]: int(row["count"]) for row in rows}
        return [[self.guild, user, by_user.get(user, 0)] for user in users]

    def test_a_finished_worker_leaves_no_tail_to_requeue(self) -> None:
        users = _ids("42", 5)
        self.h.call(
            "add_message_counts",
            rows=[[self.guild, user, index + 1] for index, user in enumerate(users)],
        )
        before = self._snapshot(users)
        self.assertEqual([1, 2, 3, 4, 5], [row[2] for row in before])

        # The abandoned worker went on to commit the whole batch (+2 each).
        batch = [[self.guild, user, 2] for user in users]
        self.h.call("add_message_counts", rows=batch)

        # Reconciling against the snapshot it captured must find nothing: the
        # caller hands back an empty tail instead of re-sending the batch.
        self.assertEqual([], self.h.call(
            "reconcile_message_counts", rows=batch, before=before
        ))
        self.assertEqual([3, 4, 5, 6, 7], [
            self.h.call("message_count", guild_id=self.guild, user_id=user)
            for user in users
        ])

    def test_only_the_shortfall_of_each_key_is_requeued(self) -> None:
        users = _ids("43", 3)
        full, partial, untouched = users
        self.h.call(
            "add_message_counts",
            rows=[[self.guild, user, 1] for user in users],
        )
        before = self._snapshot(users)

        # Each key is due +4. One landed completely, one landed half way, one
        # never landed at all -- the three shapes a lost response can take.
        batch = [[self.guild, user, 4] for user in users]
        self.h.call("add_message_counts", rows=[[self.guild, full, 4]])
        self.execute(
            "UPDATE simple_message_counts SET count = count + 2 "
            "WHERE guild_id = ? AND user_id = ?",
            [self.guild, partial],
        )

        shortfall = self.h.call("reconcile_message_counts", rows=batch, before=before)

        # full: 1 + 4 - 5 = 0; partial: 1 + 4 - 3 = 2; untouched: 1 + 4 - 1 = 4.
        self.assertEqual(
            [[self.guild, partial, 2], [self.guild, untouched, 4]], shortfall
        )
        self.h.call("add_message_counts", rows=shortfall)

        # Every key ends at before + delta, exactly once.
        self.assertEqual([5, 5, 5], [
            self.h.call("message_count", guild_id=self.guild, user_id=user)
            for user in users
        ])
        self.assertEqual(15, int(self.h.scalar(
            "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?", [self.guild]
        )))


class IdempotentJoinTests(HarnessTestCase):
    def test_double_tap_joins_once(self) -> None:
        gw = self.gw()

        self.assertEqual(1, self.enter(gw, USER))
        entered_at = self.h.scalar(
            "SELECT entered_at FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
            [gw["id"], USER],
        )

        # The second tap is refused by the duplicate-entry guard, not silently
        # merged into a second row or a second counter bump.
        self.assertEqual("You are already entered.", str(self.join_error(gw, USER)))

        self.assertEqual(1, self.entry_rows(gw["id"]))
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"]))
        self.assertEqual(1, int(self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )))
        self.assertEqual(entered_at, self.h.scalar(
            "SELECT entered_at FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
            [gw["id"], USER],
        ))
        self.assertEqual(1, len(self.h.call("entries", giveaway_id=gw["id"])))

    def test_a_different_member_is_unaffected_by_the_refusal(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.join_error(gw, USER)

        # A refusal for one member must not poison the next one's entry.
        self.assertEqual(2, self.enter(gw, OTHER))
        self.assertEqual(2, self.entry_rows(gw["id"]))


class DuplicateLeaveTests(HarnessTestCase):
    def test_leaving_twice_removes_once(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)

        self.assertTrue(self.h.call("leave", giveaway_id=gw["id"], user_id=USER))
        # "not entered" is the same answer the button gives a member who never
        # joined: a second leave is a no-op, not an error and not a second bump.
        self.assertFalse(self.h.call("leave", giveaway_id=gw["id"], user_id=USER))
        self.assertEqual(
            [False, 0, ""],
            self.h.call("leave_with_count", giveaway_id=gw["id"], user_id=USER),
        )

        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"]))
        self.assertFalse(self.h.call("has_entry", giveaway_id=gw["id"], user_id=USER))

    def test_a_repeated_leave_cannot_underflow_the_counter(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.enter(gw, OTHER)

        self.assertTrue(self.h.call("leave", giveaway_id=gw["id"], user_id=USER))
        for _ in range(3):
            self.assertFalse(self.h.call("leave", giveaway_id=gw["id"], user_id=USER))

        # GREATEST(0, ...) in the DELETE path: the counter is not a signed
        # ledger, so a hammered Leave button cannot drive it below zero.
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"]))
        self.assertEqual(1, int(self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )))
        self.assertTrue(self.h.call("has_entry", giveaway_id=gw["id"], user_id=OTHER))


class EndRerollRaceTests(HarnessTestCase):
    def test_two_ends_transition_once(self) -> None:
        gw = self.gw(duration_seconds=60)
        entrants = _ids("51", 3)
        for member in entrants:
            self.enter(gw, member)
        now = self.h.advance(61_000)
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: h.call(
                "end", giveaway_id=gw["id"], now=now, require_due=True
            )),
            (second, lambda h: h.call(
                "end", giveaway_id=gw["id"], now=now, require_due=True
            )),
        ])

        # One guarded UPDATE: exactly one caller stores a draw, the other is
        # told the giveaway is already over.
        self.assertEqual(["ok", "refused"], sorted(status for status, _ in outcomes))
        by_status = dict(outcomes)
        ended, winners = by_status["ok"]
        self.assertEqual("This giveaway has already ended.", str(by_status["refused"]))
        self.assertEqual("ended", ended["status"])
        self.assertEqual(1, len(winners))
        self.assertIn(winners[0], entrants)

        stored = self.h.call("get", giveaway_id=gw["id"])
        self.assertEqual("ended", stored["status"])
        self.assertEqual(winners, stored["winners"])
        self.assertEqual(3, stored["entrant_count"])
        raw = self.h.scalar(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )
        self.assertEqual(winners, json.loads(raw))
        self.assertEqual(now, int(self.h.scalar(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )))

    def test_end_after_end_keeps_the_first_draw(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        ended, winners = self.end(gw)
        raw_before = self.h.scalar(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )
        ended_at = self.h.scalar(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )

        for _ in range(2):
            with self.assertRaises(ServiceError) as caught:
                self.end(gw)
            self.assertEqual("This giveaway has already ended.", str(caught.exception))

        # The loser of the race must not rewrite the winner set it lost to.
        self.assertEqual(winners, ended["winners"])
        self.assertEqual(raw_before, self.h.scalar(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))
        self.assertEqual(ended_at, self.h.scalar(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))

    def test_reroll_racing_end_never_duplicates_a_winner(self) -> None:
        gw = self.gw(winner_count=1)
        entrants = set(_ids("52", 4))
        for member in entrants:
            self.enter(gw, member)
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: h.call("end", giveaway_id=gw["id"])),
            (second, lambda h: h.call("reroll", giveaway_id=gw["id"], count=1)),
        ])

        # end always wins its own guarded flip; the reroll either read the row
        # while it was still active (refused) or rebased onto the stored draw.
        self.assertEqual("ok", outcomes[0][0])
        _ended, end_winners = outcomes[0][1]
        reroll_status, reroll_value = outcomes[1]
        self.assertIn(reroll_status, {"ok", "refused"})
        if reroll_status == "refused":
            self.assertIn(
                str(reroll_value),
                {
                    "End the giveaway before rerolling.",
                    "Reroll collided with another draw. Try again.",
                },
            )
            fresh: list[str] = []
        else:
            fresh = list(reroll_value[1])

        stored = self.h.call("get", giveaway_id=gw["id"])["winners"]
        self.assertEqual(len(set(stored)), len(stored), "a winner was named twice")
        self.assertTrue(set(end_winners) <= set(stored))
        self.assertTrue(set(fresh) <= set(stored))
        self.assertLessEqual(len(stored), 2)
        self.assertEqual("ended", self.h.call("get_status", giveaway_id=gw["id"]))
        self.assertEqual(4, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))


class ExtendVsEndTests(HarnessTestCase):
    def test_a_stale_due_snapshot_loses_to_extend(self) -> None:
        gw = self.gw(duration_seconds=60)
        self.enter(gw, USER)
        now = self.h.advance(61_000)
        # The row the auto-draw tick read before the extend landed.
        stale = self.h.call("due", now=now)[0]
        self.assertEqual(gw["id"], stale["id"])

        extended = self.h.call("extend", giveaway_id=gw["id"], extra_seconds=600)

        with self.assertRaises(ServiceError) as caught:
            self.end(stale, now=now, require_due=True, known=stale)
        self.assertEqual("This giveaway is not due to end yet.", str(caught.exception))

        # The extend is not overwritten: still running, no winners, new deadline.
        stored = self.h.call("get", giveaway_id=gw["id"])
        self.assertEqual("active", stored["status"])
        self.assertEqual([], stored["winners"])
        self.assertEqual(extended["ends_at"], stored["ends_at"])
        self.assertEqual(gw["ends_at"] + 600_000, stored["ends_at"])
        self.assertEqual("[]", self.h.scalar(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))

    def test_the_guarded_flip_rechecks_the_deadline(self) -> None:
        gw = self.gw(duration_seconds=60)
        now = self.h.advance(61_000)
        # A copy whose ends_at is already in the past: every pre-check passes,
        # so only the deadline inside the UPDATE can refuse this call.
        stale = dict(self.h.call("get", giveaway_id=gw["id"]))
        stale["ends_at"] = now - 1
        self.h.call("extend", giveaway_id=gw["id"], extra_seconds=60)

        with self.assertRaises(ServiceError) as caught:
            self.end(stale, now=now, require_due=True, known=stale)
        self.assertEqual("This giveaway is not due to end yet.", str(caught.exception))
        self.assertEqual("active", self.h.call("get_status", giveaway_id=gw["id"]))

    def test_extend_after_end_refuses_and_keeps_the_draw(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        _, winners = self.end(gw)

        with self.assertRaises(ServiceError) as caught:
            self.h.call("extend", giveaway_id=gw["id"], extra_seconds=60)
        self.assertEqual("Only a running giveaway can be extended.", str(caught.exception))
        self.assertEqual(winners, self.h.call("get", giveaway_id=gw["id"])["winners"])


class ClaimExpiryRecoveryTests(HarnessTestCase):
    def _draw_with_claims(self, entrants: int = 4) -> tuple[dict, list[str], list[str]]:
        gw = self.gw(claim_timeout_seconds=300)
        users = _ids("61", entrants)
        for member in users:
            self.enter(gw, member)
        _, winners = self.end(gw)
        self.assertEqual(1, len(winners))
        return gw, list(winners), users

    def test_expiry_replaces_the_winner_and_settles_the_row(self) -> None:
        gw, winners, users = self._draw_with_claims()
        pending = self.h.call("pending_claims", giveaway_id=gw["id"])
        self.assertEqual([winners[0]], [row["user_id"] for row in pending])
        deadline = int(pending[0]["deadline_ms"])

        # Too early: the guarded UPDATE matches nothing and draws no one.
        with self.assertRaises(ServiceError) as caught:
            self.h.call(
                "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline - 1
            )
        self.assertEqual("That claim is no longer pending.", str(caught.exception))

        ended, replacements = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline + 1
        )

        self.assertEqual(1, len(replacements))
        self.assertIn(replacements[0], users)
        self.assertNotIn(replacements[0], winners)
        # The expired winner stays named; the replacement is added alongside.
        self.assertEqual(2, len(ended["winners"]))
        self.assertEqual(len(set(ended["winners"])), len(ended["winners"]))
        self.assertIn(winners[0], ended["winners"])
        self.assertIn(replacements[0], ended["winners"])

        statuses = self.h.call("claim_status", giveaway_id=gw["id"])
        self.assertEqual(2, len(statuses), "one claim row per drawn member, no duplicates")
        self.assertEqual(len({row["user_id"] for row in statuses}), len(statuses))
        by_user = {row["user_id"]: row["status"] for row in statuses}
        deadlines = {row["user_id"]: int(row["deadline_ms"]) for row in statuses}
        self.assertEqual("expired", by_user[winners[0]])
        self.assertEqual("pending", by_user[replacements[0]])
        self.assertEqual(
            [replacements[0]],
            [row["user_id"] for row in self.h.call("pending_claims", giveaway_id=gw["id"])],
        )
        # The replacement's window starts at the expiry, not at the draw.
        self.assertEqual(deadline + 1 + 300_000, deadlines[replacements[0]])

        # The expired slot is closed; the replacement can still claim.
        with self.assertRaises(ServiceError) as caught:
            self.h.call("claim", giveaway_id=gw["id"], user_id=winners[0])
        self.assertEqual("This claim window has closed.", str(caught.exception))
        self.h.call("claim", giveaway_id=gw["id"], user_id=replacements[0])
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))

        # Expiring the same slot twice is refused rather than drawing twice.
        with self.assertRaises(ServiceError) as caught:
            self.h.call(
                "expire_claim",
                giveaway_id=gw["id"],
                user_id=winners[0],
                now=deadline + 10_000,
            )
        self.assertEqual("That claim is no longer pending.", str(caught.exception))
        self.assertEqual(2, len(self.h.call("claim_status", giveaway_id=gw["id"])))

    def test_an_expired_claim_survives_a_restart(self) -> None:
        gw, winners, _ = self._draw_with_claims()
        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])
        ended_at = int(self.h.scalar(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?", [gw["id"]]
        ))
        self.assertEqual(ended_at + 300_000, deadline)

        _, replacements = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline + 1
        )
        self.assertEqual(1, len(replacements))

        # A restart must not resurrect the expired slot nor lose the replacement:
        # both rows are durable, and the pending one is still pending.
        self.reopen()

        pending = self.h.call("pending_claims", giveaway_id=gw["id"])
        self.assertEqual([replacements[0]], [row["user_id"] for row in pending])
        self.assertEqual(deadline + 1 + 300_000, int(pending[0]["deadline_ms"]))
        statuses = {row["user_id"]: row["status"] for row in self.h.call(
            "claim_status", giveaway_id=gw["id"]
        )}
        self.assertEqual("expired", statuses[winners[0]])
        self.assertEqual("pending", statuses[replacements[0]])
        stored = self.h.call("get", giveaway_id=gw["id"])["winners"]
        self.assertEqual(2, len(stored))
        self.assertEqual(len(set(stored)), len(stored))

        # The tick finds it due and can expire it into a fresh replacement.
        due = self.h.call("due_claims", now=deadline + 1 + 300_000)
        self.assertEqual([replacements[0]], [row["user_id"] for row in due])

    def test_expiry_with_an_exhausted_pool_draws_nothing(self) -> None:
        gw, winners, _ = self._draw_with_claims(entrants=1)
        deadline = int(self.h.call("pending_claims", giveaway_id=gw["id"])[0]["deadline_ms"])

        ended, replacements = self.h.call(
            "expire_claim", giveaway_id=gw["id"], user_id=winners[0], now=deadline + 1
        )

        self.assertEqual([], replacements)
        self.assertEqual(winners, ended["winners"])
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))
        statuses = self.h.call("claim_status", giveaway_id=gw["id"])
        self.assertEqual(1, len(statuses))
        self.assertEqual("expired", statuses[0]["status"])


class TimeoutPenaltyRecoveryTests(HarnessTestCase):
    def _remaining(self, user_id: str = USER) -> int:
        return int(self.h.call(
            "timeout_ban_remaining", guild_id=self.guild, user_id=user_id
        ))

    def _hits(self, user_id: str = USER) -> int:
        return int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ?",
            [self.guild, user_id],
        ))

    def test_the_penalty_decrements_to_zero_and_stops(self) -> None:
        trigger = self.gw()
        elsewhere = self.gw()
        self.enter(trigger, USER)
        self.enter(elsewhere, USER)

        error = self.timed_join_error(trigger, USER)
        self.assertEqual("timeout_ban", error.kind)
        self.assertEqual(PENALTY, self._remaining())
        # The timeout drops every entry the member already held.
        self.assertEqual(0, self.entry_rows(trigger["id"]))
        self.assertEqual(0, self.entry_rows(elsewhere["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=trigger["id"]))
        self.assertEqual(1, self._hits(), "the trigger giveaway is counted, not spent")

        blocked = [self.gw() for _ in range(PENALTY)]
        left: list[int] = []
        for gw in blocked:
            # No timed_out flag here: the penalty row alone keeps refusing.
            self.assertIn("penalty", str(self.join_error(gw, USER)))
            self.assertEqual(0, self.entry_rows(gw["id"]))
            left.append(self._remaining())

        self.assertEqual([2, 1, 0], left, "exactly one giveaway per blocked attempt")
        self.assertEqual(PENALTY + 1, self._hits(), "one hit row per giveaway sat out")
        # At zero the row is deleted rather than left behind as a listing entry.
        self.assertEqual([], self.h.call("list_timeout_bans", guild_id=self.guild))
        self.assertEqual(0, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?",
            [self.guild, USER],
        )))

        # The giveaway that spent the last unit still refuses: rejoining it
        # would hand the penalty straight back.
        error = self.join_error(blocked[-1], USER)
        self.assertEqual("timeout_ban", error.kind)
        self.assertIn("sat out this giveaway", str(error))
        self.assertEqual(0, self._remaining())

        # A giveaway never seen before is enterable again.
        self.assertEqual(1, self.enter(self.gw(), USER))

    def test_a_lifted_penalty_can_start_again(self) -> None:
        trigger = self.gw()
        self.timed_join_error(trigger, USER)
        for _ in range(PENALTY):
            self.join_error(self.gw(), USER)
        self.assertEqual(0, self._remaining())
        self.assertEqual(PENALTY + 1, self._hits())

        # Being caught timed out again starts a fresh penalty; the remembered
        # giveaways are not repaid and nothing stacks.
        again = self.gw()
        self.assertEqual("timeout_ban", self.timed_join_error(again, USER).kind)
        self.assertEqual(PENALTY, self._remaining())
        self.assertEqual(PENALTY + 2, self._hits())

        # Same giveaway, still timed out: the hit row is already there, so the
        # penalty is neither restacked nor spent twice.
        for _ in range(3):
            self.assertIn("3 giveaway(s) left", str(self.timed_join_error(again, USER)))
        self.assertEqual(PENALTY, self._remaining())
        self.assertEqual(PENALTY + 2, self._hits())


class StaleEntryWipeTests(HarnessTestCase):
    def test_the_wipe_never_touches_the_blacklist_or_a_penalty(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.enter(gw, OTHER)
        self.end(gw)
        self.execute(
            "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
            [self.h.now() - RETENTION_MS - 1000, gw["id"]],
        )
        # Both rows are seeded through the service so the test asserts the real
        # write path, not a hand-written INSERT the service would never produce.
        self.h.call("blacklist_add", guild_id=self.guild, user_id=USER)
        self.h.call(
            "apply_timeout_penalty",
            guild_id=self.guild,
            user_id=OTHER,
            giveaways=2,
            giveaway_id=gw["id"],
        )

        self.assertEqual(2, self.h.call("wipe_stale_entries"))

        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"]))
        self.assertEqual(0, int(self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )))
        # The retention sweep is entries-only, by design: a blacklist row and a
        # penalty are not stale data, they are live moderation state.
        self.assertTrue(self.h.call("is_blacklisted", guild_id=self.guild, user_id=USER))
        self.assertEqual(1, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_blacklist WHERE guild_id = ?", [self.guild]
        )))
        self.assertEqual(2, self.h.call(
            "timeout_ban_remaining", guild_id=self.guild, user_id=OTHER
        ))
        self.assertEqual(1, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaway_bans WHERE guild_id = ?", [self.guild]
        )))
        self.assertEqual(1, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaway_ban_hits WHERE guild_id = ?", [self.guild]
        )))

        # A second sweep is a no-op and still leaves both tables alone.
        self.assertEqual(0, self.h.call("wipe_stale_entries"))
        self.assertTrue(self.h.call("is_blacklisted", guild_id=self.guild, user_id=USER))
        self.assertEqual(2, self.h.call(
            "timeout_ban_remaining", guild_id=self.guild, user_id=OTHER
        ))


class CacheInvalidationTests(HarnessTestCase):
    """The harness never has Redis; every cache path must degrade to a no-op."""

    def test_flush_is_a_safe_noop_without_redis(self) -> None:
        self.assertIsNone(self.h.call("cache_get", key="gw:missing"))
        self.assertIsNone(self.h.call("cache_get_int", key="count:missing"))
        # Neither of these may raise: every write path calls them unconditionally.
        self.h.call("cache_put", key="gw:x", value={"id": "x"}, ttl=8.0)
        self.h.call("cache_drop", suffix="gw:x")

        self.h.call("mark_stale", suffixes=["gw:x", "guild:g:active"])
        self.assertEqual([], self.h.call("drain_invalidations"))
        self.assertEqual(0, self.h.call("flush_invalidations"))
        self.assertEqual(0, self.h.call("flush_invalidations"))

    def test_a_real_write_path_flushes_safely(self) -> None:
        gw = self.gw()
        self.enter(gw, USER)
        self.h.call("set_notify_role", guild_id=self.guild, role_id="r1")
        self.end(gw)

        # create/join/end/set_notify_role all queue invalidations internally; with
        # no cache the queue stays empty and the flush still answers zero.
        self.assertEqual(0, self.h.call("flush_invalidations"))
        self.assertIsNone(self.h.call("cache_get", key=f"count:{gw['id']}"))


if __name__ == "__main__":  # pragma: no cover - convenience for a single module
    unittest.main()
