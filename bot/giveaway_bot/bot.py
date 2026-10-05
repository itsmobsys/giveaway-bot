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
        self.tick.change_interval(seconds=max(5, self.settings.tick_seconds))
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
            view._notify_handler = self.handle_notify
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

    # -- entrants role (ping everyone in the giveaway) --------------------
    async def _member_for(
        self, guild: discord.Guild, user_id: str
    ) -> discord.Member | None:
        try:
            member = guild.get_member(int(user_id))
        except (TypeError, ValueError):
            return None
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(user_id))
        except (discord.NotFound, discord.HTTPException, ValueError):
            return None

    def _role_for(self, gw: Giveaway) -> discord.Role | None:
        if not gw.entrants_role_id or not gw.guild_id:
            return None
        try:
            guild = self.get_guild(int(gw.guild_id))
            role_id = int(gw.entrants_role_id)
        except (TypeError, ValueError):
            return None
        return guild.get_role(role_id) if guild else None

    async def _notify_mention(self, guild_id: str) -> str:
        """`<@&...>` for this server's notify role, or empty when unset/gone."""
        try:
            role_id = await asyncio.to_thread(self.service.get_notify_role, guild_id)
        except Exception:
            return ""
        if not role_id:
            return ""
        try:
            guild = self.get_guild(int(guild_id))
            role = guild.get_role(int(role_id)) if guild else None
        except (TypeError, ValueError):
            return ""
        return role.mention if role is not None else ""

    async def _grant_entrants_role(self, gw: Giveaway, member: discord.Member) -> None:
        role = self._role_for(gw)
        if role is None:
            return
        try:
            await member.add_roles(role, reason=f"Joined giveaway {gw.id}")
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not grant entrants role for %s", gw.id)

    async def _take_entrants_role(self, gw: Giveaway, user_id: str) -> None:
        role = self._role_for(gw)
        if role is None:
            return
        try:
            guild = self.get_guild(int(gw.guild_id))
        except (TypeError, ValueError):
            return
        if guild is None:
            return
        member = await self._member_for(guild, user_id)
        if member is None:
            return
        try:
            await member.remove_roles(role, reason=f"Left giveaway {gw.id}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _strip_entrants_role(self, gw: Giveaway) -> None:
        """Take the role from every entrant, then delete it. Win or lose."""
        role = self._role_for(gw)
        if role is None:
            return
        try:
            entrants = await asyncio.to_thread(self.service.entries, gw.id)
        except Exception:
            log.exception("could not list entrants for role strip (%s)", gw.id)
            entrants = []
        try:
            guild = self.get_guild(int(gw.guild_id))
        except (TypeError, ValueError):
            guild = None
        if guild is not None:
            for row in entrants:
                member = await self._member_for(guild, str(row["user_id"]))
                if member is None:
                    continue
                try:
                    await member.remove_roles(role, reason=f"Giveaway {gw.id} ended")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        try:
            await role.delete(reason=f"Giveaway {gw.id} ended")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

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
        await self._grant_entrants_role(fresh, member)
        await self._refresh_embed(fresh)

    async def _safe_followup(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_notify(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        """🔔 toggle: give/remove the server notify role for this member."""
        del giveaway_id  # the role is per-server, not per-giveaway
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
        try:
            role_id = await asyncio.to_thread(
                self.service.get_notify_role, str(interaction.guild.id)
            )
        except Exception:
            role_id = None
        if not role_id:
            await self._safe_followup(
                interaction,
                "No notify role is set up yet — staff, run `/giveaway_notifyer` once.",
            )
            return
        role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
        if role is None:
            await self._safe_followup(
                interaction, "The notify role no longer exists — staff, re-run `/giveaway_notifyer`."
            )
            return
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Notify-me toggled off")
                await self._safe_followup(
                    interaction, f"🔕 You will no longer be pinged ({role.name})."
                )
            else:
                await member.add_roles(role, reason="Notify-me toggled on")
                await self._safe_followup(
                    interaction, f"🔔 You will be pinged for giveaways ({role.name})."
                )
        except (discord.Forbidden, discord.HTTPException):
            await self._safe_followup(
                interaction, "⚠️ I cannot manage that role (need Manage Roles)."
            )

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
        if removed:
            await self._take_entrants_role(fresh, str(interaction.user.id))
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
            await self._strip_entrants_role(ended)
        # Live timer: re-render active embeds every tick so the countdown
        # visibly ticks down (soonest deadline first, capped per tick).
        try:
            live = await asyncio.to_thread(self.service.list_all_active, 10)
        except Exception:
            log.exception("live list failed")
            return
        for gw in live:
            try:
                await self._refresh_embed(gw)
            except Exception:
                log.exception("embed refresh failed for %s", gw.id)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.wait_until_ready()

    async def _announce(self, gw: Giveaway, winners: list[str]) -> None:
        """Winner celebration. Runs BEFORE the entrants role is stripped so the
        role mention below still reaches everyone who joined."""
        try:
            channel = self.get_channel(int(gw.channel_id))
        except (TypeError, ValueError):
            return
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            entries = await asyncio.to_thread(self.service.entry_count, gw.id)
        except Exception:
            entries = 0
        embed = embeds.winner_embed(gw, winners, entries, self.settings.embed_color)
        parts: list[str] = []
        if winners:
            parts.append("🎉 " + " ".join(f"<@{w}>" for w in winners))
        notify = await self._notify_mention(gw.guild_id)
        if notify:
            parts.append(f"{notify} — results are in!")
        role = self._role_for(gw)
        if role is not None:
            parts.append(f"{role.mention} — thanks to everyone who entered!")
        elif not winners:
            parts.append("No valid entries — no winners this time.")
        content = "\n".join(parts) or None
        mentions = discord.AllowedMentions(users=True, roles=True)
        try:
            if gw.message_id:
                try:
                    msg = await channel.fetch_message(int(gw.message_id))
                    await msg.edit(embed=embed, view=None)
                    if content:
                        await msg.reply(content, allowed_mentions=mentions)
                    return
                except discord.HTTPException:
                    pass
            await channel.send(embed=embed, content=content)
        except discord.HTTPException:
            log.warning("announce failed for %s", gw.id)


def wire_commands(bot: GiveawayBot) -> None:
    svc = bot.service

    async def _gw_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest running giveaways so ids never need typing."""
        if interaction.guild is None:
            return []
        try:
            active = await asyncio.to_thread(svc.list_active, str(interaction.guild.id))
        except Exception:
            return []
        needle = (current or "").lower()
        choices = [
            app_commands.Choice(name=f"🏆 {gw.prize} ({gw.id})"[:100], value=gw.id)
            for gw in active
            if not needle or needle in gw.prize.lower() or needle in gw.id.lower()
        ]
        return choices[:25]

    @bot.tree.command(name="giveaway_create", description="Start a giveaway")
    @app_commands.describe(
        prize="What the winner gets",
        winners="Number of winners (1-25)",
        minutes="How long it runs (minutes)",
        required_role_1="Role that can enter, pinged on create (optional)",
        required_role_2="Another role that can enter (optional)",
        required_role_3="Another role that can enter (optional)",
        blocked_role="This role cannot enter (optional)",
        min_account_age_days="Min Discord account age in days (optional)",
        min_messages="Min messages sent in this server (optional)",
        host="The hoster shown on the embed — e.g. the prize giver (defaults to you)",
        image="Prize photo URL, e.g. a gift-card picture (optional)",
    )
    async def giveaway_create(
        interaction: discord.Interaction,
        prize: str,
        winners: int = 1,
        minutes: int = 60,
        required_role_1: discord.Role | None = None,
        required_role_2: discord.Role | None = None,
        required_role_3: discord.Role | None = None,
        blocked_role: discord.Role | None = None,
        min_account_age_days: int = 0,
        min_messages: int = 0,
        host: discord.Member | None = None,
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
        role_slots = [required_role_1, required_role_2, required_role_3]
        need_roles = [str(r.id) for r in role_slots if r is not None]
        try:
            gw = await asyncio.to_thread(
                svc.create,
                guild_id=str(interaction.guild.id),
                channel_id=str(channel.id),
                prize=prize,
                winner_count=winners,
                duration_seconds=max(30, minutes * 60),
                created_by=str(interaction.user.id),
                required_role_ids=need_roles,
                blocked_role_id=str(blocked_role.id) if blocked_role else None,
                min_account_age_days=max(0, min_account_age_days),
                min_messages=max(0, min_messages),
                image_url=image,
                host_id=str(host.id) if host is not None else str(interaction.user.id),
                host_name=host.display_name if host is not None else interaction.user.display_name,
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
        # Ping everyone who should know: the server notify role (one-time
        # setup) plus each required role (the people allowed to join).
        pings: list[str] = []
        notify = await bot._notify_mention(str(interaction.guild.id))
        if notify:
            pings.append(notify)
        pings.extend(f"<@&{rid}>" for rid in need_roles)
        create_content = f"📢 New giveaway! {' '.join(pings)}" if pings else None
        msg = await channel.send(
            content=create_content,
            embed=embeds.giveaway_embed(gw, 0, bot.settings.embed_color),
            view=bot._register_view(gw.id),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await asyncio.to_thread(svc.set_message, gw.id, str(msg.id))
        role_note = ""
        if interaction.guild is not None:
            try:
                role = await interaction.guild.create_role(
                    name=f"🎉 {prize[:60]}",
                    mentionable=True,
                    reason=f"Entrants role for giveaway {gw.id}",
                )
                await asyncio.to_thread(svc.set_entrants_role, gw.id, str(role.id))
            except (discord.Forbidden, discord.HTTPException):
                role_note = " (no entrants role — I need **Manage Roles**)"
                log.warning("could not create entrants role in %s", interaction.guild.id)
        await bot._safe_followup(interaction, f"✅ Giveaway started: {msg.jump_url}{role_note}")

    @bot.tree.command(name="giveaway_end", description="End a giveaway now and draw")
    @app_commands.autocomplete(giveaway_id=_gw_autocomplete)
    async def giveaway_end(interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw = await asyncio.to_thread(
                svc.resolve, str(interaction.guild.id), giveaway_id
            )
            ended, winners = await asyncio.to_thread(svc.end, gw.id)
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        await bot._announce(ended, winners)
        await bot._strip_entrants_role(ended)
        await bot._safe_followup(interaction, f"Ended with {len(winners)} winner(s).")

    @bot.tree.command(name="giveaway_reroll", description="Draw new winner(s)")
    @app_commands.autocomplete(giveaway_id=_gw_autocomplete)
    async def giveaway_reroll(interaction: discord.Interaction, giveaway_id: str, count: int = 1) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw = await asyncio.to_thread(
                svc.resolve, str(interaction.guild.id), giveaway_id
            )
            ended, fresh = await asyncio.to_thread(svc.reroll, gw.id, max(1, count))
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        await bot._announce(ended, fresh)
        await bot._safe_followup(interaction, "🔁 Rerolled.")

    @bot.tree.command(name="giveaway_cancel", description="Cancel an active giveaway")
    @app_commands.autocomplete(giveaway_id=_gw_autocomplete)
    async def giveaway_cancel(interaction: discord.Interaction, giveaway_id: str) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            gw = await asyncio.to_thread(
                svc.resolve, str(interaction.guild.id), giveaway_id
            )
            ended = await asyncio.to_thread(svc.cancel, gw.id)
        except ServiceError as exc:
            await interaction.response.send_message(f"⚠️ {exc.message}", ephemeral=True)
            return
        await bot._strip_entrants_role(ended)
        await interaction.response.send_message("Giveaway cancelled.", ephemeral=True)
        try:
            channel = bot.get_channel(int(ended.channel_id))
        except (TypeError, ValueError):
            channel = None
        if isinstance(channel, discord.TextChannel):
            notify = await bot._notify_mention(str(interaction.guild.id))
            text = f"🚫 Giveaway **{ended.prize}** was cancelled."
            if notify:
                text += f" {notify}"
            try:
                await channel.send(
                    text, allowed_mentions=discord.AllowedMentions(roles=True)
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    @bot.tree.command(name="giveaway_list", description="Show entrants, or active giveaways")
    @app_commands.autocomplete(giveaway_id=_gw_autocomplete)
    @app_commands.describe(giveaway_id="Leave empty to list active giveaways")
    async def giveaway_list(
        interaction: discord.Interaction, giveaway_id: str | None = None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        gid = str(interaction.guild.id)
        if giveaway_id:
            try:
                gw = await asyncio.to_thread(svc.resolve, gid, giveaway_id)
            except ServiceError as exc:
                await bot._safe_followup(interaction, f"⚠️ {exc.message}")
                return
            entrants = await asyncio.to_thread(svc.entries, gw.id)
            host = f" by <@{gw.host_id}>" if gw.host_id else ""
            if not entrants:
                await bot._safe_followup(
                    interaction, f"🏆 **{gw.prize}**{host} — no entrants yet."
                )
                return
            shown = [f"<@{row['user_id']}>" for row in entrants[:50]]
            extra = f"\n…and {len(entrants) - 50} more." if len(entrants) > 50 else ""
            status = "running 🟢" if gw.active else gw.status
            await bot._safe_followup(
                interaction,
                f"🏆 **{gw.prize}**{host} — **{len(entrants)}** entrant(s) ({status}):\n"
                + ", ".join(shown)
                + extra,
            )
            return
        active = await asyncio.to_thread(svc.list_active, gid)
        if not active:
            await bot._safe_followup(interaction, "No active giveaways.")
            return
        lines = []
        for gw in active[:10]:
            n = await asyncio.to_thread(svc.entry_count, gw.id)
            lines.append(f"• **{gw.prize}** — {n} entries — `{gw.id}` — <t:{int(gw.ends_at/1000)}:R>")
        lines.append("\nTip: run `/giveaway_list` with an id to see who entered.")
        await bot._safe_followup(interaction, "\n".join(lines))

    @bot.tree.command(
        name="giveaway_notifyer", description="One-time setup: role pinged on giveaway news"
    )
    @app_commands.describe(role="Role pinged on every giveaway (omit to view current)")
    async def giveaway_notifyer(
        interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        gid = str(interaction.guild.id)
        if role is None:
            try:
                current = await asyncio.to_thread(svc.get_notify_role, gid)
            except Exception:
                current = None
            if current and interaction.guild.get_role(int(current)) is not None:
                await interaction.response.send_message(
                    f"🔔 Notify role: <@&{current}> — members grab it with the"
                    " **Notify me** button.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "No notify role set. Run `/giveaway_notifyer role:@YourRole` once.",
                    ephemeral=True,
                )
            return
        try:
            await role.edit(mentionable=True, reason="Giveaway notify role setup")
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await asyncio.to_thread(svc.set_notify_role, gid, str(role.id))
        except Exception:
            log.exception("notify role save failed")
            await interaction.response.send_message("⚠️ Could not save. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ {role.mention} will now be pinged on every giveaway (new, winners,"
            " rerolls, cancellations). Members opt in with the **Notify me** button.",
            ephemeral=True,
        )

    @bot.tree.command(name="giveaway_ping", description="Ping everyone who joined a giveaway")
    @app_commands.autocomplete(giveaway_id=_gw_autocomplete)
    async def giveaway_ping(
        interaction: discord.Interaction, giveaway_id: str, text: str | None = None
    ) -> None:
        """Ping all entrants — via their entrants role, or direct mentions."""
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw = await asyncio.to_thread(
                svc.resolve, str(interaction.guild.id), giveaway_id
            )
            entrants = await asyncio.to_thread(svc.entries, gw.id)
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        if not entrants:
            await bot._safe_followup(interaction, "Nobody has joined this giveaway yet.")
            return
        body = f"📢 **{gw.prize}**" + (f" — {text}" if text else "")
        role = bot._role_for(gw)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await bot._safe_followup(interaction, "Run this in a text channel.")
            return
        try:
            if role is not None:
                await channel.send(
                    f"{body}\n{role.mention}",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            else:
                ids = [str(row["user_id"]) for row in entrants]
                for i in range(0, len(ids), 80):
                    chunk = " ".join(f"<@{uid}>" for uid in ids[i : i + 80])
                    await channel.send(
                        f"{body}\n{chunk}" if i == 0 else chunk,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
        except (discord.Forbidden, discord.HTTPException):
            await bot._safe_followup(interaction, "⚠️ I cannot send messages there.")
            return
        await bot._safe_followup(
            interaction, f"📢 Pinged {len(entrants)} entrant(s)."
        )


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
