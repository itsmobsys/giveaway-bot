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
            count = await asyncio.to_thread(self.service.entry_count, gw.id)
            await message.edit(
                embed=embeds.giveaway_embed(gw, count, self.settings.embed_color),
                view=self._register_view(gw.id),
            )
        except discord.HTTPException:
            pass

    # -- message counting (min-messages requirement) ----------------------
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not message.guild:
            return
        gid, uid = str(message.guild.id), str(message.author.id)
        asyncio.get_running_loop().run_in_executor(None, self.service.record_message, gid, uid)

    # -- button handlers ------------------------------------------------
    async def _safe_defer(self, interaction: discord.Interaction) -> bool:
        """Ack first, before any DB work. Returns False if the token is dead."""
        try:
            if interaction.response.is_done():
                return True
            await interaction.response.defer(ephemeral=True, thinking=True)
            return True
        except (discord.NotFound, discord.HTTPException):
            return False

    async def handle_join(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            try:
                await interaction.response.send_message(
                    "Use this button inside the server.", ephemeral=True
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if not await self._safe_defer(interaction):
            return
        member = interaction.user
        roles, created_ts = self._member_info(member)
        uid, name = str(member.id), member.display_name
        try:
            gw = await asyncio.to_thread(self.service.get, giveaway_id)
        except ServiceError:
            await self._safe_followup(interaction, "Giveaway not found.")
            return
        try:
            count = await asyncio.to_thread(
                self.service.join,
                gw,
                user_id=uid,
                username=name,
                member_roles=roles,
                account_created_ts=created_ts,
            )
        except ServiceError as exc:
            await self._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        except Exception:
            log.exception("join failed for %s", giveaway_id)
            await self._safe_followup(interaction, "⚠️ Could not enter you. Try again.")
            return
        await self._safe_followup(interaction, f"🎟️ You're in! Entry #{count}.")
        try:
            fresh = await asyncio.to_thread(self.service.get, giveaway_id)
        except ServiceError:
            return
        await self._refresh_embed(fresh)

    async def _safe_followup(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_leave(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        if not await self._safe_defer(interaction):
            return
        removed = await asyncio.to_thread(
            self.service.leave, giveaway_id, str(interaction.user.id)
        )
        await self._safe_followup(
            interaction, "You left the giveaway." if removed else "You were not entered."
        )
        try:
            fresh = await asyncio.to_thread(self.service.get, giveaway_id)
        except ServiceError:
            return
        await self._refresh_embed(fresh)

    # -- auto-draw timer -------------------------------------------------
    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        try:
            due = await asyncio.to_thread(self.service.due)
        except Exception:
            log.exception("due check failed")
            return
        for gw in due:
            try:
                ended, winners = await asyncio.to_thread(self.service.end, gw.id)
            except ServiceError:
                continue
            except Exception:
                log.exception("end failed for %s", gw.id)
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
        min_messages="Min messages sent in this server (optional)",
        image="Prize photo URL, e.g. a gift-card picture (optional)",
    )
    async def giveaway_create(
        interaction: discord.Interaction,
        prize: str,
        winners: int = 1,
        minutes: int = 60,
        required_role: discord.Role | None = None,
        blocked_role: discord.Role | None = None,
        min_account_age_days: int = 0,
        min_messages: int = 0,
        image: str | None = None,
    ) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        channel = bot._target_channel(interaction)
        if channel is None:
            await interaction.response.send_message("No text channel available.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw = await asyncio.to_thread(
                svc.create,
                guild_id=str(interaction.guild.id),
                channel_id=str(channel.id),
                prize=prize,
                winner_count=winners,
                duration_seconds=max(30, minutes * 60),
                created_by=str(interaction.user.id),
                required_role_id=str(required_role.id) if required_role else None,
                blocked_role_id=str(blocked_role.id) if blocked_role else None,
                min_account_age_days=max(0, min_account_age_days),
                min_messages=max(0, min_messages),
                image_url=image,
            )
        except ServiceError as exc:
            try:
                await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        except Exception:
            log.exception("create failed")
            try:
                await interaction.followup.send("⚠️ Could not create. Try again.", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        msg = await channel.send(
            embed=embeds.giveaway_embed(gw, 0, bot.settings.embed_color),
            view=bot._register_view(gw.id),
        )
        await asyncio.to_thread(svc.set_message, gw.id, str(msg.id))
        await bot._safe_followup(interaction, f"✅ Giveaway started: {msg.jump_url}")

    @bot.tree.command(name="giveaway_end", description="End a giveaway now and draw")
    async def giveaway_end(interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw, winners = await asyncio.to_thread(svc.end, giveaway_id.strip())
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        await bot._announce(gw, winners)
        await bot._safe_followup(interaction, f"Ended with {len(winners)} winner(s).")

    @bot.tree.command(name="giveaway_reroll", description="Draw new winner(s)")
    async def giveaway_reroll(interaction: discord.Interaction, giveaway_id: str, count: int = 1) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw, fresh = await asyncio.to_thread(svc.reroll, giveaway_id.strip(), max(1, count))
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        await bot._announce(gw, fresh)
        await bot._safe_followup(interaction, "🔁 Rerolled.")

    @bot.tree.command(name="giveaway_cancel", description="Cancel an active giveaway")
    async def giveaway_cancel(interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await asyncio.to_thread(svc.cancel, giveaway_id.strip())
        except ServiceError as exc:
            await interaction.response.send_message(f"⚠️ {exc.message}", ephemeral=True)
            return
        await interaction.response.send_message("Giveaway cancelled.", ephemeral=True)

    @bot.tree.command(name="giveaway_list", description="Show active giveaways")
    async def giveaway_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        active = await asyncio.to_thread(svc.list_active, str(interaction.guild.id))
        if not active:
            await bot._safe_followup(interaction, "No active giveaways.")
            return
        lines = []
        for gw in active[:10]:
            n = await asyncio.to_thread(svc.entry_count, gw.id)
            lines.append(f"• **{gw.prize}** — {n} entries — `{gw.id}` — <t:{int(gw.ends_at/1000)}:R>")
        await bot._safe_followup(interaction, "\n".join(lines))


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
