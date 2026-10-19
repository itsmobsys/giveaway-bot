"""Standalone bot: slash commands + buttons + auto-draw timer. No dashboard."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import re
import signal
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import embeds
from .config import Settings
from .db import Database
from .redis_cache import RedisCache
from .service import TIMEOUT_BAN_KIND, Giveaway, GiveawayService, PartialFlush, ServiceError
from .views import ClaimView, GiveawayView, ParticipantsPages

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
    def __init__(self, settings: Settings, db: Database, redis=None) -> None:
        super().__init__(command_prefix="!", intents=build_intents(), help_command=None)
        self.settings = settings
        self.db = db
        #: Shared short-TTL read cache + concurrency damper. Built once in
        #: amain() next to db (so it survives login retries) and passed in;
        #: None (or an unconfigured cache) means every read goes to Turso
        #: exactly as before, which is also what every existing test builds.
        #: Turso stays the single source of truth: nothing here is ever
        #: confirmed to a member from cached data alone.
        self.redis = redis
        self.service = GiveawayService(db, redis)
        #: guild_id -> [(giveaway_id, prize)] rebuilt every tick. Discord kills
        #: autocomplete after 3s and a Turso round-trip can exceed that, so
        #: suggestions come from this snapshot — never from the database.
        self._autocomplete_cache: dict[str, list[tuple[str, str]]] = {}
        #: Same snapshot for ended giveaways. Reroll/skipclaim only work on
        #: ended rows, and the live cache above never contains one — without
        #: this they would suggest nothing usable.
        self._ended_autocomplete_cache: dict[str, list[tuple[str, str]]] = {}
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
        #: Bounded DB lane: Turso serializes writes and every to_thread call
        #: opens its own thread-local connection (TLS + auth on cold threads).
        #: An unbounded burst of 100 joins would spawn ~200 default-pool
        #: threads and ~200 streams. 32 workers keep ~100 concurrent joins
        #: flowing with headroom for ticks/commands; the semaphore caps live
        #: Turso streams at 24 so bursts queue in memory (cheap) instead of
        #: opening streams (expensive) that then fight over one writer.
        # Pinned lanes: 50 concurrent joins each need ~3 quick waves
        # (snapshot + insert + refresh). One process-wide pool (64 workers)
        # shared by every bot instance: per-bot pools leaked 64 threads per
        # unit-test bot and stalled shutdown. 64 lanes cover a 50-burst with
        # headroom for ticks/commands; bursts queue in memory (cheap) instead
        # of opening 200 streams (expensive) that fight over one writer.
        self._db_gate = asyncio.Semaphore(64)
        #: Leading-edge refresh debounce: giveaway_id -> last epoch seconds.
        #: A 100-click burst would PATCH the same embed 100 times (~2s of
        #: rate-limit burn); the first click refreshes immediately, the rest
        #: within the window reuse the ack-time snapshot count. Failures never
        #: populate the stamp so a dead edit retries on the next click.
        self._refresh_stamp: dict[str, float] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        #: P1-2 role pacing: 50 concurrent add_roles HTTP calls trip the
        #: per-route rate limit. Five at a time keeps grants flowing while
        #: Discord stays happy; the ack already went out, so waiting here
        #: only delays handler return, never the member's confirmation.
        self._role_gate = asyncio.Semaphore(5)
        #: Hot giveaway header cache: giveaway_id -> (Giveaway, monotonic ts).
        #: Immutable mid-burst fields (prize/ends_at/status/roles) re-read per
        #: click otherwise cost 1 RTT each; 50 clicks = 50 identical reads.
        #: TTL 2s; invalidated on end/cancel. Eligibility (ban/msgs/hit) is
        #: NEVER cached — always read live per click. Not a dupe-cache: the
        #: atomic INSERT guard remains the source of truth.
        self._gw_cache: dict[str, tuple[object, float]] = {}
        #: entered_latch: giveaway_id -> {user_id: monotonic ts} for joins
        #: that COMMITTED in this process. A second tap of the same button
        #: (~1 in 20 members, two gateway events) is refused with the exact
        #: service text after one dict lookup and zero network. Deliberately
        #: tiny and self-expiring: only ever used to SHORT-CIRCUIT A REFUSAL,
        #: so a miss (eviction, restart, another process) simply runs the
        #: normal path where the atomic INSERT guard still decides.
        self._entered: dict[str, dict[str, float]] = {}
        self._last_list_refresh: float = 0.0
        #: One-shot guild-command cleanup flag (see on_ready). Discord fires
        #: on_ready on every reconnect, not just startup, so the sweep below
        #: must run once per process, never per reconnect.
        self._guild_cmd_cleanup_done = False

    # -- bounded DB lane --------------------------------------------------
    #: Seconds a single DB call may hold a lane before the join gives up.
    #: Turso warm RTT is ~0.1s; 8s is generations of headroom, but a hung
    #: stream must never park a click (or a lane) forever.
    DB_CALL_TIMEOUT = 8.0

    #: Leading-edge refresh window in seconds (see _refresh_stamp above).
    REFRESH_DEBOUNCE_SECONDS = 2.0

    #: Process-wide pinned pool: 64 workers shared by every bot instance.
    #: Created once at first use so unit tests that only build bots (never a
    #: loop) leak nothing. The default executor grows lazily and opens a
    #: fresh TLS+auth Turso stream per cold thread (your 342ms cold); this one
    #: is warm after the first burst and reused for the process lifetime.
    _DB_POOL: concurrent.futures.ThreadPoolExecutor | None = None

    @classmethod
    def _pool(cls) -> concurrent.futures.ThreadPoolExecutor:
        pool = cls._DB_POOL
        if pool is None:
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=64, thread_name_prefix="gw-db"
            )
            cls._DB_POOL = pool
        return pool

    async def _flush_invalidation(self) -> None:
        """Drain the service's pending cache invalidations. Never raises.

        The service records key suffixes inside its synchronous write paths
        (which cannot await Redis); this runs on the event loop immediately
        after that write returns, so the next read is live. A Redis outage
        only means the keys expire by TTL instead -- correctness lives in
        Turso, never in this cache.
        """
        try:
            await self.service.flush_invalidations()
        except Exception:
            log.debug("cache invalidation flush failed", exc_info=True)


    async def _db_call(self, func, *args, **kwargs):
        """Run a blocking service/DB call on the pinned pool, gated."""
        loop = asyncio.get_running_loop()
        pool = self._pool()
        async with self._db_gate:
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(pool, lambda: func(*args, **kwargs)),
                    timeout=self.DB_CALL_TIMEOUT,
                )
            except TimeoutError:
                log.warning("db call %s timed out", getattr(func, "__name__", func))
                raise

    #: Seconds a cached giveaway header stays valid mid-burst.
    GW_CACHE_TTL = 2.0
    #: How long one process's claim on the tick's live-list sweep lasts. The
    #: window itself is 120s, so a lease shorter than that can never skip a
    #: window: after a sweep at t=120 the lease is gone by t=210 while the
    #: next one is not due until t=240. Redis can only *narrow* duplicate
    #: work here -- never a correctness input.
    SWEEP_LEASE_SECONDS = 90.0

    def _gw_cached(self, giveaway_id: str):
        """Fresh cached header, or None on miss/expiry/corruption."""
        try:
            entry = self._gw_cache.get(giveaway_id)
        except Exception:
            return None
        if not entry:
            return None
        try:
            gw, ts = entry
        except (TypeError, ValueError):
            return None
        try:
            if time.monotonic() - float(ts) > self.GW_CACHE_TTL:
                return None
        except Exception:
            return None
        return gw

    def _gw_store(self, giveaway_id: str, gw) -> None:
        try:
            self._gw_cache[giveaway_id] = (gw, time.monotonic())
        except Exception:
            pass

    def _gw_drop(self, giveaway_id: str) -> None:
        try:
            self._gw_cache.pop(giveaway_id, None)
        except Exception:
            pass

    # -- duplicate-click latch (process-local) ----------------------------
    #: Seconds an entry marker stays hot. It only has to outlive a double tap
    #: (two gateway events, milliseconds apart), so 120s is generous while
    #: keeping the whole map tiny; the database remains the real guard.
    ENTERED_TTL = 120.0
    #: Hard cap on tracked giveaways, so a very long uptime with many
    #: giveaways cannot grow this map without bound.
    ENTERED_MAX_GIVEAWAYS = 64

    def _entered_recently(self, giveaway_id: str, user_id: str) -> bool:
        """True only if THIS process saw this member's join commit.

        A False here means "unknown", never "not entered": the caller then
        runs the ordinary path and Turso decides. Expired markers are pruned
        on read so no timer is needed."""
        try:
            members = self._entered.get(giveaway_id)
            if not members:
                return False
            ts = members.get(user_id)
            if ts is None:
                return False
            if time.monotonic() - ts > self.ENTERED_TTL:
                members.pop(user_id, None)
                return False
            return True
        except Exception:
            return False

    def _mark_entered(self, giveaway_id: str, user_id: str) -> None:
        """Record a committed join. Called only after the write returned."""
        try:
            members = self._entered.get(giveaway_id)
            if members is None:
                if len(self._entered) >= self.ENTERED_MAX_GIVEAWAYS:
                    self._entered.clear()
                members = {}
                self._entered[giveaway_id] = members
            members[user_id] = time.monotonic()
        except Exception:
            pass

    def _clear_entered(self, giveaway_id: str, user_id: str) -> None:
        """Forget one member's marker (they left, or were dropped)."""
        try:
            members = self._entered.get(giveaway_id)
            if members is not None:
                members.pop(user_id, None)
        except Exception:
            pass

    def _should_refresh(self, giveaway_id: str) -> bool:
        """Leading-edge debounce: first click refreshes, burst mates skip."""
        now = time.monotonic()
        try:
            last = self._refresh_stamp.get(giveaway_id, 0.0)
        except Exception:
            last = 0.0
        if now - last < self.REFRESH_DEBOUNCE_SECONDS:
            return False
        try:
            self._refresh_stamp[giveaway_id] = now
        except Exception:
            pass
        return True

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
        # Duplicate-command cleanup (one-shot per process): aa22c23 mirrored
        # the global tree into every guild for instant visibility, but that
        # left TWO registrations per command (global + guild copy), so every
        # slash command now renders twice. Guild sync offers no lasting win
        # here — the global tree already propagates on its own — so clear
        # the guild copies once and go back to global-only. Runs once because
        # on_ready fires on every reconnect, and an empty guild sync is the
        # supported way to wipe guild-scoped leftovers without touching global.
        if not self._guild_cmd_cleanup_done:
            self._guild_cmd_cleanup_done = True
            try:
                for guild in list(self.guilds):
                    try:
                        self.tree.clear_commands(guild=guild)
                        await self.tree.sync(guild=guild)
                    except Exception:
                        log.exception(
                            "guild command cleanup failed for %s",
                            getattr(guild, "id", "?"),
                        )
                log.info(
                    "guild command cleanup done (%d guild(s)); global-only from here",
                    len(self.guilds),
                )
            except Exception:
                log.exception("guild command cleanup sweep failed")
        try:
            # Off the event loop: this is a network round-trip, and blocking
            # here delays the gateway heartbeat that keeps the bot online.
            live = await asyncio.to_thread(self.service.list_all_active, 200)
        except Exception:
            log.exception("could not preload active giveaways")
            live = []
        log.info("tracking %d active giveaway(s) from the database", len(live))
        # P1-1 warmup: spin all 64 pool threads now so the first real burst
        # never pays thread-spawn + TLS + Turso-auth per cold lane (~342ms).
        # One cheap SELECT per lane, every lane: a partial warm left most lanes
        # cold, so the first burst still paid ~340ms+ on them. Failures only
        # log (first burst warms it).
        try:
            pool = self._pool()
            warm = asyncio.get_running_loop()
            # return_exceptions=True: one flaky lane must not fail the other
            # 63 warms, and a single reconnect storm must not read as fatal.
            results = await asyncio.gather(
                *(
                    warm.run_in_executor(pool, self.db.query_one, "SELECT 1", ())
                    for _ in range(64)
                ),
                return_exceptions=True,
            )
            failed = sum(1 for r in results if isinstance(r, Exception))
            if failed:
                log.warning("db pool warmup: %d/64 lane(s) failed", failed)
        except Exception:
            log.exception("db pool warmup failed")
        self._cache_autocomplete(live)
        try:
            ended_pre = await asyncio.to_thread(self.service.list_all_ended, 200)
        except Exception:
            log.exception("could not preload ended giveaways")
            ended_pre = []
        self._cache_ended_autocomplete(ended_pre)
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

    def _spawn_bg(self, coro) -> None:
        """Fire-and-forget post-ack work that must never block the next click.

        Role grants + embed PATCHes run here so the ack returns while Discord
        API calls finish in the background. Each step is isolated upstream;
        failures only log. Tracked in _bg_tasks (cancelled in close()) so a
        shutdown never strands a bare task the loop then warns about."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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
        for task in list(self._bg_tasks):
            task.cancel()
        # Shared _DB_POOL lives for the process lifetime: shutting it here
        # would break every later test-bot in the same suite run. Threads are
        # daemon-idle after bursts; the OS reclaims them at process exit.
        await super().close()
        # Every thread's connection, not just this one's.
        self.db.close_all()
        # The Redis client deliberately outlives this bot: amain() owns it and
        # closes it once at the end, so a login retry reuses the same pool
        # instead of leaving a discarded client behind (see amain).

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
            claim=self.handle_claim,
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

    def _cache_ended_autocomplete(self, ended: list[Giveaway]) -> None:
        """Rebuild the per-guild snapshot for ended giveaways.

        Reroll/skipclaim only accept ended rows, and the live cache above
        never holds one — without this twin they suggest nothing usable.
        Same guild-keyed shape so one server cannot crowd out another.
        """
        cache: dict[str, list[tuple[str, str]]] = {}
        for gw in ended:
            cache.setdefault(gw.guild_id, []).append((gw.id, gw.prize))
        self._ended_autocomplete_cache = cache

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

    async def _refresh_embed(
        self, gw: Giveaway, *, entry_count: int | None = None,
        live_status: str | None = None,
    ) -> str | None:
        if not gw.active or not gw.message_id:
            return None
        # B05: every refresh re-checks live status immediately before the
        # Discord edit. The gw snapshot may pre-date an end()/cancel() that
        # flipped status since (join fast-path race, or the tick's own live
        # list going stale between list_all_active and the edit). Editing now
        # would overwrite the winner announcement _announce just posted.
        # Join fast-path passes live_status from its refresh_snapshot so the
        # common case costs zero extra trips; None keeps the old recheck.
        # Tick budgets (REFRESH_CONCURRENCY/PER_TICK/ROTATING) are unchanged.
        if live_status is None:
            try:
                live_status = await self._db_call(self.service.get_status, gw.id)
            except Exception:
                log.exception("refresh status recheck failed for %s", gw.id)
                return None
        if live_status != "active":
            return live_status
        try:
            channel = self.get_channel(int(gw.channel_id))
            message_id = int(gw.message_id)
        except (TypeError, ValueError):
            return None
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            if entry_count is None:
                # Display-only fallback. entry_count_cached is async (the
                # Turso read it may fall back to is blocking), so it is
                # awaited directly: _db_call runs its callable in a worker
                # thread, which would return this coroutine un-awaited and
                # print its repr into the embed's entry count.
                count = await self.service.entry_count_cached(gw.id)
            else:
                count = entry_count
            # A partial message edits by id. fetch_message() would spend a full
            # GET per giveaway per tick on a message object nothing here reads.
            await channel.get_partial_message(message_id).edit(
                embed=embeds.giveaway_embed(gw, count, self.settings.embed_color),
                view=GiveawayView(gw.id, self.settings.dashboard_url),
            )
            return live_status
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
    #: may pile up before an early flush. 15s halves flush writes competing
    #: with join bursts on Turso's single writer; chatty servers still flush
    #: early at 5000 buffered users.
    MESSAGE_FLUSH_SECONDS = 15
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
            role_id = await self.service.get_notify_role_cached(guild_id)
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

    async def _grant_entrants_role(
        self, gw: Giveaway, member: discord.Member, live_status: str | None = None
    ) -> None:
        role = self._role_for(gw)
        if role is None:
            return
        try:
            async with self._role_gate:
                await member.add_roles(role, reason=f"Joined giveaway {gw.id}")
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not grant entrants role for %s", gw.id)
            return
        # Grant-after-end TOCTOU: end() may have flipped status during the
        # add_roles call. Re-check live status after the grant; when the
        # giveaway is no longer active, take the role back immediately so the
        # member is not left holding the ping-everyone role of an ended
        # giveaway. Fail-closed on lookup failure (take) so a DB blip can
        # never leave a stale role behind; the entry itself is untouched.
        # The join background worker passes the snapshot status it already
        # read, so the common case costs zero extra trips; live_status=None
        # (tick/legacy callers) keeps the old recheck behaviour.
        live = live_status
        if live is None:
            try:
                live = await self._db_call(self.service.get_status, gw.id)
            except Exception:
                log.exception("post-grant status recheck failed for %s", gw.id)
                live = None
        if live != "active":
            try:
                await member.remove_roles(role, reason=f"Giveaway {gw.id} ended")
            except (discord.Forbidden, discord.HTTPException):
                pass
            log.info("revoked stale entrants role for %s (ended during grant)", gw.id)

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
        guild_id_str = str(interaction.guild.id)
        # entered_latch: a plain in-process set of (giveaway, user) ids whose
        # join COMMITTED in this process. It answers the second tap with the
        # exact refusal text the service gives, for one dict lookup (zero
        # network), and it only ever SHORT-CIRCUITS A REFUSAL: a missing
        # marker runs the normal path, and the database still decides.
        if self._entered_recently(giveaway_id, uid):
            await self._safe_followup(interaction, "⚠️ You are already entered.")
            return
        # Hot-header shortcut: immutable fields served from a 2s cache so a
        # 50-burst pays 1 header read, not 50. Eligibility still goes to the
        # DB per click via eligibility_snapshot (never cached).
        try:
            cached = self._gw_cached(giveaway_id)
            if cached is not None and str(getattr(cached, "guild_id", "")) == guild_id_str:
                gw = cached
                try:
                    snapshot = await self._db_call(
                        self.service.eligibility_snapshot, guild_id_str, uid, giveaway_id
                    )
                except Exception:
                    snapshot = None
            else:
                # Fast-path: one round-trip returns the giveaway row + the 4
                # eligibility subqueries (was: 2 parallel queries on 2 threads).
                gw, snapshot = await self._db_call(
                    self.service.join_snapshot, giveaway_id, guild_id_str, uid
                )
                self._gw_store(giveaway_id, gw)
        except ServiceError:
            # Log the id plus what IS live: tells a stale button (id absent
            # from the live list) apart from a phantom empty read (id present
            # but the lookup missed it).
            try:
                live = await self._db_call(
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
        join_kwargs = {
            "user_id": uid,
            "username": name,
            "member_roles": roles,
            "account_created_ts": created_ts,
            # Messages this member sent since the last flush still count.
            "pending_messages": self._pending_messages(guild_id_str, uid),
            "timed_out": timed_out,
            "snapshot": snapshot,
        }
        try:
            # P0-2 single-lane bundle: join() + refresh_snapshot() share one
            # _db_call (one semaphore acquire, one thread hop). Same 3
            # statements in the same order -- only lane overhead is removed.
            # Falls back to split waves if a test stubs join() with an
            # incompatible signature (e.g. join_then_end wrappers).
            try:
                status, count = await self._db_call(
                    self.service.join_and_refresh, gw, **join_kwargs
                )
            except TypeError:
                # Test double replaced join() with an incompatible wrapper
                # (e.g. join_then_end): fall back to split waves.
                await self._db_call(
                    self.service.join, gw, **join_kwargs, fetch_count=False
                )
                try:
                    status, count = await self._db_call(
                        self.service.refresh_snapshot, giveaway_id
                    )
                except Exception:
                    log.exception("post-join refresh lookup failed for %s", giveaway_id)
                    self._mark_entered(giveaway_id, uid)
                    await self._safe_followup(interaction, "🎟️ You're in!")
                    return
            except ServiceError:
                # join() refused (already entered / banned / penalty): the
                # outer handler reports it with the refusal text + role
                # cleanup. Re-raise untouched -- swallowing it here as a
                # dead-read would turn every refusal into "You're in!".
                raise
            except Exception:
                # join_and_refresh already bundles join + refresh in one lane:
                # a dead refresh read inside the bundle raises here instead of
                # at the old standalone call. Commit landed, so ack honestly
                # with the generic message (same as the old dead-read path).
                log.exception("post-join refresh lookup failed for %s", giveaway_id)
                self._mark_entered(giveaway_id, uid)
                await self._safe_followup(interaction, "🎟️ You're in!")
                return
        except ServiceError as exc:
            await self._safe_followup(interaction, f"⚠️ {exc.message}")
            if exc.kind == TIMEOUT_BAN_KIND:
                # The gate dropped the entry they already had, so the entrants
                # role it earned has to go too — in every active giveaway
                # whose entries the service dropped, not just this one.
                # The same sweep clears this process's entry markers: their
                # joins no longer exist, so a cached "already entered" would
                # otherwise refuse their next honest click.
                try:
                    active = await self._db_call(self.service.list_active, gw.guild_id)
                except Exception:
                    log.exception("timeout role lookup failed for %s", uid)
                else:
                    for live in active:
                        self._clear_entered(live.id, uid)
                        try:
                            await self._take_entrants_role(live, uid)
                        except Exception:
                            log.exception(
                                "timeout role take failed for %s/%s", live.id, uid
                            )
            return
        except Exception:
            log.exception("join failed for %s", giveaway_id)
            await self._safe_followup(interaction, "⚠️ Could not enter you. Try again.")
            return
        # Bundle already returned status + count in the same lane (commit
        # landed, so a dead read still acks honestly, just without the
        # number). The same pair feeds grant-vs-take + refresh with zero
        # further trips.
        # join() queued the keys its own write just made wrong (the cached
        # display count). The DEL runs as background work: a cache hop must
        # never sit between the commit and the member's answer, and on a
        # blackholed Redis one awaited DEL costs the full cache timeout. The
        # number the member is told comes out of the join snapshot already in
        # hand, never from the cache, and anything that does read the count
        # heals on its 4s TTL -- Turso decided this entry, so a stale display
        # number is cosmetic.
        self._spawn_bg(self._flush_invalidation())
        # A committed join is the only thing that sets this marker: the
        # refusal branch above never reaches here, so a refused click can
        # never poison the next one.
        self._mark_entered(giveaway_id, uid)
        if status is None:
            await self._safe_followup(interaction, "🎟️ You're in!")
            return
        # Ack first: the entry is committed, so the count is honest even if
        # Discord API work below fails. Post-ack steps stay awaited (not
        # fire-and-forget): each handle_join runs as its own asyncio task, so
        # 50 concurrent clicks still overlap their role grants — awaiting here
        # only delays the handler return, never the ack, and keeps tests +
        # shutdown deterministic (no orphan tasks after the loop closes).
        await self._safe_followup(interaction, f"🎟️ You're in! Entry #{count}.")
        if status == "active":
            # Each post-ack step is isolated: a role-grant failure must not
            # cancel the embed refresh (or vice versa), and neither may undo
            # the valid entry or crash the button callback after the ack.
            # Debounce: every click grants the role, but only the first in
            # the window PATCHes the embed (a 100-click burst re-rendered it
            # 100 times for one visible change).
            steps = [self._grant_entrants_role(gw, member, live_status=status)]
            if self._should_refresh(giveaway_id):
                steps.append(
                    self._refresh_embed(gw, entry_count=count, live_status=status)
                )
            results = await asyncio.gather(*steps, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.exception("post-join background step failed for %s", giveaway_id)
        else:
            # The draw won the race: this member joined in the same instant the
            # giveaway ended, and the strip pass may already have walked past
            # them. Granting the role now would leave them holding the
            # ping-everyone role of a giveaway that is over. The entry stays
            # (it landed before the draw claimed status) but missed this draw;
            # say so so "You're in!" above is not the last word.
            try:
                await self._take_entrants_role(gw, uid)
            except Exception:
                log.exception("post-join role take failed for %s", giveaway_id)
            await self._safe_followup(
                interaction,
                "This giveaway just ended — your entry arrived too late for the draw.",
            )

    async def _reply(self, interaction: discord.Interaction, text: str) -> None:
        """Win 1 (direct reply): one Discord RTT instead of defer + followup.

        When nothing answered yet, response.send_message completes the click
        in a single call (~120ms saved). After a defer (slow path) it falls
        through to the followup. Ephemeral in both cases."""
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(text, ephemeral=True)
                return
            await interaction.followup.send(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as exc:
            # The ack never reached the member: log so a silent join (entry
            # stored, user told nothing) leaves a trace instead of looking
            # like the button did nothing.
            log.warning("reply failed (%s): %s", type(exc).__name__, exc)

    async def _safe_followup(self, interaction: discord.Interaction, text: str) -> None:
        """Followup send that never raises; a silent ack leaves a warning."""
        try:
            await interaction.followup.send(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as exc:
            log.warning("reply failed (%s): %s", type(exc).__name__, exc)

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
        # A leave undoes the entry the latch remembers, so the marker goes
        # first: otherwise the same member's next join would be refused with
        # "already entered" for up to ENTERED_TTL.
        if removed:
            self._clear_entered(giveaway_id, str(interaction.user.id))
        # Reply first, drop the queued keys afterwards. leave() queued the
        # display-count key it just made wrong, but a slow Redis DEL must
        # never sit between the member's action and their answer: the count
        # key heals on its 4s TTL either way, and the refreshed embed below
        # is what actually shows the new total. The DEL runs on a background
        # task so a blackholed Redis (one DEL = the full 4s timeout) cannot
        # delay it either.
        await self._safe_followup(
            interaction, "You left the giveaway." if removed else "You were not entered."
        )
        self._spawn_bg(self._flush_invalidation())
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

    async def handle_claim(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        if not await self._safe_defer(interaction):
            return
        try:
            await asyncio.to_thread(
                self.service.claim, giveaway_id, str(interaction.user.id)
            )
        except ServiceError as exc:
            await self._safe_followup(interaction, f"\u26a0\ufe0f {exc.message}")
            return
        except Exception:
            log.exception("claim failed for %s", giveaway_id)
            await self._safe_followup(interaction, "\u26a0\ufe0f Could not record your claim. Try again.")
            return
        log.info("claim recorded for %s/%s", giveaway_id, interaction.user.id)
        await self._safe_followup(interaction, "\U0001f381 Prize claimed! A host will contact you.")

    # -- auto-draw timer -------------------------------------------------
    #: Live/ended autocomplete lists refresh at most this often inside tick.
    #: Rebuilding + re-rendering embeds every 30s pass costs 2 list queries +
    #: N PATCHes even when nothing changed; a 50-burst landing on a tick
    #: would share its lanes with that sweep. Due/claims stay per-tick
    #: (correctness); the cosmetic half is gated here.
    LIST_REFRESH_SECONDS = 120.0

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        try:
            due = await self._db_call(self.service.due)
        except Exception:
            log.exception("due check failed")
            return
        for gw in due:
            try:
                # B04: stale due rows must not beat a concurrent extend — the
                # flip re-checks ends_at itself and refuses when extended.
                ended, winners = await self._db_call(
                    self.service.end, gw.id, require_due=True
                )
            except ServiceError as exc:
                # Usually another ender won the claim (the tick and
                # /giveaway_end firing together), but a giveaway that never ends
                # should leave a trace rather than look like a quiet pass.
                log.info("could not end %s: %s", gw.id, exc.message)
                continue
            except Exception:
                log.exception("end failed for %s", gw.id)
                continue
            self._gw_drop(gw.id)
            # end() queued the keys its flip invalidated (header, guild list,
            # global list, count). Fire-and-forget so a slow Redis DEL never
            # delays the announcement, which is what members actually see.
            self._spawn_bg(self._flush_invalidation())
            try:
                await self._announce(ended, winners)
            except Exception:
                log.exception("announcement failed for %s", gw.id)
            try:
                await self._strip_entrants_role(ended)
            except Exception:
                log.exception("role strip failed for %s", gw.id)
        # Claim expiry: sweep due pending claims, expire + auto-reroll.
        # Restart recovery lives here too: deadlines are durable rows, so a
        # restart just picks up whatever is due on the next tick.
        try:
            due_claims = await self._db_call(self.service.due_claims)
        except Exception:
            log.exception("due claims check failed")
            due_claims = []
        for row in due_claims:
            gid = str(row.get("giveaway_id") or "")
            uid = str(row.get("user_id") or "")
            if not gid or not uid:
                continue
            try:
                ended_claim, fresh = await self._db_call(
                    self.service.expire_claim, gid, uid
                )
            except ServiceError as exc:
                log.info("could not expire claim %s/%s: %s", gid, uid, exc.message)
                continue
            except Exception:
                log.exception("claim expiry failed for %s/%s", gid, uid)
                continue
            log.info("claim expired for %s/%s; replacement=%s", gid, uid, fresh)
            # expire_claim() rerolled the winner, so the count + list keys it
            # invalidated are dropped in the background (same reason as end).
            self._spawn_bg(self._flush_invalidation())
            if fresh:
                try:
                    await self._announce(ended_claim, fresh, reroll=True)
                except Exception:
                    log.exception("replacement announce failed for %s", gid)
        # Live timer: re-render active embeds so the countdown visibly ticks
        # down. Cosmetic half is gated to LIST_REFRESH_SECONDS: due/claims
        # above run every tick (correctness), but these 2 list queries + N
        # PATCHes would otherwise steal lanes from a 50-burst landing mid-tick.
        # First tick always refreshes (cold autocomplete caches). Autocomplete
        # stays usable between refreshes; embeds just tick slower.
        now_mono = time.monotonic()
        last_list = getattr(self, "_last_list_refresh", 0.0)
        due = now_mono - last_list >= self.LIST_REFRESH_SECONDS or not self._autocomplete_cache
        # One poll body per window across processes. The free plan restarts
        # (deploys, OOM, idle spin-down) and an old instance overlaps a new
        # one for a few seconds; both would otherwise run the identical two
        # list reads plus every embed edit, and the loser's edits can land
        # after the winner's, briefly re-widening a countdown that was just
        # re-rendered. A lease makes one of them do the work.
        # A refused or unreachable Redis yields no lease, and the sweep then
        # runs exactly as it did before Redis existed: this can never skip
        # the sweep, only ever narrow who runs it.
        lease: str | None = None
        may_sweep = True
        if due and self.redis is not None:
            try:
                lease, answered = await self.redis.acquire_checked(
                    "sweep:live", self.SWEEP_LEASE_SECONDS
                )
            except Exception:
                lease, answered = None, False
            # Only a real answer may narrow the sweep. "Somebody else holds
            # it" means another instance is already doing this pass; "no
            # answer" (over budget, down, timed out) must leave the tick
            # exactly as it was before Redis existed, or a broken cache
            # would quietly freeze the lists and every countdown.
            may_sweep = lease is not None or not answered
        try:
            if due and may_sweep:
                live = await self._db_call(self.service.list_all_active, 200)
                self._cache_autocomplete(live)
                try:
                    ended_list = await self._db_call(self.service.list_all_ended, 200)
                except Exception:
                    log.exception("ended list failed")
                    ended_list = []
                self._cache_ended_autocomplete(ended_list)
                self._last_list_refresh = now_mono
                await self._refresh_embeds(self._refresh_window(live))
        except Exception:
            # A failed live-list read used to return from here and leak the
            # lease: this process would then get its OWN token back on the
            # next tick and read it as "somebody else is sweeping", skipping
            # the window on every instance for the rest of the 90s TTL. The
            # sweep body is exactly as unconditional as it was before Redis;
            # only freeing the lease is new, and it happens on every path.
            log.exception("live list failed")
        finally:
            if lease is not None:
                try:
                    await self.redis.release("sweep:live", lease)
                except Exception:
                    log.debug("sweep lease release failed", exc_info=True)
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
        claim_deadline_ms: int | None = None
        claim_deadlines: dict[str, int] = {}
        claim_view = None
        try:
            if gw.claim_enabled and winners:
                pending = await asyncio.to_thread(self.service.pending_claims, gw.id)
                for r in pending:
                    uid = str(r.get("user_id") or "")
                    if uid in winners and r.get("deadline_ms"):
                        try:
                            claim_deadlines[uid] = int(r.get("deadline_ms") or 0)
                        except (TypeError, ValueError):
                            continue
                claim_deadlines = {k: v for k, v in claim_deadlines.items() if v}
                if claim_deadlines:
                    claim_deadline_ms = min(claim_deadlines.values())
                    claim_view = ClaimView(gw.id)
        except Exception:
            log.exception("claim lookup failed for %s", gw.id)
            claim_deadline_ms = None
            claim_deadlines = {}
            claim_view = None
        embed = embeds.winner_embed(
            gw, winners, entries, self.settings.embed_color,
            claim_deadline_ms, claim_deadlines or None,
        )
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
                    await msg.edit(embed=embed, view=claim_view)
                except discord.HTTPException:
                    pass
                else:
                    # An unsuccessful reply must not send a second winner embed.
                    if content:
                        await msg.reply(content, allowed_mentions=mentions)
                    return
            await channel.send(embed=embed, content=content, view=claim_view, allowed_mentions=mentions)
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

    async def _gw_ended_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest ended giveaways for reroll/skipclaim.

        Same cache-never-database rule as above: the live cache never holds
        an ended row, so without this twin those commands suggest nothing
        usable and operators must paste raw ids.
        """
        try:
            if interaction.guild is None:
                return []
            gid = str(interaction.guild.id)
            needle = (current or "").lower()
            choices = [
                app_commands.Choice(name=f"🏆 {prize} ({gw_id})"[:100], value=gw_id)
                for (gw_id, prize) in bot._ended_autocomplete_cache.get(gid, [])
                if not needle or needle in prize.lower() or needle in gw_id.lower()
            ]
            return choices[:25]
        except Exception:
            log.exception("ended autocomplete failed")
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
        claim_minutes="Winner claim window in minutes, 0 = off (optional)",
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
        claim_minutes: int = 0,
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
                claim_timeout_seconds=max(0, claim_minutes) * 60,
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
        bot._gw_drop(gw.id)
        await bot._announce(ended, winners)
        await bot._strip_entrants_role(ended)
        await bot._safe_followup(interaction, f"Ended with {len(winners)} winner(s).")

    @bot.tree.command(name="giveaway_reroll", description="Draw new winner(s)")
    @app_commands.autocomplete(giveaway_id=_gw_ended_autocomplete)
    @app_commands.describe(
        giveaway_id="Ended giveaway to reroll",
        count="How many new winners to draw (ignored when user is picked)",
        user="Replace only this winner (e.g. the one who never claimed)",
    )
    async def giveaway_reroll(
        interaction: discord.Interaction,
        giveaway_id: str,
        count: int = 1,
        user: discord.Member | None = None,
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
                svc.resolve_ended, str(interaction.guild.id), giveaway_id
            )
            if user is not None:
                ended, fresh = await asyncio.to_thread(
                    svc.reroll, gw.id, 1, str(user.id)
                )
            else:
                ended, fresh = await asyncio.to_thread(svc.reroll, gw.id, max(1, count))
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        await bot._announce(ended, fresh, reroll=True)
        if user is not None and fresh:
            await bot._safe_followup(
                interaction, f"🔁 Replaced {user.mention} with <@{fresh[0]}>."
            )
        else:
            await bot._safe_followup(interaction, "🔁 Rerolled.")

    @bot.tree.command(name="skipclaim", description="Mark a winner's claim verified (no replacement)")
    @app_commands.autocomplete(giveaway_id=_gw_ended_autocomplete)
    @app_commands.describe(giveaway_id="Ended giveaway with a claim window", user="The winner to verify")
    async def giveaway_skipclaim(
        interaction: discord.Interaction, giveaway_id: str, user: discord.Member
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
                svc.resolve_ended, str(interaction.guild.id), giveaway_id
            )
            await asyncio.to_thread(
                svc.skip_claim, gw.id, str(user.id), str(interaction.user.id)
            )
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        except Exception:
            log.exception("skipclaim failed for %s", giveaway_id)
            await bot._safe_followup(interaction, "⚠️ Could not verify. Try again.")
            return
        log.info("claim skipped for %s/%s by %s", gw.id, user.id, interaction.user.id)
        await bot._safe_followup(
            interaction, f"✅ {user.mention}'s claim verified — no replacement will be drawn."
        )

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
        bot._gw_drop(gw.id)
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
        bot._gw_drop(gw.id)
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
        # Invalidate the cached notify-role now, not in 10s: the mention
        # appended to the very next announcement must be the role just set.
        try:
            await svc.invalidate_notify_role(gid)
        except Exception:
            log.debug("notify role cache invalidation failed", exc_info=True)
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
            bot._clear_entered(gw.id, uid)
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

    @bot.tree.command(name="giveaway_ban", description="Ban a user for N hours (0 = permanent)")
    @app_commands.describe(
        user="The member to ban",
        hours="0 = permanent, 1-720 hours",
        reason="Reason shown to the member and staff",
    )
    async def giveaway_ban(
        interaction: discord.Interaction, user: discord.Member, hours: int = 0, reason: str = ""
    ) -> None:
        """Timed ban + yank entries + strip roles. hours=0 means permanent."""
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        gid, uid = str(interaction.guild.id), str(user.id)
        try:
            hours_int = int(hours)
        except (TypeError, ValueError):
            await bot._safe_followup(
                interaction, "⚠️ Ban duration must be 0-720 hours (0 = permanent)."
            )
            return
        try:
            info = await asyncio.to_thread(
                svc.blacklist_add, gid, uid,
                hours=hours_int, reason=reason or "",
                banned_by=str(getattr(interaction.user, "id", "")),
            )
            active = await asyncio.to_thread(svc.list_active, gid)
        except ServiceError as exc:
            await bot._safe_followup(interaction, f"⚠️ {exc.message}")
            return
        except Exception:
            log.exception("ban failed for %s", uid)
            await bot._safe_followup(interaction, "⚠️ Could not update the ban. Try again.")
            return
        for gw in active:
            # Their entries are gone, so this process's entry markers must go
            # too, or the next honest click is refused as "already entered".
            bot._clear_entered(gw.id, uid)
            try:
                await bot._take_entrants_role(gw, uid)
            except Exception:
                log.exception("ban role take failed for %s/%s", gw.id, uid)
        log.info("banned %s in guild %s for %sh reason=%r", uid, gid, hours_int, (reason or "")[:64])
        clean_reason = (reason or "").strip()
        reason_bit = f" Reason: {clean_reason[:256]}" if clean_reason else ""
        try:
            exp = info.get("expires_at") if isinstance(info, dict) else None
            permanent = bool(info.get("permanent")) if isinstance(info, dict) else hours_int == 0
        except Exception:
            exp, permanent = None, hours_int == 0
        if permanent:
            when = "permanently"
        elif exp:
            try:
                when = f"until <t:{int(exp) // 1000}:R>"
            except (TypeError, ValueError):
                when = f"for {hours_int}h"
        else:
            when = f"for {hours_int}h"
        await bot._safe_followup(
            interaction,
            f"🚫 {user.mention} is banned {when} — entries removed"
            f" from {len(active)} running giveaway(s).{reason_bit}",
        )

    @bot.tree.command(name="giveaway_unban", description="Unban a user from giveaways")
    @app_commands.describe(user="The member to unban")
    async def giveaway_unban(
        interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            removed = await asyncio.to_thread(
                svc.blacklist_remove, str(interaction.guild.id), str(user.id)
            )
        except Exception:
            log.exception("unban failed for %s", user.id)
            await bot._safe_followup(
                interaction, "⚠️ Could not update the ban. Try again."
            )
            return
        await bot._safe_followup(
            interaction,
            f"✅ {user.mention} can join giveaways again."
            if removed
            else f"{user.mention} was not banned.",
        )

    @bot.tree.command(name="giveaway_ban_list", description="Show banned users with expiry and reason")
    async def giveaway_ban_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _can_manage(interaction.user):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return
        try:
            gid = str(interaction.guild.id)
            try:
                await asyncio.to_thread(svc.purge_expired_bans)
            except Exception:
                log.exception("ban purge failed")
            rows = await asyncio.to_thread(svc.blacklist_list_detailed, gid)
            total = await asyncio.to_thread(svc.count_blacklisted, gid)
        except Exception:
            log.exception("ban list failed")
            await bot._safe_followup(
                interaction, "⚠️ Could not load the ban list. Try again."
            )
            return
        if not rows:
            await bot._safe_followup(interaction, "Ban list is empty — nobody is banned.")
            return
        shown = rows[:20]
        lines: list[str] = []
        for r in shown:
            uid = str(r.get("user_id"))
            reason = str(r.get("reason") or "").strip()
            exp = r.get("expires_at")
            if exp is None:
                when = "permanent"
            else:
                try:
                    when = f"expires <t:{int(exp) // 1000}:R>"  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    when = "timed"
            tail = f" — {reason[:100]}" if reason else ""
            lines.append(f"<@{uid}> ({when}){tail}")
        body = "\n".join(lines)
        extra = f"\n…plus {total - len(shown)} more." if total > len(shown) else ""
        await bot._safe_followup(interaction, f"🚫 **Banned ({total}):**\n{body}{extra}")

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

    @bot.tree.command(
        name="gw_request_time",
        description="Time a real giveaway request (Redis + Turso + Discord legs)",
    )
    async def gw_request_time(interaction: discord.Interaction) -> None:
        """Live cost of one request, timed leg by leg — not a gateway ping.

        Each leg runs the same call the Join button pays (Redis cache reads,
        join-shaped SELECT, guarded write, Discord ack + REST send) and times
        it with perf_counter, lane queue + thread hop included, so the numbers
        are this server's real Redis + Turso + Discord latency right now.

        The Redis legs and the two cache-warm reads are measured, not
        simulated: one PING through the governed client, then the real cached
        readers on their real keys, reported as a HIT or a MISS with the
        matching cost. A command that showed only the warm path would look
        fast while saying nothing, so the cold path is always printed next to
        it. Ephemeral; the write probe touches one probe-only Redis key and
        one probe-only Turso row and removes both right after.
        """
        if interaction.guild is None:
            try:
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        t0 = time.perf_counter()
        try:
            # Reference only: one sleep(0) round-trip through the event loop.
            # A healthy loop answers in ~0.01ms; milliseconds here mean the loop
            # was stalled (blocked callback / CPU steal), and that same stall
            # sits inside the ack number below. Never in the slowest-leg race.
            t_lag = time.perf_counter()
            await asyncio.sleep(0)
            loop_lag_ms: float | None = (time.perf_counter() - t_lag) * 1000.0
        except Exception:
            loop_lag_ms = None
        try:
            t_ack = time.perf_counter()
            await interaction.response.defer(ephemeral=True, thinking=True)
            ack_ms: float | None = (time.perf_counter() - t_ack) * 1000.0
        except (discord.NotFound, discord.HTTPException):
            return
        gid = str(interaction.guild.id)
        try:
            caller = str(getattr(interaction.user, "id", "0"))
        except Exception:
            caller = "0"
        try:
            gateway_ms: float | None = float(bot.latency) * 1000.0
            if gateway_ms != gateway_ms:  # NaN before the first heartbeat
                gateway_ms = None
        except Exception:
            gateway_ms = None
        redis_ms: float | None = None
        redis_note = ""
        cache_ops: int | None = None
        redis_hits: int | None = None
        redis_misses: int | None = None
        redis_skipped: int | None = None
        redis_failed: int | None = None
        count_ms: float | None = None
        count_note = ""
        role_ms: float | None = None
        role_note = ""
        # The same pair of cached reads as a total, first (cold) then second
        # (warm) run, so the repeat-click cost can be compared honestly.
        cold_total_ms: float | None = None
        warm_total_ms: float | None = None
        cache_gets: int | None = None
        cache_stores: int | None = None
        redis_probe_ms: float | None = None
        redis_probe_note = ""
        ping_ms: float | None = None
        ping_note = ""
        lookup_ms: float | None = None
        lookup_note = ""
        read_ms: float | None = None
        read_note = ""
        write_ms: float | None = None
        write_note = ""
        rest_ms: float | None = None
        # Leg 2: minimal Turso round-trip on the same gated lane Join uses.
        # "Ping" is one SELECT 1 query + row fetch, not ICMP: warm it costs
        # ~1 RTT, while a cold/stale lane pays TLS + Turso auth inside this
        # same number (that is the cold-vs-warm gap, not server slowness).
        try:
            t_leg = time.perf_counter()
            await bot._db_call(bot.db.query_one, "SELECT 1", ())
            ping_ms = (time.perf_counter() - t_leg) * 1000.0
            ping_note = "SELECT 1 round-trip (warm ~= 1 RTT; cold lane adds TLS+auth)"
        except Exception:
            log.exception("request-time ping probe failed")
            ping_note = "ping probe failed"
        # Leg 0: the same pre-ack list the Join button reads, timed. It also
        # supplies the giveaway id the cached-count leg below reads, so the
        # cache is measured on a real key, never a synthetic one.
        try:
            t_lookup = time.perf_counter()
            live = await bot._db_call(svc.list_active, gid)
            lookup_ms = (time.perf_counter() - t_lookup) * 1000.0
            lookup_note = f"{len(live)} live giveaway(s) listed"
        except Exception:
            live = []
            lookup_ms = None
            lookup_note = "lookup failed"
        gw_id = live[0].id if live else ""
        # Leg 1: the real Redis path this request takes, in the order it takes
        # it — one PING through the governed shared client, then the two cached
        # readers on their real keys. Both readers are read-through, so each run
        # reports HIT or MISS with the cost that actually came with it; nothing
        # here is simulated. The pair is run twice on purpose: the second run is
        # the warm cost a click pays, and the first shows what filling an empty
        # cache costs. A report that printed only the warm run would look fast
        # while saying nothing.
        redis = getattr(bot, "redis", None)
        if redis is None or not getattr(redis, "configured", False):
            redis_note = "not configured (no REDIS_URL) — every read here is a Turso read"
        else:

            async def _timed_cached(reader, *args):
                """One cached reader call: (ms, value, served).

                'served' is read off the cache's own hit counter, never guessed,
                so the report can say HIT or MISS with the cost that came with
                it. A warm read and a fall-through to Turso are very different
                numbers and must never be blurred into one.
                """
                try:
                    c0 = int((redis.stats().get("counters") or {}).get("hits", 0))
                except Exception:
                    c0 = None
                started = time.perf_counter()
                try:
                    value = await reader(*args)
                except Exception:
                    return None, None, None
                elapsed = (time.perf_counter() - started) * 1000.0
                served = None
                try:
                    c1 = int((redis.stats().get("counters") or {}).get("hits", 0))
                    if c0 is not None:
                        served = c1 > c0
                except Exception:
                    served = None
                return elapsed, value, served

            def _label(served: bool | None) -> str:
                if served is None:
                    return "hit/miss unknown"
                return "HIT" if served else "MISS (fell through to Turso)"

            def _runs_note(runs: list[tuple[float | None, str]]) -> str:
                parts = []
                for index, (ms, label) in enumerate(runs, 1):
                    shown = "failed" if ms is None else f"{ms:.0f}ms"
                    parts.append(f"{'1st' if index == 1 else '2nd'} {shown} {label}")
                return "; ".join(parts)

            try:
                before = dict(redis.stats().get("counters") or {})
            except Exception:
                before = {}
            try:
                t_leg = time.perf_counter()
                redis_ms = await redis.ping_ms()
                redis_note = "PING through the shared client (auth + pool reuse included)"
            except Exception:
                log.exception("request-time redis ping failed")
                redis_note = "redis ping failed"
            # The cached readers live on the service; it only has them when a
            # cache is wired in. Reading through svc is what a real click does.
            gwc = bot.service if getattr(bot.service, "_redis", None) is not None else None
            if gwc is None:
                count_note = role_note = "no cache wired into the service"
            elif not gw_id:
                count_note = role_note = "no live giveaway id to read a cached count for"
            else:
                # Twice on purpose: run 1 is what a cold cache costs (it falls
                # through to Turso), run 2 is the warm cost a click pays. Both
                # numbers are printed; a report showing only the warm run would
                # look fast while saying nothing.
                count_runs: list[tuple[float | None, str]] = []
                role_runs: list[tuple[float | None, str]] = []
                n_cached = None
                for _ in range(2):
                    ms, n_cached, served = await _timed_cached(
                        gwc.entry_count_cached, gw_id
                    )
                    count_runs.append((ms, "failed" if ms is None else _label(served)))
                    ms, _role, served = await _timed_cached(
                        gwc.get_notify_role_cached, gid
                    )
                    role_runs.append((ms, "failed" if ms is None else _label(served)))
                # The headline number is the warm run (what a repeat click
                # pays); both runs stay visible in the note beside it.
                warm_count_ms = count_runs[-1][0]
                warm_role_ms = role_runs[-1][0]
                count_ms = warm_count_ms
                role_ms = warm_role_ms
                cold_vals = [runs[0][0] for runs in (count_runs, role_runs) if runs[0][0]]
                if len(cold_vals) == 2:
                    cold_total_ms = sum(cold_vals)
                warm_vals = [v for v in (warm_count_ms, warm_role_ms) if v is not None]
                if len(warm_vals) == 2:
                    warm_total_ms = sum(warm_vals)
                count_note = f"{_runs_note(count_runs)} - {n_cached} entrant(s)"
                role_note = _runs_note(role_runs)
            try:
                after = dict(redis.stats().get("counters") or {})
            except Exception:
                after = {}
            deltas: dict[str, int] = {}
            for name in ("hits", "misses", "skipped", "failed", "attempted", "ok"):
                try:
                    deltas[name] = int(after.get(name, 0)) - int(before.get(name, 0))
                except Exception:
                    pass
            redis_hits = deltas.get("hits")
            redis_misses = deltas.get("misses")
            redis_skipped = deltas.get("skipped")
            redis_failed = deltas.get("failed")
            cache_ops = deltas.get("attempted")
            cache_stores = deltas.get("ok")
            cache_gets = (
                redis_hits + redis_misses
                if None not in (redis_hits, redis_misses)
                else None
            )
            if before and not after:
                count_note = role_note = "cache stats unavailable"
        # Leg 3: the exact pre-ack read the Join button runs. With a live
        # giveaway that is join_snapshot (row + 4 eligibility subqueries, one
        # trip); without one, the same-shaped eligibility SELECT, labelled so
        # nobody mistakes a synthetic read for a live one.
        try:
            t_leg = time.perf_counter()
            if live:
                target = live[0]
                try:
                    await bot._db_call(svc.join_snapshot, target.id, gid, caller)
                    read_note = f"join-shaped read on `{target.id}`"
                except ServiceError as exc:
                    read_note = f"join-shaped read refused ({exc.message[:80]})"
            else:
                await bot._db_call(svc.eligibility_snapshot, gid, caller, None)
                read_note = "no live giveaway — eligibility-shaped read (synthetic ids)"
            read_ms = (time.perf_counter() - t_leg) * 1000.0
        except Exception:
            log.exception("request-time read probe failed")
            if not read_note:
                read_note = "read probe failed"
        # Leg 4: a real guarded write + cleanup on a probe-only row. The key
        # is non-numeric so it can never collide with a member snowflake.
        probe_uid = "gw_request_time_probe"
        try:
            t_leg = time.perf_counter()
            t_ins = time.perf_counter()
            await bot._db_call(
                bot.db.execute,
                "INSERT INTO simple_message_counts (guild_id, user_id, count)"
                " VALUES (?, ?, 0) ON CONFLICT (guild_id, user_id) DO NOTHING",
                (gid, probe_uid),
            )
            insert_ms = (time.perf_counter() - t_ins) * 1000.0
            t_del = time.perf_counter()
            await bot._db_call(
                bot.db.execute,
                "DELETE FROM simple_message_counts WHERE guild_id = ? AND user_id = ?",
                (gid, probe_uid),
            )
            delete_ms = (time.perf_counter() - t_del) * 1000.0
            write_ms = (time.perf_counter() - t_leg) * 1000.0
            write_note = (
                f"insert {insert_ms:.0f}ms + delete {delete_ms:.0f}ms of one probe"
                " row (2 writes x execute+commit)"
            )
        except Exception:
            log.exception("request-time write probe failed")
            write_note = "write probe failed"
            try:
                await bot._db_call(
                    bot.db.execute,
                    "DELETE FROM simple_message_counts WHERE guild_id = ? AND user_id = ?",
                    (gid, probe_uid),
                )
            except Exception:
                pass
        # Leg 5: one real SET + DEL on a probe-only Redis key, so the write
        # path is measured and not just the reads. The key is namespaced and
        # deleted again on every path, including the failure path below.
        redis_probe_key = "probe:gw_request_time"
        if redis is not None and getattr(redis, "configured", False):
            try:
                t_probe = time.perf_counter()
                wrote = await redis.set(redis_probe_key, "1", 10)
                wrote = wrote and await redis.delete(redis_probe_key) >= 0
                redis_probe_ms = (time.perf_counter() - t_probe) * 1000.0
                redis_probe_note = (
                    "SET then DEL one probe key (2 round trips)"
                    if wrote
                    else "SET/DEL refused (Redis degraded or over budget)"
                )
            except Exception:
                log.exception("request-time redis write probe failed")
                redis_probe_ms = None
                redis_probe_note = "redis write probe failed"
                try:
                    await redis.delete(redis_probe_key)
                except Exception:
                    pass
        else:
            redis_probe_note = "not configured"
        # Leg 6: a real Discord REST send, timed. It is then edited into the
        # report so the invoker sees one ephemeral reply, not two.
        probe_msg = None
        try:
            t_leg = time.perf_counter()
            probe_msg = await interaction.followup.send(
                "Measuring Discord reply leg…", ephemeral=True
            )
            rest_ms = (time.perf_counter() - t_leg) * 1000.0
        except (discord.NotFound, discord.HTTPException):
            probe_msg = None
        total_ms = (time.perf_counter() - t0) * 1000.0
        try:
            stats = bot.db.get_stats()
        except Exception:
            stats = {}
        try:
            backend = str(getattr(bot.db, "backend", "?"))
        except Exception:
            backend = "?"
        host = ""
        try:
            url = str(bot.settings.turso_url or "")
            if "://" in url:
                host = url.split("://", 1)[1].split("@")[-1].split("/")[0][:48]
        except Exception:
            host = ""
        try:
            lanes = f"{int(bot._db_gate._value)}/64"
        except Exception:
            lanes = "?"

        def _fmt(value: float | None) -> str:
            return f"{value:.0f}ms" if value is not None else "failed"

        def _ops() -> str:
            if cache_gets is None:
                return "n/a"
            bits = [f"{cache_gets} get(s)"]
            if cache_stores is not None:
                bits.append(f"{cache_stores} set(s)")
            if cache_ops is not None:
                bits.append(f"{cache_ops} op(s)")
            if redis_hits is not None and redis_misses is not None:
                bits.append(f"{redis_hits} hit / {redis_misses} miss")
            if redis_skipped:
                bits.append(f"{redis_skipped} skipped over budget")
            if redis_failed:
                bits.append(f"{redis_failed} failed")
            return ", ".join(bits)

        short = [
            ("Discord ack", ack_ms),
            ("Discord reply", rest_ms),
            ("Redis ping", redis_ms),
            ("Redis cache count", count_ms),
            ("Redis cache notify-role", role_ms),
            ("Redis write", redis_probe_ms),
            ("Turso ping", ping_ms),
            ("Turso lookup", lookup_ms),
            ("Turso read", read_ms),
            ("Turso write", write_ms),
        ]
        known = [(name, value) for name, value in short if value is not None]
        slowest = max(known, key=lambda item: item[1])[0] if known else "n/a"
        gw_txt = f"{gateway_ms:.0f}ms" if gateway_ms is not None else "n/a (offline)"
        lag_txt = f"{loop_lag_ms:.2f}ms" if loop_lag_ms is not None else "n/a"
        try:
            sess = f"{int(stats.get('reads', 0))} reads / {int(stats.get('writes', 0))} writes"
        except Exception:
            sess = "n/a"
        where = f" @ {host}" if host else ""
        if warm_total_ms is None:
            warm_txt = "n/a"
        elif cold_total_ms is None or cold_total_ms <= warm_total_ms:
            warm_txt = f"{warm_total_ms:.0f}ms"
        else:
            warm_txt = f"{cold_total_ms:.0f}ms cold -> {warm_total_ms:.0f}ms warm"
        lines = [
            "Request-time breakdown — live legs, timed just now:",
            f"• Discord ack (defer): **{_fmt(ack_ms)}**",
            f"• Discord reply (REST send): **{_fmt(rest_ms)}**",
            f"• Redis ping: **{_fmt(redis_ms)}** — {redis_note or 'n/a'}",
            f"• Redis cache count: **{_fmt(count_ms)}** — {count_note or 'n/a'}",
            f"• Redis cache notify-role: **{_fmt(role_ms)}** — {role_note or 'n/a'}",
            f"• Redis write: **{_fmt(redis_probe_ms)}** — {redis_probe_note or 'n/a'}",
            f"• Redis ops this command: {_ops()}",
            f"• Turso ping: **{_fmt(ping_ms)}** — {ping_note or 'n/a'}",
            f"• Turso lookup (list_active): **{_fmt(lookup_ms)}** — {lookup_note or 'n/a'}",
            f"• Turso read: **{_fmt(read_ms)}** — {read_note or 'n/a'}",
            f"• Turso write: **{_fmt(write_ms)}** — {write_note or 'n/a'}",
            f"• Gateway heartbeat: **{gw_txt}** (websocket, for reference)",
            f"• Event-loop lag: **{lag_txt}** (sleep(0) sample, for reference)",
            f"• Total measured: **{total_ms:.0f}ms** (excludes this message's edit)",
            f"• Warm cache legs (2nd run, what a repeat click pays): **{warm_txt}** —"
            " timed in this same command, next to the cold/Turso legs above",
            f"• Slowest leg: **{slowest}**",
            f"• Backend: `{backend}`{where} · db lanes free: {lanes} · session: {sess}",
        ]
        report = "\n".join(lines)
        try:
            if probe_msg is not None and hasattr(probe_msg, "edit"):
                await probe_msg.edit(content=report)
            else:
                await bot._safe_followup(interaction, report)
        except (discord.NotFound, discord.HTTPException):
            pass


#: Base seconds to wait before retrying a rate-limited Discord login, one
#: entry per attempt; _login_wait caps and jitters the actual sleep. On Oct 11
#: Cloudflare answered 429 + Error 1015 to GET /users/@me at static_login and
#: discord.py raised HTTPException carrying that HTML page; the old code let it
#: crash the container, Render restarted it, and every restart re-hit the same
#: throttle. Backing off in-process keeps the process alive while it cools.
LOGIN_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0)

#: Hard cap on one login-retry sleep: a rogue server hint can never park the
#: bot longer than this; the loop simply tries again and re-reads the hint.
LOGIN_RETRY_MAX_WAIT = 300.0

#: Jitter spread on every login-retry sleep so concurrent restarts desync
#: instead of re-hitting the limit in lockstep.
LOGIN_RETRY_JITTER = 5.0


def _login_retry_after(exc: BaseException) -> float | None:
    """Server-provided retry delay in seconds, or None when absent/unusable.

    discord.RateLimited carries it as an attribute; some 429 bodies carry a
    retry_after field. Cloudflare 1015 pages are HTML with no hint, so this
    returns None there and the caller uses the bounded schedule. The body is
    scanned manually: its shape is not a contract worth a regex.
    """
    try:
        hint = getattr(exc, "retry_after", None)
        if isinstance(hint, bool):
            hint = None
        if isinstance(hint, (int, float)) and hint >= 0:
            return float(hint)
    except Exception:
        pass
    try:
        text = getattr(exc, "text", "")
        if isinstance(text, str):
            key = "retry_after"
            idx = text.find(key)
            if idx >= 0:
                num = ""
                started = False
                for ch in text[idx + len(key):]:
                    if ch.isdigit() or (ch == "." and started):
                        num += ch
                        started = True
                    elif started:
                        break
                if num:
                    return max(0.0, float(num))
    except Exception:
        pass
    return None


def _login_rate_limited(exc: BaseException) -> bool:
    """True only when the login was refused with a 429 / rate-limit signal.

    Deliberately narrow: LoginFailure (bad token, 401) and every other error
    re-raise immediately. Retrying an invalid token would burn the backoff
    for nothing and hide a config mistake behind retry logs. A 1015 page
    means the throttle is upstream of Discord for now — still transient, so
    it retries too.
    """
    try:
        if isinstance(exc, discord.LoginFailure):
            return False
        if isinstance(exc, discord.HTTPException) and getattr(exc, "status", None) == 429:
            return True
        if isinstance(exc, discord.RateLimited):
            return True
    except Exception:
        pass
    text = str(exc)
    return "429" in text or "1015" in text or "rate limit" in text.lower()


def _login_wait(attempt: int, exc: BaseException) -> float:
    """Seconds to sleep before login attempt number attempt (1-based).

    Prefers the server retry_after hint when present and sane, else the
    bounded exponential schedule. Always capped at LOGIN_RETRY_MAX_WAIT plus
    one jitter spread. Pure function of its inputs so tests pin the schedule
    without sleeping.
    """
    hint = _login_retry_after(exc)
    if hint is not None and hint <= LOGIN_RETRY_MAX_WAIT:
        base = hint
    else:
        base = LOGIN_RETRY_DELAYS[min(attempt - 1, len(LOGIN_RETRY_DELAYS) - 1)]
    capped = LOGIN_RETRY_MAX_WAIT + LOGIN_RETRY_JITTER
    jittered = base + random.uniform(0, LOGIN_RETRY_JITTER)  # noqa: S311
    return min(jittered, capped)


def _login_exc_summary(exc: BaseException) -> str:
    """One safe log line for a refused login: class, status, short snippet.

    Never includes the token (login failures carry no token) and never the
    full body: Cloudflare 1015 pages are kilobytes of HTML, so only the first
    160 collapsed characters are kept, plus the Ray ID when present so the
    block can be correlated with Cloudflare/Discord support.
    """
    try:
        status = getattr(exc, "status", None)
    except Exception:
        status = None
    try:
        flat = " ".join(str(exc).split())
    except Exception:
        flat = ""
    ray = ""
    try:
        at = flat.find("Ray ID")
        if at >= 0:
            ray = " ray=" + "".join(flat[at:at + 48].split())
    except Exception:
        ray = ""
    return type(exc).__name__ + " status=" + str(status) + ray + " :: " + flat[:160]


async def amain(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    from .health import start_health_server

    # Mutable flag the /health page reads: "starting" until the gateway is up,
    # "online" after login, "retrying-N" while cooling down from a 429. A
    # plain dict keeps the health thread (no asyncio there) race-free enough
    # for a status string.
    ready_state: dict[str, str] = {"status": "starting"}
    start_health_server(max(1, settings.port), ready_state=ready_state)
    db = Database(settings)
    db.init_schema()
    # Redis is a short-TTL read cache plus a concurrency damper, never a
    # source of truth: every entry is still confirmed by a Turso commit
    # before anyone is told they are in. It is built here, next to db, so ONE
    # client and one connection pool outlive the login-retry loop below
    # (a fresh bot per attempt must not leak a fresh client per attempt).
    redis = RedisCache(
        getattr(settings, "redis_url", ""),
        enabled=bool(getattr(settings, "redis_enabled", True)),
    )
    try:
        if await redis.start():
            log.info("redis cache ready (%s)", redis.describe())
        else:
            log.warning(
                "redis not available (%s); every read falls back to the database",
                "disabled" if not redis.configured else "unreachable",
            )
    except Exception:
        log.warning("redis start failed; continuing without the cache", exc_info=True)
    loop = asyncio.get_running_loop()
    shutdown_task: asyncio.Task | None = None
    current: dict[str, object] = {"bot": None}

    def shutdown() -> None:
        nonlocal shutdown_task
        if shutdown_task is None:
            bot_obj = current.get("bot")
            close = getattr(bot_obj, "close", None)
            if callable(close):
                shutdown_task = asyncio.create_task(close())

    try:
        loop.add_signal_handler(signal.SIGTERM, shutdown)
    except NotImplementedError:  # Windows event loops do not support add_signal_handler.
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(shutdown))
    attempt = 0
    ready_state["discord"] = "starting"
    # The finally below is the ONLY place the cache client is closed: every
    # exit path (clean stop, SIGTERM, auth failure, cancellation) leaves
    # through it, so the pool never outlives the process that opened it.
    try:
        while True:
            # Fresh client per login attempt: when static_login raises 429,
            # "async with bot" still runs __aexit__ -> close(), leaving the old
            # instance closed. Re-entering it misreads as a deliberate stop
            # (is_closed() True) and re-starting a closed client is unreliable,
            # so never reuse an instance that just failed to log in.
            bot = GiveawayBot(settings, db, redis)
            wire_commands(bot)
            current["bot"] = bot
            try:
                async with bot:
                    ready_state["status"] = "online"
                    ready_state["discord"] = "online"
                    await bot.start(settings.bot_token)
                if shutdown_task is not None:
                    await shutdown_task
                break
            except Exception as exc:
                # A SIGTERM during start() resolves as a clean stop, never a
                # retry: shutdown_task is only ever created by shutdown().
                if shutdown_task is not None:
                    ready_state["discord"] = "stopped"
                    try:
                        await shutdown_task
                    except Exception:
                        pass
                    break
                if not _login_rate_limited(exc):
                    ready_state["discord"] = "auth-failed"
                    raise
                attempt += 1
                wait = _login_wait(attempt, exc)
                ready_state["status"] = f"retrying-{attempt}"
                ready_state["discord"] = "rate-limited"
                log.warning(
                    "Discord login rate-limited (attempt %d); retrying in %.1fs"
                    " — /health stays green (liveness only), /ready reports"
                    " discord=%s :: %s",
                    attempt, wait, ready_state["discord"], _login_exc_summary(exc),
                )
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    ready_state["discord"] = "stopped"
                    raise
                if shutdown_task is not None:
                    await shutdown_task
                    break
            else:
                break
    finally:
        try:
            await redis.aclose()
        except Exception:
            log.debug("redis close failed", exc_info=True)


def run_forever(settings: Settings) -> None:
    asyncio.run(amain(settings))
