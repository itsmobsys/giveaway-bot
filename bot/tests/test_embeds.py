"""Embed text: countdown formatting, requirements lines, winner announcements."""

from __future__ import annotations

import re
import time
import unittest

from giveaway_bot import embeds
from giveaway_bot.service import Giveaway

COLOR = 0x7C5CFF


def in_seconds(seconds: float) -> int:
    return int((time.time() + seconds) * 1000)


def a_giveaway(**overrides) -> Giveaway:
    fields = {
        "id": "gw_0123456789ab",
        "guild_id": "111111111111111111",
        "channel_id": "222222222222222222",
        "message_id": "333333333333333333",
        "prize": "Steam key",
        "winner_count": 1,
        "ends_at": in_seconds(3600),
        "status": "active",
        "required_role_id": None,
        "required_role_ids": [],
        "blocked_role_id": None,
        "min_account_age_days": 0,
        "min_messages": 0,
        "image_url": None,
        "entrants_role_id": None,
        "host_id": None,
        "host_name": None,
        "winners": [],
    }
    fields.update(overrides)
    return Giveaway(**fields)


class CountdownTests(unittest.TestCase):
    def test_shape_per_magnitude(self) -> None:
        self.assertRegex(embeds.countdown(in_seconds(59)), r"^0m \d\ds$")
        self.assertRegex(embeds.countdown(in_seconds(90)), r"^1m \d\ds$")
        self.assertRegex(embeds.countdown(in_seconds(3700)), r"^1h \d+m \d\ds$")
        self.assertRegex(embeds.countdown(in_seconds(90000)), r"^1d \d+h \d+m$")

    def test_expired_never_goes_negative(self) -> None:
        self.assertTrue(re.fullmatch(r"0m 0\ds", embeds.countdown(in_seconds(-5))))
        self.assertTrue(re.fullmatch(r"0m 0\ds", embeds.countdown(0)))


class GiveawayEmbedTests(unittest.TestCase):
    def test_basics(self) -> None:
        embed = embeds.giveaway_embed(a_giveaway(), 12, COLOR)
        self.assertIn("Steam key", embed.description)
        self.assertIn("Winners: **1**", embed.description)
        self.assertIn("Entries: **12**", embed.description)
        self.assertIn("ID: gw_0123456789ab", embed.footer.text)
        self.assertEqual(embed.colour.value, COLOR)
        self.assertIsNone(embed.image.url)

    def test_requirements_block_is_omitted_when_empty(self) -> None:
        self.assertNotIn("Requirements", embeds.giveaway_embed(a_giveaway(), 0, COLOR).description)

    def test_every_requirement_is_listed(self) -> None:
        embed = embeds.giveaway_embed(
            a_giveaway(
                required_role_ids=["11", "22"],
                blocked_role_id="33",
                min_account_age_days=30,
                min_messages=5,
            ),
            0,
            COLOR,
        )
        for needle in ("<@&11>", "<@&22>", "<@&33>", "Account 30+ days old", "Send 5+ messages"):
            self.assertIn(needle, embed.description)

    def test_host_and_image(self) -> None:
        embed = embeds.giveaway_embed(
            a_giveaway(host_id="444444444444444444", host_name="Mod", image_url="https://x/y.png"),
            1,
            COLOR,
        )
        self.assertIn("<@444444444444444444> (Mod)", embed.description)
        self.assertEqual(embed.image.url, "https://x/y.png")

    def test_host_without_a_name_still_renders(self) -> None:
        embed = embeds.giveaway_embed(a_giveaway(host_id="444444444444444444"), 1, COLOR)
        self.assertIn("(host)", embed.description)


class ParticipantsEmbedTests(unittest.TestCase):
    def rows(self, n: int) -> list[dict]:
        return [{"user_id": str(10 ** 17 + i), "username": "u", "entered_at": i} for i in range(n)]

    def test_paging_header_and_odds(self) -> None:
        embed = embeds.participants_embed(
            prize="Nitro", rows=self.rows(10), page=1, pages=3, total=25, mine=1,
            winner_count=2, color=COLOR,
        )
        self.assertEqual(embed.title, "👥 Participants — page 2/3")
        self.assertIn("Total Participants: 25", embed.description)
        self.assertIn("Your Entries: 1", embed.description)
        self.assertIn("8%", embed.description)

    def test_zero_odds_without_an_entry(self) -> None:
        embed = embeds.participants_embed(
            prize="Nitro", rows=self.rows(1), page=0, pages=1, total=1, mine=0,
            winner_count=1, color=COLOR,
        )
        self.assertIn("Your Chance of Winning: 0%", embed.description)

    def test_empty_page_does_not_crash(self) -> None:
        embed = embeds.participants_embed(
            prize="Nitro", rows=[], page=4, pages=5, total=50, mine=0,
            winner_count=1, color=COLOR,
        )
        self.assertIn("Total Participants: 50", embed.description)


class WinnerEmbedTests(unittest.TestCase):
    def test_winners_are_mentioned(self) -> None:
        embed = embeds.winner_embed(a_giveaway(winners=["1" * 18]), ["1" * 18], 7, COLOR)
        self.assertEqual(embed.title, "Giveaway Ended")
        self.assertIn("<@111111111111111111>", embed.description)
        self.assertIn("Entries: **7**", embed.description)

    def test_no_winners_says_so(self) -> None:
        embed = embeds.winner_embed(a_giveaway(), [], 0, COLOR)
        self.assertIn("No valid entries", embed.description)

    def test_host_line_and_image_carry_over(self) -> None:
        embed = embeds.winner_embed(
            a_giveaway(host_id="5" * 18, host_name="Mod", image_url="https://x/y.png"),
            ["6" * 18],
            2,
            COLOR,
        )
        self.assertIn("Hosted by <@555555555555555555> (Mod)", embed.description)
        self.assertEqual(embed.image.url, "https://x/y.png")


if __name__ == "__main__":
    unittest.main()