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
from .views import GiveawayView, ParticipantsPages

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
        #: guild_id -> [(giveaway_id, prize)] rebuilt every tick. Discord kills
        #: autocomplete after 3s and a Turso round-trip can exceed that, so
        #: suggestions come from this snapshot — never from the database.
        self._autocomplete_cache: dict[str, list[tuple[str, str]]] = {}
        #: Giveaway ids whose entrants-role delete is already scheduled.
        #: Stops end + cancel + tick racing to queue the same role twice.
        self._scheduled_role_deletes: set[str] = set()
        #: Strong references to those one-shot tasks. asyncio only keeps weak
        #: ones, so without this a task can be collected mid-sleep and the role
        #: would then never be deleted.
        self._role_tasks: set[asyncio.Task] = set()
        #: (guild_id, user_id) -> messages seen since the last flush. Counting in
        #: memory turns one Turso write per message into one per flush interval.
        self._message_buffer: dict[tuple[str, str], int] = {}
        #: Tick counter; the entry-wipe sweep runs every ~20 ticks so a
        #: usually-empty DELETE doesn't cost a Turso write on every pass.
        self._tick_count = 0

    # -- lifecycle ------------------------------------------------------
    async def setup_hook(self) -> None:
        self.register_components()
        await self.tree.sync()
        log.info("commands synced (%d)", len(self.tree.get_commands()))
        self.tick.change_interval(seconds=max(5, self.settings.tick_seconds))
        self.tick.start()
        self.heartbeat.start()
        self.flush_messages.start()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%d guilds)", self.user, len(self.guilds))
        try:
            # Off the event loop: this is a network round-trip, and blocking
            # here delays the gateway heartbeat that keeps the bot online.
            live = await asyncio.to_thread(self.service.list_all_active, 200)
        except Exception:
            log.exception("could not preload active giveaways")
            live = []
        log.info("tracking %d active giveaway(s) from the database", len(live))
        self._cache_autocomplete(live)
        # Restart recovery: finished giveaways whose entrants role was never
        # deleted (bot was down during the 5-minute window, or the delete
        # failed). Clean the pileup now instead of leaving roles forever.
        try:
            leftovers = await asyncio.to_thread(self.service.ended_with_roles)
        except Exception:
            log.exception("leftover role sweep failed")
            leftovers = []
        if leftovers:
            log.info("cleaning up %d leftover entrants role(s)", len(leftovers))
            for row in leftovers:
                self._schedule_role_delete(
                    str(row.guild_id), str(row.entrants_role_id or ""), row.id,
                    delay=60.0,
                )

    async def close(self) -> None:
        for loop in (self.tick, self.heartbeat, self.flush_messages):
            try:
                loop.cancel()
            except Exception:
                pass
        try:
            # Last chance to persist counts: the gateway is about to go away and
            # whatever is still buffered would be lost with it.
            await self._flush_message_buffer()
        except Exception:
            log.exception("final message-count flush failed")
        for task in list(self._role_tasks):
            task.cancel()
        await super().close()
        # Every thread's connection, not just this one's.
        self.db.close_all()

    def register_components(self) -> None:
        """Make every giveaway button clickable, forever.

        One registration for every giveaway this process will ever post: clicks
        are matched against the custom_id patterns in views.py rather than a
        view registered per giveaway, so nothing accumulates as giveaways come
        and go. Safe to call more than once, and on a client that never starts.
        """
        GiveawayView.register(
            self,
            join=self.handle_join,
            leave=self.handle_leave,
            participants=self.handle_participants,
        )

    def _cache_autocomplete(self, live: list[Giveaway]) -> None:
        """Rebuild the per-guild autocomplete snapshot from active giveaways.

        Keyed by guild so one busy server with 40 giveaways cannot crowd the
        suggestions of every other server out of the list.
        """
        cache: dict[str, list[tuple[str, str]]] = {}
        for gw in live:
            cache.setdefault(gw.guild_id, []).append((gw.id, gw.prize))
        self._autocomplete_cache = cache

    def _pending_messages(self, guild_id: str, user_id: str) -> int:
        """Counts seen but not yet flushed to the database."""
        return self._message_buffer.get((guild_id, user_id), 0)

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

    #: How many embeds may be re-rendered at the same time. Each refresh is an
    #: HTTP PATCH, so this guards the rate limit as much as the loop.
    REFRESH_CONCURRENCY = 5

    async def _refresh_embeds(self, giveaways: list[Giveaway]) -> None:
        """Re-render several embeds concurrently, a few at a time."""
        gate = asyncio.Semaphore(self.REFRESH_CONCURRENCY)

        async def one(gw: Giveaway) -> None:
            async with gate:
                try:
                    await self._refresh_embed(gw)
                except Exception:
                    log.exception("embed refresh failed for %s", gw.id)

        await asyncio.gather(*(one(gw) for gw in giveaways))

    async def _refresh_embed(self, gw: Giveaway) -> None:
        if not gw.message_id:
            return
        try:
            channel = self.get_channel(int(gw.channel_id))
            message_id = int(gw.message_id)
        except (TypeError, ValueError):
            return
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            count = await asyncio.to_thread(self.service.entry_count, gw.id)
            # A partial message edits by id. fetch_message() would spend a full
            # GET per giveaway per tick on a message object nothing here reads.
            await channel.get_partial_message(message_id).edit(
                embed=embeds.giveaway_embed(gw, count, self.settings.embed_color),
                view=GiveawayView(gw.id, self.settings.dashboard_url),
            )
        except discord.HTTPException:
            pass

    # -- message counting (min-messages requirement) ----------------------
    #: How often buffered counts reach the database, and how many distinct users
    #: may pile up before an early flush.
    MESSAGE_FLUSH_SECONDS = 10
    MESSAGE_BUFFER_MAX = 5000

    async def on_message(self, message: discord.Message) -> None:
        """Count a message towards its author's min-messages requirement.

        The count lives in memory until flush_messages() writes it. The old
        version queued one database write per message, so every line typed in
        the server cost a Turso round-trip (and a write) of its own.
        """
        if message.guild is None or message.author.bot:
            return
        key = (str(message.guild.id), str(message.author.id))
        self._message_buffer[key] = self._message_buffer.get(key, 0) + 1
        if len(self._message_buffer) >= self.MESSAGE_BUFFER_MAX:
            await self._flush_message_buffer()

    @tasks.loop(seconds=MESSAGE_FLUSH_SECONDS)
    async def flush_messages(self) -> None:
        try:
            await self._flush_message_buffer()
        except Exception:
            log.exception("message-count flush failed")

    @flush_messages.before_loop
    async def _before_flush_messages(self) -> None:
        await self.wait_until_ready()

    async def _flush_message_buffer(self) -> None:
        """Write every buffered count in as few statements as possible."""
        if not self._message_buffer:
            return
        # Swap first: anything counted while the write is in flight lands in the
        # fresh dict instead of being overwritten by the batch we are sending.
        batch, self._message_buffer = self._message_buffer, {}
        try:
            await asyncio.to_thread(
                self.service.add_message_counts,
                [(guild_id, user_id, n) for (guild_id, user_id), n in batch.items()],
            )
        except Exception:
            # Hand the counts back instead of dropping them on a Turso blip.
            for key, n in batch.items():
                self._message_buffer[key] = self._message_buffer.get(key, 0) + n
            raise

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

    #: Grace period between a giveaway ending and its entrants role being
    #: deleted from the server. Users are stripped immediately; the role
    #: itself lingers so the winner announcement's mention still resolves,
    #: then goes away for good instead of piling up.
    ROLE_DELETE_DELAY = 300.0

    #: How many members may be stripped of the entrants role at the same time.
    ROLE_STRIP_CONCURRENCY = 5

    async def _strip_entrants_role(self, gw: Giveaway) -> None:
        """Take the role from every entrant now, delete it in 5 minutes."""
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
        if guild is not None and entrants:
            # Concurrently, but a few at a time. One member at a time made a
            # 1000-entrant giveaway take minutes to clean up; all of them at
            # once would trip the per-route rate limit.
            gate = asyncio.Semaphore(self.ROLE_STRIP_CONCURRENCY)

            async def strip_one(user_id: str) -> None:
                async with gate:
                    member = await self._member_for(guild, user_id)
                    if member is None:
                        return
                    try:
                        await member.remove_roles(role, reason=f"Giveaway {gw.id} ended")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            await asyncio.gather(*(strip_one(str(row["user_id"])) for row in entrants))
        self._schedule_role_delete(
            str(gw.guild_id), str(role.id), gw.id, delay=self.ROLE_DELETE_DELAY
        )
        log.info(
            "stripped entrants role for %s; deleting it in %.0fs",
            gw.id, self.ROLE_DELETE_DELAY,
        )

    def _schedule_role_delete(
        self, guild_id: str, role_id: str, giveaway_id: str, delay: float
    ) -> None:
        """Queue a one-shot task that deletes the role after `delay` seconds."""
        if not guild_id or not role_id or giveaway_id in self._scheduled_role_deletes:
            return
        self._scheduled_role_deletes.add(giveaway_id)
        task = asyncio.get_running_loop().create_task(
            self._delete_entrants_role_later(guild_id, role_id, giveaway_id, delay),
            name=f"delete-role-{giveaway_id}",
        )
        self._role_tasks.add(task)
        task.add_done_callback(self._role_tasks.discard)

    async def _delete_entrants_role_later(
        self, guild_id: str, role_id: str, giveaway_id: str, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            try:
                guild = self.get_guild(int(guild_id))
            except (TypeError, ValueError):
                guild = None
            if guild is None:
                return
            role = guild.get_role(int(role_id)) if role_id.isdigit() else None
            if role is not None:
                try:
                    await role.delete(reason=f"Giveaway {giveaway_id} ended")
                    log.info("deleted entrants role for %s", giveaway_id)
                except discord.Forbidden:
                    log.warning(
                        "no permission to delete entrants role for %s —"
                        " move the bot role above it / grant Manage Roles",
                        giveaway_id,
                    )
                    # Left in the database on purpose: the restart sweep picks
                    # it up again once the permissions are fixed.
                    return
                except (discord.HTTPException, discord.NotFound):
                    log.warning("role delete failed for %s", giveaway_id)
            # Role already gone (or the delete failed for good): record that so
            # the restart sweep stops queueing it.
            try:
                await asyncio.to_thread(self.service.set_entrants_role, giveaway_id, None)
            except Exception:
                log.exception("could not clear entrants role for %s", giveaway_id)
        except Exception:
            log.exception("role cleanup crashed for %s", giveaway_id)
        finally:
            # Always released, even on cancellation or a crash: otherwise this
            # giveaway could never be scheduled again while the process lives.
            self._scheduled_role_deletes.discard(giveaway_id)

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
            # Log the id plus what IS live: tells a stale button (id absent
            # from the live list) apart from a phantom empty read (id present
            # but the lookup missed it).
            try:
                live = await asyncio.to_thread(
                    self.service.list_all_active, 50
                )
                live_ids = [g.id for g in live]
            except Exception:
                live_ids = []
            log.warning(
                "join for unknown giveaway %s (guild %s, live: %s)",
                giveaway_id, interaction.guild.id if interaction.guild else "?",
                ",".join(live_ids) or "none",
            )
            await self._safe_followup(
                interaction, f"Giveaway not found (`{giveaway_id}`)."
            )
            return
        except Exception:
            log.exception("join lookup failed for %s", giveaway_id)
            await self._safe_followup(
                interaction, "⚠️ Could not load the giveaway. Try again."
            )
            return
        try:
            count = await asyncio.to_thread(
                self.service.join,
                gw,
                user_id=uid,
                username=name,
                member_roles=roles,
                account_created_ts=created_ts,
                # Messages this member sent since the last flush still count.
                pending_messages=self._pending_messages(str(interaction.guild.id), uid),
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
        except Exception:
            log.exception("post-join refresh lookup failed for %s", giveaway_id)
            return
        await self._grant_entrants_role(fresh, member)
        await self._refresh_embed(fresh)

    async def _safe_followup(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_participants(
        self, interaction: discord.Interaction, giveaway_id: str
    ) -> None:
        if not await self._safe_defer(interaction):
            return
        try:
            gw = await asyncio.to_thread(self.service.get, giveaway_id)
            entrants = await asyncio.to_thread(self.service.entries, gw.id)
        except ServiceError as exc:
            await self._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        except Exception:
            log.exception("participants lookup failed for %s", giveaway_id)
            await self._safe_followup(interaction, "⚠️ Could not load the list. Try again.")
            return
        if not entrants:
            await self._safe_followup(
                interaction, f"🏆 **{gw.prize}** — no entrants yet."
            )
            return
        mine = 1 if str(interaction.user.id) in {str(r["user_id"]) for r in entrants} else 0
        total = len(entrants)
        pages = max(1, (total + 9) // 10)
        color = self.settings.embed_color

        def render(page: int) -> discord.Embed:
            start = page * 10
            return embeds.participants_embed(
                prize=gw.prize,
                rows=entrants[start : start + 10],
                page=page,
                pages=pages,
                total=total,
                mine=mine,
                winner_count=gw.winner_count,
                color=color,
            )

        view = ParticipantsPages(render=render, pages=pages)
        try:
            await interaction.followup.send(embed=render(0), view=view, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_leave(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        if not await self._safe_defer(interaction):
            return
        try:
            removed = await asyncio.to_thread(
                self.service.leave, giveaway_id, str(interaction.user.id)
            )
        except Exception:
            # Previously this escaped the button callback: the member saw a bare
            # "interaction failed" and nothing tied the error to the giveaway.
            log.exception("leave failed for %s", giveaway_id)
            await self._safe_followup(interaction, "⚠️ Could not update your entry. Try again.")
            return
        await self._safe_followup(
            interaction, "You left the giveaway." if removed else "You were not entered."
        )
        if not removed:
            return
        try:
            fresh = await asyncio.to_thread(self.service.get, giveaway_id)
        except ServiceError:
            return
        except Exception:
            log.exception("post-leave lookup failed for %s", giveaway_id)
            return
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
            # One query feeds both consumers: the per-guild autocomplete cache
            # wants them all, the embeds below only the soonest ten.
            live = await asyncio.to_thread(self.service.list_all_active, 200)
        except Exception:
            log.exception("live list failed")
            return
        self._cache_autocomplete(live)
        await self._refresh_embeds(live[:10])
        # Privacy sweep: join data (who entered) older than 5h past the end
        # is wiped. Giveaway records + winner lists stay; message counts are
        # already cleared at end-time.
        self._tick_count += 1
        if self._tick_count % 20 == 0:
            try:
                wiped = await asyncio.to_thread(self.service.wipe_stale_entries)
            except Exception:
                log.exception("entry wipe failed")
            else:
                if wiped:
                    log.info("wiped %d stale entry row(s)", wiped)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.wait_until_ready()

    # -- 1-minute health heartbeat ---------------------------------------
    @tasks.loop(seconds=60)
    async def heartbeat(self) -> None:
        """Ping our own /health like an external monitor would.

        Proves the web server half of the process is alive and leaves a
        visible 1-minute heartbeat in the logs. (Note: localhost traffic does
        not count as external traffic for Render's free-tier sleep — keep the
        UptimeRobot monitor for that.)
        """
        import time
        import urllib.request

        port = max(1, self.settings.port)

        def _ping() -> tuple[int, float]:
            started = time.perf_counter()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
                return resp.status, (time.perf_counter() - started) * 1000

        try:
            status, ms = await asyncio.to_thread(_ping)
        except Exception as exc:
            log.warning("health check failed: %s", exc)
            return
        log.info("health check ok (%d, %.0fms)", status, ms)

    @heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
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
        """Suggest running giveaways so ids never need typing.

        Served from the tick cache, never the database: autocomplete dies at
        3s and a Turso round-trip can exceed that. Any failure returns no
        suggestions instead of an "Unknown interaction" traceback.
        """
        try:
            if interaction.guild is None:
                return []
            gid = str(interaction.guild.id)
            needle = (current or "").lower()
            choices = [
                app_commands.Choice(name=f"🏆 {prize} ({gw_id})"[:100], value=gw_id)
                for (gw_id, prize) in bot._autocomplete_cache.get(gid, [])
                if not needle or needle in prize.lower() or needle in gw_id.lower()
            ]
            return choices[:25]
        except Exception:
            log.exception("autocomplete failed")
            return []

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Last line of defence for a slash command.

        Without this a command that raises leaves the member staring at "The
        application did not respond" while the only trace in the log is a bare
        traceback with no hint of which command or server produced it.
        """
        command = getattr(interaction.command, "qualified_name", "?")
        log.error(
            "command /%s failed in guild %s", command, interaction.guild_id, exc_info=error
        )
        text = f"⚠️ /{command} failed. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    @bot.tree.command(name="giveaway_create", description="Start a giveaway")
    @app_commands.describe(
        prize="What the winner gets",
        winners="Number of winners (1-25)",
        minutes="How long it runs (minutes)",
        required_role_1="Role that can enter, pinged on create (optional)",
        required_role_2="Another role that can enter (optional)",
        required_role_3="Another role that can enter (optional)",
        required_role_4="Another role that can enter (optional)",
        required_role_5="Another role that can enter (optional)",
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
        required_role_4: discord.Role | None = None,
        required_role_5: discord.Role | None = None,
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
        role_slots = [required_role_1, required_role_2, required_role_3, required_role_4, required_role_5]
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
        try:
            msg = await channel.send(
                content=create_content,
                embed=embeds.giveaway_embed(gw, 0, bot.settings.embed_color),
                view=GiveawayView(gw.id, bot.settings.dashboard_url),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            # The row already exists. Leaving it would run a giveaway nobody can
            # see and then auto-draw winners into a channel that already refused
            # the bot, so the record is dropped and the reason reported.
            log.warning("cannot post giveaway %s in %s: %s", gw.id, channel.id, exc)
            await asyncio.to_thread(svc.discard, gw.id)
            await bot._safe_followup(
                interaction,
                f"⚠️ I cannot post in {channel.mention}. Give me **Send Messages**"
                " there (or set the DISCORD_GIVEAWAY_CHANNEL_ID env var) and try again.",
            )
            return
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
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gw = await asyncio.to_thread(
                svc.resolve, str(interaction.guild.id), giveaway_id
            )
            ended = await asyncio.to_thread(svc.cancel, gw.id)
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        # Answer before the cleanup. Stripping entrants one by one can take much
        # longer than the 3 seconds Discord allows for the first response, and
        # the old order made a successful cancel look like a broken command.
        await bot._safe_followup(interaction, f"🚫 Cancelled **{ended.prize}**.")
        await bot._strip_entrants_role(ended)
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

    @bot.tree.command(name="giveaway_extend", description="Add more time to a running giveaway")
    @app_commands.autocomplete(giveaway_id=_gw_autocomplete)
    @app_commands.describe(minutes="Extra minutes to add (1-43200)")
    async def giveaway_extend(
        interaction: discord.Interaction, giveaway_id: str, minutes: int
    ) -> None:
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
            fresh = await asyncio.to_thread(svc.extend, gw.id, max(1, minutes) * 60)
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        await bot._refresh_embed(fresh)
        await bot._safe_followup(
            interaction,
            f"⏳ **{fresh.prize}** extended — now ends <t:{int(fresh.ends_at/1000)}:R>.",
        )
        # Tell the people waiting: ping every entrant in the giveaway channel.
        try:
            entrants = await asyncio.to_thread(svc.entries, fresh.id)
        except Exception:
            log.exception("entrant lookup failed for extend notice (%s)", fresh.id)
            entrants = []
        if not entrants:
            return
        try:
            channel = bot.get_channel(int(fresh.channel_id))
        except (TypeError, ValueError):
            channel = None
        if not isinstance(channel, discord.TextChannel):
            return
        notice = (
            f"⏳ **{fresh.prize}** got more time — now ends"
            f" <t:{int(fresh.ends_at/1000)}:R>!"
        )
        try:
            role = bot._role_for(fresh)
            if role is not None:
                await channel.send(
                    f"{notice}\n{role.mention}",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            else:
                ids = [str(row["user_id"]) for row in entrants]
                for i in range(0, len(ids), 80):
                    chunk = " ".join(f"<@{uid}>" for uid in ids[i : i + 80])
                    await channel.send(
                        f"{notice}\n{chunk}" if i == 0 else chunk,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("extend notice failed for %s", fresh.id)

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
            chance = min(100.0, gw.winner_count / len(entrants) * 100) if entrants else 0.0
            odds = (
                f"\n📊 Each entrant has a **{chance:.1f}%** chance"
                f" ({gw.winner_count} winner(s) / {len(entrants)} entries)."
            )
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
                f"🏆 **{gw.prize}**{host} — **{len(entrants)}** entrant(s) ({status})"
                + odds + ":\n"
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
            each = f" — **{min(100.0, gw.winner_count / n * 100):.1f}%** each" if n else ""
            lines.append(
                f"• **{gw.prize}** — {n} entries{each} — `{gw.id}`"
                f" — <t:{int(gw.ends_at/1000)}:R>"
            )
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
                    f"🔔 Notify role: <@&{current}> — give it to members so they"
                    " get pinged on giveaway news.",
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
            " rerolls, cancellations).",
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

    @bot.tree.command(name="giveaway_blacklist_add", description="Block a user from all giveaways")
    @app_commands.describe(user="The member to block")
    async def giveaway_blacklist_add(
        interaction: discord.Interaction, user: discord.Member
    ) -> None:
        """Block + yank their entries from running giveaways + strip roles."""
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        gid, uid = str(interaction.guild.id), str(user.id)
        try:
            await asyncio.to_thread(svc.blacklist_add, gid, uid)
            active = await asyncio.to_thread(svc.list_active, gid)
        except Exception:
            log.exception("blacklist add failed for %s", uid)
            await bot._safe_followup(interaction, "⚠️ Could not update the blacklist. Try again.")
            return
        for gw in active:
            await bot._take_entrants_role(gw, uid)
        log.info("blacklisted %s in guild %s", uid, gid)
        await bot._safe_followup(
            interaction,
            f"🚫 {user.mention} is blocked from giveaways — entries removed"
            f" from {len(active)} running giveaway(s).",
        )

    @bot.tree.command(name="giveaway_blacklist_remove", description="Unblock a user from giveaways")
    @app_commands.describe(user="The member to unblock")
    async def giveaway_blacklist_remove(
        interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            removed = await asyncio.to_thread(
                svc.blacklist_remove, str(interaction.guild.id), str(user.id)
            )
        except Exception:
            log.exception("blacklist remove failed for %s", user.id)
            await interaction.response.send_message(
                "⚠️ Could not update the blacklist. Try again.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ {user.mention} can join giveaways again."
            if removed
            else f"{user.mention} was not on the blacklist.",
            ephemeral=True,
        )

    @bot.tree.command(name="giveaway_blacklist_list", description="Show blocked users")
    async def giveaway_blacklist_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            ids = await asyncio.to_thread(
                svc.blacklist_list, str(interaction.guild.id)
            )
        except Exception:
            log.exception("blacklist list failed")
            await interaction.response.send_message(
                "⚠️ Could not load the blacklist. Try again.", ephemeral=True
            )
            return
        if not ids:
            await interaction.response.send_message(
                "Blacklist is empty — nobody is blocked.", ephemeral=True
            )
            return
        lines = "\n".join(f"<@{uid}>" for uid in ids[:100])
        extra = f"\n…plus {len(ids) - 100} more." if len(ids) > 100 else ""
        await interaction.response.send_message(
            f"🚫 **Blocked ({len(ids)}):**\n{lines}{extra}", ephemeral=True
        )


async def amain(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    from .health import start_health_server

    start_health_server(max(1, settings.port))
    db = Database(settings)
    db.init_schema()
    bot = GiveawayBot(settings, db)
    wire_commands(bot)
    async with bot:
        await bot.start(settings.bot_token)


def run_forever(settings: Settings) -> None:
    asyncio.run(amain(settings))
