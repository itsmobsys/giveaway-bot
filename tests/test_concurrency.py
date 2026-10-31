"""Cross-process races: two bot processes, one SQLite file.

The Python suite simulated a race with threads inside one event loop. The Rust
engine is what actually runs in production, where the auto-draw tick and a
slash command are *separate processes*, so these tests use real sibling
processes on the same database file (WAL + a 10s busy timeout, see db.rs).

Each test asserts an invariant that holds no matter who wins the race. Never
"process A wins": the scheduler decides that, and a test that depends on it is
a flake generator.
"""

from __future__ import annotations

import unittest
from functools import partial

from tests.harness import Harness, HarnessTestCase, ServiceError, join


def join_batch(harness: Harness, gw: dict, users: list[str]) -> list[int]:
    """Join several members from one process, in order."""
    return [join(harness, gw, user) for user in users]


def leave_batch(harness: Harness, giveaway_id: str, users: list[str]) -> list[bool]:
    return [
        bool(harness.call("leave", giveaway_id=giveaway_id, user_id=user)) for user in users
    ]


def end_giveaway(harness: Harness, giveaway_id: str) -> tuple[dict, list[str]]:
    return harness.call("end", giveaway_id=giveaway_id)


class EndRaceTests(HarnessTestCase):
    def test_two_end_calls_cannot_both_draw(self) -> None:
        gw = self.gw(duration_seconds=60)
        self.enter(gw, "900000000000000001")
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

        # The flip is a single guarded UPDATE, so exactly one caller stores a
        # draw and the other is told the giveaway is already over.
        self.assertEqual(["ok", "refused"], sorted(status for status, _ in outcomes))
        by_status = dict(outcomes)
        ended, winners = by_status["ok"]
        self.assertEqual("ended", ended["status"])
        self.assertEqual(1, len(winners))
        self.assertEqual("This giveaway has already ended.", str(by_status["refused"]))

        stored = self.h.call("get", giveaway_id=gw["id"])
        self.assertEqual("ended", stored["status"])
        self.assertEqual(winners, stored["winners"])

    def test_parallel_ends_of_different_giveaways_all_succeed(self) -> None:
        giveaways = [self.gw() for _ in range(4)]
        for index, gw in enumerate(giveaways):
            self.enter(gw, f"91000000000000{index:04d}")
        workers = [self.sibling() for _ in range(4)]

        outcomes = self.run_concurrently([
            (worker, partial(end_giveaway, giveaway_id=gw["id"]))
            for worker, gw in zip(workers, giveaways)
        ])

        self.assertEqual(["ok"] * 4, [status for status, _ in outcomes])
        for gw in giveaways:
            stored = self.h.call("get", giveaway_id=gw["id"])
            self.assertEqual("ended", stored["status"])
            self.assertEqual(1, len(stored["winners"]))


class JoinRaceTests(HarnessTestCase):
    def test_parallel_joins_never_duplicate_or_lose_entries(self) -> None:
        gw = self.gw()
        users = [f"92000000000000{index:04d}" for index in range(20)]
        workers = [self.sibling() for _ in range(4)]
        batches = [users[index * 5:(index + 1) * 5] for index in range(4)]

        outcomes = self.run_concurrently([
            (worker, partial(join_batch, gw=gw, users=batch))
            for worker, batch in zip(workers, batches)
        ])

        self.assertEqual(["ok"] * 4, [status for status, _ in outcomes])
        counts = [count for _, batch in outcomes for count in batch]
        self.assertTrue(all(isinstance(count, int) and count > 0 for count in counts))

        rows = self.h.call("entries", giveaway_id=gw["id"], limit=50)
        self.assertEqual(20, len(rows))
        self.assertEqual(set(users), {row["user_id"] for row in rows})
        self.assertEqual(20, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))

    def test_same_user_joining_from_two_processes_enters_once(self) -> None:
        gw = self.gw()
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: join(h, gw, "930000000000000001")),
            (second, lambda h: join(h, gw, "930000000000000001")),
        ])

        # The unique entry index turns the loser's INSERT into a refusal; it
        # must never read as a second successful join.
        self.assertEqual(["ok", "refused"], sorted(status for status, _ in outcomes))
        by_status = dict(outcomes)
        self.assertEqual("You are already entered.", str(by_status["refused"]))
        self.assertEqual(1, self.entry_rows(gw["id"]))
        self.assertEqual(1, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))

    def test_blacklist_racing_a_join_leaves_no_entry_behind(self) -> None:
        gw = self.gw()
        member = "940000000000000001"
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: join(h, gw, member)),
            (second, lambda h: h.call(
                "blacklist_add", guild_id=self.guild, user_id=member, hours=0
            )),
        ])

        # Either the ban lands first (join refused) or the join lands first and
        # blacklist_add purges it. A surviving entry would let a banned member
        # win a prize they were banned before.
        self.assertNotIn("error", [status for status, _ in outcomes])
        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))
        self.assertTrue(self.h.call("is_blacklisted", guild_id=self.guild, user_id=member))

    def test_two_simultaneous_clicks_cannot_stack_the_penalty(self) -> None:
        gw = self.gw()
        member = "950000000000000001"
        second = self.sibling()

        def click(harness: Harness) -> None:
            join(harness, gw, member, timed_out=True)

        outcomes = self.run_concurrently([(self.h, click), (second, click)])

        self.assertEqual(["refused", "refused"], sorted(status for status, _ in outcomes))
        for refusal in (value for _, value in outcomes):
            self.assertIsInstance(refusal, ServiceError)
            self.assertEqual("timeout_ban", refusal.kind)
            self.assertRegex(str(refusal), r"timed out|penalty")

        # The click that starts the penalty records this giveaway as its
        # trigger, so the other click's consume finds the hit already there and
        # spends nothing. Both interleavings therefore leave exactly three.
        self.assertEqual(
            3, self.h.call("timeout_ban_remaining", guild_id=self.guild, user_id=member)
        )
        self.assertEqual(1, self.h.call("count_timeout_bans", guild_id=self.guild))

    def test_parallel_joins_and_leaves_keep_the_counter_honest(self) -> None:
        gw = self.gw()
        users = [f"96000000000000{index:04d}" for index in range(10)]
        second = self.sibling()

        joined = self.run_concurrently([
            (self.h, partial(join_batch, gw=gw, users=users[:5])),
            (second, partial(join_batch, gw=gw, users=users[5:])),
        ])
        self.assertEqual(["ok", "ok"], [status for status, _ in joined])

        left = self.run_concurrently([
            (self.h, partial(leave_batch, giveaway_id=gw["id"], users=users[:5])),
            (second, partial(leave_batch, giveaway_id=gw["id"], users=users[5:])),
        ])
        self.assertEqual(["ok", "ok"], [status for status, _ in left])
        self.assertTrue(all(flag for _, batch in left for flag in batch))

        self.assertEqual(0, self.entry_rows(gw["id"]))
        self.assertEqual(0, self.h.call("entry_count", giveaway_id=gw["id"], exact=True))
        self.assertEqual(0, second.call("entry_count", giveaway_id=gw["id"], exact=True))


class ClaimRaceTests(HarnessTestCase):
    def _ended_with_claims(self, entrants: int = 4) -> tuple[dict, list[str]]:
        gw = self.gw(claim_timeout_seconds=300)
        users = [f"97000000000000{index:04d}" for index in range(entrants)]
        for member in users:
            self.enter(gw, member)
        _, winners = self.end(gw)
        self.assertEqual(1, len(winners))
        return gw, winners

    def test_two_claims_settle_exactly_once(self) -> None:
        gw, winners = self._ended_with_claims()
        winner = winners[0]
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: h.call("claim", giveaway_id=gw["id"], user_id=winner)),
            (second, lambda h: h.call("claim", giveaway_id=gw["id"], user_id=winner)),
        ])

        self.assertEqual(["ok", "refused"], sorted(status for status, _ in outcomes))
        by_status = dict(outcomes)
        self.assertEqual("This prize was already claimed.", str(by_status["refused"]))

        statuses = [
            row["status"] for row in self.h.call("claim_status", giveaway_id=gw["id"])
        ]
        self.assertEqual(["claimed"], statuses)
        self.assertEqual([], self.h.call("pending_claims", giveaway_id=gw["id"]))

    def test_parallel_rerolls_never_duplicate_or_drop_a_winner(self) -> None:
        gw, first = self._ended_with_claims(entrants=5)
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: h.call("reroll", giveaway_id=gw["id"], count=1)),
            (second, lambda h: h.call("reroll", giveaway_id=gw["id"], count=1)),
        ])

        # Both may win the CAS race (append one each) or one may exhaust its
        # retries and refuse. Either way the stored list is a superset of the
        # original draw with no duplicate.
        self.assertIn("ok", [status for status, _ in outcomes])
        stored = self.h.call("get", giveaway_id=gw["id"])["winners"]
        self.assertEqual(len(set(stored)), len(stored))
        self.assertTrue(set(first) <= set(stored))
        self.assertGreaterEqual(len(stored), 2)
        self.assertLessEqual(len(stored), 3)

    def test_parallel_targeted_rerolls_replace_the_slot_once(self) -> None:
        gw = self.gw(winner_count=2)
        users = [f"98000000000000{index:04d}" for index in range(5)]
        for member in users:
            self.enter(gw, member)
        _, winners = self.end(gw)
        self.assertEqual(2, len(winners))
        target = winners[0]
        second = self.sibling()

        outcomes = self.run_concurrently([
            (self.h, lambda h: h.call(
                "reroll", giveaway_id=gw["id"], count=1, replace_user_id=target
            )),
            (second, lambda h: h.call(
                "reroll", giveaway_id=gw["id"], count=1, replace_user_id=target
            )),
        ])

        # The second writer rebases onto the first swap, finds the target gone
        # and refuses: the slot is replaced once, never twice.
        self.assertEqual(["ok", "refused"], sorted(status for status, _ in outcomes))
        by_status = dict(outcomes)
        self.assertEqual(
            "That member is not a winner of this giveaway.", str(by_status["refused"])
        )
        stored = self.h.call("get", giveaway_id=gw["id"])["winners"]
        self.assertEqual(2, len(stored))
        self.assertEqual(len(set(stored)), len(stored))
        self.assertNotIn(target, stored)
        self.assertIn(winners[1], stored)


if __name__ == "__main__":
    unittest.main()
