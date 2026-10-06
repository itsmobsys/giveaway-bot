"""Bot-level tests that need no gateway connection.

GiveawayBot is a plain object until it is started, so the pieces that used to
be pure event-loop footguns -- the in-memory message counter, the autocomplete
snapshot, the entrants-role task bookkeeping -- can be driven directly here.
"""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
import uuid
from types import SimpleNamespace

from giveaway_bot import views
from giveaway_bot.bot import GiveawayBot, build_intents, wire_commands
from giveaway_bot.config import Settings
from giveaway_bot.db import Database
from giveaway_bot.service import GiveawayService


def fake_message(guild_id: str, user_id: str, *, bot: bool = False):
    return SimpleNamespace(
        guild=SimpleNamespace(id=int(guild_id)),
        author=SimpleNamespace(id=int(user_id), bot=bot),
    )


class BotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.uri = f"file:gwbot_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(self.uri, uri=True)
        self.db = Database(
            Settings(turso_url=""),
            connect=lambda: sqlite3.connect(self.uri, uri=True, check_same_thread=False),
        )
        self.db.init_schema()
        self.settings = Settings(turso_url="", dashboard_url="https://dash.test/")
        self.bot = GiveawayBot(self.settings, self.db)
        self.svc = GiveawayService(self.db)
        self.guild = "111111111111111111"
        # Mirror how amain builds a bot: commands wired and buttons registered.
        wire_commands(self.bot)
        self.bot.register_components()

    def tearDown(self) -> None:
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


class IntentTests(unittest.TestCase):
    def test_member_intent_is_required_for_role_checks(self) -> None:
        self.assertTrue(build_intents().members)


class WiringTests(BotTestCase):
    """The slash-command surface and the button dispatch, wired but not started."""

    EXPECTED = (
        "giveaway_blacklist_add",
        "giveaway_blacklist_list",
        "giveaway_blacklist_remove",
        "giveaway_cancel",
        "giveaway_create",
        "giveaway_end",
        "giveaway_extend",
        "giveaway_list",
        "giveaway_notifyer",
        "giveaway_ping",
        "giveaway_reroll",
    )

    def test_every_command_is_registered_once(self) -> None:
        names = sorted(command.name for command in self.bot.tree.get_commands())
        self.assertEqual(names, list(self.EXPECTED))

    def test_an_error_handler_is_installed(self) -> None:
        self.assertTrue(callable(self.bot.tree.on_error))

    def test_click_handlers_are_wired_to_the_bot(self) -> None:
        """A click is matched by custom_id pattern, not by a per-giveaway view."""
        item = asyncio.run(
            views.JoinButton.from_custom_id(
                None, None, views.JoinButton.__discord_ui_compiled_template__.fullmatch("gw_join:gw_1")
            )
        )
        self.assertEqual(item.giveaway_id, "gw_1")
        self.assertIs(views._HANDLERS["gw_join"].__self__, self.bot)

    def test_an_unknown_id_shape_never_matches(self) -> None:
        pattern = views.JoinButton.__discord_ui_compiled_template__
        self.assertIsNone(pattern.fullmatch("gw_join:" + "x" * 65))


class CommandWiringTests(BotTestCase):
    def test_messages_are_buffered_not_written_per_message(self) -> None:
        for _ in range(5):
            asyncio.run(self.bot.on_message(fake_message(self.guild, "3" * 18)))
        self.assertEqual(self.bot._pending_messages(self.guild, "3" * 18), 5)
        self.assertEqual(self.svc.message_count(self.guild, "3" * 18), 0,
                         "nothing reaches the database before a flush")

    def test_bots_and_dms_are_ignored(self) -> None:
        asyncio.run(self.bot.on_message(fake_message(self.guild, "3" * 18, bot=True)))
        asyncio.run(self.bot.on_message(SimpleNamespace(guild=None, author=SimpleNamespace(id=1, bot=False))))
        self.assertEqual(self.bot._message_buffer, {})

    def test_flush_writes_every_buffered_count_and_clears(self) -> None:
        for _ in range(3):
            asyncio.run(self.bot.on_message(fake_message(self.guild, "3" * 18)))
        asyncio.run(self.bot.on_message(fake_message(self.guild, "4" * 18)))
        asyncio.run(self.bot._flush_message_buffer())
        self.assertEqual(self.svc.message_count(self.guild, "3" * 18), 3)
        self.assertEqual(self.svc.message_count(self.guild, "4" * 18), 1)
        self.assertEqual(self.bot._message_buffer, {})
        asyncio.run(self.bot._flush_message_buffer())
        self.assertEqual(self.svc.message_count(self.guild, "3" * 18), 3, "no double counting")

    def test_a_failed_flush_keeps_the_counts(self) -> None:
        asyncio.run(self.bot.on_message(fake_message(self.guild, "3" * 18)))
        original = self.bot.service.add_message_counts
        self.bot.service.add_message_counts = lambda rows: (_ for _ in ()).throw(OSError("turso down"))
        with self.assertRaises(OSError):
            asyncio.run(self.bot._flush_message_buffer())
        self.bot.service.add_message_counts = original
        self.assertEqual(self.bot._pending_messages(self.guild, "3" * 18), 1,
                         "a blip must not lose counts")
        asyncio.run(self.bot._flush_message_buffer())
        self.assertEqual(self.svc.message_count(self.guild, "3" * 18), 1)

    def test_buffer_overflow_flushes_early(self) -> None:
        self.bot.MESSAGE_BUFFER_MAX = 2
        asyncio.run(self.bot.on_message(fake_message(self.guild, "1" * 18)))
        asyncio.run(self.bot.on_message(fake_message(self.guild, "2" * 18)))
        self.assertEqual(self.bot._message_buffer, {}, "flushed at the cap")
        self.assertEqual(self.svc.message_count(self.guild, "2" * 18), 1)


class AutocompleteCacheTests(BotTestCase):
    def test_snapshot_is_grouped_per_guild(self) -> None:
        mine = self.make(prize="mine")
        theirs = self.make(prize="theirs", guild_id="555555555555555555")
        self.bot._cache_autocomplete([self.svc.get(mine.id), self.svc.get(theirs.id)])
        self.assertEqual(self.bot._autocomplete_cache[self.guild], [(mine.id, "mine")])
        self.assertEqual(
            self.bot._autocomplete_cache["555555555555555555"], [(theirs.id, "theirs")]
        )
        self.assertEqual(self.bot._autocomplete_cache.get("999", []), [])

    def test_snapshot_is_replaced_not_appended(self) -> None:
        first = self.make(prize="first")
        self.bot._cache_autocomplete([self.svc.get(first.id)])
        self.bot._cache_autocomplete([])
        self.assertEqual(self.bot._autocomplete_cache, {})


class RoleTaskTests(BotTestCase):
    def test_scheduled_deletes_are_kept_alive_and_deduplicated(self) -> None:
        async def scenario() -> tuple[int, int]:
            self.bot._schedule_role_delete(self.guild, "123", "gw_a", delay=60)
            self.bot._schedule_role_delete(self.guild, "123", "gw_a", delay=60)
            self.bot._schedule_role_delete(self.guild, "123", "gw_b", delay=60)
            held = len(self.bot._role_tasks)
            for task in list(self.bot._role_tasks):
                task.cancel()
            await asyncio.gather(*list(self.bot._role_tasks), return_exceptions=True)
            return held, len(self.bot._scheduled_role_deletes)

        held, in_flight = asyncio.run(scenario())
        self.assertEqual(held, 2, "one task per giveaway, the duplicate is dropped")
        self.assertEqual(in_flight, 2)

    def test_incomplete_targets_are_ignored(self) -> None:
        async def scenario() -> int:
            self.bot._schedule_role_delete("", "1", "gw_x", delay=1)
            self.bot._schedule_role_delete(self.guild, "", "gw_y", delay=1)
            return len(self.bot._role_tasks)

        self.assertEqual(asyncio.run(scenario()), 0)


if __name__ == "__main__":
    unittest.main()