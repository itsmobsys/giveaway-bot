"""Statistical invariants for the draw, not for a particular draw.

Every assertion here is about the *shape* of a random outcome: which members
are eligible, how many can win, whether a reroll can hand the same prize out
twice. No test may name a winner: the RNG is seeded from entropy, so a test
that expects member X to win is a test that fails one run in five.

Sizes are deliberately modest (hundreds of draws, not thousands) so the whole
module stays a couple of seconds against the debug binary while still making a
broken RNG (a constant, a first-element pick, an off-by-one slice) impossible
to miss.
"""

from __future__ import annotations

import unittest
from collections import Counter

from tests.harness import HarnessTestCase, ServiceError

#: Five members are enough for a 1-of-N draw to be visibly biased and cheap
#: enough to run 400 of them.
POOL = [f"70000000000000{index:04d}" for index in range(5)]

#: 400 draws of 1-of-5: uniform expectation is 80 per member, sd ~8. 45% (180)
#: is over twelve standard deviations away, so it only trips on real bias.
DRAWS = 400


class PickDistributionTests(HarnessTestCase):
    def test_one_from_five_covers_every_entrant_without_dominating(self) -> None:
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)

        counts: Counter[str] = Counter()
        for _ in range(DRAWS):
            picked = self.h.call("pick", giveaway_id=gw["id"], n=1)
            self.assertEqual(1, len(picked))
            self.assertIn(picked[0], POOL)
            counts[picked[0]] += 1

        self.assertEqual(set(POOL), set(counts), "an entrant was never drawn")
        self.assertLess(
            max(counts.values()),
            int(DRAWS * 0.45),
            f"one entrant took more than 45% of the draws: {dict(counts)}",
        )
        self.assertGreater(
            min(counts.values()),
            int(DRAWS * 0.05),
            f"one entrant was almost never drawn: {dict(counts)}",
        )

        # Chi-square for 4 degrees of freedom. 30 sits around p = 5e-6, so this
        # catches a skewed picker without turning a lucky run into a failure.
        expected = DRAWS / len(POOL)
        chi_square = sum((count - expected) ** 2 / expected for count in counts.values())
        self.assertLess(chi_square, 30.0, f"draw distribution is skewed: {dict(counts)}")

    def test_two_from_five_never_repeats_within_one_draw(self) -> None:
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)

        seen: set[str] = set()
        for _ in range(200):
            picked = self.h.call("pick", giveaway_id=gw["id"], n=2)
            self.assertEqual(2, len(picked))
            # choose_multiple samples without replacement: a duplicate would
            # mean the same member got two copies of one prize.
            self.assertEqual(len(set(picked)), len(picked))
            self.assertTrue(set(picked) <= set(POOL))
            seen.update(picked)

        self.assertEqual(set(POOL), seen)

    def test_excluded_members_are_never_picked(self) -> None:
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)
        excluded = POOL[:3]

        for _ in range(50):
            picked = self.h.call("pick", giveaway_id=gw["id"], n=2, exclude=excluded)
            self.assertEqual(set(POOL[3:]), set(picked))

    def test_pick_is_capped_by_the_pool_size(self) -> None:
        gw = self.gw()
        for member in POOL[:3]:
            self.enter(gw, member)

        picked = self.h.call("pick", giveaway_id=gw["id"], n=10)

        self.assertEqual(set(POOL[:3]), set(picked))
        self.assertEqual(3, len(picked))

    def test_non_positive_pick_size_returns_nothing(self) -> None:
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)

        for n in (0, -3):
            with self.subTest(n=n):
                self.assertEqual([], self.h.call("pick", giveaway_id=gw["id"], n=n))

    def test_pick_on_an_empty_giveaway_returns_nothing(self) -> None:
        gw = self.gw()
        self.assertEqual([], self.h.call("pick", giveaway_id=gw["id"], n=3))


class EndDrawTests(HarnessTestCase):
    def test_eighty_draws_cover_every_entrant(self) -> None:
        # The same five members enter eighty separate giveaways, so the draws
        # are independent and a picker that always takes the first row would
        # leave four members with zero wins.
        wins: Counter[str] = Counter()
        for _ in range(80):
            gw = self.gw()
            for member in POOL:
                self.enter(gw, member)
            _, winners = self.end(gw)
            self.assertEqual(1, len(winners))
            self.assertIn(winners[0], POOL)
            wins[winners[0]] += 1

        self.assertEqual(set(POOL), set(wins), f"an entrant never won: {dict(wins)}")
        # 80 draws: a fair picker gives each member ~16 wins. 40 (50%) is far
        # outside the tail; the exact 45% bound belongs to the 400-draw test.
        self.assertLess(max(wins.values()), 40, f"one entrant dominated: {dict(wins)}")

    def test_winner_count_never_exceeds_the_pool_or_repeats(self) -> None:
        gw = self.gw(winner_count=3)
        for member in POOL[:2]:
            self.enter(gw, member)

        _, winners = self.end(gw)

        # Only two members entered, so three slots collapse to two winners.
        self.assertEqual(set(POOL[:2]), set(winners))
        self.assertEqual(2, len(winners))

    def test_draw_never_names_the_same_member_twice(self) -> None:
        for _ in range(20):
            gw = self.gw(winner_count=4)
            for member in POOL:
                self.enter(gw, member)
            _, winners = self.end(gw)
            self.assertEqual(4, len(winners))
            self.assertEqual(len(set(winners)), len(winners))
            self.assertTrue(set(winners) <= set(POOL))


class RerollFairnessTests(HarnessTestCase):
    def test_reroll_only_promotes_members_who_have_never_won(self) -> None:
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)
        _, first = self.end(gw)

        ever_drawn = set(first)
        winners = list(first)
        for _ in range(4):
            _, fresh = self.h.call("reroll", giveaway_id=gw["id"], count=1)
            self.assertEqual(1, len(fresh))
            self.assertIn(fresh[0], POOL)
            self.assertNotIn(fresh[0], ever_drawn, "reroll handed the prize out twice")
            ever_drawn.add(fresh[0])
            winners.extend(fresh)

        # A plain reroll is additive: it appends to the stored list.
        stored = self.h.call("get", giveaway_id=gw["id"])["winners"]
        self.assertEqual(winners, stored)
        self.assertEqual(len(set(stored)), len(stored))

    def test_reroll_returns_nothing_once_everyone_has_won(self) -> None:
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)
        self.end(gw)
        # Four rerolls exhaust a five-member pool whose single winner is stored.
        for _ in range(4):
            self.h.call("reroll", giveaway_id=gw["id"], count=1)

        with self.assertRaises(ServiceError) as caught:
            self.h.call("reroll", giveaway_id=gw["id"], count=1)
        self.assertEqual("No other entrants left to reroll", str(caught.exception))

    def test_targeted_reroll_swaps_one_slot_and_keeps_the_rest(self) -> None:
        gw = self.gw(winner_count=3)
        for member in POOL:
            self.enter(gw, member)
        _, winners = self.end(gw)
        self.assertEqual(3, len(winners))

        target = winners[1]
        _, replacement = self.h.call(
            "reroll", giveaway_id=gw["id"], count=1, replace_user_id=target
        )

        self.assertEqual(1, len(replacement))
        self.assertIn(replacement[0], POOL)
        self.assertNotIn(replacement[0], winners)
        updated = self.h.call("get", giveaway_id=gw["id"])["winners"]
        self.assertEqual(3, len(updated))
        # Only the named slot changes; order is preserved so the announcement
        # edit does not shuffle the other winners.
        self.assertEqual([winners[0], replacement[0], winners[2]], updated)

    def test_draw_pool_is_the_join_order_not_a_reused_snapshot(self) -> None:
        # A late joiner must be drawable: a picker that caches the first
        # snapshot would never see members 4 and 5.
        gw = self.gw()
        for member in POOL:
            self.enter(gw, member)
            self.assertEqual(
                POOL.index(member) + 1,
                self.h.call("entry_count", giveaway_id=gw["id"], exact=True),
            )

        seen: set[str] = set()
        for _ in range(100):
            seen.update(self.h.call("pick", giveaway_id=gw["id"], n=1))
        self.assertEqual(set(POOL), seen)


if __name__ == "__main__":
    unittest.main()
