"""Load and throughput: the same rules, at the sizes the bot actually sees.

These tests are not benchmarks and do not assert a latency number. They assert
that bulk paths stay *correct* when the row count grows:

* hundreds of entries keep one row per member and a snapshot whose window and
  total disagree only where the ``LIMIT`` says they should;
* dozens of giveaways across guilds stay scoped per guild while the global
  listing honours its cap;
* a thousand message-count keys flush in one call, chunked internally;
* repeated full-size draws never repeat a winner;
* four bot processes joining, leaving and joining again keep the stored set and
  the counter in agreement.

Each test also carries a wall-clock ceiling. It is deliberately loose (a minute
for work that measures in seconds) so it catches a hang or an accidental
quadratic loop without turning a slow CI box into a flake. The module as a whole
is held to the same ceiling.
"""

from __future__ import annotations

import time
import unittest
from functools import partial

from tests.harness import Harness, HarnessTestCase, join

#: Entries in the "big giveaway" test: comfortably past a display page.
ENTRY_LOAD = 400

#: Giveaways spread over three guilds.
GIVEAWAY_LOAD = 50

#: Distinct keys in the single-call flush test.
FLUSH_USERS = 1000

#: Mirrors ``MESSAGE_FLUSH_CHUNK``: the storage layer writes 300 rows per batch.
FLUSH_CHUNK = 300

#: Full-size draws: ``MAX_WINNERS`` is 25, so every draw uses the whole cap.
DRAW_ROUNDS = 20
DRAW_WINNERS = 25
DRAW_POOL = 30

#: Concurrent workers and the users each one owns.
WORKERS = 4
PER_WORKER = 40
CHURN_PER_WORKER = 10

#: A hang detector, not a performance target.
BUDGET_SECONDS = 60.0

_MODULE_STARTED = 0.0


def setUpModule() -> None:
    global _MODULE_STARTED
    _MODULE_STARTED = time.monotonic()


def tearDownModule() -> None:
    elapsed = time.monotonic() - _MODULE_STARTED
    if elapsed >= BUDGET_SECONDS:
        raise AssertionError(
            f"test_load.py took {elapsed:.1f}s, budget {BUDGET_SECONDS:.0f}s"
        )


def _ids(prefix: str, count: int) -> list[str]:
    """Deterministic, unique user ids that never collide across tests."""
    return [f"{prefix}{index:013d}" for index in range(count)]


def join_batch(harness: Harness, gw: dict, users: list[str]) -> list[int]:
    """Join several members from one process, in order."""
    return [join(harness, gw, user) for user in users]


class LoadBudget:
    """Shared wall-clock assertion for work whose size is the point."""

    def assert_within_budget(self, started: float, label: str) -> None:
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, BUDGET_SECONDS, f"{label} took {elapsed:.1f}s")


class ManyEntriesTests(LoadBudget, HarnessTestCase):
    def test_four_hundred_entries_stay_exact_and_snapshot_cleanly(self) -> None:
        started = time.monotonic()
        gw = self.gw(winner_count=DRAW_WINNERS)
        users = _ids("71", ENTRY_LOAD)

        counts = join_batch(self.h, gw, users)
        self.assert_within_budget(started, f"{ENTRY_LOAD} joins")

        # Sequential joins return the running count, so a lost or doubled insert
        # would show up as a gap or a repeat, not just a wrong total.
        self.assertEqual(list(range(1, ENTRY_LOAD + 1)), counts)

        self.assertEqual(
            ENTRY_LOAD, self.h.call("entry_count", giveaway_id=gw["id"], exact=True)
        )
        # The cheap counter must agree with COUNT(*): joins maintain it, and no
        # bulk path here is allowed to leave it behind.
        self.assertEqual(ENTRY_LOAD, self.h.call("entry_count", giveaway_id=gw["id"]))
        self.assertEqual(ENTRY_LOAD, int(self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )))
        self.assertEqual(ENTRY_LOAD, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?", [gw["id"]]
        )))

        rows = self.h.call("entries", giveaway_id=gw["id"])
        self.assertEqual(ENTRY_LOAD, len(rows))
        self.assertEqual(set(users), {row["user_id"] for row in rows})
        self.assertEqual(ENTRY_LOAD, len({row["user_id"] for row in rows}))

        # No LIMIT: the window is the whole pool and the caller is in it. The
        # embed's chance is winner_count / total, so a wrong total here would
        # misreport every participant's odds.
        total, window, mine = self.h.call(
            "entries_snapshot", giveaway_id=gw["id"], user_id=users[-1]
        )
        self.assertEqual(ENTRY_LOAD, total)
        self.assertEqual(ENTRY_LOAD, len(window))
        self.assertEqual(1, mine)
        self.assertEqual(set(users), {row["user_id"] for row in window})

        # With a LIMIT the window shrinks but COUNT(*) OVER () still reports the
        # true pool: the "first N of T" line depends on that split.
        non_member = "799999999999999999"
        total, window, mine = self.h.call(
            "entries_snapshot",
            giveaway_id=gw["id"],
            limit=50,
            user_id=non_member,
        )
        self.assertEqual(ENTRY_LOAD, total)
        self.assertEqual(50, len(window))
        self.assertEqual(0, mine, "a non-member must not be reported as entered")
        self.assertEqual(50, len({row["user_id"] for row in window}))
        self.assertTrue({row["user_id"] for row in window} <= set(users))


class ManyGiveawaysTests(LoadBudget, HarnessTestCase):
    def test_fifty_giveaways_stay_scoped_and_capped(self) -> None:
        guilds = [self.guild, self.other_guild, "444444444444444444"]
        started = time.monotonic()
        created: list[dict] = []
        for index in range(GIVEAWAY_LOAD):
            created.append(self.gw(
                guild_id=guilds[index % len(guilds)],
                duration_seconds=60 + index * 60,
            ))
        self.assert_within_budget(started, f"{GIVEAWAY_LOAD} creates")

        # The fake clock does not move during creates, so a strictly longer
        # duration means a strictly later deadline: creation order is deadline
        # order across the whole database.
        deadlines = [gw["ends_at"] for gw in created]
        self.assertEqual(sorted(deadlines), deadlines)
        self.assertEqual(GIVEAWAY_LOAD, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_giveaways WHERE status = 'active'"
        )))

        # Global listing: soonest first, capped, and the cap clamps at both ends.
        every_id = [gw["id"] for gw in created]
        self.assertEqual(
            every_id[:25],
            [gw["id"] for gw in self.h.call("list_all_active", limit=25)],
        )
        self.assertEqual(
            every_id[:25],
            [gw["id"] for gw in self.h.call("list_all_active")],
        )
        self.assertEqual(
            [every_id[0]], [gw["id"] for gw in self.h.call("list_all_active", limit=0)]
        )
        self.assertEqual(
            every_id, [gw["id"] for gw in self.h.call("list_all_active", limit=200)]
        )
        self.assertEqual(
            every_id, [gw["id"] for gw in self.h.call("list_all_active", limit=500)]
        )

        # Per-guild listing is unbounded but never leaks another guild's row.
        for guild in guilds:
            expected = [gw["id"] for gw in created if gw["guild_id"] == guild]
            listed = self.h.call("list_active", guild_id=guild)
            self.assertEqual(expected, [gw["id"] for gw in listed])
            self.assertTrue(all(gw["guild_id"] == guild for gw in listed))
            self.assertEqual(expected, sorted(
                (gw["id"] for gw in listed),
                key=lambda giveaway_id: next(
                    gw["ends_at"] for gw in created if gw["id"] == giveaway_id
                ),
            ))
        self.assertEqual(GIVEAWAY_LOAD, sum(
            len(self.h.call("list_active", guild_id=guild)) for guild in guilds
        ))


class ChunkedFlushAtScaleTests(LoadBudget, HarnessTestCase):
    def test_a_thousand_keys_flush_in_one_call(self) -> None:
        users = _ids("72", FLUSH_USERS)
        rows = [[self.guild, user, 1] for user in users]
        started = time.monotonic()

        receipt = self.h.call("add_message_counts", rows=rows)

        self.assert_within_budget(started, f"{FLUSH_USERS}-row flush")
        # ``applied`` counts rows written, not batches issued; ``before`` counts
        # distinct keys snapshotted. 1000 rows are 4 chunks of at most 300.
        self.assertEqual({"applied": FLUSH_USERS, "before": FLUSH_USERS}, receipt)
        self.assertEqual(4, -(-FLUSH_USERS // FLUSH_CHUNK))

        self.assertEqual(FLUSH_USERS, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ?", [self.guild]
        )))
        self.assertEqual(FLUSH_USERS, int(self.h.scalar(
            "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?", [self.guild]
        )))
        self.assertEqual(0, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ? AND count <> 1",
            [self.guild],
        )))
        self.assertEqual(1, self.h.call(
            "message_count", guild_id=self.guild, user_id=users[FLUSH_USERS - 1]
        ))

        # A complete flush reconciled against its zero snapshot has no tail left
        # to resend: the crash-recovery path stays a no-op at this size too.
        self.assertEqual([], self.h.call(
            "reconcile_message_counts",
            rows=rows,
            before=[[self.guild, user, 0] for user in users],
        ))


class DrawThroughputTests(LoadBudget, HarnessTestCase):
    def test_twenty_full_size_draws_never_repeat_a_winner(self) -> None:
        started = time.monotonic()
        draws: list[tuple[dict, list[str], list[str]]] = []
        for round_index in range(DRAW_ROUNDS):
            gw = self.gw(winner_count=DRAW_WINNERS)
            pool = _ids(f"8{round_index}", DRAW_POOL)
            join_batch(self.h, gw, pool)
            _, winners = self.end(gw)
            draws.append((gw, pool, winners))
        self.assert_within_budget(started, f"{DRAW_ROUNDS} full draws")

        for gw, pool, winners in draws:
            self.assertEqual(DRAW_WINNERS, len(winners))
            self.assertEqual(DRAW_WINNERS, len(set(winners)), "a winner was named twice")
            self.assertTrue(set(winners) <= set(pool))
            self.assertEqual(DRAW_POOL - DRAW_WINNERS, len(set(pool) - set(winners)))

            stored = self.h.call("get", giveaway_id=gw["id"])
            self.assertEqual("ended", stored["status"])
            self.assertEqual(winners, stored["winners"])
            self.assertEqual(DRAW_POOL, stored["entrant_count"])
            self.assertEqual(
                DRAW_POOL, self.h.call("entry_count", giveaway_id=gw["id"], exact=True)
            )

        # Every round draws from its own pool: a leaked winner id would break
        # the "25 of 30" shape somewhere, even though the ids are opaque.
        self.assertEqual(
            DRAW_ROUNDS * DRAW_WINNERS,
            sum(len(winners) for _, _, winners in draws),
        )


class ConcurrentMixedWorkloadTests(LoadBudget, HarnessTestCase):
    def test_four_workers_join_leave_and_join_again(self) -> None:
        gw = self.gw(winner_count=DRAW_WINNERS)
        workers = [self.sibling() for _ in range(WORKERS)]
        users = _ids("73", WORKERS * PER_WORKER)
        batches = [
            users[index * PER_WORKER:(index + 1) * PER_WORKER]
            for index in range(WORKERS)
        ]
        started = time.monotonic()

        joined = self.run_concurrently([
            (worker, partial(join_batch, gw=gw, users=batch))
            for worker, batch in zip(workers, batches)
        ])
        self.assertEqual(["ok"] * WORKERS, [status for status, _ in joined])
        self.assert_within_budget(started, f"{WORKERS * PER_WORKER} concurrent joins")

        # The snapshot is the reference: it reports the true pool in one read,
        # independent of the counter the join path maintains.
        total, window, mine = self.h.call(
            "entries_snapshot", giveaway_id=gw["id"], user_id=users[-1]
        )
        self.assertEqual(WORKERS * PER_WORKER, total)
        self.assertEqual(WORKERS * PER_WORKER, len(window))
        self.assertEqual(1, mine)
        self.assertEqual(set(users), {row["user_id"] for row in window})
        self.assertEqual(
            WORKERS * PER_WORKER,
            self.h.call("entry_count", giveaway_id=gw["id"], exact=True),
        )

        fresh = _ids("74", WORKERS * CHURN_PER_WORKER)
        removed = {user for batch in batches for user in batch[:CHURN_PER_WORKER]}

        def churn(index: int, harness: Harness) -> tuple[list[bool], list[int]]:
            left = [
                bool(harness.call("leave", giveaway_id=gw["id"], user_id=user))
                for user in batches[index][:CHURN_PER_WORKER]
            ]
            added = join_batch(
                harness,
                gw,
                fresh[index * CHURN_PER_WORKER:(index + 1) * CHURN_PER_WORKER],
            )
            return left, added

        churned = self.run_concurrently([
            (worker, partial(churn, index)) for index, worker in enumerate(workers)
        ])
        self.assertEqual(["ok"] * WORKERS, [status for status, _ in churned])
        for status, (left, added) in churned:
            self.assertEqual("ok", status)
            self.assertEqual([True] * CHURN_PER_WORKER, left)
            self.assertTrue(all(isinstance(count, int) and count > 0 for count in added))

        # The net set is the snapshot's answer: every departure gone, every
        # arrival present, nobody duplicated, and the counter agrees.
        expected = (set(users) - removed) | set(fresh)
        self.assertEqual(WORKERS * PER_WORKER, len(expected))
        total, window, mine = self.h.call(
            "entries_snapshot", giveaway_id=gw["id"], user_id=next(iter(removed))
        )
        self.assertEqual(WORKERS * PER_WORKER, total)
        self.assertEqual(WORKERS * PER_WORKER, len(window))
        self.assertEqual(0, mine, "a member who left must not read as entered")
        self.assertEqual(expected, {row["user_id"] for row in window})
        self.assertEqual(
            WORKERS * PER_WORKER,
            self.h.call("entry_count", giveaway_id=gw["id"], exact=True),
        )
        self.assertEqual(WORKERS * PER_WORKER, int(self.h.scalar(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", [gw["id"]]
        )))
        self.assertEqual(WORKERS * PER_WORKER, int(self.h.scalar(
            "SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?", [gw["id"]]
        )))


if __name__ == "__main__":  # pragma: no cover - convenience for a single module
    unittest.main()
