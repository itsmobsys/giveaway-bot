"""Slash commands for both members and moderators.

Command surface:

``/giveaway create``      start a giveaway (Manage Server)
``/giveaway list``        list giveaways in this server (Manage Server)
``/giveaway info``        show a giveaway with its fairness commitment
``/giveaway participants`` participant counts (Manage Server)
``/giveaway history``     past draws + winners (Manage Server)
``/giveaway verify``      re-verify the last draw locally (Manage Server)
``/giveaway reveal``      post the revealed seed (Manage Server)
``/giveaway pause|resume|extend|shorten|end|reroll``  lifecycle (Manage Server)
``/giveaway join|leave``   enter/withdraw from Discord without the button
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .. import embeds
from ..repositories import draws as draws_repo
from ..repositories import entries as entries_repo
from ..repositories import giveaways as gw_repo
from ..service import ServiceError

log = logging.getLogger("giveaway_bot.cogs.giveaways")


class GiveawayCommands(commands.Cog, name="giveaway"):
    """User- and moderator-facing slash commands."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # -------------------------------------------------------------- helpers
    async def _require_manage(self, interaction: discord.Interaction) -> bool:
        return await self.bot._assert_manage(interaction)  # noqa: SLF001 - same package

    async def _find_giveaway(self, guild_id: str, identifier: str) -> Any | None:
        """Accept either a giveaway id or a message id."""
        identifier = identifier.strip()
        if identifier.startswith("gw_"):
            # Guild-scoped: /giveaway join accepts a global giveaway id, and this
            # builds the member context from `interaction.guild`. Without the check
            # guild B's rules (min_guild_join_days and the rest) would be applied
            # to a member of guild A and admitted into B's prize pool.
            record = gw_repo.get_giveaway(self.bot.db, identifier)
            if record is not None and record.guild_id != str(guild_id):
                return None
            return record
        if identifier.isdigit():
            return gw_repo.get_by_message(self.bot.db, guild_id, identifier)
        return None

    # --------------------------------------------------------------- commands
    giveawaway = app_commands.Group(name="giveaway", description="Giveaway commands")

    @giveawaway.command(name="create", description="Create a giveaway")
    @app_commands.describe(
        title="Short title shown in the embed",
        prize="What the winner receives",
        duration="How long it runs, e.g. 30m, 12h, 3d (or a plain number of minutes)",
        winners="How many winners to draw",
        entries="Maximum entries per person (default 1)",
        description="Extra details shown in the embed",
        required_roles="Only members with one of these roles may enter (mention or ID, comma separated)",
        blacklist_roles="Members with any of these roles may not enter",
        entry_limit="Total entry cap for the whole giveaway (0 = unlimited)",
        min_account_age_days="Minimum account age in days",
        min_guild_join_days="Minimum time since joining this server, in days",
        require_all_roles="Require every listed role instead of any",
        min_messages="Messages a member must have sent to be eligible (0 = off)",
        message_channels="Only count messages in these channels (blank = whole server)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        prize: str,
        duration: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        entries: app_commands.Range[int, 1, 100] = 1,
        description: str = "",
        required_roles: str = "",
        blacklist_roles: str = "",
        entry_limit: app_commands.Range[int, 0, 1_000_000] = 0,
        min_account_age_days: app_commands.Range[int, 0, 3650] = 0,
        min_guild_join_days: app_commands.Range[int, 0, 3650] = 0,
        require_all_roles: bool = False,
        min_messages: app_commands.Range[int, 0, 100_000] = 0,
        message_channels: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        # The giveaway always lives in the bot's own channel, never wherever the
        # command happened to be run.
        from ..queue import _resolve_giveaway_channel

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("⚠️ This command only works inside a server.", ephemeral=True)
            return
        try:
            target = _resolve_giveaway_channel(self.bot, guild)
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return

        message_channel_ids = _parse_ids(message_channels)
        payload = {
            "title": title,
            "prize": prize,
            "description": description,
            "duration": duration,
            "winner_count": winners,
            "max_entries_per_user": entries,
            "required_role_ids": _parse_ids(required_roles),
            "blacklist_role_ids": _parse_ids(blacklist_roles),
            "required_mode": "all" if require_all_roles else "any",
            "entry_limit": entry_limit,
            "min_account_age_days": min_account_age_days,
            "min_guild_join_days": min_guild_join_days,
            "min_messages": min_messages,
            "message_count_channel_ids": message_channel_ids,
            "message_count_scope": "channel" if message_channel_ids else "guild",
        }

        try:
            giveaway = self.bot.service.create(
                self.bot.actor_for(interaction.user),
                guild_id=str(interaction.guild_id),
                channel_id=str(target.id),
                payload=payload,
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        except Exception as exc:  # ValidationError and friends
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        # Single-giveaway-per-guild: the entrants role is only meaningful for one
        # open giveaway at a time.
        existing = gw_repo.find_active(self.bot.db, str(interaction.guild_id))
        if existing is not None:
            await interaction.followup.send(
                f"⚠️ This server already has an active giveaway: **{existing['title']}** "
                f"(`{existing['id']}`).\nEnd it first with `/admin give end {existing['id']}`.",
                ephemeral=True,
            )
            return

        # Temporary entrants role, created lazily and reused across giveaways.
        giveaway = await self.bot.attach_entrants_role(giveaway)
        await self.bot.render_giveaway(giveaway, announce=True)
        fresh = self.bot.service.get(giveaway.id)
        # A new requirement means this guild now needs counting; recompute the
        # watched set so no message is missed from here on.
        self.bot.activity_tracker.refresh_requirements()
        role_line = ""
        if fresh.participant_role_id:
            role_line = (
                f"\nEntrants will receive <@&{fresh.participant_role_id}> so you can "
                "ping everyone at once — it is removed when the giveaway ends."
            )
        activity_line = ""
        if fresh.min_messages > 0:
            scope = (
                f"{len(fresh.message_count_channel_ids)} specific channel(s)"
                if fresh.message_count_scope == "channel"
                else "the whole server"
            )
            activity_line = f"\nRequires **{fresh.min_messages}** messages in {scope}."
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Giveaway created",
                description=(
                    f"**{fresh.title}**\n"
                    f"Entries close {embeds.format_timestamp(fresh.ends_at, style='R')}."
                    f"{activity_line}{role_line}\n"
                    f"Seed commitment published before entries open:\n`{fresh.seed_commitment}`\n"
                    f"<#{target.id}>"
                ),
                colour=0x10B981,
            ),
            ephemeral=True,
        )

    @giveawaway.command(name="join", description="Enter a giveaway with a command")
    @app_commands.describe(giveaway="Giveaway ID from the message footer")
    async def join(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        from ..views import member_context

        context = member_context(interaction.user, interaction.guild)
        context["channel_id"] = str(interaction.channel_id)
        outcome = self.bot.service.join(record, context)
        if not outcome.joined:
            message = outcome.eligibility.message if not outcome.duplicate else "You are already entered."
            await interaction.followup.send(f"🎟️ {message}", ephemeral=True)
            return
        await interaction.followup.send(
            f"🎟️ Entered **{record.title}** (entry {outcome.entry_seq}).", ephemeral=True
        )
        await self.bot._sync_live_embed(record.id)  # noqa: SLF001

    @giveawaway.command(name="leave", description="Leave a giveaway")
    @app_commands.describe(giveaway="Giveaway ID from the message footer")
    async def leave(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        try:
            removed = self.bot.service.leave(record, str(interaction.user.id))
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        await interaction.followup.send(
            "↩️ Entry removed." if removed else "You were not entered.", ephemeral=True
        )
        if removed:
            await self.bot._sync_live_embed(record.id)  # noqa: SLF001

    @giveawaway.command(name="list", description="List this server's giveaways")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_giveaways(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        records = gw_repo.list_for_guild(self.bot.db, str(interaction.guild_id), limit=15)
        if not records:
            await interaction.followup.send("No giveaways yet.", ephemeral=True)
            return
        lines = []
        for record in records:
            lines.append(
                f"`{record.id}` · **{record.title}** · {record.status.value} · "
                f"{record.participant_count} entries · id `{record.id}`"
            )
        await interaction.followup.send("\n".join(lines)[:1900] or "No giveaways yet.", ephemeral=True)

    @giveawaway.command(name="info", description="Show a giveaway and its fairness data")
    @app_commands.describe(giveaway="Giveaway ID")
    async def info(self, interaction: discord.Interaction, giveaway: str) -> None:
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        embed = self.bot._embed_for(record)  # noqa: SLF001
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @giveawaway.command(name="participants", description="Show who is entered (Manage Server)")
    @app_commands.describe(giveaway="Giveaway ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def participants(self, interaction: discord.Interaction, giveaway: str) -> None:
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        entries, participants = entries_repo.active_entry_totals(self.bot.db, record.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"🎟️ {record.title}",
                description=(
                    f"**{participants}** participant(s) · **{entries}** entr"
                    f"{'y' if entries == 1 else 'ies'}\n"
                    f"Entry limit per person: {record.max_entries_per_user}\n"
                    "The full participant list, with per-user eligibility validation, is on the dashboard."
                ),
                colour=0x7C5CFF,
            ),
            ephemeral=True,
        )

    @giveawaway.command(name="history", description="Winner history for a giveaway (Manage Server)")
    @app_commands.describe(giveaway="Giveaway ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def history(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        draws = draws_repo.list_draws(self.bot.db, record.id)
        winners = draws_repo.list_winners(self.bot.db, record.id)
        if not draws:
            await interaction.response.send_message("This giveaway has not been drawn yet.", ephemeral=True)
            return
        lines = []
        for draw in draws:
            names = [
                f"<@{winner.user_id}> (rank {winner.rank})"
                for winner in winners
                if winner.round == draw.round
            ]
            lines.append(
                f"**Round {draw.round}** · {draw.participant_count} participants · "
                f"{draw.winner_count} winner(s) · {embeds.format_timestamp(draw.created_at, style='R')}\n"
                + (", ".join(names) or "_no winners_")
            )
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"🗂 {record.title} · winner history",
                description="\n\n".join(lines)[:4000],
                colour=0x7C5CFF,
            ).set_footer(text=f"{len(draws)} draw(s) · all rounds are public and reproducible"),
            ephemeral=True,
        )

    @giveawaway.command(name="verify", description="Re-verify the last draw (Manage Server)")
    @app_commands.describe(giveaway="Giveaway ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def verify(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        verification = self.bot.service.verification_for(record.id)
        if verification is None:
            await interaction.followup.send("This giveaway has not been drawn yet.", ephemeral=True)
            return
        await self.bot.post_verify(record, verification)
        await interaction.followup.send(
            f"{'✅ Verified' if verification['ok'] else '⚠️ Verification reported differences'} — "
            f"{len(verification['checks'])} checks recomputed.",
            ephemeral=True,
        )

    @giveawaway.command(name="reveal", description="Post the revealed seed publicly (Manage Server)")
    @app_commands.describe(giveaway="Giveaway ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reveal(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._find_giveaway(str(interaction.guild_id), giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        if record.seed_revealed_at is None:
            await interaction.followup.send("This giveaway has not been drawn yet.", ephemeral=True)
            return
        await self.bot.post_verify(record, self.bot.service.verification_for(record.id))
        await interaction.followup.send("🔐 Seed revealed in this channel.", ephemeral=True)

    # ------------------------------------------------------------ autocomplete
    async def _giveaway_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest this guild's giveaways for a `giveaway` option.

        Two things were wrong. The callback returned plain strings, and
        discord.py builds the reply with ``[option.to_dict() for option in
        choices]``, so every keystroke raised ``AttributeError: 'str' object has
        no attribute 'to_dict'`` and Discord logged "Ignoring exception in
        autocomplete". And the value it offered was ``"<id> - <title>"``, which
        ``_find_giveaway`` looks up verbatim and so could never resolve - even a
        working suggestion would have failed.

        A Choice separates the displayed ``name`` from the submitted ``value``:
        the title is shown, the bare id is sent.
        """
        if interaction.guild_id is None:
            return []
        # A database read must never run on the gateway thread.
        records = await asyncio.to_thread(
            gw_repo.list_for_guild, self.bot.db, str(interaction.guild_id), limit=50
        )
        term = (current or "").strip().lower()
        return [
            app_commands.Choice(name=record.title[:100], value=record.id)
            for record in records
            if term in record.title.lower() or term in record.id
        ][:25]

    async def duration_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest a duration.

        Must return Choice objects, not strings: discord.py serialises each one
        with ``option.to_dict()``, so bare strings crash the callback.
        """
        return [
            app_commands.Choice(name=value, value=value)
            for value in ("30m", "1h", "6h", "12h", "24h", "3d", "7d")
            if current in value
        ][:25]

    async def cog_load(self) -> None:
        self._attach_autocomplete()
        log.info("giveaway commands loaded")

    def _attach_autocomplete(self) -> None:
        """Wire the autocomplete callbacks onto the commands that need them.

        ``autocomplete`` is a method on the Command object rather than an argument
        to its decorator, so it can only be attached once every command exists.
        It also cannot be done by bare name inside the class body: ``create``,
        ``join`` and friends are class attributes, and those names are not in
        scope inside a method, so this goes through ``getattr`` on the class.
        """
        cls = type(self)
        cls.create.autocomplete("duration")(self.duration_autocomplete)
        for name in ("join", "leave", "info", "participants", "history", "verify", "reveal"):
            getattr(cls, name).autocomplete("giveaway")(self._giveaway_autocomplete)


def _parse_ids(value: str) -> list[str]:
    """Accept ``<@&123>``, ``123``, ``123,456`` or space separated lists."""
    if not value:
        return []
    cleaned = re.sub(r"<@&(\d+)>", r"\1", value)
    return [item for item in re.split(r"[,\s]+", cleaned) if item.isdigit()]


__all__ = ["GiveawayCommands", "setup"]


async def setup(bot: Any) -> None:
    """Entry point required by ``Bot.load_extension``.

    Without this, ``setup_hook`` raised NoEntryPointError the moment the gateway
    connected - the class existed and was never registered.
    """
    await bot.add_cog(GiveawayCommands(bot))
