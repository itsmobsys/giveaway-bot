"""Bot-level tests that need no gateway connection.

GiveawayBot is a plain object until it is started, so the pieces that used to
be pure event-loop footguns -- the in-memory message counter, the autocomplete
snapshot, the entrants-role task bookkeeping -- can be driven directly here.
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import discord

from giveaway_bot import views
from giveaway_bot.bot import GiveawayBot, amain, build_intents, wire_commands
from giveaway_bot.config import Settings
from giveaway_bot.db import Database
from giveaway_bot.service import GiveawayService, PartialFlush


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
        # views._HANDLERS is module-global: leaving this bot bound there would
        # let a later test dispatch a click into a torn-down database.
        views._HANDLERS.clear()
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
        "giveaway_timeout_bans",
        "skipclaim",
    )

    def test_every_command_is_registered_once(self) -> None:
        names = sorted(command.name for command in self.bot.tree.get_commands())
        self.assertEqual(names, list(self.EXPECTED))

    def test_an_error_handler_is_installed(self) -> None:
        # CommandTree already defines on_error, so callable() proves nothing:
        # @tree.error is what writes the handler into the instance.
        self.assertIn("on_error", self.bot.tree.__dict__)

    def test_the_error_handler_answers_without_leaking_the_error(self) -> None:
        handler = self.bot.tree.__dict__["on_error"]
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user())
        with self.assertLogs("giveaway_bot", level="ERROR"):
            asyncio.run(handler(interaction, RuntimeError("boom")))
        self.assertEqual(interaction.texts, ["⚠️ /? failed. Please try again."])

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

    def test_a_partial_flush_hands_back_only_what_did_not_land(self) -> None:
        for _ in range(3):
            asyncio.run(self.bot.on_message(fake_message(self.guild, "3" * 18)))
        asyncio.run(self.bot.on_message(fake_message(self.guild, "4" * 18)))
        original = self.bot.service.add_message_counts

        def half_written(rows):
            # One of the two buffered users made it; the other did not.
            raise PartialFlush(applied=1, error=OSError("turso down"))

        self.bot.service.add_message_counts = half_written
        with self.assertRaises(PartialFlush):
            asyncio.run(self.bot._flush_message_buffer())
        self.bot.service.add_message_counts = original
        self.assertEqual(
            self.bot._message_buffer,
            {(self.guild, "4" * 18): 1},
            "only the tail goes back: the committed head must not be counted twice",
        )
        asyncio.run(self.bot._flush_message_buffer())
        self.assertEqual(self.svc.message_count(self.guild, "4" * 18), 1)

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
        async def scenario() -> tuple[int, int, int]:
            self.bot._schedule_role_delete(self.guild, "123", "gw_a", delay=60)
            self.bot._schedule_role_delete(self.guild, "123", "gw_a", delay=60)
            self.bot._schedule_role_delete(self.guild, "123", "gw_b", delay=60)
            held = len(self.bot._role_tasks)
            queued = len(self.bot._scheduled_role_deletes)
            for task in list(self.bot._role_tasks):
                task.cancel()
            await asyncio.gather(*list(self.bot._role_tasks), return_exceptions=True)
            return held, queued, len(self.bot._scheduled_role_deletes)

        held, queued, left = asyncio.run(scenario())
        self.assertEqual(held, 2, "one task per giveaway, the duplicate is dropped")
        self.assertEqual(queued, 2, "both giveaways are marked as scheduled")
        self.assertEqual(left, 0, "and released again once the tasks finish")

    def test_incomplete_targets_are_ignored(self) -> None:
        async def scenario() -> int:
            self.bot._schedule_role_delete("", "1", "gw_x", delay=1)
            self.bot._schedule_role_delete(self.guild, "", "gw_y", delay=1)
            return len(self.bot._role_tasks)

        self.assertEqual(asyncio.run(scenario()), 0)


class FakeMember(discord.Member):
    """A Member with no gateway behind it: only what the join path reads."""

    def __init__(self, user_id: int, *, name: str = "tester", timed_out: bool = False) -> None:
        # Member.id / .name / .global_name are read-only properties that read
        # straight through to _user (the flatten_user decorator), so the stub
        # user is where identity has to come from.
        self._user = SimpleNamespace(
            id=user_id,
            name=name,
            global_name=None,
            bot=False,
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
        self.nick = None
        self._roles: dict = {}
        # Member.roles walks the guild's roles, so that stub keeps it offline.
        self.guild = SimpleNamespace(get_role=lambda role_id: None, default_role=None)
        self._fake_timed_out = timed_out

    def is_timed_out(self) -> bool:
        return self._fake_timed_out


class FakeGuild:
    """Just enough guild for the member cache and the id checks."""

    def __init__(self, guild_id: str = "1" * 18, members: list | None = None) -> None:
        self.id = int(guild_id)
        self._members = {member.id: member for member in (members or [])}

    @property
    def members(self) -> list:
        return list(self._members.values())

    def get_member(self, user_id: int):
        return self._members.get(user_id)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.sent)

    async def defer(self, **kwargs) -> None:
        self.deferred = True

    async def send_message(self, content=None, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


class FakeDiscordMessage:
    def __init__(self, message_id: int = 123456789012345678) -> None:
        self.id = message_id
        self.jump_url = "https://discord.test/giveaway"
        self.edits: list[dict] = []
        self.replies: list[dict] = []
        self.edit_fails = False
        self.reply_fails = False

    async def edit(self, **kwargs) -> None:
        if self.edit_fails:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="error"), "edit failed")
        self.edits.append(kwargs)

    async def reply(self, content=None, **kwargs) -> None:
        if self.reply_fails:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="error"), "reply failed")
        self.replies.append({"content": content, **kwargs})


class FakeTextChannel(discord.TextChannel):
    """Only the HTTP operations, without constructing a Discord guild/client."""

    def __init__(self) -> None:
        self.id = int("2" * 18)
        self.message = FakeDiscordMessage()
        self.sent: list[dict] = []

    def get_partial_message(self, message_id: int) -> FakeDiscordMessage:
        return self.message

    async def fetch_message(self, message_id: int) -> FakeDiscordMessage:
        return self.message

    async def send(self, content=None, **kwargs) -> FakeDiscordMessage:
        self.sent.append({"content": content, **kwargs})
        return self.message


class FakeInteraction:
    """The parts of an Interaction the join path and the commands touch."""

    def __init__(self, *, guild: FakeGuild, user: object) -> None:
        self.guild = guild
        self.user = user
        # Real interactions carry the command that produced them (or None) and
        # the guild id, and the error handler reads both for its log line.
        self.command = None
        self.guild_id = guild.id
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    @property
    def replies(self) -> list[dict]:
        """Everything sent, whichever channel the command used to answer.

        Commands that defer answer through the followup, so the tests read this
        instead of assuming which one holds the reply.
        """
        return self.response.sent + self.followup.sent

    @property
    def texts(self) -> list[str]:
        return [m["content"] for m in self.replies]


def fake_user(*, manage_guild: bool = False, administrator: bool = False):
    """A command invoker: only the permission bits _can_manage reads."""
    return SimpleNamespace(
        id=999,
        guild_permissions=SimpleNamespace(
            manage_guild=manage_guild, administrator=administrator
        ),
    )


class TimeoutDetectionTests(unittest.TestCase):
    """Discord's own timeout state is the only source of truth."""

    def test_the_library_timeout_state_is_used(self) -> None:
        self.assertTrue(GiveawayBot._timed_out(SimpleNamespace(is_timed_out=lambda: True)))
        self.assertFalse(GiveawayBot._timed_out(SimpleNamespace(is_timed_out=lambda: False)))

    def test_an_object_without_the_api_is_not_timed_out(self) -> None:
        self.assertFalse(GiveawayBot._timed_out(SimpleNamespace(id=1)))
        self.assertFalse(GiveawayBot._timed_out(SimpleNamespace(is_timed_out=None)))

    def test_an_unreadable_timeout_never_penalises(self) -> None:
        def boom() -> bool:
            raise RuntimeError("member went away")

        self.assertFalse(GiveawayBot._timed_out(SimpleNamespace(is_timed_out=boom)))


class JoinTimeoutTests(BotTestCase):
    """The Join button's half of the rule: real bot, real database, no gateway."""

    def press_join(self, giveaway_id: str, member: FakeMember) -> FakeInteraction:
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_join(interaction, giveaway_id))
        return interaction

    def test_a_timed_out_member_cannot_enter_and_is_penalised(self) -> None:
        gw = self.make()
        member = FakeMember(333333333333333333, timed_out=True)
        interaction = self.press_join(gw.id, member)
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, str(member.id)), 3)
        self.assertTrue(any("timed out" in text for text in interaction.texts))

    def test_an_untimed_out_member_enters_as_usual(self) -> None:
        gw = self.make()
        member = FakeMember(444444444444444444)
        interaction = self.press_join(gw.id, member)
        self.assertEqual(self.svc.entry_count(gw.id), 1)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, str(member.id)), 0)
        self.assertTrue(any("You're in!" in text for text in interaction.texts))

    def test_a_dropped_entry_loses_roles_in_every_active_giveaway(self) -> None:
        gw = self.make()
        other = self.make(prize="other active giveaway")
        foreign = self.make(guild_id="3" * 18)
        member = FakeMember(666666666666666666, timed_out=True)
        for active in (gw, other, foreign):
            self.svc.join(
                active, user_id=str(member.id), username="member", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        calls: list[tuple[str, str]] = []

        async def fake_take(giveaway, user_id: str) -> None:
            calls.append((giveaway.id, user_id))

        self.bot._take_entrants_role = fake_take
        self.press_join(gw.id, member)
        self.assertEqual(
            set(calls), {(gw.id, str(member.id)), (other.id, str(member.id))},
            "the service removed entries across this guild, so both roles go",
        )
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertEqual(self.svc.entry_count(other.id), 0)
        self.assertEqual(self.svc.entry_count(foreign.id), 1)

    def test_an_ordinary_refusal_leaves_roles_alone(self) -> None:
        gw = self.make()
        member = FakeMember(777777777777777777)
        self.press_join(gw.id, member)  # enters normally first
        calls: list[tuple[str, str]] = []

        async def fake_take(giveaway, user_id: str) -> None:
            calls.append((giveaway.id, user_id))

        self.bot._take_entrants_role = fake_take
        interaction = self.press_join(gw.id, member)
        self.assertTrue(any("already entered" in text for text in interaction.texts))
        self.assertEqual(calls, [], "a plain refusal must not strip anything")

    def test_a_penalised_member_is_refused_by_the_button(self) -> None:
        first = self.make(prize="trigger")
        member = FakeMember(555555555555555555, timed_out=True)
        self.press_join(first.id, member)
        second = self.make(prize="next")
        member._fake_timed_out = False  # the timeout ended; the penalty has not
        interaction = self.press_join(second.id, member)
        self.assertEqual(self.svc.entry_count(second.id), 0)
        self.assertEqual(self.svc.timeout_ban_remaining(self.guild, str(member.id)), 2)
        self.assertTrue(any("penalty" in text for text in interaction.texts))


class GiveawayListTests(BotTestCase):
    """The entrant listing prints one bounded page but counts everybody."""

    def run_command(self, interaction: FakeInteraction, giveaway_id=None) -> None:
        command = self.bot.tree.get_command("giveaway_list")
        self.assertIsNotNone(command, "the command must be registered")
        asyncio.run(command.callback(interaction, giveaway_id))

    def test_the_page_is_capped_while_the_header_counts_everyone(self) -> None:
        gw = self.make()
        for index in range(60):
            self.svc.join(
                gw,
                user_id=f"{index + 1:0>18}",
                username=f"user{index}",
                member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user())
        self.run_command(interaction, gw.id)
        text = interaction.followup.sent[-1]["content"]
        self.assertIn("**60** entrant(s)", text)
        self.assertIn("…and 10 more.", text)
        self.assertEqual(text.count("<@"), 51, "50 entrants listed, plus the host mention")

    def test_no_entrants_says_so(self) -> None:
        gw = self.make()
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user())
        self.run_command(interaction, gw.id)
        self.assertIn("no entrants yet", interaction.followup.sent[-1]["content"])


class BlacklistListTests(BotTestCase):
    """The blocked-user listing has to survive a long blacklist."""

    def run_command(self, interaction: FakeInteraction) -> None:
        command = self.bot.tree.get_command("giveaway_blacklist_list")
        self.assertIsNotNone(command, "the command must be registered")
        asyncio.run(command.callback(interaction))

    def test_the_body_fits_and_the_header_counts_the_whole_list(self) -> None:
        for index in range(90):
            self.svc.blacklist_add(self.guild, f"{index + 1:0>18}")
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )
        self.run_command(interaction)
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.sent, [])
        content = interaction.followup.sent[0]["content"]
        self.assertLessEqual(len(content), 2000, "Discord rejects anything longer")
        self.assertIn("Blocked (90)", content)
        self.assertIn("…plus 10 more.", content)
        self.assertEqual(content.count("<@"), 80)

    def test_slow_blacklist_queries_run_only_after_defer_and_errors_use_followup(self) -> None:
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )

        def slow_failure(guild_id):
            self.assertTrue(interaction.response.deferred)
            raise OSError("turso down")

        self.bot.service.blacklist_list = slow_failure
        with self.assertLogs("giveaway_bot", level="ERROR"):
            self.run_command(interaction)
        self.assertEqual(interaction.response.sent, [])
        self.assertIn("Could not load the blacklist", interaction.followup.sent[0]["content"])


class TimeoutBanCommandTests(BotTestCase):
    """The /giveaway_timeout_bans listing: who is sitting out, and only who."""

    def run_command(self, interaction: FakeInteraction) -> None:
        command = self.bot.tree.get_command("giveaway_timeout_bans")
        self.assertIsNotNone(command, "the command must be registered")
        asyncio.run(command.callback(interaction))

    def banned(self, user_id: str, remaining: int = 3) -> None:
        self.svc.apply_timeout_penalty(
            self.guild, user_id, giveaways=remaining, giveaway_id="gw_x"
        )

    def test_lists_active_bans_with_mentions_and_remaining(self) -> None:
        first, second = "1" * 18, "2" * 18
        self.banned(first, 3)
        self.banned(second, 1)
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild, [FakeMember(int(first)), FakeMember(int(second))]),
            user=fake_user(manage_guild=True),
        )
        self.run_command(interaction)
        sent = interaction.replies[0]
        self.assertTrue(sent["ephemeral"])
        self.assertIsInstance(sent["view"], views.ParticipantsPages)
        desc = sent["embed"].description
        self.assertIn(f"<@{first}>", desc)
        self.assertIn(f"<@{second}>", desc)
        self.assertIn("**3**", desc)
        self.assertIn("**1**", desc)
        self.assertLess(desc.index(first), desc.index(second), "longest penalty first")
        self.assertIn("Total: **2**", desc)

    def test_users_with_no_giveaways_left_are_excluded(self) -> None:
        self.db.execute(
            "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining)"
            " VALUES (?, ?, 0)",
            (self.guild, "3" * 18),
        )
        self.banned("4" * 18)
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user(administrator=True))
        self.run_command(interaction)
        desc = interaction.replies[0]["embed"].description
        self.assertNotIn("3" * 18, desc)
        self.assertIn("4" * 18, desc)

    def test_an_empty_list_says_so(self) -> None:
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )
        self.run_command(interaction)
        self.assertEqual(interaction.texts, ["No users are currently banned from giveaways."])
        self.assertNotIn("embed", interaction.replies[0])

    def test_unauthorised_members_are_denied(self) -> None:
        self.banned("1" * 18)
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user())
        self.run_command(interaction)
        self.assertEqual(interaction.texts, ["You need **Manage Server**."])
        self.assertNotIn("embed", interaction.replies[0])
        self.assertNotIn("1" * 18, str(interaction.texts), "no data for non-moderators")

    def test_a_database_blip_is_reported_not_raised(self) -> None:
        self.banned("1" * 18)

        def boom(*args, **kwargs):
            raise OSError("turso down")

        self.bot.service.list_timeout_bans = boom
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )
        self.run_command(interaction)
        self.assertEqual(interaction.texts, ["⚠️ Could not load the timeout bans. Try again."])

    def test_a_member_who_left_is_shown_by_id(self) -> None:
        gone = "9" * 18
        self.banned(gone)
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild, [FakeMember(111111111111111111)]),
            user=fake_user(manage_guild=True),
        )
        self.run_command(interaction)
        desc = interaction.replies[0]["embed"].description
        self.assertIn("`" + gone + "` (left the server)", desc)
        self.assertNotIn(f"<@{gone}>", desc)

    def test_an_unnumbered_id_does_not_break_the_list(self) -> None:
        self.banned("not-a-snowflake")
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild, [FakeMember(111111111111111111)]),
            user=fake_user(manage_guild=True),
        )
        self.run_command(interaction)
        self.assertIn("not-a-snowflake", interaction.replies[0]["embed"].description)

    def test_a_capped_list_admits_how_many_it_hid(self) -> None:
        for index in range(5):
            self.banned(f"{index + 1:0>18}")
        # Shrink the one-response limit instead of seeding 101 penalties: what
        # is under test is that the embed reports the real total, not the page.
        self.bot.service.list_timeout_bans = (
            lambda guild_id, limit=100: self.svc.list_timeout_bans(guild_id, limit=3)
        )
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )
        self.run_command(interaction)
        desc = interaction.replies[0]["embed"].description
        self.assertIn("Total: **5**", desc)
        self.assertIn("2 more not shown.", desc)

    def test_long_lists_are_paginated(self) -> None:
        for index in range(11):
            self.banned(f"{index + 1:0>18}", remaining=3 - index % 3)
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user(manage_guild=True))
        self.run_command(interaction)
        sent = interaction.replies[0]
        self.assertIn("page 1/2", sent["embed"].title)
        self.assertIn("Total: **11**", sent["embed"].description)
        self.assertIsInstance(sent["view"], views.ParticipantsPages)


class RoleCleanupTests(BotTestCase):
    """The delayed entrants-role delete, actually driven instead of scheduled."""

    @staticmethod
    async def run_pending(bot) -> None:
        await asyncio.gather(*list(bot._role_tasks), return_exceptions=True)

    def test_the_task_deletes_the_role_and_clears_the_row(self) -> None:
        deleted: list[str] = []
        cleared: list[str] = []

        class FakeRole:
            async def delete(self, *, reason=None) -> None:
                deleted.append(reason or "")

        class FakeGuild:
            def get_role(self, role_id):
                return FakeRole() if role_id == 123 else None

        self.bot.get_guild = lambda guild_id: FakeGuild()
        self.bot.service.set_entrants_role = lambda gid, rid: cleared.append(str(rid))

        async def scenario() -> None:
            self.bot._schedule_role_delete(self.guild, "123", "gw_a", delay=0)
            await self.run_pending(self.bot)

        asyncio.run(scenario())
        self.assertEqual(len(deleted), 1, "the role is deleted after the delay")
        self.assertEqual(cleared, ["None"], "the row is cleared so the sweep stops")
        self.assertEqual(self.bot._scheduled_role_deletes, set())

    def test_an_unreachable_guild_still_clears_the_row(self) -> None:
        cleared: list[str] = []
        self.bot.get_guild = lambda guild_id: None
        self.bot.service.set_entrants_role = lambda gid, rid: cleared.append(str(rid))

        async def scenario() -> None:
            self.bot._schedule_role_delete(self.guild, "123", "gw_b", delay=0)
            await self.run_pending(self.bot)

        asyncio.run(scenario())
        # Otherwise the restart sweep re-queues this giveaway on every boot, for
        # a role it can never reach.
        self.assertEqual(cleared, ["None"])
        self.assertEqual(self.bot._scheduled_role_deletes, set())


class LeaveTests(BotTestCase):
    """The Leave button: the entry goes, and a database blip is reported."""

    def press_leave(self, giveaway_id: str, member: FakeMember) -> FakeInteraction:
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_leave(interaction, giveaway_id))
        return interaction

    def join(self, gw, user_id: str) -> int:
        return self.svc.join(
            gw,
            user_id=user_id,
            username="member",
            member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
        )

    def test_leave_removes_the_entry(self) -> None:
        gw = self.make()
        member = FakeMember(888888888888888888)
        self.join(gw, str(member.id))
        interaction = self.press_leave(gw.id, member)
        self.assertEqual(self.svc.entry_count(gw.id), 0)
        self.assertIn("You left the giveaway.", interaction.texts)

    def test_a_failing_leave_is_reported_not_raised(self) -> None:
        gw = self.make()
        member = FakeMember(999999999999999999)
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)

        def boom(*args, **kwargs):
            raise OSError("turso down")

        self.bot.service.leave = boom
        asyncio.run(self.bot.handle_leave(interaction, gw.id))
        # This used to escape the button callback: a bare "interaction failed"
        # with nothing tying the error to the giveaway.
        self.assertIn("Could not update your entry", interaction.texts[0])


class ParticipantsPanelTests(BotTestCase):
    def test_the_page_is_bounded_and_finds_me_by_lookup(self) -> None:
        gw = self.make()
        member = FakeMember(222222222222222222)
        for index in range(60):
            self.svc.join(
                gw,
                user_id=f"{index + 1:0>18}",
                username="member",
                member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        self.svc.join(
            gw,
            user_id=str(member.id),
            username="me",
            member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
        )
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_participants(interaction, gw.id))
        embed = interaction.followup.sent[-1]["embed"]
        self.assertIn("Total Participants: 61", embed.description)
        self.assertIn("Your Entries: 1", embed.description, "answered by a lookup")
        self.assertNotIn("Showing the first", embed.description, "61 fits the window")

    def test_a_giveaway_bigger_than_the_window_says_so(self) -> None:
        gw = self.make()
        member = FakeMember(222222222222222222)
        self.bot.PARTICIPANT_WINDOW = 5
        for index in range(8):
            self.svc.join(
                gw,
                user_id=f"{index + 1:0>18}",
                username="member",
                member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_participants(interaction, gw.id))
        embed = interaction.followup.sent[-1]["embed"]
        self.assertIn("Total Participants: 8", embed.description)
        self.assertIn("Showing the first 5 of 8.", embed.description)
        self.assertIn("page 1/1", embed.title)


class GiveawayLifecycleTests(BotTestCase):
    def test_ended_or_cancelled_giveaway_never_refreshes_live_buttons(self) -> None:
        channel = FakeTextChannel()
        self.bot.get_channel = lambda channel_id: channel
        ended = self.make(prize="ended")
        cancelled = self.make(prize="cancelled")
        active = self.make(prize="active")
        for gw in (ended, cancelled, active):
            self.svc.set_message(gw.id, str(channel.message.id))
        self.svc.end(ended.id)
        self.svc.cancel(cancelled.id)
        for gw in (ended, cancelled):
            asyncio.run(self.bot._refresh_embed(self.svc.get(gw.id)))
        self.assertEqual(channel.message.edits, [])
        asyncio.run(self.bot._refresh_embed(self.svc.get(active.id)))
        self.assertIsInstance(channel.message.edits[0]["view"], views.GiveawayView)

    def test_join_race_with_draw_does_not_restore_the_live_embed(self) -> None:
        gw = self.make()
        channel = FakeTextChannel()
        self.svc.set_message(gw.id, str(channel.message.id))
        self.bot.get_channel = lambda channel_id: channel
        original_join = self.bot.service.join

        def join_then_end(*args, **kwargs):
            count = original_join(*args, **kwargs)
            self.svc.end(gw.id)
            return count

        self.bot.service.join = join_then_end
        member = FakeMember(888888888888888888)
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_join(interaction, gw.id))
        self.assertIn("You're in!", interaction.texts[0])
        self.assertEqual(channel.message.edits, [])

    def test_leave_race_with_cancel_does_not_restore_the_live_embed(self) -> None:
        gw = self.make()
        channel = FakeTextChannel()
        self.svc.set_message(gw.id, str(channel.message.id))
        self.bot.get_channel = lambda channel_id: channel
        member = FakeMember(888888888888888888)
        self.svc.join(
            gw, user_id=str(member.id), username="entrant", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
        )
        original_leave = self.bot.service.leave

        def leave_then_cancel(*args):
            removed = original_leave(*args)
            self.svc.cancel(gw.id)
            return removed

        self.bot.service.leave = leave_then_cancel
        interaction = FakeInteraction(guild=FakeGuild(self.guild, [member]), user=member)
        asyncio.run(self.bot.handle_leave(interaction, gw.id))
        self.assertIn("You left the giveaway.", interaction.texts)
        self.assertEqual(channel.message.edits, [])

    def test_tick_continues_after_announcement_and_role_strip_failures(self) -> None:
        first, second = self.make(), self.make()
        self.bot.service.due = lambda: [first, second]
        self.bot.service.end = lambda gw_id: (self.svc.get(gw_id), [])
        calls: list[tuple[str, str]] = []

        async def announce(gw, winners) -> None:
            calls.append(("announce", gw.id))
            if gw.id == first.id:
                raise RuntimeError("send failed")

        async def strip(gw) -> None:
            calls.append(("strip", gw.id))
            if gw.id == first.id:
                raise RuntimeError("role failed")

        self.bot._announce = announce
        self.bot._strip_entrants_role = strip
        with self.assertLogs("giveaway_bot", level="ERROR"):
            asyncio.run(self.bot.tick.coro(self.bot))
        self.assertEqual(calls, [
            ("announce", first.id), ("strip", first.id),
            ("announce", second.id), ("strip", second.id),
        ])

    def test_tick_error_handler_logs_and_restarts_loop(self) -> None:
        with (patch.object(self.bot.tick, "restart") as restart,
              self.assertLogs("giveaway_bot", level="ERROR") as logs):
            asyncio.run(self.bot._tick_error(RuntimeError("unhandled")))
        restart.assert_called_once_with()
        self.assertIn("tick loop crashed", logs.output[0])


class AnnouncementTests(BotTestCase):
    def channel_for(self, gw) -> FakeTextChannel:
        channel = FakeTextChannel()
        self.bot.get_channel = lambda channel_id: channel
        self.svc.set_message(gw.id, str(channel.message.id))
        return channel

    def test_a_failed_reply_after_edit_does_not_duplicate_winner_embed(self) -> None:
        gw = self.make()
        channel = self.channel_for(gw)
        channel.message.reply_fails = True
        with self.assertLogs("giveaway_bot", level="WARNING"):
            asyncio.run(self.bot._announce(self.svc.get(gw.id), ["4" * 18]))
        self.assertEqual(len(channel.message.edits), 1)
        self.assertEqual(channel.sent, [], "the original was already edited")

    def test_a_failed_edit_falls_back_once_with_explicit_mention_policy(self) -> None:
        gw = self.make(prize="@everyone")
        channel = self.channel_for(gw)
        channel.message.edit_fails = True
        asyncio.run(self.bot._announce(self.svc.get(gw.id), ["4" * 18]))
        self.assertEqual(len(channel.sent), 1)
        mentions = channel.sent[0]["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertTrue(mentions.users)
        self.assertTrue(mentions.roles)

    def test_roleless_ping_is_capped_and_says_what_it_missed(self) -> None:
        from giveaway_bot import bot as bot_module

        gw = self.make(prize="big")
        for index in range(600):
            self.svc.join(
                gw,
                user_id=f"{index + 1:0>18}",
                username="entrant",
                member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        channel = FakeTextChannel()
        interaction = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )
        interaction.channel = channel
        self.bot._role_for = lambda _gw: None
        command = self.bot.tree.get_command("giveaway_ping")
        asyncio.run(command.callback(interaction, gw.id, None))
        self.assertLessEqual(len(channel.sent), bot_module.MAX_MENTION_MESSAGES)
        self.assertIn("capped", interaction.texts[-1])
        self.assertIn("600", interaction.texts[-1])
        # A small giveaway still pings everybody with no cap notice.
        small = self.make(prize="small")
        self.svc.join(
            small, user_id="4" * 18, username="entrant", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
        )
        interaction2 = FakeInteraction(
            guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
        )
        interaction2.channel = channel
        asyncio.run(command.callback(interaction2, small.id, None))
        self.assertIn("Pinged 1 entrant", interaction2.texts[-1])

    def test_reroll_posts_fresh_winner_without_editing_original_result(self) -> None:
        gw = self.make()
        channel = self.channel_for(gw)
        for uid in ("4" * 18, "5" * 18):
            self.svc.join(
                gw, user_id=uid, username="entrant", member_roles=[],
                account_created_ts=time.time() - 400 * 86400,
            )
        _, originals = self.svc.end(gw.id)
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user(manage_guild=True))
        command = self.bot.tree.get_command("giveaway_reroll")
        asyncio.run(command.callback(interaction, gw.id))
        self.assertEqual(channel.message.edits, [])
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(channel.sent[0]["embed"].title, "Giveaway Rerolled")
        self.assertNotIn(f"<@{originals[0]}>", channel.sent[0]["embed"].description)

    def test_cancel_replaces_live_embed_and_removes_buttons(self) -> None:
        gw = self.make(prize="@everyone")
        channel = self.channel_for(gw)

        async def strip(_gw) -> None:
            pass

        self.bot._strip_entrants_role = strip
        interaction = FakeInteraction(guild=FakeGuild(self.guild), user=fake_user(manage_guild=True))
        command = self.bot.tree.get_command("giveaway_cancel")
        asyncio.run(command.callback(interaction, gw.id))
        self.assertFalse(self.svc.get(gw.id).active)
        self.assertEqual(channel.message.edits[0]["view"], None)
        self.assertEqual(channel.message.edits[0]["embed"].title, "Giveaway Cancelled")
        self.assertFalse(channel.sent[0]["allowed_mentions"].everyone)

    def test_ping_and_extend_disable_everyone_in_both_mention_modes(self) -> None:
        gw = self.make(prize="@everyone")
        self.svc.join(
            gw, user_id="4" * 18, username="entrant", member_roles=[],
            account_created_ts=time.time() - 400 * 86400,
        )
        channel = FakeTextChannel()
        self.bot.get_channel = lambda channel_id: channel
        for role in (SimpleNamespace(mention="<@&123>"), None):
            self.bot._role_for = lambda _gw, role=role: role
            for command_name in ("giveaway_ping", "giveaway_extend"):
                interaction = FakeInteraction(
                    guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
                )
                interaction.channel = channel
                command = self.bot.tree.get_command(command_name)
                if command_name == "giveaway_ping":
                    asyncio.run(command.callback(interaction, gw.id, "@everyone"))
                else:
                    asyncio.run(command.callback(interaction, gw.id, 1))
                mentions = channel.sent[-1]["allowed_mentions"]
                self.assertFalse(mentions.everyone)
                self.assertEqual(mentions.roles, bool(role))
                self.assertEqual(mentions.users, not bool(role))


class CreatePersistenceTests(BotTestCase):
    def test_posted_giveaway_still_reports_success_when_message_or_role_save_fails(self) -> None:
        channel = FakeTextChannel()
        self.bot._target_channel = lambda interaction: channel

        async def create_role(**kwargs):
            return SimpleNamespace(id=777)

        for failing_step in ("set_message", "set_entrants_role"):
            interaction = FakeInteraction(
                guild=FakeGuild(self.guild), user=fake_user(manage_guild=True)
            )
            interaction.guild.create_role = create_role
            interaction.user.display_name = "admin"

            def fail(*args):
                raise OSError("turso down")

            original = getattr(self.bot.service, failing_step)
            setattr(self.bot.service, failing_step, fail)
            try:
                command = self.bot.tree.get_command("giveaway_create")
                with self.assertLogs("giveaway_bot", level="ERROR"):
                    asyncio.run(command.callback(interaction, "@everyone"))
            finally:
                setattr(self.bot.service, failing_step, original)
            self.assertTrue(interaction.response.deferred)
            self.assertIn("Giveaway started:", interaction.texts[-1])
            self.assertEqual(len(self.svc.list_active(self.guild)), len(channel.sent))
            self.assertFalse(channel.sent[-1]["allowed_mentions"].everyone)
            self.assertFalse(channel.sent[-1]["allowed_mentions"].users)


class GracefulShutdownTests(unittest.TestCase):
    def test_sigterm_closes_bot_and_awaits_its_buffer_flush(self) -> None:
        async def scenario(windows: bool) -> list[str]:
            events: list[str] = []
            loop = asyncio.get_running_loop()
            handler = None

            def add_handler(kind, callback):
                nonlocal handler
                self.assertEqual(kind, signal.SIGTERM)
                if windows:
                    raise NotImplementedError
                handler = callback

            def fallback_handler(kind, callback):
                nonlocal handler
                self.assertEqual(kind, signal.SIGTERM)
                def invoke_handler() -> None:
                    callback(kind, None)

                handler = invoke_handler

            class FakeBot:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def start(self, token):
                    handler()
                    await asyncio.sleep(0)

                async def close(self):
                    await asyncio.sleep(0)
                    events.append("buffer flushed")

            fake_db = SimpleNamespace(init_schema=lambda: None)
            with (patch("giveaway_bot.health.start_health_server"),
                  patch("giveaway_bot.bot.Database", return_value=fake_db),
                  patch("giveaway_bot.bot.GiveawayBot", return_value=FakeBot()),
                  patch("giveaway_bot.bot.wire_commands"),
                  patch.object(loop, "add_signal_handler", side_effect=add_handler),
                  patch("giveaway_bot.bot.signal.signal", side_effect=fallback_handler)):
                await amain(Settings(bot_token=uuid.uuid4().hex, turso_url=""))
            return events

        for windows in (False, True):
            self.assertEqual(asyncio.run(scenario(windows)), ["buffer flushed"])


if __name__ == "__main__":
    unittest.main()
