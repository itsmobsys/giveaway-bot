"""Standalone bot: slash commands + buttons + auto-draw timer. No dashboard."""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import embeds
from .config import Settings
from .db import Database
from .service import Giveaway, GiveawayService, ServiceError
from .views import GiveawayView

log = logging.getLogger("giveaway_bot")
_SNOWFLAKE = re.compile(r"^\d{15,25}$")


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True  # needed to read roles for requirements
    return intents


def _can_manage(member: object) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.manage_guild or perms.administrator)


class GiveawayBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        super().__init__(command_prefix="!", intents=build_intents(), help_command=None)
        self.settings = settings
        self.db = db
        self.service = GiveawayService(db)
        self._views: dict[str, GiveawayView] = {}

    # -- lifecycle ------------------------------------------------------
    async def setup_hook(self) -> None:
        self.add_view(GiveawayView("placeholder"))
        await self.tree.sync()
        log.info("commands synced (%d)", len(self.tree.get_commands()))
        self.tick.start()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%d guilds)", self.user, len(self.guilds))
        for gw in self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active' LIMIT 200",
        ):
            self._register_view(gw["id"])

    async def close(self) -> None:
        try:
            self.tick.cancel()
        except Exception:
            pass
        await super().close()
        self.db.close()

    def _register_view(self, giveaway_id: str) -> GiveawayView:
        view = self._views.get(giveaway_id)
        if view is None:
            view = GiveawayView(giveaway_id)
            view._join_handler = self.handle_join
            view._leave_handler = self.handle_leave
            self._views[giveaway_id] = view
            self.add_view(view)
        return view

    # -- helpers --------------------------------------------------------
    def _target_channel(self, interaction: discord.Interaction) -> discord.TextChannel | None:
        if self.settings.giveaway_channel_id and _SNOWFLAKE.match(self.settings.giveaway_channel_id):
            ch = self.get_channel(int(self.settings.giveaway_channel_id))
            if isinstance(ch, discord.TextChannel):
                return ch
        ch = interaction.channel
        return ch if isinstance(ch, discord.TextChannel) else None

    @staticmethod
    def _member_info(member: object) -> tuple[list[str], float | None]:
        roles: list[str] = []
        for role in getattr(member, "roles", []) or []:
            rid = getattr(role, "id", None)
            if rid is not None:
                roles.append(str(rid))
        user = getattr(member, "_user", None) or member
        created = getattr(user, "created_at", None)
        ts = created.timestamp() if created is not None else None
        return roles, ts

    async def _refresh_embed(self, gw: Giveaway) -> None:
        if not gw.message_id:
            return
        try:
            channel = self.get_channel(int(gw.channel_id))
        except (TypeError, ValueError):
            return
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(int(gw.message_id))
        except discord.HTTPException:
            return
        try:
            await message.edit(
                embed=embeds.giveaway_embed(gw, self.service.entry_count(gw.id), self.settings.embed_color),
                view=self._register_view(gw.id),
            )
        except discord.HTTPException:
            pass

    # -- button handlers ------------------------------------------------
    async def handle_join(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        try:
            gw = self.service.get(giveaway_id)
        except ServiceError:
            await interaction.response.send_message("Giveaway not found.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this button inside the server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        roles, created_ts = self._member_info(interaction.user)
        try:
            count = self.service.join(
                gw,
                user_id=str(interaction.user.id),
                username=interaction.user.display_name,
                member_roles=roles,
                account_created_ts=created_ts,
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        await interaction.followup.send(f"🎟️ You're in! Entry #{count}.", ephemeral=True)
        await self._refresh_embed(self.service.get(giveaway_id))

    async def handle_leave(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = self.service.leave(giveaway_id, str(interaction.user.id))
        await interaction.followup.send(
            "You left the giveaway." if removed else "You were not entered.", ephemeral=True
        )
        try:
            await self._refresh_embed(self.service.get(giveaway_id))
        except ServiceError:
            pass

    # -- auto-draw timer -------------------------------------------------
    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        for gw in self.service.due():
            try:
                ended, winners = self.service.end(gw.id)
            except ServiceError:
                continue
            await self._announce(ended, winners)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.wait_until_ready()

    async def _announce(self, gw: Giveaway, winners: list[str]) -> None:
        try:
            channel = self.get_channel(int(gw.channel_id))
        except (TypeError, ValueError):
            return
        if not isinstance(channel, discord.TextChannel):
            return
        entries = self.service.entry_count(gw.id)
        embed = embeds.winner_embed(gw, winners, entries, self.settings.embed_color)
        mentions = " ".join(f"<@{w}>" for w in winners) if winners else ""
        try:
            if gw.message_id:
                try:
                    msg = await channel.fetch_message(int(gw.message_id))
                    await msg.edit(embed=embed, view=None)
                    if mentions:
                        await msg.reply(mentions, allowed_mentions=discord.AllowedMentions(users=True))
                    return
                except discord.HTTPException:
                    pass
            await channel.send(embed=embed, content=mentions or None)
        except discord.HTTPException:
            log.warning("announce failed for %s", gw.id)


def wire_commands(bot: GiveawayBot) -> None:
    svc = bot.service

    @bot.tree.command(name="giveaway_create", description="Start a giveaway")
    @app_commands.describe(
        prize="What the winner gets",
        winners="Number of winners (1-25)",
        minutes="How long it runs (minutes)",
        required_role="Only this role can enter (optional)",
        blocked_role="This role cannot enter (optional)",
        min_account_age_days="Min Discord account age in days (optional)",
    )
    async def giveaway_create(
        interaction: discord.Interaction,
        prize: str,
        winners: int = 1,
        minutes: int = 60,
        required_role: discord.Role | None = None,
        blocked_role: discord.Role | None = None,
        min_account_age_days: int = 0,
    ) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        channel = bot._target_channel(interaction)
        if channel is None:
            await interaction.response.send_message("No text channel available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            gw = svc.create(
                guild_id=str(interaction.guild.id),
                channel_id=str(channel.id),
                prize=prize,
                winner_count=winners,
                duration_seconds=max(30, minutes * 60),
                created_by=str(interaction.user.id),
                required_role_id=str(required_role.id) if required_role else None,
                blocked_role_id=str(blocked_role.id) if blocked_role else None,
                min_account_age_days=max(0, min_account_age_days),
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        msg = await channel.send(
            embed=embeds.giveaway_embed(gw, 0, bot.settings.embed_color),
            view=bot._register_view(gw.id),
        )
        svc.set_message(gw.id, str(msg.id))
        await interaction.followup.send(f"✅ Giveaway started: {msg.jump_url}", ephemeral=True)

    @bot.tree.command(name="giveaway_end", description="End a giveaway now and draw")
    async def giveaway_end(interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            gw, winners = svc.end(giveaway_id.strip())
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        await bot._announce(gw, winners)
        await interaction.followup.send(
            f"Ended with {len(winners)} winner(s).", ephemeral=True
        )

    @bot.tree.command(name="giveaway_reroll", description="Draw new winner(s)")
    async def giveaway_reroll(interaction: discord.Interaction, giveaway_id: str, count: int = 1) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            gw, fresh = svc.reroll(giveaway_id.strip(), max(1, count))
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        await bot._announce(gw, fresh)
        await interaction.followup.send("🔁 Rerolled.", ephemeral=True)

    @bot.tree.command(name="giveaway_cancel", description="Cancel an active giveaway")
    async def giveaway_cancel(interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            svc.cancel(giveaway_id.strip())
        except ServiceError as exc:
            await interaction.response.send_message(f"⚠️ {exc.message}", ephemeral=True)
            return
        await interaction.response.send_message("Giveaway cancelled.", ephemeral=True)

    @bot.tree.command(name="giveaway_list", description="Show active giveaways")
    async def giveaway_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        active = svc.list_active(str(interaction.guild.id))
        if not active:
            await interaction.response.send_message("No active giveaways.", ephemeral=True)
            return
        lines = []
        for gw in active[:10]:
            n = svc.entry_count(gw.id)
            lines.append(f"• **{gw.prize}** — {n} entries — `{gw.id}` — <t:{int(gw.ends_at/1000)}:R>")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def amain(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    db = Database(settings)
    db.init_schema()
    bot = GiveawayBot(settings, db)
    wire_commands(bot)
    async with bot:
        await bot.start(settings.bot_token)


def run_forever(settings: Settings) -> None:
    asyncio.run(amain(settings))
