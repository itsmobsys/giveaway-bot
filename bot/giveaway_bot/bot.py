"""Standalone bot: slash commands + buttons + auto-draw timer. No dashboard."""

from __future__ import annotations

import asyncio
import logging
import re
import signal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import embeds
from .config import Settings
from .db import Database
from .service import TIMEOUT_BAN_KIND, Giveaway, GiveawayService, PartialFlush, ServiceError
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


#: Discord's content limit for one message, and what one mention costs there
#: ("<@1234567890123456789> " is 22 characters, rounded up for slack).
CONTENT_LIMIT = 2000
MENTION_CHARS = 23

#: Cap on the per-user fallback messages when the entrants role is missing.
#: Five messages cover ~400 mentions; beyond that the command would stall for
#: minutes inside a rate-limited send loop, so it says what it did not cover.
MAX_MENTION_MESSAGES = 5


def _split_mentions(ids: list[str], head: str) -> list[tuple[str, int]]:
    """Split mentions into messages that fit Discord's content cap.

    The first chunk carries the announcement text, so its budget is smaller.
    Sizing chunks on a fixed 80 mentions ignored that: a 256-character prize
    pushed the first message past 2000, Discord answered 400, and the whole ping
    was dropped instead of being split. Returns (message, mentions) pairs so
    callers can report exactly how many members a capped send reached.
    """
    per = max(1, (CONTENT_LIMIT - len(head) - 1) // MENTION_CHARS)
    out: list[tuple[str, int]] = []
    for start in range(0, len(ids), per):
        block_ids = ids[start : start + per]
        block = " ".join(f"<@{uid}>" for uid in block_ids)
        out.append((f"{head}\n{block}" if start == 0 else block, len(block_ids)))
    return out


def _still_in_guild(guild: discord.Guild, user_id: str) -> bool:
    """Whether this id is still cached as a member of the guild.

    Cache-only on purpose: the ban list is a snapshot, and fetching per row
    would cost one HTTP request per banned member. A non-numeric id — only
    reachable through a hand-edited row — counts as gone instead of raising.
    """
    try:
        return guild.get_member(int(user_id)) is not None
    except (TypeError, ValueError):
        return False


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
        #: Where the rotating embed-refresh window starts on the next tick.
        self._refresh_cursor = 0

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

    @staticmethod
    def _timed_out(member: object) -> bool:
        """Whether Discord has this member timed out right now (native /mute).

        Member.is_timed_out() is the library's own read of the member's
        communication_disabled_until, kept current by member updates: no role
        named anything, and no second guess at what "muted" means. An object
        without that API (an older client, or a plain User) and an unreadable
        timeout both count as "not timed out" — a penalty must never come out
        of a broken member object.
        """
        check = getattr(member, "is_timed_out", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:
            return False

    #: How many embeds may be re-rendered at the same time. Each refresh is an
    #: HTTP PATCH, so this guards the rate limit as much as the loop.
    REFRESH_CONCURRENCY = 5

    #: Embeds re-rendered every tick. The soonest deadlines always get a slot, so
    #: the countdown members are watching stays live; the rest rotate below.
    REFRESH_PER_TICK = 10

    #: Rotating slots for the giveaways past that cap. Without them the eleventh
    #: giveaway on a busy server shows the entry count it had when it was posted,
    #: for as long as earlier giveaways keep running.
    REFRESH_ROTATING = 5

    #: Entrants loaded for the Participants panel (10 per page). The panel shows
    #: a window instead of the whole table: a giveaway with 100k entries should
    #: not pull every row into memory, for every member who clicks the button.
    PARTICIPANT_WINDOW = 500

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

    def _refresh_window(self, live: list[Giveaway]) -> list[Giveaway]:
        """The soonest deadlines, plus a rotating slice of everything else."""
        head = live[: self.REFRESH_PER_TICK]
        rest = live[self.REFRESH_PER_TICK :]
        if not rest:
            return head
        start = self._refresh_cursor % len(rest)
        count = min(self.REFRESH_ROTATING, len(rest))
        extra = [rest[(start + step) % len(rest)] for step in range(count)]
        self._refresh_cursor = (start + count) % len(rest)
        return head + extra

    async def _refresh_embed(self, gw: Giveaway) -> None:
        if not gw.active or not gw.message_id:
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
        except discord.NotFound:
            # The message is gone for good: nothing to edit, and nothing worth
            # repeating in the log every tick.
            log.debug("giveaway message for %s no longer exists", gw.id)
        except discord.HTTPException as exc:
            # Swallowing this made a frozen embed invisible: the countdown simply
            # stopped moving, with no line in the log to say why.
            log.warning("embed refresh failed for %s (%s)", gw.id, exc)

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
        rows = [(guild_id, user_id, n) for (guild_id, user_id), n in batch.items()]
        try:
            await asyncio.to_thread(self.service.add_message_counts, rows)
        except PartialFlush as exc:
            # The chunks before the failure are already stored, so only the tail
            # goes back: restoring the whole batch would write the committed head
            # a second time and inflate everybody's count.
            self._requeue(rows[exc.applied :])
            raise
        except Exception:
            # Nothing is known to have landed; hand the whole batch back.
            self._requeue(rows)
            raise

    def _requeue(self, rows: list[tuple[str, str, int]]) -> None:
        """Put counts that did not reach the database back in the buffer."""
        for guild_id, user_id, count in rows:
            key = (guild_id, user_id)
            self._message_buffer[key] = self._message_buffer.get(key, 0) + count

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

    #: Entrants per gather. One task per entrant would allocate 50k tasks for a
    #: 50k-entry giveaway; the semaphore caps concurrency, not the allocation.
    STRIP_BATCH = 200

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

            for start in range(0, len(entrants), self.STRIP_BATCH):
                window = entrants[start : start + self.STRIP_BATCH]
                await asyncio.gather(*(strip_one(str(row["user_id"])) for row in window))
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
        # Also released from the callback, not only from the task's own finally:
        # a task cancelled before it ever started never runs that block, and the
        # giveaway would stay marked as scheduled for the life of the process.
        task.add_done_callback(
            lambda _task: self._scheduled_role_deletes.discard(giveaway_id)
        )

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
                # The bot cannot see the guild any more (removed, or unavailable
                # right now). The role is unreachable either way, so clear the
                # row: leaving it set makes the restart sweep re-queue this
                # giveaway on every boot, forever, for nothing.
                log.warning(
                    "guild %s unavailable; dropping entrants role cleanup for %s",
                    guild_id, giveaway_id,
                )
                await asyncio.to_thread(self.service.set_entrants_role, giveaway_id, None)
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
        # Discord's native timeout, read here but acted on inside the service's
        # eligibility gate, so it lands in the same place as every other
        # "you may not enter" rule instead of becoming a second join path.
        # For a component click interaction.user is a Member built from the
        # interaction payload, so this is the state Discord reported when they
        # clicked — not a cache read that could be minutes old.
        timed_out = self._timed_out(member)
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
                timed_out=timed_out,
            )
        except ServiceError as exc:
            await self._safe_followup(interaction, f"⚠️ {exc.message}")
            if exc.kind == TIMEOUT_BAN_KIND:
                # The gate dropped the entry they already had, so the entrants
                # role it earned has to go too — in every active giveaway
                # whose entries the service dropped, not just this one.
                try:
                    active = await asyncio.to_thread(self.service.list_active, gw.guild_id)
                except Exception:
                    log.exception("timeout role lookup failed for %s", uid)
                else:
                    for live in active:
                        await self._take_entrants_role(live, uid)
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
        if fresh.active:
            await self._grant_entrants_role(fresh, member)
        else:
            # The draw won the race: this member joined in the same instant the
            # giveaway ended, and the strip pass may already have walked past
            # them. Granting the role now would leave them holding the
            # ping-everyone role of a giveaway that is over.
            await self._take_entrants_role(fresh, uid)
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
            total = await asyncio.to_thread(self.service.entry_count, gw.id)
            # One bounded window instead of the whole table, and the "am I in it"
            # question answered by a lookup rather than by scanning the rows we
            # happened to load.
            entrants = await asyncio.to_thread(
                self.service.entries, gw.id, self.PARTICIPANT_WINDOW
            )
            mine = (
                1
                if await asyncio.to_thread(
                    self.service.has_entry, gw.id, str(interaction.user.id)
                )
                else 0
            )
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
        pages = max(1, (len(entrants) + 9) // 10)
        color = self.settings.embed_color
        # The panel is a bounded window, not the whole table: say so when the
        # giveaway is bigger than the window, or the header total looks like a
        # lie next to a pager that stops early.
        windowed = len(entrants) if total > len(entrants) else None

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
                shown=windowed,
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
            except ServiceError as exc:
                # Usually another ender won the claim (the tick and
                # /giveaway_end firing together), but a giveaway that never ends
                # should leave a trace rather than look like a quiet pass.
                log.info("could not end %s: %s", gw.id, exc.message)
                continue
            except Exception:
                log.exception("end failed for %s", gw.id)
                continue
            try:
                await self._announce(ended, winners)
            except Exception:
                log.exception("announcement failed for %s", gw.id)
            try:
                await self._strip_entrants_role(ended)
            except Exception:
                log.exception("role strip failed for %s", gw.id)
        # Live timer: re-render active embeds every tick so the countdown
        # visibly ticks down. The soonest deadlines always get a slot; the rest
        # rotate, so no running giveaway is left showing a stale count.
        try:
            # One query feeds both consumers: the per-guild autocomplete cache
            # wants them all, the embeds below only the soonest ten.
            live = await asyncio.to_thread(self.service.list_all_active, 200)
        except Exception:
            log.exception("live list failed")
            return
        self._cache_autocomplete(live)
        await self._refresh_embeds(self._refresh_window(live))
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

    @tick.error
    async def _tick_error(self, error: Exception) -> None:
        log.error("tick loop crashed; restarting", exc_info=error)
        if not self.is_closed():
            self.tick.restart()

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

    async def _announce(
        self, gw: Giveaway, winners: list[str], *, reroll: bool = False
    ) -> None:
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
        if reroll:
            embed.title = "Giveaway Rerolled"
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
        mentions = discord.AllowedMentions(everyone=False, users=True, roles=True)
        try:
            if gw.message_id and not reroll:
                try:
                    msg = await channel.fetch_message(int(gw.message_id))
                    await msg.edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass
                else:
                    # An unsuccessful reply must not send a second winner embed.
                    if content:
                        await msg.reply(content, allowed_mentions=mentions)
                    return
            await channel.send(embed=embed, content=content, allowed_mentions=mentions)
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
            # A thread looks like a channel to the person running the command but
            # is not a TextChannel, and "no text channel available" hid that.
            problem = (
                "Giveaways need a text channel — a thread cannot hold one."
                if isinstance(interaction.channel, discord.Thread)
                else "No text channel available."
            )
            await interaction.response.send_message(problem, ephemeral=True)
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
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
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
        message_note = ""
        try:
            await asyncio.to_thread(svc.set_message, gw.id, str(msg.id))
        except Exception:
            log.exception("could not save message id for giveaway %s", gw.id)
            message_note = " (message tracking could not be saved)"
        role_note = ""
        if interaction.guild is not None:
            try:
                role = await interaction.guild.create_role(
                    name=f"🎉 {prize[:60]}",
                    mentionable=True,
                    reason=f"Entrants role for giveaway {gw.id}",
                )
                try:
                    await asyncio.to_thread(svc.set_entrants_role, gw.id, str(role.id))
                except Exception:
                    role_note = " (entrants role could not be saved)"
                    log.exception("could not save entrants role for giveaway %s", gw.id)
            except (discord.Forbidden, discord.HTTPException):
                role_note = " (no entrants role — I need **Manage Roles**)"
                log.warning("could not create entrants role in %s", interaction.guild.id)
        await bot._safe_followup(
            interaction, f"✅ Giveaway started: {msg.jump_url}{message_note}{role_note}"
        )

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
        await bot._announce(ended, fresh, reroll=True)
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
        try:
            channel = bot.get_channel(int(ended.channel_id))
        except (TypeError, ValueError):
            channel = None
        if isinstance(channel, discord.TextChannel) and ended.message_id:
            cancelled = discord.Embed(
                title="Giveaway Cancelled",
                description=f"🚫 **{ended.prize}** — this giveaway was cancelled.",
                colour=bot.settings.embed_color,
            )
            cancelled.set_footer(text=f"ID: {ended.id}")
            try:
                await channel.get_partial_message(int(ended.message_id)).edit(
                    embed=cancelled, view=None
                )
            except (discord.HTTPException, TypeError, ValueError):
                log.warning("could not close cancelled giveaway message %s", ended.id)
        await bot._strip_entrants_role(ended)
        if isinstance(channel, discord.TextChannel):
            notify = await bot._notify_mention(str(interaction.guild.id))
            text = f"🚫 Giveaway **{ended.prize}** was cancelled."
            if notify:
                text += f" {notify}"
            try:
                await channel.send(
                    text,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, users=False, roles=True
                    ),
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
                    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
                )
            else:
                ids = [str(row["user_id"]) for row in entrants]
                pairs = _split_mentions(ids, notice)
                capped = pairs[:MAX_MENTION_MESSAGES]
                for chunk, _ in capped:
                    await channel.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, users=True, roles=False
                        ),
                    )
                if len(pairs) > len(capped):
                    reached = sum(n for _, n in capped)
                    await bot._safe_followup(
                        interaction,
                        f"⏳ Extended, but only {reached}"
                        f" of {len(ids)} entrants could be pinged — recreate the"
                        " entrants role (delete it and re-run the command) to ping the rest.",
                    )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("extend notice failed for %s (%s)", fresh.id, exc)

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
            # Count and page separately: the total is what the header needs, and
            # only 50 of them are ever printed.
            total = await asyncio.to_thread(svc.entry_count, gw.id)
            entrants = await asyncio.to_thread(svc.entries, gw.id, 50)
            host = f" by <@{gw.host_id}>" if gw.host_id else ""
            chance = min(100.0, gw.winner_count / total * 100) if total else 0.0
            odds = (
                f"\n📊 Each entrant has a **{chance:.1f}%** chance"
                f" ({gw.winner_count} winner(s) / {total} entries)."
            )
            if not entrants:
                await bot._safe_followup(
                    interaction, f"🏆 **{gw.prize}**{host} — no entrants yet."
                )
                return
            shown = [f"<@{row['user_id']}>" for row in entrants]
            extra = f"\n…and {total - len(entrants)} more." if total > len(entrants) else ""
            status = "running 🟢" if gw.active else gw.status
            await bot._safe_followup(
                interaction,
                f"🏆 **{gw.prize}**{host} — **{total}** entrant(s) ({status})"
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
            # Ten lines carrying full 256-character prizes pass Discord's 2000
            # character cap, and the resulting 400 would be swallowed by
            # _safe_followup — the moderator would get no reply at all.
            prize = gw.prize if len(gw.prize) <= 80 else gw.prize[:77] + "…"
            lines.append(
                f"• **{prize}** — {n} entries{each} — `{gw.id}`"
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
        # Ack first: this command does an HTTP role edit plus a database write
        # before it can answer, and Discord only allows 3 seconds.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        if role is None:
            try:
                current = await asyncio.to_thread(svc.get_notify_role, gid)
            except Exception:
                current = None
            # A hand-edited non-numeric id must read as "unset", not crash the
            # command with a ValueError.
            if (
                current
                and str(current).isdigit()
                and interaction.guild.get_role(int(current)) is not None
            ):
                await bot._safe_followup(
                    interaction,
                    f"🔔 Notify role: <@&{current}> — give it to members so they"
                    " get pinged on giveaway news.",
                )
            else:
                await bot._safe_followup(
                    interaction,
                    "No notify role set. Run `/giveaway_notifyer role:@YourRole` once.",
                )
            return
        note = ""
        try:
            await role.edit(mentionable=True, reason="Giveaway notify role setup")
        except (discord.Forbidden, discord.HTTPException):
            # A role that cannot be made mentionable pings nobody. Saving it
            # anyway is fine, but claiming success is not.
            log.warning("could not make notify role %s mentionable", role.id)
            note = " — but I could not make it mentionable (I need **Manage Roles**)"
        try:
            await asyncio.to_thread(svc.set_notify_role, gid, str(role.id))
        except Exception:
            log.exception("notify role save failed")
            await bot._safe_followup(interaction, "⚠️ Could not save. Try again.")
            return
        await bot._safe_followup(
            interaction,
            f"✅ {role.mention} will now be pinged on every giveaway (new, winners,"
            f" rerolls, cancellations){note}.",
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
        # Discord accepts up to 6000 characters on an option but only allows
        # 2000 in a message, so a long announcement has to be cut here rather
        # than turning into a 400 that reads like a permissions problem.
        text = (text or "").strip()
        if len(text) > 1400:
            text = text[:1397] + "…"
        body = f"📢 **{gw.prize}**" + (f" — {text}" if text else "")
        role = bot._role_for(gw)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await bot._safe_followup(interaction, "Run this in a text channel.")
            return
        pairs: list[tuple[str, int]] = []
        try:
            if role is not None:
                await channel.send(
                    f"{body}\n{role.mention}",
                    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
                )
            else:
                ids = [str(row["user_id"]) for row in entrants]
                pairs = _split_mentions(ids, body)
                sent = 0
                for chunk, n in pairs[:MAX_MENTION_MESSAGES]:
                    await channel.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, users=True, roles=False
                        ),
                    )
                    sent += n
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("ping failed for %s (%s)", gw.id, exc)
            await bot._safe_followup(interaction, "⚠️ I cannot send messages there.")
            return
        if role is None and len(pairs) > MAX_MENTION_MESSAGES:
            await bot._safe_followup(
                interaction,
                f"📢 Pinged {sent} of {len(entrants)} entrant(s) — capped at"
                f" {MAX_MENTION_MESSAGES} messages without an entrants role."
                " Re-run once the role exists to reach the rest.",
            )
        else:
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
        # Ack first: the write below is a database round-trip, and a slow one
        # used to blow the 3-second window and surface as "the application did
        # not respond" even though the unblock had happened.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            removed = await asyncio.to_thread(
                svc.blacklist_remove, str(interaction.guild.id), str(user.id)
            )
        except Exception:
            log.exception("blacklist remove failed for %s", user.id)
            await bot._safe_followup(
                interaction, "⚠️ Could not update the blacklist. Try again."
            )
            return
        await bot._safe_followup(
            interaction,
            f"✅ {user.mention} can join giveaways again."
            if removed
            else f"{user.mention} was not on the blacklist.",
        )

    @bot.tree.command(name="giveaway_blacklist_list", description="Show blocked users")
    async def giveaway_blacklist_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            ids = await asyncio.to_thread(
                svc.blacklist_list, str(interaction.guild.id)
            )
            total = await asyncio.to_thread(
                svc.count_blacklisted, str(interaction.guild.id)
            )
        except Exception:
            log.exception("blacklist list failed")
            await bot._safe_followup(
                interaction, "⚠️ Could not load the blacklist. Try again."
            )
            return
        if not ids:
            await bot._safe_followup(interaction, "Blacklist is empty — nobody is blocked.")
            return
        # A mention is 21 characters and Discord rejects a message over 2000,
        # so the body stops at 80 — the same chunk size /giveaway_ping uses —
        # while the header keeps the real count instead of the shown one.
        shown = ids[:80]
        lines = "\n".join(f"<@{uid}>" for uid in shown)
        extra = f"\n…plus {total - len(shown)} more." if total > len(shown) else ""
        await bot._safe_followup(interaction, f"🚫 **Blocked ({total}):**\n{lines}{extra}")

    @bot.tree.command(
        name="giveaway_timeout_bans",
        description="List members banned from giveaways by the timed-out penalty",
    )
    async def giveaway_timeout_bans(interaction: discord.Interaction) -> None:
        """Who is sitting out the penalty for joining while timed out."""
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        guild = interaction.guild
        # Ack first: two database round-trips before the answer used to be able
        # to exceed the 3-second window.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            rows = await asyncio.to_thread(svc.list_timeout_bans, str(guild.id))
            # A second cheap query: the listing is capped, and the embed has to
            # state the real total instead of the size of the page set.
            total = await asyncio.to_thread(svc.count_timeout_bans, str(guild.id))
        except Exception:
            log.exception("timeout ban list failed")
            await bot._safe_followup(
                interaction, "⚠️ Could not load the timeout bans. Try again."
            )
            return
        if not rows:
            await bot._safe_followup(
                interaction, "No users are currently banned from giveaways."
            )
            return
        # Only from the member cache: a fetch per row would be one HTTP request
        # per banned member for what is a snapshot anyway. With nothing cached
        # (members intent off) every id would look like it had left, so plain
        # mentions are used instead of an invented label.
        cached = bool(guild.members)
        for row in rows:
            row["in_guild"] = not cached or _still_in_guild(guild, row["user_id"])
        per_page = ParticipantsPages.PAGE_SIZE
        pages = max(1, (len(rows) + per_page - 1) // per_page)
        color = bot.settings.embed_color

        def render(page: int) -> discord.Embed:
            start = page * per_page
            return embeds.timeout_bans_embed(
                rows=rows[start : start + per_page],
                page=page,
                pages=pages,
                total=total,
                hidden=max(0, total - len(rows)),
                color=color,
            )

        try:
            await interaction.followup.send(
                embed=render(0),
                view=ParticipantsPages(render=render, pages=pages),
                ephemeral=True,
            )
        except (discord.NotFound, discord.HTTPException):
            pass


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
    loop = asyncio.get_running_loop()
    shutdown_task: asyncio.Task | None = None

    def shutdown() -> None:
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(bot.close())

    try:
        loop.add_signal_handler(signal.SIGTERM, shutdown)
    except NotImplementedError:  # Windows event loops do not support add_signal_handler.
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(shutdown))
    try:
        async with bot:
            await bot.start(settings.bot_token)
    finally:
        if shutdown_task is not None:
            await shutdown_task


def run_forever(settings: Settings) -> None:
    asyncio.run(amain(settings))
