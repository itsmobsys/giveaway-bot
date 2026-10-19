"""Giveaway rules: create / join / leave / end / reroll. Nothing else."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from .db import Database, _is_ambiguous_loss

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .redis_cache import RedisCache

log = logging.getLogger("giveaway_bot.service")

DAY = 86_400_000

#: TTL of the short-lived read caches. Everything here is derivable from
#: Turso, so a stale hit only ever costs a few seconds of cosmetic staleness
#: (a list one giveaway behind, a count one entry behind) -- never a wrong
#: decision, because every mutating statement re-checks the database itself.
LIST_CACHE_TTL = 8.0
#: Guild settings change only through the bot's own command, which clears the
#: key, so this TTL is just a safety net for a missed invalidation.
SETTINGS_CACHE_TTL = 10.0
#: Giveaway-id-shaped keys are self-invalidating: a new giveaway gets a new
#: id, so its id can never address a *different* giveaway's data.
GW_CACHE_TTL = 6.0
COUNT_CACHE_TTL = 4.0
#: Longest key suffix this bot will cache. Ids are short (gw_xxxxxxxx) and an
#: unbounded caller-supplied string would let one odd request write an
#: arbitrarily long key into a 30 MB plan.
KEY_SUFFIX_MAX = 96
#: Suffixes carried by ONE invalidation DEL. A burst (100 joins) plus the
#: sweep's list keys can queue far more than this; the rest goes back on the
#: queue for the next flush instead of being dropped.
FLUSH_BATCH_MAX = 64
#: Hard cap on the pending queue itself, so a deployment where nothing flushes
#: for a long stretch cannot grow it without bound. Over cap the OLDEST
#: suffixes go: their keys expire on their own TTL (4-10s) anyway.
PENDING_INVALIDATIONS_MAX = 1024


def now_ms() -> int:
    return int(time.time() * 1000)


def _changed(cur: Any) -> bool:
    """True when a statement that must match a row actually matched one.

    rowcount is not part of every driver's contract, and a missing count reads
    as "did not match": for a status flip that means refusing to draw rather
    than drawing a second time.
    """
    try:
        return int(cur.rowcount or 0) > 0
    except (TypeError, ValueError):
        return False


#: Tag carried by the refusals a caller has to treat differently from a plain
#: "no": the member was stopped by the timed-out rule and their entry was
#: dropped, so anything granted on joining has to go with it.
TIMEOUT_BAN_KIND = "timeout_ban"


@dataclass
class PartialFlush(Exception):
    """A batched write stopped part way, after applied rows had landed.

    A batch is written in chunks that commit individually, so the rows before
    the failure are already stored. Retrying the whole batch would add them a
    second time; the caller hands back only the tail.
    """

    applied: int
    error: Exception


@dataclass
class ServiceError(Exception):
    message: str
    #: Optional machine-readable tag. Empty for an ordinary refusal.
    kind: str = ""


@dataclass
class Giveaway:
    id: str
    guild_id: str
    channel_id: str
    message_id: str | None
    prize: str
    winner_count: int
    ends_at: int
    status: str
    required_role_id: str | None
    required_role_ids: list[str]
    blocked_role_id: str | None
    min_account_age_days: int
    min_messages: int
    image_url: str | None
    entrants_role_id: str | None
    host_id: str | None
    host_name: str | None
    winners: list[str]
    #: Optional winner-claim window in seconds. 0 = disabled (legacy default).
    claim_timeout_seconds: int = 0
    #: Cached entrant count. None means "unknown" (a row that predates the
    #: counter column, or a read that never stored one); 0 is a known answer.
    entrant_count: int | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def claim_enabled(self) -> bool:
        return self.claim_timeout_seconds > 0

    @classmethod
    def from_row(cls, row: dict) -> Giveaway:
        try:
            winners = json.loads(row.get("winners_json") or "[]")
        except (ValueError, TypeError):
            winners = []
        required: list[str] = []
        raw_ids = row.get("required_role_ids")
        if raw_ids:
            try:
                parsed = json.loads(raw_ids)
                if isinstance(parsed, list):
                    required = [str(r) for r in parsed if str(r).strip()]
            except (ValueError, TypeError):
                pass
        legacy = str(row.get("required_role_id") or "").strip()
        if legacy and legacy not in required:
            required.append(legacy)
        # Old rows (or rows written before the counter was known) keep NULL:
        # that stays "unknown" so entry_count() can heal it, rather than
        # pretending a missing counter means zero entrants.
        try:
            entrant_count = int(row.get("entrant_count"))
        except (TypeError, ValueError):
            entrant_count = None
        return cls(
            id=str(row["id"]),
            guild_id=str(row["guild_id"]),
            channel_id=str(row["channel_id"]),
            message_id=str(row["message_id"]) if row.get("message_id") else None,
            prize=str(row["prize"]),
            winner_count=int(row["winner_count"]),
            ends_at=int(row["ends_at"]),
            status=str(row["status"]),
            required_role_id=str(row["required_role_id"]) if row.get("required_role_id") else None,
            required_role_ids=required,
            blocked_role_id=str(row["blocked_role_id"]) if row.get("blocked_role_id") else None,
            min_account_age_days=int(row.get("min_account_age_days") or 0),
            min_messages=int(row.get("min_messages") or 0),
            image_url=str(row["image_url"]) if row.get("image_url") else None,
            entrants_role_id=str(row["entrants_role_id"]) if row.get("entrants_role_id") else None,
            host_id=str(row["host_id"]) if row.get("host_id") else None,
            host_name=str(row["host_name"]) if row.get("host_name") else None,
            winners=[str(w) for w in winners] if isinstance(winners, list) else [],
            claim_timeout_seconds=int(row.get("claim_timeout_seconds") or 0),
            entrant_count=entrant_count,
        )


class GiveawayService:
    #: Hard bound on winners per giveaway. create() and reroll() both enforce it:
    #: the winner announcement has to fit in one Discord message.
    MAX_WINNERS = 25

    def __init__(
        self, db: Database, redis: RedisCache | None = None, off_loop: Any | None = None
    ) -> None:
        self.db = db
        #: How a blocking read gets off the event loop. The cached readers
        #: used the loop's default executor, which is min(32, cpu+4) workers
        #: -- 5 on the deployed 1-CPU instance -- and every cold worker opens
        #: its own database stream. The bot injects its pinned, gated 64-lane
        #: pool so a click's display read queues with the rest of the database
        #: work instead of competing for five threads. Defaults to
        #: asyncio.to_thread, which is what every existing test builds.
        self._off_loop = off_loop or asyncio.to_thread
        #: Optional short-TTL read cache. Turso is the source of truth: every
        #: write goes straight there, and a cache miss/error/timeout simply
        #: falls through to a normal query. None = every read hits the DB.
        self._redis = redis
        #: Key suffixes whose cached value a just-committed write invalidated.
        #: The write path is synchronous (it runs inside a worker thread) and
        #: cannot await a Redis delete, so it records them here and the event
        #: loop drains the list -- immediately after that same DB call, so the
        #: next read is live rather than waiting out a TTL. Plain list append
        #: from worker threads: under the GIL this can lose nothing, and the
        #: drain/clear below runs on the single event-loop thread.
        self._pending_invalidations: list[str] = []
        self._inval_lock = threading.Lock()
        self._rand = secrets.SystemRandom()

    # -- optional redis read cache --------------------------------------
    async def _cache_get(self, key: str) -> Any | None:
        """Read one cached value. Never raises; a degraded Redis means None."""
        cache = self._redis
        if cache is None:
            return None
        try:
            return await cache.get_json(key)
        except Exception:
            # The cache guarantees safe defaults itself; this is a last line
            # of defence so no cache bug can ever break a user-facing path.
            log.debug("redis read failed for %s", key, exc_info=True)
            return None

    async def _cache_get_int(self, key: str) -> int | None:
        raw = await self._cache_get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def _cache_put(self, key: str, value: Any, ttl: float) -> None:
        """Best-effort write-through. A failure only costs a later read."""
        cache = self._redis
        if cache is None or len(key) > KEY_SUFFIX_MAX:
            return
        try:
            await cache.set_json(key, value, ttl)
        except Exception:
            log.debug("redis write failed for %s", key, exc_info=True)

    def _mark_stale(self, *suffixes: str) -> None:
        """Record keys a committed write just made wrong (never raises)."""
        if self._redis is None:
            return
        try:
            with self._inval_lock:
                pending = self._pending_invalidations
                pending.extend(suffixes)
                if len(pending) > PENDING_INVALIDATIONS_MAX:
                    # Bounded queue: the oldest suffixes are the ones whose
                    # keys have had the longest to expire on their own TTL.
                    overflow = len(pending) - PENDING_INVALIDATIONS_MAX
                    del pending[:overflow]
                    log.debug("invalidation queue over cap, dropped %d oldest", overflow)
        except Exception:
            pass

    def drain_invalidations(self) -> list[str]:
        """Take the pending key suffixes (clearing them) for an async flush."""
        try:
            with self._inval_lock:
                pending = list(self._pending_invalidations)
                self._pending_invalidations.clear()
            return pending
        except Exception:
            return []

    async def flush_invalidations(self) -> int:
        """Delete every key a committed write invalidated. Never raises.

        Callers run this right after the write returns, as best-effort
        housekeeping: the click paths and the tick schedule it in the
        background so one DEL can never sit between a commit and the member's
        answer, and a missed batch is harmless because every cached value
        expires on its own TTL (4-10s). One DEL carries at most
        FLUSH_BATCH_MAX suffixes; the rest goes back on the queue.
        """
        cache = self._redis
        pending = self.drain_invalidations()
        if cache is None or not pending:
            return 0
        # Deduplicate: a burst of joins for one giveaway queues the same
        # count key N times, and one DEL is enough for all of them.
        unique = list(dict.fromkeys(pending))
        if not unique:
            return 0
        batch, rest = unique[:FLUSH_BATCH_MAX], unique[FLUSH_BATCH_MAX:]
        if rest:
            # More suffixes than one DEL should carry (a 100-entry burst plus
            # the sweep's list keys). The overflow goes BACK on the queue
            # instead of being dropped, so nothing has to wait out a TTL, and
            # the queue is re-capped to keep it bounded across repeated bursts.
            # The rebuild happens INSIDE the lock: a worker thread that marks a
            # key stale while this runs must not have its suffix overwritten by
            # the assignment below.
            with self._inval_lock:
                keep = list(dict.fromkeys(rest))
                keep.extend(k for k in self._pending_invalidations if k not in keep)
                del keep[PENDING_INVALIDATIONS_MAX:]
                self._pending_invalidations[:] = keep
        try:
            return await cache.delete(*batch)
        except Exception:
            log.debug("redis invalidation flush failed", exc_info=True)
            return 0

    async def _cache_drop(self, suffix: str) -> None:
        """Delete one key. Called after a write so the next read is live."""
        cache = self._redis
        if cache is None:
            return
        try:
            await cache.delete(suffix)
        except Exception:
            log.debug("redis delete failed for %s", suffix, exc_info=True)

    async def _invalidate_guild(self, guild_id: str, giveaway_id: str | None = None) -> None:
        """Drop everything a finished/cancelled giveaway could have changed.

        Called synchronously inside the write path (after the statement has
        landed), so the very next read is served from the database rather
        than from a row that says the giveaway is still running.
        """
        cache = self._redis
        if cache is None or not guild_id:
            return
        keys = [f"guild:{guild_id}:active", "all:active"]
        if giveaway_id:
            keys.append(f"gw:{giveaway_id}")
            keys.append(f"count:{giveaway_id}")
        try:
            await cache.delete(*keys)
        except Exception:
            log.debug("redis invalidation failed for guild %s", guild_id, exc_info=True)

    @staticmethod
    def _dump(gw: Giveaway) -> dict[str, Any]:
        """Flat, self-describing snapshot: never a dataclass the cache owns.

        A hand-written shape (rather than asdict) means the cached blob
        cannot silently break every reader when a field is added.
        """
        return {
            "id": gw.id,
            "guild_id": gw.guild_id,
            "channel_id": gw.channel_id,
            "message_id": gw.message_id,
            "prize": gw.prize,
            "winner_count": gw.winner_count,
            "ends_at": gw.ends_at,
            "status": gw.status,
            "required_role_id": gw.required_role_id,
            "required_role_ids": list(gw.required_role_ids),
            "blocked_role_id": gw.blocked_role_id,
            "min_account_age_days": gw.min_account_age_days,
            "min_messages": gw.min_messages,
            "image_url": gw.image_url,
            "entrants_role_id": gw.entrants_role_id,
            "host_id": gw.host_id,
            "host_name": gw.host_name,
            "winners": list(gw.winners),
            "claim_timeout_seconds": gw.claim_timeout_seconds,
        }

    @staticmethod
    def _load_gw(blob: Any) -> Giveaway | None:
        """Rebuild a Giveaway from a cached blob, or None if it is unusable."""
        if not isinstance(blob, dict) or not blob.get("id"):
            return None
        try:
            return Giveaway(
                id=str(blob["id"]),
                guild_id=str(blob.get("guild_id") or ""),
                channel_id=str(blob.get("channel_id") or ""),
                message_id=str(blob["message_id"]) if blob.get("message_id") else None,
                prize=str(blob.get("prize") or ""),
                winner_count=int(blob.get("winner_count") or 0),
                ends_at=int(blob.get("ends_at") or 0),
                status=str(blob.get("status") or ""),
                required_role_id=(
                    str(blob["required_role_id"]) if blob.get("required_role_id") else None
                ),
                required_role_ids=[str(r) for r in (blob.get("required_role_ids") or [])],
                blocked_role_id=(
                    str(blob["blocked_role_id"]) if blob.get("blocked_role_id") else None
                ),
                min_account_age_days=int(blob.get("min_account_age_days") or 0),
                min_messages=int(blob.get("min_messages") or 0),
                image_url=str(blob["image_url"]) if blob.get("image_url") else None,
                entrants_role_id=(
                    str(blob["entrants_role_id"]) if blob.get("entrants_role_id") else None
                ),
                host_id=str(blob["host_id"]) if blob.get("host_id") else None,
                host_name=str(blob["host_name"]) if blob.get("host_name") else None,
                winners=[str(w) for w in (blob.get("winners") or [])],
                claim_timeout_seconds=int(blob.get("claim_timeout_seconds") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # -- create ---------------------------------------------------------
    def create(
        self,
        *,
        guild_id: str,
        channel_id: str,
        prize: str,
        winner_count: int,
        duration_seconds: int,
        created_by: str,
        required_role_id: str | None = None,
        required_role_ids: list[str] | None = None,
        blocked_role_id: str | None = None,
        min_account_age_days: int = 0,
        min_messages: int = 0,
        image_url: str | None = None,
        host_id: str | None = None,
        host_name: str | None = None,
        claim_timeout_seconds: int = 0,
    ) -> Giveaway:
        prize = prize.strip()
        if not prize or len(prize) > 256:
            raise ServiceError("Prize must be 1-256 characters.")
        claim_timeout_seconds = int(claim_timeout_seconds or 0)
        if claim_timeout_seconds < 0 or claim_timeout_seconds > 60 * 86400:
            raise ServiceError("Claim window must be 0 (off) or up to 60 days in seconds.")
        if winner_count < 1 or winner_count > self.MAX_WINNERS:
            raise ServiceError(f"Winner count must be 1-{self.MAX_WINNERS}.")
        if duration_seconds < 30 or duration_seconds > 60 * 86400:
            raise ServiceError("Duration must be 30 seconds to 60 days.")
        if min_messages < 0 or min_messages > 100000:
            raise ServiceError("Minimum messages must be 0-100000.")
        if min_account_age_days < 0 or min_account_age_days > 3650:
            raise ServiceError("Minimum account age must be 0-3650 days.")
        image_url = (image_url or "").strip() or None
        if image_url and (len(image_url) > 512 or not image_url.startswith(("http://", "https://"))):
            raise ServiceError("Image must be an http(s) URL.")
        role_ids = [str(r).strip() for r in (required_role_ids or []) if str(r).strip()]
        if required_role_id and str(required_role_id).strip() not in role_ids:
            role_ids.append(str(required_role_id).strip())
        role_ids = role_ids[:5]
        gid = "gw_" + uuid.uuid4().hex[:12]
        created = now_ms()
        # entrant_count starts at a known 0 (not NULL): a brand-new giveaway
        # really has no entrants, and storing that lets entry_count() answer
        # in one indexed read instead of paying a COUNT(*) fallback on a
        # counter the row could never have.
        self.db.execute(
            "INSERT INTO simple_giveaways (id, guild_id, channel_id, prize, winner_count, ends_at,"
            " status, required_role_id, required_role_ids, blocked_role_id, min_account_age_days,"
            " min_messages, image_url, created_by, created_at, host_id, host_name,"
            " claim_timeout_seconds, entrant_count)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                gid, guild_id, channel_id, prize, winner_count, created + duration_seconds * 1000,
                role_ids[0] if role_ids else None, json.dumps(role_ids),
                blocked_role_id or None, max(0, min_account_age_days),
                max(0, min_messages), image_url, created_by, created,
                host_id or created_by, (host_name or "")[:64] or None,
                claim_timeout_seconds,
            ),
        )
        # A brand-new running giveaway changes both the guild list and the
        # global list; without this an autocomplete would not offer it until
        # the 8s TTL lapsed.
        self._mark_stale(f"guild:{guild_id}:active", "all:active")
        return self.get(gid)

    # -- guild settings (one-time notify-role setup) ----------------------
    def _get_notify_role_uncached(self, guild_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT notify_role_id FROM simple_guild_settings WHERE guild_id = ?", (guild_id,)
        )
        return str(row["notify_role_id"]) if row and row.get("notify_role_id") else None

    def get_notify_role(self, guild_id: str) -> str | None:
        return self._get_notify_role_uncached(guild_id)

    async def get_notify_role_cached(self, guild_id: str) -> str | None:
        """Guild notify-role, served from the cache when warm.

        A guild that has never set one has no row, so the read is cached as
        the explicit marker 'none' rather than as an absent key: otherwise a
        repeat visitor would pay a Turso round trip on every join forever.
        Returns None for both "not configured" and "cache/DB unavailable" --
        the caller's behavior (no ping) is identical and never unsafe.
        """
        key = f"settings:{guild_id}:notify_role"
        cached = await self._cache_get(key)
        if cached == "none":
            return None
        if isinstance(cached, str) and cached:
            return cached
        try:
            # Off the event loop: _get_notify_role_uncached is a blocking Turso
            # read, and every other DB touch in this process is kept off the
            # loop (bot._db_call). Awaiting it directly here would freeze
            # heartbeats, ticks and every button click for the length of one
            # round trip, with no timeout at all.
            # The injected runner (the bot's pinned pool) is a callable
            # runner, not a deferral helper: _db_call takes a zero-arg
            # callable, so the partial binds the argument here.
            role = await self._off_loop(
                partial(self._get_notify_role_uncached, guild_id))
        except Exception:
            return None
        await self._cache_put(key, role if role else "none", SETTINGS_CACHE_TTL)
        return role

    def set_notify_role(self, guild_id: str, role_id: str | None) -> None:
        self.db.execute(
            "INSERT INTO simple_guild_settings (guild_id, notify_role_id) VALUES (?, ?)"
            " ON CONFLICT (guild_id) DO UPDATE SET notify_role_id = excluded.notify_role_id",
            (guild_id, role_id),
        )
    async def invalidate_notify_role(self, guild_id: str) -> None:
        """Drop the cached notify-role after a write.

        Kept separate from set_notify_role, which must stay synchronous (it is
        called from the same thread path as the rest of the service). The
        command that changes the role awaits this right after the UPDATE, so
        the next reader sees the new value instead of waiting out the TTL.
        """
        await self._cache_drop(f"settings:{guild_id}:notify_role")

    # -- read -----------------------------------------------------------
    def get(self, giveaway_id: str) -> Giveaway:
        row = self.db.query_one("SELECT * FROM simple_giveaways WHERE id = ?", (giveaway_id,))
        if row is None:
            raise ServiceError("Giveaway not found.")
        return Giveaway.from_row(row)

    def get_by_message(self, message_id: str) -> Giveaway | None:
        row = self.db.query_one("SELECT * FROM simple_giveaways WHERE message_id = ?", (message_id,))
        return Giveaway.from_row(row) if row else None

    def list_active(self, guild_id: str) -> list[Giveaway]:
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE guild_id = ? AND status = 'active' ORDER BY ends_at ASC",
            (guild_id,),
        )
        return [Giveaway.from_row(r) for r in rows]

    def list_all_active(self, limit: int = 25) -> list[Giveaway]:
        """Every active giveaway, soonest deadline first.

        Feeds both the embed refresher and the autocomplete cache, so the cap is
        per tick rather than per guild: 200 rows is one small query and keeps
        suggestions available in every guild the bot is in.
        """
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active'"
            " ORDER BY ends_at ASC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        return [Giveaway.from_row(r) for r in rows]

    async def list_active_cached(self, guild_id: str) -> list[Giveaway]:
        """This guild's active giveaways, from the cache when warm.

        Used by the paths where 8s of staleness is cosmetic (an autocomplete
        list, a role sweep). Anything that must see a giveaway the moment it
        appears or disappears keeps calling list_active() directly.
        """
        key = f"guild:{guild_id}:active"
        cached = await self._cache_get(key)
        if isinstance(cached, list):
            loaded = [self._load_gw(b) for b in cached]
            return [g for g in loaded if g is not None]
        # Same rule as the other cached reads: the fallback is blocking Turso
        # I/O, so it runs in a worker thread instead of on the event loop.
        live = await self._off_loop(partial(self.list_active, guild_id))
        await self._cache_put(key, [self._dump(g) for g in live], LIST_CACHE_TTL)
        return live


    def resolve(self, guild_id: str, raw_id: str) -> Giveaway:
        """Find a giveaway by id, falling back to the live one.

        Operators keep mistyping ids, so when the id does not match and
        exactly one giveaway is running in this server, that one is used
        instead of failing. With zero or several running, a valid id is
        required (the slash commands suggest them as you type).
        """
        raw_id = (raw_id or "").strip()
        if raw_id:
            try:
                gw = self.get(raw_id)
            except ServiceError:
                pass
            else:
                # An id can be typed by hand (or copied from another server's
                # message), and every command here is guild-scoped: refuse
                # rather than end someone else's giveaway.
                if str(gw.guild_id) != str(guild_id):
                    raise ServiceError("That giveaway belongs to another server.")
                return gw
        active = self.list_active(guild_id)
        if len(active) == 1:
            return active[0]
        if not active:
            raise ServiceError("No active giveaway in this server.")
        raise ServiceError(
            "That ID did not match. Pick one from the suggestions as you type,"
            " or copy the ID from /giveaway_list."
        )

    def list_ended(self, guild_id: str, limit: int = 25) -> list[Giveaway]:
        """Ended giveaways in one server, most recently ended first."""
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE guild_id = ? AND status = 'ended'"
            " ORDER BY ended_at DESC LIMIT ?",
            (guild_id, max(1, min(int(limit), 100))),
        )
        return [Giveaway.from_row(r) for r in rows]

    def list_all_ended(self, limit: int = 200) -> list[Giveaway]:
        """Every ended giveaway, most recently ended first. Feeds the ended autocomplete cache."""
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'ended'"
            " ORDER BY ended_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        return [Giveaway.from_row(r) for r in rows]

    def resolve_ended(self, guild_id: str, raw_id: str) -> Giveaway:
        """Find an ended giveaway by id, falling back to the ended one.

        Reroll/skipclaim only work on ended giveaways, so unlike resolve() this
        never falls back to a live one: handing back a running giveaway here
        would only fail one step later with a confusing message (or worse, act
        on the wrong giveaway when the typed id was a typo).
        """
        raw_id = (raw_id or "").strip()
        if raw_id:
            try:
                gw = self.get(raw_id)
            except ServiceError:
                # Unknown id: fall through to the ended-list errors below, so a
                # typo says "did not match" (or "No ended giveaway") instead of
                # a bare "Giveaway not found." Same shape as resolve().
                pass
            else:
                if str(gw.guild_id) != str(guild_id):
                    raise ServiceError("That giveaway belongs to another server.")
                if gw.active or gw.status != "ended":
                    raise ServiceError("That giveaway has not ended yet.")
                return gw
        ended = self.list_ended(guild_id)
        if len(ended) == 1:
            return ended[0]
        if not ended:
            raise ServiceError("No ended giveaway in this server.")
        raise ServiceError(
            "That ID did not match. Pick one from the suggestions as you type,"
            " or copy the ID from /giveaway_list."
        )

    def entry_count(self, giveaway_id: str, *, exact: bool = False) -> int:
        """Live entry count, counter-first with COUNT(*) backfill.

        exact=True forces COUNT(*) (retention tests, audits): the counter is
        a fast path, not the source of truth after bulk deletes. Otherwise
        the counter column answers in 1 indexed-row read. NULL means unknown
        (a row from before the column, or one whose counter was never
        stored), so only then does COUNT(*) run and, when it finds real
        entries, write the true value back: old giveaways converge without a
        migration. A stored 0 is a known answer and returns immediately --
        treating it as "not cached" made every display read of an empty
        giveaway pay the fallback a second time.
        """
        if exact:
            true_row = self.db.query_one(
                "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?",
                (giveaway_id,),
            )
            try:
                return int((true_row or {}).get("n") or 0)
            except (TypeError, ValueError):
                return 0
        row = self.db.query_one(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?", (giveaway_id,)
        )
        cached = row.get("entrant_count") if row is not None else None
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                pass
        true_row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?", (giveaway_id,)
        )
        try:
            true_n = int((true_row or {}).get("n") or 0)
        except (TypeError, ValueError):
            true_n = 0
        if true_n > 0:
            try:
                self.db.execute(
                    "UPDATE simple_giveaways SET entrant_count = ?"
                    " WHERE id = ? AND (entrant_count IS NULL OR entrant_count < ?)",
                    (true_n, giveaway_id, true_n),
                )
            except Exception:
                pass
        return true_n

    async def entry_count_cached(self, giveaway_id: str) -> int:
        """Entry count for display paths, cached briefly.

        Returns the same value entry_count() would, only possibly up to a few
        seconds old. Display-only: nothing that decides an outcome reads this.
        """
        key = f"count:{giveaway_id}"
        cached = await self._cache_get_int(key)
        if cached is not None:
            return cached
        # Off the event loop for the same reason as the notify-role read: the
        # counter fallback (and the legacy-row heal it may run) is blocking
        # Turso I/O, and a display number must never stall the loop.
        value = await self._off_loop(partial(self.entry_count, giveaway_id))
        if value is not None:
            # A count of zero is a real answer and gets cached like any other.
            # Skipping it meant a giveaway with no entrants yet could never be
            # served from the cache: every display read paid the counter
            # fallback (a second query, and on a remote database a second
            # round trip) while the surrounding reads were cache hits.
            await self._cache_put(key, value, COUNT_CACHE_TTL)
        return value

    def _sync_counter(self, giveaway_id: str) -> None:
        """Reset counter to COUNT(*) after a bulk drop (ban/timeout).

        Bulk drops remove entries without going through leave(), so the live
        counter would otherwise stay high. Rare path (bans only), so a full
        COUNT(*) here is fine; failures only log (the read path heals it).
        """
        try:
            row = self.db.query_one(
                "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?",
                (giveaway_id,),
            )
            try:
                n = int((row or {}).get("n") or 0)
            except (TypeError, ValueError):
                return
            self.db.execute(
                "UPDATE simple_giveaways SET entrant_count = ? WHERE id = ?",
                (n, giveaway_id),
            )
            # The cached display count for this giveaway is now wrong: the rows
            # were dropped here, not through leave(), so nothing else marks it.
            self._mark_stale(f"count:{giveaway_id}")
        except Exception:
            pass

    def entries_snapshot(
        self, giveaway_id: str, limit: int | None, user_id: str
    ) -> tuple[int, list[dict], int] | None:
        """Window + true count + membership flag in ONE read of the entries table.

        The participants panel used to read the same table three times: the
        count, the window, then "am I in it". One statement answers all three --
        the rows the window needs, COUNT(*) OVER () for the true total (SQLite
        window functions, 3.25+), and whether the caller's own row is among the
        rows the window returned. Returns None when the read fails, so the
        caller can keep its own "could not load the list" text rather than
        printing a zero as if it were a fact.
        """
        if limit is None:
            statement = (
                "SELECT user_id, username, entered_at,"
                " COUNT(*) OVER () AS total,"
                " (user_id = ?) AS me"
                " FROM simple_entries WHERE giveaway_id = ?"
                " ORDER BY entered_at ASC"
            )
            params: tuple = (str(user_id or ""), giveaway_id)
        else:
            statement = (
                "SELECT user_id, username, entered_at,"
                " COUNT(*) OVER () AS total,"
                " (user_id = ?) AS me"
                " FROM simple_entries WHERE giveaway_id = ?"
                " ORDER BY entered_at ASC LIMIT ?"
            )
            params = (str(user_id or ""), giveaway_id, max(1, int(limit)))
        try:
            rows = self.db.query(statement, params)
        except Exception:
            return None
        if not rows:
            return 0, [], 0
        try:
            total = int(rows[0].get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        # COUNT(*) OVER () counts every matching row even though LIMIT caps the
        # rows we keep, so the header total stays the true one on a giveaway
        # bigger than the window (the "Showing the first N of T" line depends
        # on that). The flag is the caller's own row, compared as a string.
        wanted = str(user_id or "")
        mine = 0
        window: list[dict] = []
        for row in rows:
            if str(row.get("user_id")) == wanted:
                mine = 1
            window.append(
                {
                    "user_id": row.get("user_id"),
                    "username": row.get("username"),
                    "entered_at": row.get("entered_at"),
                }
            )
        return total, window, mine

    def entries(self, giveaway_id: str, limit: int | None = None) -> list[dict]:
        """Entrants in entry order. Pass limit for a display path.

        The draw needs the whole pool, but a listing shows the first page only:
        a giveaway with tens of thousands of entries should not ship all of them
        over the wire (and allocate them) to print fifty mentions.
        """
        statement = (
            "SELECT user_id, username, entered_at FROM simple_entries"
            " WHERE giveaway_id = ? ORDER BY entered_at ASC"
        )
        if limit is None:
            return self.db.query(statement, (giveaway_id,))
        return self.db.query(statement + " LIMIT ?", (giveaway_id, max(1, int(limit))))

    def entry_user_ids(self, giveaway_id: str) -> list[str]:
        """Entrant ids in entry order, without the display-only columns.

        The draw only ever looks at user_id; entries() also selects username
        and entered_at, so drawing from a large pool shipped and allocated
        rows it immediately threw away. Order is the same (entered_at ASC),
        so random.sample sees exactly the same pool as before.
        """
        rows = self.db.query(
            "SELECT user_id FROM simple_entries WHERE giveaway_id = ?"
            " ORDER BY entered_at ASC",
            (giveaway_id,),
        )
        return [str(row["user_id"]) for row in rows]

    def has_entry(self, giveaway_id: str, user_id: str) -> bool:
        """Whether this member is entered: one lookup, no rows loaded.

        The Participants panel shows a bounded window, so "am I in it" cannot be
        answered by scanning the rows it happens to hold.
        """
        if not user_id:
            return False
        return (
            self.db.query_one(
                "SELECT 1 AS one FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, user_id),
            )
            is not None
        )

    # -- join / leave ---------------------------------------------------
    def check_eligible(
        self,
        gw: Giveaway,
        *,
        member_roles: list[str],
        account_created_ts: float | None,
        user_id: str = "",
        pending_messages: int = 0,
        timed_out: bool = False,
        snapshot: tuple[bool, int, int, bool] | None = None,
    ) -> None:
        snap_bl: bool | None = None
        snap_ban: int | None = None
        snap_msgs: int | None = None
        snap_hit: bool | None = None
        if snapshot is not None:
            snap_bl, snap_ban, snap_msgs, snap_hit = snapshot
        if not gw.active:
            raise ServiceError("This giveaway has ended.")
        if user_id and snap_bl:
            # Snapshot says blocked: confirm live before refusing, so a
            # blacklist_remove landing between the snapshot and this thread
            # cannot falsely refuse. Snapshot-False proceeds: the INSERT
            # guard below still blocks a racing blacklist_add.
            if self.is_blacklisted(gw.guild_id, user_id):
                raise ServiceError(self.ban_message(gw.guild_id, user_id))
        elif user_id and snap_bl is None and self.is_blacklisted(gw.guild_id, user_id):
            raise ServiceError(self.ban_message(gw.guild_id, user_id))
        if gw.ends_at <= now_ms():
            raise ServiceError("This giveaway has ended.")
        # Discord's native timeout (/mute), and anyone still sitting out the
        # penalty that started from it. Deliberately placed after the "is this
        # giveaway still joinable" checks, so a stale button on an ended one
        # cannot hand out a penalty. This is the only check here that writes:
        # it also drops an entry the blocked member already had.
        self.check_timeout_ban(
            gw, user_id=user_id, timed_out=timed_out,
            _snap_ban=snap_ban, _snap_hit=snap_hit,
        )
        roles = set(member_roles)
        if gw.required_role_ids and not (set(gw.required_role_ids) & roles):
            raise ServiceError("You need one of the required roles to enter.")
        if gw.blocked_role_id and gw.blocked_role_id in roles:
            raise ServiceError("Your role is not allowed to enter.")
        if gw.min_account_age_days > 0:
            if account_created_ts is None:
                # The age could not be read, so the requirement cannot be
                # checked. Refusing is the safe side of that trade: waving it
                # through would silently disable the alt-account filter.
                raise ServiceError("Could not check your account age — try again in a moment.")
            age_days = (time.time() - account_created_ts) / 86400
            if age_days < gw.min_account_age_days:
                raise ServiceError(f"Account must be {gw.min_account_age_days}+ days old.")
        if gw.min_messages > 0:
            # pending_messages adds the rows still sitting in the bot's in-memory
            # buffer, so nobody is told they sent fewer messages than they did
            # since the last flush.
            if snap_msgs is not None:
                sent = snap_msgs + max(0, pending_messages)
            else:
                sent = self.message_count(gw.guild_id, user_id) + max(0, pending_messages)
            if sent < gw.min_messages:
                # Snapshot staleness: the tuple was read before this thread
                # started, so a flush landing in between reads low and would
                # falsely refuse. Re-read live once before refusing; the happy
                # path (snapshot already sufficient) costs nothing extra.
                if snap_msgs is not None:
                    sent = self.message_count(gw.guild_id, user_id) + max(0, pending_messages)
                if sent < gw.min_messages:
                    raise ServiceError(
                        f"You need {gw.min_messages}+ messages in this server ({sent} counted)."
                    )

    def message_count(self, guild_id: str, user_id: str) -> int:
        row = self.db.query_one(
            "SELECT count FROM simple_message_counts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return int(row["count"]) if row else 0

    def get_status(self, giveaway_id: str) -> str | None:
        """Lightweight post-join liveness check: status only, no full row.

        handle_join already holds the Giveaway it joined; re-reading the whole
        row just to decide grant-vs-take + refresh would cost a second full
        SELECT. Status is all the race needs (ended/cancelled vs active).
        """
        row = self.db.query_one(
            "SELECT status FROM simple_giveaways WHERE id = ?", (giveaway_id,)
        )
        return str(row["status"]) if row and row.get("status") else None

    def refresh_snapshot(self, giveaway_id: str) -> tuple[str | None, int]:
        """Status + entry count in one round-trip for the post-ack refresh.

        handle_join used to pay get_status() then entry_count() sequentially
        (2 waves, ~200ms warm) just to decide grant-vs-take and render the
        embed. One SELECT with scalar subqueries returns both; a dead read
        returns (None, 0) so the caller skips background work fail-closed
        instead of mis-rendering. P2-2: count comes from the live counter
        column (1 indexed row), never COUNT(*) — the scan cost no longer
        grows with entries.
        """
        try:
            row = self.db.query_one(
                "SELECT status, COALESCE(entrant_count, 0) AS n"
                " FROM simple_giveaways WHERE id = ?",
                (giveaway_id,),
            )
        except Exception:
            return None, 0
        if not row:
            return None, 0
        try:
            status = row.get("status")
            status_s = str(status) if status else None
        except Exception:
            status_s = None
        try:
            count = int(row.get("n") or 0)
        except (TypeError, ValueError):
            count = 0
        return status_s, count

    def join_snapshot(
        self, giveaway_id: str, guild_id: str, user_id: str
    ) -> tuple[Giveaway, tuple[bool, int, int, bool] | None]:
        """Giveaway row + eligibility tuple in one round-trip (fast-path).

        handle_join used to gather(get, eligibility_snapshot) in parallel:
        2 threads + 2 Turso streams per click. At 100 simultaneous joins the
        default to_thread pool (~8 threads on Render free) queued ~200 tasks
        for ~2.4s of pure queueing. One SELECT returns the giveaway columns
        plus the 4 snapshot subqueries, so the pre-ack phase costs 1 wave on
        1 thread. Snapshot parse failures return None (fail-closed to the
        individual checks); a missing giveaway raises ServiceError.
        """
        snap_now = now_ms()
        try:
            row = self.db.query_one(
                "SELECT g.*, "
                "(SELECT 1 FROM simple_blacklist WHERE guild_id = ?"
                " AND user_id = ? AND (expires_at IS NULL OR expires_at > ?)) AS bl, "
                "(SELECT giveaways_remaining FROM simple_giveaway_bans"
                " WHERE guild_id = ? AND user_id = ?) AS ban, "
                "(SELECT count FROM simple_message_counts"
                " WHERE guild_id = ? AND user_id = ?) AS msgs, "
                "(SELECT 1 FROM simple_giveaway_ban_hits WHERE guild_id = ?"
                " AND user_id = ? AND giveaway_id = ?) AS hit "
                "FROM simple_giveaways AS g WHERE g.id = ?",
                (guild_id, user_id, snap_now, guild_id, user_id,
                 guild_id, user_id, guild_id, user_id, giveaway_id, giveaway_id),
            )
        except Exception:
            # Read failed ambiguously: fall back to the two-query path so a
            # dead stream can never look like "giveaway not found".
            gw = self.get(giveaway_id)
            try:
                snap = self.eligibility_snapshot(guild_id, user_id, giveaway_id)
            except Exception:
                snap = None
            return gw, snap
        if row is None:
            raise ServiceError("Giveaway not found.")
        gw = Giveaway.from_row(row)
        try:
            bl = row.get("bl") is not None
        except Exception:
            return gw, None
        try:
            ban = max(0, int(row.get("ban") or 0))
        except (TypeError, ValueError):
            ban = 0
        try:
            msgs = int(row.get("msgs") or 0)
        except (TypeError, ValueError):
            msgs = 0
        try:
            hit = row.get("hit") is not None
        except Exception:
            hit = False
        return gw, (bl, ban, msgs, hit)

    def eligibility_snapshot(
        self, guild_id: str, user_id: str, giveaway_id: str | None = None
    ) -> tuple[bool, int, int, bool] | None:
        """Blacklist + timeout-ban + message count + ban-hit in one round-trip.

        The happy path used to spend four sequential Turso reads here
        (is_blacklisted, timeout_ban_remaining, _was_blocked_from,
        message_count). One SELECT with scalar subqueries returns all four;
        each subquery hits its own PK so no new index is needed. Returns
        (blacklisted, ban_remaining, msg_count, was_blocked), or None when
        the read itself failed: None forces the caller back onto the
        individual checks (fail-closed), so a dead stream can never look
        like "not blacklisted, no penalty".
        """
        if not guild_id or not user_id:
            return False, 0, 0, False
        snap_now = now_ms()
        try:
            row = self.db.query_one(
                "SELECT "
                " (SELECT 1 FROM simple_blacklist"
                " WHERE guild_id = ? AND user_id = ?"
                " AND (expires_at IS NULL OR expires_at > ?)) AS bl, "
                " (SELECT giveaways_remaining FROM simple_giveaway_bans"
                " WHERE guild_id = ? AND user_id = ?) AS ban, "
                " (SELECT count FROM simple_message_counts"
                " WHERE guild_id = ? AND user_id = ?) AS msgs, "
                " (SELECT 1 FROM simple_giveaway_ban_hits WHERE guild_id = ?"
                " AND user_id = ? AND giveaway_id = ?) AS hit",
                (guild_id, user_id, snap_now, guild_id, user_id, guild_id, user_id,
                 guild_id, user_id, giveaway_id or ""),
            )
        except Exception:
            return None
        if not row:
            return None
        try:
            bl = row.get("bl") is not None
        except Exception:
            bl = False
        try:
            ban = max(0, int(row.get("ban") or 0))
        except (TypeError, ValueError):
            ban = 0
        try:
            msgs = int(row.get("msgs") or 0)
        except (TypeError, ValueError):
            msgs = 0
        try:
            hit = row.get("hit") is not None
        except Exception:
            hit = False
        return bl, ban, msgs, hit

    def record_message(self, guild_id: str, user_id: str) -> None:
        self.db.execute(
            "INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES (?, ?, 1)"
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
            (guild_id, user_id),
        )

    #: Rows per INSERT when flushing buffered counts. SQLite's default parameter
    #: limit is 999 and each row takes 3, so 300 leaves plenty of headroom.
    MESSAGE_FLUSH_CHUNK = 300

    def add_message_counts(self, rows: list[tuple[str, str, int]]) -> int:
        """Apply many (guild_id, user_id, delta) counts. Returns rows applied.

        One statement per flush instead of one per message is the entire point:
        N messages in the server cost ceil(N / MESSAGE_FLUSH_CHUNK) round-trips
        per flush interval rather than N immediate ones.

        Each chunk commits on its own, so a failure part way leaves the earlier
        chunks stored. The failure is raised as PartialFlush, which says how many
        rows landed: the caller hands back only the tail, because restoring the
        whole batch would write the committed head a second time.
        """
        applied = 0
        for start in range(0, len(rows), self.MESSAGE_FLUSH_CHUNK):
            chunk = rows[start : start + self.MESSAGE_FLUSH_CHUNK]
            # Snapshot first: if this chunk later fails ambiguously (a mid-write
            # "connection reset" db.execute will not replay), the snapshot
            # lets the retry requeue only what is still missing instead of the
            # whole chunk. One extra read per chunk, not per key.
            before = self._read_counts(chunk)
            values = ", ".join("(?, ?, ?)" for _ in chunk)
            params: list[Any] = []
            for guild_id, user_id, count in chunk:
                params.extend((guild_id, user_id, count))
            # Interpolated text is the placeholder list only: every value still
            # travels as a bound parameter, so there is nothing to inject.
            statement = f"INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES {values}"  # noqa: S608
            try:
                self.db.execute(
                    statement
                    + " ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
                    params,
                )
            except Exception as exc:
                if _is_ambiguous_loss(str(exc).lower()):
                    # The write may have landed before the response was lost.
                    # applied counts whole chunks and the caller requeues
                    # rows[applied:], so the reconciled shortfall replaces the
                    # chunk in place (padded with zero-deltas to keep the list
                    # length stable) and the retry adds each key once.
                    short = self._reconcile_chunk(chunk, before)
                    filler = [(g, u, 0) for g, u, _ in chunk[len(short):]]
                    rows[start : start + len(chunk)] = short + filler
                raise PartialFlush(applied, exc) from exc
            applied += len(chunk)
        return applied

    def _count_rows(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], int] | None:
        """Stored counts for many keys in ONE tuple-IN read.

        The text is interpolated from the number of keys only; every value
        stays a bound parameter, so there is nothing to inject. 300 keys is
        600 parameters -- under SQLite's 999 default -- and the chunking above
        keeps any caller inside that bound. Returns None when the read itself
        failed: callers cannot tell stored-zero from unread, so they requeue
        those keys whole instead of risking a silent loss.
        """
        if not keys:
            return {}
        marks = ", ".join("(?, ?)" for _ in keys)
        params: list[Any] = []
        for guild_id, user_id in keys:
            params.extend((guild_id, user_id))
        try:
            statement = (
                "SELECT guild_id, user_id, count FROM simple_message_counts"  # noqa: S608
                f" WHERE (guild_id, user_id) IN ({marks})"  # noqa: S608
            )
            rows = self.db.query(statement, params)
        except Exception:
            return None
        found: dict[tuple[str, str], int] = {}
        for row in rows:
            try:
                found[(str(row["guild_id"]), str(row["user_id"]))] = int(
                    row.get("count") or 0
                )
            except (KeyError, TypeError, ValueError):
                continue
        return found

    def _read_counts(
        self, chunk: list[tuple[str, str, int]]
    ) -> dict[tuple[str, str], int]:
        """Stored count per key before a flush chunk runs.

        One tuple-IN read for the whole chunk instead of a read per key: a
        300-message flush used to spend 300 round-trips here (the placeholder
        SQL is generated from the key count only, never from user data).
        Every key is seeded at 0 because a successful batched read proves the
        row is absent, which is exactly what the old per-key read recorded.
        Keys that cannot be read at all are left out -- _reconcile_chunk
        requeues those whole rather than risking silent loss.
        """
        keys = [(str(guild_id), str(user_id)) for guild_id, user_id, _ in chunk]
        found = self._count_rows(keys)
        if found is None:
            # One warning per chunk, not one per key: a dead stream would
            # otherwise log 300 lines for a single failure.
            log.warning("flush snapshot batch read failed for %d keys", len(chunk))
            return {}
        return {key: found.get(key, 0) for key in keys}

    def _reconcile_chunk(
        self, chunk: list[tuple[str, str, int]], before: dict[tuple[str, str], int]
    ) -> list[tuple[str, str, int]]:
        """Shortfall per key after an ambiguous chunk failure.

        `before` holds each key's count read just before the chunk ran. Whatever
        is already reflected in the table is not requeued, so retrying the flush
        cannot double-add the failed chunk. Keys that cannot be re-read -- the
        batched read failed or returned no row -- are requeued whole:
        over-counting beats silently losing messages.
        """
        keys = [(str(guild_id), str(user_id)) for guild_id, user_id, _ in chunk]
        after = self._count_rows(keys)
        shortfall: list[tuple[str, str, int]] = []
        for guild_id, user_id, delta in chunk:
            key = (str(guild_id), str(user_id))
            if after is None or key not in after:
                shortfall.append((guild_id, user_id, delta))
                continue
            stored = after[key]
            if key not in before:
                # No snapshot for this key (the pre-read failed): the stored
                # value cannot be compared, so requeue the whole delta rather
                # than risk silently losing messages.
                shortfall.append((guild_id, user_id, delta))
                continue
            missing = before[key] + delta - stored
            if missing > 0:
                shortfall.append((guild_id, user_id, missing))
        return shortfall

    def join(
        self,
        gw: Giveaway,
        *,
        user_id: str,
        username: str,
        member_roles: list[str],
        account_created_ts: float | None,
        pending_messages: int = 0,
        timed_out: bool = False,
        snapshot: tuple[bool, int, int, bool] | None = None,
        fetch_count: bool = True,
    ) -> int:
        self.check_eligible(
            gw, member_roles=member_roles, account_created_ts=account_created_ts,
            user_id=user_id, pending_messages=pending_messages, timed_out=timed_out,
            snapshot=snapshot,
        )
        # The giveaway, its deadline, the blacklist, the timeout-ban row and
        # the ban-hit row are re-checked inside the statement itself.
        # check_eligible above reads rows fetched moments earlier (the snapshot
        # is older still: read in parallel with get, before this thread even
        # started), so an end() / blacklist_add / penalty landing in between
        # would otherwise let the entry through. The statement makes each of
        # those impossible without a second round-trip. Roles / account age /
        # message counts need no guard here: roles and age come from the live
        # Discord member object (not the database), and message counts only
        # move fail-closed (a stale low count refuses; a wipe is skipped while
        # an active min-messages giveaway exists -- see end()).
        # A currently-timed-out member (timed_out=True) never reaches this
        # statement: check_eligible above always refuses them first. The bans
        # guard below therefore covers the penalty-row race only.
        now = now_ms()
        try:
            cur = self.db.execute(
                "INSERT INTO simple_entries (giveaway_id, user_id, username, entered_at)"
                " SELECT ?, ?, ?, ? WHERE EXISTS ("
                "   SELECT 1 FROM simple_giveaways"
                "   WHERE id = ? AND status = 'active' AND ends_at > ?"
                " ) AND NOT EXISTS ("
                "   SELECT 1 FROM simple_blacklist WHERE guild_id = ? AND user_id = ?"
                "   AND (expires_at IS NULL OR expires_at > ?)"
                " ) AND NOT EXISTS ("
                "   SELECT 1 FROM simple_giveaway_bans"
                "   WHERE guild_id = ? AND user_id = ? AND giveaways_remaining > 0"
                " ) AND NOT EXISTS ("
                "   SELECT 1 FROM simple_giveaway_ban_hits"
                "   WHERE guild_id = ? AND user_id = ? AND giveaway_id = ?"
                " )",
                (gw.id, user_id, username[:64], now, gw.id, now,
                 gw.guild_id, user_id, now, gw.guild_id, user_id,
                 gw.guild_id, user_id, gw.id),
            )
        except Exception as exc:
            msg = str(exc).upper()
            # Only the duplicate-entry constraint means "already entered". Any
            # other constraint failure (NOT NULL, CHECK, ...) is a real bug and
            # must surface instead of being reported as a harmless duplicate.
            if "UNIQUE" in msg or "PRIMARY" in msg:
                raise ServiceError("You are already entered.") from None
            # Ambiguous mid-write loss: the server may have applied the INSERT
            # before the response was lost. Disambiguate by entry age without
            # weakening duplicate protection: a row written within seconds is
            # this attempt (idempotent success); an older row is a duplicate
            # that raced the network failure ("already entered"); absent ->
            # surface so the caller can retry deliberately.
            if _is_ambiguous_loss(str(exc).lower()):
                try:
                    row = self.db.query_one(
                        "SELECT entered_at FROM simple_entries"
                        " WHERE giveaway_id = ? AND user_id = ?",
                        (gw.id, user_id),
                    )
                    if row is not None and row.get("entered_at") is not None:
                        try:
                            age = now_ms() - int(row.get("entered_at"))
                        except (TypeError, ValueError):
                            age = None
                        if age is not None and age < 15000:
                            # This attempt's row landed but the counter bump
                            # below was skipped (the exception path returns
                            # first), so the counter is knowingly stale here:
                            # ask COUNT(*) rather than trust a known 0.
                            return self.entry_count(gw.id, exact=True) if fetch_count else -1
                        raise ServiceError("You are already entered.") from None
                except ServiceError:
                    raise
                except Exception:
                    pass
            raise
        try:
            inserted = int(cur.rowcount or 0)
        except (TypeError, ValueError):
            # Unreadable rowcount: diagnose live below instead of assuming
            # success -- the row may pre-date this attempt.
            inserted = 0
        # Some drivers report -1 when the affected count is unknown: treat
        # any non-positive value as "guard refused" so a blocked join can
        # never read as success. Guards are diagnosed first (live, ended ->
        # blacklist -> timeout); only when every guard passes does a present
        # entry mean "the INSERT landed but the count is unknown".
        if inserted <= 0:
            # The statement guard can refuse for several reasons; diagnose with
            # live reads (no snapshot) so a stale snapshot cannot mislabel the
            # refusal. Ended/deadline first (no side effects, and a timed-out
            # member must never earn a penalty from a stale button on an ended
            # giveaway), then blacklist, then the timeout-ban gate live (it
            # consumes the penalty and drops entries, exactly as the non-race
            # path would have).
            try:
                fresh = self.get(gw.id)
            except ServiceError:
                raise ServiceError("This giveaway has ended.") from None
            if not fresh.active or fresh.ends_at <= now_ms():
                raise ServiceError("This giveaway has ended.")
            if self.is_blacklisted(gw.guild_id, user_id):
                raise ServiceError(self.ban_message(gw.guild_id, user_id))
            try:
                self.check_timeout_ban(
                    fresh, user_id=user_id, timed_out=timed_out
                )
            except ServiceError:
                raise
            # Every guard passes yet nothing inserted: either the block raced
            # away between the statement and this re-check (retry honestly) or
            # the driver hid a successful INSERT (confirm by entry).
            try:
                if self.has_entry(gw.id, user_id):
                    # The INSERT landed but the counter bump was skipped (this
                    # branch runs instead of it), so COUNT(*) is the only
                    # trustworthy answer: a stored 0 is otherwise "known".
                    return self.entry_count(gw.id, exact=True) if fetch_count else -1
            except Exception:
                pass
            raise ServiceError("Could not enter you. Try again.")
        # Counter bump on success (P2-2): same lane, 1 extra write, only when
        # the INSERT actually landed. COALESCE heals legacy NULL counters;
        # the status guard keeps a join that raced the draw from bumping a
        # frozen end/cancel count. Concurrent bumps never overwrite: +1 is
        # atomic in the statement itself, not read-modify-write.
        try:
            self.db.execute(
                "UPDATE simple_giveaways SET entrant_count = COALESCE(entrant_count, 0) + 1"
                " WHERE id = ? AND status = 'active'",
                (gw.id,),
            )
        except Exception:
            pass
        # The cached display count is now one behind. Both cached numbers are
        # cosmetic (nothing decides eligibility from them) but a fresh joiner
        # must not be shown a count that predates their own entry, so it is
        # dropped rather than left to expire.
        self._mark_stale(f"count:{gw.id}")
        # fetch_count=False skips the COUNT so the ack never waits on it:
        # the post-ack refresh_snapshot() supplies the count for the embed.
        # -1 means "committed, count unknown"; callers must not print it.
        if not fetch_count:
            return -1
        return self.entry_count(gw.id)

    def join_and_refresh(
        self,
        gw: Giveaway,
        *,
        user_id: str,
        username: str,
        member_roles: list[str],
        account_created_ts: float | None,
        pending_messages: int = 0,
        timed_out: bool = False,
        snapshot: tuple[bool, int, int, bool] | None = None,
    ) -> tuple[str | None, int]:
        """P0-2: commit + status/count in ONE db lane acquisition.

        handle_join used to pay _db_call(join) then _db_call(refresh_snapshot):
        2 semaphore acquires + 2 thread hops for 3 statements. This bundles
        the write (INSERT + counter bump) and the read (status + counter) in
        one lane, so a 50-burst holds 50 lanes instead of 150 sequential
        acquires. Same statements, same order, same atomic guards -- only the
        lane/thread overhead is removed. Refresh never raises (returns
        (None, 0) on dead read), so any exception here means the join failed.
        """
        self.join(
            gw,
            user_id=user_id,
            username=username,
            member_roles=member_roles,
            account_created_ts=account_created_ts,
            pending_messages=pending_messages,
            timed_out=timed_out,
            snapshot=snapshot,
            fetch_count=False,
        )
        return self.refresh_snapshot(gw.id)

    def leave(self, giveaway_id: str, user_id: str) -> bool:
        """Drop this member's entry. Only while the giveaway is running.

        Leaving an ended giveaway would rewrite who took part after the draw (and
        a cancelled one keeps its entries on purpose), so it reads as "not
        entered" there: the caller already reports False that way.
        """
        cur = self.db.execute(
            "DELETE FROM simple_entries WHERE giveaway_id = ? AND user_id = ? AND EXISTS ("
            " SELECT 1 FROM simple_giveaways WHERE id = ? AND status = 'active')",
            (giveaway_id, user_id, giveaway_id),
        )
        try:
            removed = (cur.rowcount or 0) > 0
        except Exception:
            removed = True
        if removed:
            # Keep the live counter exact: a leave must undo the join bump.
            # GREATEST-equivalent via CASE (portable sqlite/libsql) so a
            # racing double-leave can never drive it negative.
            try:
                self.db.execute(
                    "UPDATE simple_giveaways SET entrant_count ="
                    " CASE WHEN COALESCE(entrant_count, 0) > 0"
                    " THEN entrant_count - 1 ELSE 0 END WHERE id = ?",
                    (giveaway_id,),
                )
            except Exception:
                pass
            self._mark_stale(f"count:{giveaway_id}")
        return removed

    # -- end / reroll / cancel ------------------------------------------
    def _pick(self, giveaway_id: str, n: int, exclude: list[str] | None = None) -> list[str]:
        # The exclusion set is built once per draw: rebuilding it for every row
        # made picking from a large giveaway quadratic for no reason.
        skipped = set(exclude or ())
        pool = [uid for uid in self.entry_user_ids(giveaway_id) if uid not in skipped]
        if not pool or n < 1:
            return []
        return self._rand.sample(pool, min(n, len(pool)))

    def _read_winners_raw(self, giveaway_id: str) -> tuple[list[str], str]:
        """Current winners plus the stored raw JSON for a CAS update.

        json.dumps has one canonical form here (no spaces), but comparing the
        raw text instead of re-encoding avoids any mismatch tripping the
        compare-and-swap in reroll/replace/expire.
        """
        row = self.db.query_one(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?", (giveaway_id,)
        )
        raw = str((row or {}).get("winners_json") or "[]")
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = []
        winners = [str(w) for w in parsed] if isinstance(parsed, list) else []
        return winners, raw

    def end(
        self, giveaway_id: str, *, now: int | None = None, require_due: bool = False
    ) -> tuple[Giveaway, list[str]]:
        """End a running giveaway and draw its winners.

        The draw happens first, then one UPDATE writes status, ended_at, the
        winners and the entrant count together, with "status = 'active'" in the
        WHERE clause as the claim: the auto-draw tick and /giveaway_end can fire
        in the same second, and only one of them may store (and announce) its
        draw. Whoever loses the claim is told the giveaway is already over. One
        statement also means a failure can no longer strand a giveaway as
        'ended' with no winners and no announcement.
        Claim windows open right after the flip as separate statements (no
        migration, no cross-driver transaction), but a crash in between cannot
        strand winners: start_claims is idempotent (INSERT OR IGNORE), post-flip
        failures return instead of raising (the draw is stored, so the caller
        still announces it), and repair_missing_claims — also run by every
        claim read — recreates any lost rows with the same deadline.
        B04: the auto-draw tick must not beat a concurrent extend. due() is a
        snapshot, so a stale due row plus a status-only claim would end a
        giveaway whose deadline just moved. Tick callers pass require_due=True
        and the flip gains "AND ends_at <= ?": the statement itself re-checks
        the deadline, so a win means still-due at flip time and a loss means
        extended (or already ended). Manual /giveaway_end leaves require_due
        off and keeps the early-end behavior.
        """
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("This giveaway has already ended.")
        # One timestamp for the due check, the flip and the claim windows: the
        # repair sweep rebuilds deadlines as ended_at + timeout, so both must
        # share a base, and the tick test needs a deterministic clock.
        ts = now if now is not None else now_ms()
        if require_due and gw.ends_at > ts:
            raise ServiceError("This giveaway is not due to end yet.")
        pool = self.entry_user_ids(gw.id)
        winners = self._rand.sample(pool, min(gw.winner_count, len(pool))) if pool else []
        if require_due:
            cur = self.db.execute(
                "UPDATE simple_giveaways SET status = 'ended', ended_at = ?, winners_json = ?,"
                " entrant_count = ? WHERE id = ? AND status = 'active' AND ends_at <= ?",
                (ts, json.dumps(winners), len(pool), gw.id, ts),
            )
        else:
            cur = self.db.execute(
                "UPDATE simple_giveaways SET status = 'ended', ended_at = ?, winners_json = ?,"
                " entrant_count = ? WHERE id = ? AND status = 'active'",
                (ts, json.dumps(winners), len(pool), gw.id),
            )
        if not _changed(cur):
            try:
                live = self.get(gw.id)
            except ServiceError:
                raise ServiceError("This giveaway has already ended.") from None
            if live.active and require_due and live.ends_at > ts:
                raise ServiceError("This giveaway is not due to end yet.")
            raise ServiceError("This giveaway has already ended.")
        # Fresh grind for the next giveaway *in this server*: message counts go
        # back to zero here, so the next min-messages requirement measures
        # activity after this giveaway rather than lifetime activity. Scoped by
        # guild — wiping the whole table would silently zero every other
        # server's counts — and skipped while another running giveaway here
        # still has a min-messages requirement, which would otherwise be reset
        # under the members already grinding for it.
        # Post-flip failures must not strand the announcement: the draw is
        # stored, so wipe/claim errors are logged and healed (repair sweep
        # below + every claim read) while the caller still announces. Only the
        # guarded flip above may raise.
        try:
            self.db.execute(
                "DELETE FROM simple_message_counts WHERE guild_id = ? AND NOT EXISTS ("
                " SELECT 1 FROM simple_giveaways WHERE guild_id = ? AND status = 'active'"
                " AND min_messages > 0)",
                (gw.guild_id, gw.guild_id),
            )
        except Exception:
            log.warning("end cleanup wipe failed for %s", gw.id)
        # The guarded flip above stored every field this object needs, so
        # build the ended object from the pre-flip read rather than paying
        # another row read: the success path used to read the row here and
        # again for the return. The losers/race branch above still reads
        # live data, because there the CAS did not store a draw.
        ended = replace(
            gw,
            status="ended",
            winners=list(winners),
            entrant_count=len(pool),
        )
        # Claim windows open here so the deadline survives restarts: rows are
        # durable, and the tick picks them up even if the process dies first.
        # Shared ts above keeps flip and inserts on one deadline.
        try:
            self.start_claims(ended, winners, now=ts)
        except Exception:
            log.warning("end claim insert failed for %s; repairing", gw.id)
            try:
                self.repair_missing_claims(gw.id, now=ts)
            except Exception:
                pass
        # The row just flipped to 'ended': the cached header, the cached list
        # and the cached count are all wrong the moment this commits. mark()
        # inside the write path is enough: the click paths and the tick fire a
        # best-effort flush right after the write (see flush_invalidations),
        # and whatever misses that -- /giveaway_cancel, /giveaway_extend -- is
        # left to the key's own TTL (4-10s), because each cached value is
        # derivable from Turso and never decides anything.
        self._mark_stale(
            f"gw:{gw.id}", f"guild:{gw.guild_id}:active", "all:active", f"count:{gw.id}"
        )
        return ended, winners

    def reroll(
        self, giveaway_id: str, count: int = 1, replace_user_id: str | None = None
    ) -> tuple[Giveaway, list[str]]:
        gw = self.get(giveaway_id)
        if gw.active:
            raise ServiceError("End the giveaway before rerolling.")
        if gw.status != "ended":
            # A cancelled giveaway keeps its entries, so without this a reroll
            # would hand out prizes for a giveaway the server called off.
            raise ServiceError("A cancelled giveaway cannot be rerolled.")
        if replace_user_id is not None:
            return self._reroll_replace(gw, str(replace_user_id))
        # The whole list has to stay inside MAX_WINNERS. create() bounds the first
        # draw, but a reroll only ever adds, so repeated rerolls used to grow the
        # announcement past what Discord will accept — and the post then failed.
        prev = list(gw.winners)
        room = self.MAX_WINNERS - len(prev)
        if room < 1:
            raise ServiceError(
                f"This giveaway already names {len(prev)} winners, the most one"
                " announcement can hold."
            )
        wanted = max(1, min(int(count or gw.winner_count), room))
        # Claim-aware exclusion: winners_json holds every manual draw,
        # simple_claims holds every auto draw (expired/claimed/skipped). Union
        # both so a manual /reroll can never re-pick someone the claim timer
        # already drew, and a later expiry can never re-pick this reroll.
        drawn: set[str] = set(prev)
        for r in self.db.query(
            "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (gw.id,)
        ):
            drawn.add(str(r.get("user_id")))
        fresh = self._pick(gw.id, wanted, exclude=sorted(drawn))
        if not fresh:
            # Redrawing from everyone would announce earlier winners as new
            # ones (or nobody at all once the entries were wiped): say so.
            raise ServiceError("No other entrants left to reroll")
        # CAS append: two rerolls/expiries racing both read the same
        # winners_json and the loser would overwrite the winner's draw.
        # Re-read + compare-and-swap so the loser rebases and retries.
        for _ in range(5):
            winners_now, raw_now = self._read_winners_raw(gw.id)
            room_now = self.MAX_WINNERS - len(winners_now)
            if room_now < 1:
                raise ServiceError(
                    f"This giveaway already names {len(winners_now)} winners, the most one"
                    " announcement can hold."
                )
            # Fresh was picked against a possibly stale drawn set; drop any
            # that landed while we were picking so we never duplicate.
            use = [w for w in fresh if w not in winners_now]
            if not use:
                # Everything we drew is already named (a racing draw picked
                # the same entrant): re-pick the shortfall, still excluding
                # everyone ever drawn.
                drawn_now: set[str] = set(winners_now)
                for r in self.db.query(
                    "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (gw.id,)
                ):
                    drawn_now.add(str(r.get("user_id")))
                need = max(1, min(int(count or gw.winner_count), room_now))
                repick = self._pick(gw.id, need, exclude=sorted(drawn_now))
                use = [w for w in repick if w not in winners_now]
                if not use:
                    raise ServiceError("No other entrants left to reroll")
                fresh = use
            combined = winners_now + use
            cur = self.db.execute(
                "UPDATE simple_giveaways SET winners_json = ? WHERE id = ? AND winners_json = ?",
                (json.dumps(combined), gw.id, raw_now),
            )
            if _changed(cur):
                # The CAS above stored the draw; a crash before the inserts
                # must not strand the fresh winners. start_claims is idempotent
                # (INSERT OR IGNORE), failures are logged and healed by the
                # repair sweep, and the stored draw is still returned for the
                # announce. The old winners' rows are left alone: a manual
                # reroll is additive, and each deadline still expires/skips
                # independently.
                try:
                    self.start_claims(self.get(gw.id), use)
                except Exception:
                    log.warning("reroll claim insert failed for %s; repairing", gw.id)
                    try:
                        self.repair_missing_claims(gw.id)
                    except Exception:
                        pass
                self._mark_stale(f"gw:{gw.id}")
                return self.get(gw.id), use
        raise ServiceError("Reroll collided with another draw. Try again.")

    def _reroll_replace(self, gw: Giveaway, replace_user_id: str) -> tuple[Giveaway, list[str]]:
        """Swap one named winner for a fresh entrant. Returns (giveaway, [new]).

        Targeted /reroll: the admin picks which winner loses their slot (e.g.
        C never claimed while A and B did). The old winner is swapped out in
        place so the winners list never grows, and their pending claim (if any)
        is closed as `replaced` so the tick can never expire it into a duplicate
        slot. The replacement gets its own claim window. Never re-picks anyone
        ever drawn (winners_json + every simple_claims row, any status).
        """
        prev = list(gw.winners)
        if replace_user_id not in prev:
            raise ServiceError("That member is not a winner of this giveaway.")
        # Settled claims cannot be replaced: a claimed/skipped prize is done,
        # and an expired/replaced slot already drew its replacement. Only a
        # pending (unclaimed) winner or a claims-disabled row may be swapped.
        clash = self.db.query_one(
            "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
            " ORDER BY round DESC LIMIT 1",
            (gw.id, replace_user_id),
        )
        if clash is not None:
            settled = str(clash.get("status") or "")
            if settled == self.CLAIM_CLAIMED:
                raise ServiceError("That prize was already claimed.")
            if settled == self.CLAIM_SKIPPED:
                raise ServiceError("That claim was already verified by staff.")
            if settled in (self.CLAIM_EXPIRED, self.CLAIM_REPLACED):
                raise ServiceError("That winner was already replaced.")
            if settled != self.CLAIM_PENDING:
                raise ServiceError("That claim window has already closed.")
        drawn: set[str] = set(prev)
        for r in self.db.query(
            "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (gw.id,)
        ):
            drawn.add(str(r.get("user_id")))
        fresh = self._pick(gw.id, 1, exclude=sorted(drawn))
        if not fresh:
            raise ServiceError("No other entrants left to reroll")
        new_user = fresh[0]
        # CAS swap with retry: a racing reroll/expire may rewrite winners_json
        # between our read and write; the loser rebases instead of overwriting.
        for _ in range(5):
            winners_now, raw_now = self._read_winners_raw(gw.id)
            if replace_user_id not in winners_now:
                raise ServiceError("That member is not a winner of this giveaway.")
            if new_user in winners_now:
                # Racing draw picked the same entrant: re-pick, still excluding
                # everyone ever drawn.
                drawn_now: set[str] = set(winners_now)
                for r in self.db.query(
                    "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (gw.id,)
                ):
                    drawn_now.add(str(r.get("user_id")))
                repick = self._pick(gw.id, 1, exclude=sorted(drawn_now))
                if not repick:
                    raise ServiceError("No other entrants left to reroll")
                new_user = repick[0]
            # Swap in place: first matching slot only, order preserved.
            swapped = [new_user if w == replace_user_id else w for w in winners_now]
            # Defensive: if the winners list somehow held the old id twice,
            # only the first slot swaps and the rest stay (never duplicate).
            seen_new = False
            fixed: list[str] = []
            for w in swapped:
                if w == new_user:
                    if seen_new:
                        fixed.append(replace_user_id)
                        continue
                    seen_new = True
                fixed.append(w)
            combined = fixed
            cur = self.db.execute(
                "UPDATE simple_giveaways SET winners_json = ? WHERE id = ? AND winners_json = ?",
                (json.dumps(combined), gw.id, raw_now),
            )
            if not _changed(cur):
                continue
            # Close the old slot's pending claim so a later tick cannot expire
            # it into a second replacement for the same slot. Any status counts
            # as "ever drawn", so the exclusion holds even after the swap.
            # Settle/start failures after the CAS are logged and healed (the
            # repair sweep settles the old slot and opens the fresh window)
            # while the stored swap is still returned for the announce.
            try:
                self.db.execute(
                    "UPDATE simple_claims SET status = ?"
                    " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                    (self.CLAIM_REPLACED, gw.id, replace_user_id),
                )
                row = self.db.query_one(
                    "SELECT user_id FROM simple_claims WHERE giveaway_id = ? AND user_id = ? LIMIT 1",
                    (gw.id, replace_user_id),
                )
                if row is None:
                    # No claim row at all (e.g. claims disabled): leave a marker
                    # so this user stays excluded from every future draw.
                    self.db.execute(
                        "INSERT OR IGNORE INTO simple_claims"
                        " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
                        " VALUES (?, ?, 0, ?, 0, ?)",
                        (gw.id, replace_user_id, self.CLAIM_REPLACED, now_ms()),
                    )
                # Fresh winner's own claim window. No-op when claims are disabled.
                self.start_claims(self.get(gw.id), [new_user])
            except Exception:
                log.warning("reroll-replace settle failed for %s; repairing", gw.id)
                try:
                    self.repair_missing_claims(gw.id)
                except Exception:
                    pass
            return self.get(gw.id), [new_user]
        raise ServiceError("Reroll collided with another draw. Try again.")

    # -- winner claims (optional claim timer) -----------------------------
    #: Claim row statuses. `pending` rows hold a deadline; the rest are final.
    CLAIM_PENDING = "pending"
    CLAIM_CLAIMED = "claimed"
    CLAIM_EXPIRED = "expired"
    CLAIM_SKIPPED = "skipped"
    #: A pending claim closed by a targeted /reroll (replaced, not expired).
    CLAIM_REPLACED = "replaced"

    def start_claims(self, gw: Giveaway, winners: list[str], now: int | None = None) -> None:
        """Open a claim window for freshly drawn winners. Idempotent.

        Called once per draw (end, and each claim-timeout replacement). Rows
        are INSERT OR IGNORE so a retried announce never doubles a deadline.
        No-op when the giveaway has no claim window configured. One multi-row
        statement for the whole draw: same idempotence, one write round trip
        instead of one per winner (a 25-winner draw used to spend 25).
        MAX_WINNERS bounds the row count, so the placeholder text is generated
        from a length only and every value stays bound.
        """
        if not gw.claim_enabled or not winners:
            return
        ts = now if now is not None else now_ms()
        deadline = ts + gw.claim_timeout_seconds * 1000
        values = ", ".join("(?, ?, 0, 'pending', ?, ?)" for _ in winners)
        params: list[Any] = []
        for user_id in winners:
            params.extend((gw.id, str(user_id), deadline, ts))
        self.db.execute(
            "INSERT OR IGNORE INTO simple_claims"  # noqa: S608
            " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
            f" VALUES {values}",  # noqa: S608
            params,
        )

    def repair_missing_claims(self, giveaway_id: str, now: int | None = None) -> int:
        """Recreate claim rows lost between a draw and its inserts. Idempotent.

        end()/reroll()/expire_claim() store winners first and open the claim
        windows right after as separate statements (one timestamp shared by
        the flip and the inserts, no migration, no cross-driver transaction),
        so a crash in between strands winners with no rows. Every insert here
        is INSERT OR IGNORE: winners holding any claim row (any status) are
        left alone and only winners with no row at all gain a fresh pending
        window. Pending rows for users no longer named (a targeted reroll that
        died before settling the old slot) flip to replaced so the tick can
        never expire them into a duplicate slot. Returns rows written.
        """
        try:
            gw = self.get(giveaway_id)
        except ServiceError:
            return 0
        if not gw.claim_enabled or not gw.winners:
            return 0
        try:
            rows = self.db.query(
                "SELECT user_id, status, deadline_ms FROM simple_claims WHERE giveaway_id = ?",
                (gw.id,),
            )
        except Exception:
            return 0
        have: set[str] = set()
        latest = 0
        for r in rows:
            try:
                uid = str(r.get("user_id"))
            except Exception:
                log.warning("claim repair row skipped for %s", gw.id)
                continue
            have.add(uid)
            try:
                latest = max(latest, int(r.get("deadline_ms") or 0))
            except (TypeError, ValueError):
                log.warning("claim repair deadline skipped for %s", gw.id)
                continue
        ts = now if now is not None else now_ms()
        if latest > 0:
            # Never hand a repaired winner an already-past deadline: a later
            # draw (reroll/expire) must start its own window at repair time,
            # while a partially inserted initial batch simply gains a few ms.
            deadline = max(latest, ts + gw.claim_timeout_seconds * 1000)
        else:
            try:
                erow = self.db.query_one(
                    "SELECT ended_at FROM simple_giveaways WHERE id = ?", (gw.id,)
                )
            except Exception:
                erow = None
            try:
                base = int((erow or {}).get("ended_at") or ts)
            except (TypeError, ValueError):
                base = ts
            deadline = base + gw.claim_timeout_seconds * 1000
        winners = [str(w) for w in dict.fromkeys(str(w) for w in gw.winners)]
        written = 0
        for uid in winners:
            if uid in have:
                continue
            try:
                self.db.execute(
                    "INSERT OR IGNORE INTO simple_claims"
                    " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
                    " VALUES (?, ?, 0, 'pending', ?, ?)",
                    (gw.id, uid, deadline, ts),
                )
            except Exception:
                log.warning("claim repair insert failed for %s/%s", gw.id, uid)
                continue
            written += 1
        if winners:
            # Settle pending rows for users no longer named. Healthy draws have
            # none: every claim row belongs to a current winner, so this UPDATE
            # used to write on every repair -- one write per giveaway on every
            # sweep tick -- to change nothing. `have` includes non-pending rows
            # on purpose (conservative): a stale non-pending row still triggers
            # the UPDATE, which simply matches no pending row.
            stale = have - set(winners)
            if stale:
                marks = ", ".join("?" for _ in winners)
                try:
                    cur = self.db.execute(
                        "UPDATE simple_claims SET status = ? WHERE giveaway_id = ?"  # noqa: S608
                        f" AND status = 'pending' AND user_id NOT IN ({marks})",  # noqa: S608
                        (self.CLAIM_REPLACED, gw.id, *winners),
                    )
                except Exception:
                    log.warning("claim repair settle failed for %s", gw.id)
                else:
                    try:
                        written += int(cur.rowcount or 0)
                    except (TypeError, ValueError):
                        pass
        return written

    def repair_all_missing_claims(self, now: int | None = None, limit: int = 25) -> int:
        """Heal every stranded claim window. due_claims() runs this per tick.

        A giveaway stranded with zero pending rows never appears in due_claims,
        so per-giveaway repair alone would never visit it: this bounded sweep
        (oldest ended first) recreates those windows, and the tick then picks
        them up like any restart recovery. Idempotent; failures heal on a
        later tick. Returns rows written.
        """
        ts = now if now is not None else now_ms()
        try:
            rows = self.db.query(
                "SELECT id FROM simple_giveaways WHERE status = 'ended'"
                " AND claim_timeout_seconds > 0 AND winners_json != '[]'"
                " ORDER BY COALESCE(ended_at, 0) ASC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            )
        except Exception:
            return 0
        total = 0
        for r in rows:
            try:
                gid = str(r.get("id"))
            except Exception:
                log.warning("claim repair sweep row skipped")
                continue
            try:
                total += self.repair_missing_claims(gid, now=ts)
            except Exception:
                log.warning("claim repair sweep failed for a giveaway")
                continue
        return total

    def pending_claims(self, giveaway_id: str) -> list[dict]:
        """Pending claim rows for a giveaway, oldest deadline first.

        Heals stranded windows first, so a crash between a draw and its
        inserts never reads as "no claims".
        """
        try:
            self.repair_missing_claims(giveaway_id)
        except Exception:
            pass
        return self.db.query(
            "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at,"
            " skipped_by, skipped_at, created_at FROM simple_claims"
            " WHERE giveaway_id = ? AND status = 'pending' ORDER BY deadline_ms ASC",
            (giveaway_id,),
        )

    def claim_status(self, giveaway_id: str) -> list[dict]:
        """Every claim row for a giveaway (for the dashboard/admin view)."""
        return self.db.query(
            "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at,"
            " skipped_by, skipped_at, created_at FROM simple_claims"
            " WHERE giveaway_id = ? ORDER BY created_at ASC",
            (giveaway_id,),
        )

    def due_claims(self, now: int | None = None, limit: int = 25) -> list[dict]:
        """Pending claims past their deadline. Tick feeds on this.

        Heals stranded windows first (bounded sweep, oldest ended first), so a
        crash between a draw and its inserts never hides as "no due claims".
        """
        ts = now if now is not None else now_ms()
        try:
            self.repair_all_missing_claims(now=ts)
        except Exception:
            pass
        return self.db.query(
            "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at,"
            " skipped_by, skipped_at, created_at FROM simple_claims"
            " WHERE status = 'pending' AND deadline_ms <= ?"
            " ORDER BY deadline_ms ASC LIMIT ?",
            (ts, max(1, min(int(limit), 100))),
        )

    def claim(self, giveaway_id: str, user_id: str) -> dict:
        """Claim a prize as its drawn winner. Returns the claim row.

        Guarded UPDATE: only a pending row for this exact (giveaway, winner)
        flips, so double-clicks and two processes racing both collapse into one
        success and one "already claimed" refusal.
        """
        cur = self.db.execute(
            "UPDATE simple_claims SET status = 'claimed', claimed_at = ?"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
            (now_ms(), giveaway_id, str(user_id)),
        )
        if not _changed(cur):
            # A crash between a draw and its inserts strands winners with no
            # row: heal once, then retry so the rightful winner still claims.
            try:
                self.repair_missing_claims(giveaway_id)
            except Exception:
                pass
            else:
                try:
                    retry = self.db.execute(
                        "UPDATE simple_claims SET status = 'claimed', claimed_at = ?"
                        " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                        (now_ms(), giveaway_id, str(user_id)),
                    )
                except Exception:
                    retry = None
                if retry is not None and _changed(retry):
                    cur = retry
        if not _changed(cur):
            row = self.db.query_one(
                "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
                " ORDER BY round DESC LIMIT 1",
                (giveaway_id, str(user_id)),
            )
            if row is None:
                raise ServiceError("Only the drawn winner can claim this prize.")
            status = str(row.get("status") or "")
            if status == self.CLAIM_CLAIMED:
                raise ServiceError("This prize was already claimed.")
            if status == self.CLAIM_SKIPPED:
                raise ServiceError("This prize was already verified by staff.")
            if status == self.CLAIM_REPLACED:
                raise ServiceError("This winner was replaced by a reroll.")
            raise ServiceError("This claim window has closed.")
        row = self.db.query_one(
            "SELECT * FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
            " AND status = 'claimed' ORDER BY round DESC LIMIT 1",
            (giveaway_id, str(user_id)),
        )
        return dict(row or {})

    def skip_claim(self, giveaway_id: str, user_id: str, staff_id: str) -> dict:
        """Mark a pending claim manually verified (ticket path). No replacement.

        Guarded like claim(): only a pending row flips, so a second /skipclaim
        (or a claim racing it) gets a clear refusal instead of a double write.
        """
        if not staff_id:
            raise ServiceError("A staff member must verify the claim.")
        cur = self.db.execute(
            "UPDATE simple_claims SET status = 'skipped', skipped_by = ?, skipped_at = ?"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
            (str(staff_id), now_ms(), giveaway_id, str(user_id)),
        )
        if not _changed(cur):
            # Same stranded-window heal as claim(): a missing row may just be
            # a draw whose inserts never landed.
            try:
                self.repair_missing_claims(giveaway_id)
            except Exception:
                pass
            else:
                try:
                    retry = self.db.execute(
                        "UPDATE simple_claims SET status = 'skipped',"
                        " skipped_by = ?, skipped_at = ?"
                        " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                        (str(staff_id), now_ms(), giveaway_id, str(user_id)),
                    )
                except Exception:
                    retry = None
                if retry is not None and _changed(retry):
                    cur = retry
        if not _changed(cur):
            row = self.db.query_one(
                "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
                " ORDER BY round DESC LIMIT 1",
                (giveaway_id, str(user_id)),
            )
            if row is None:
                raise ServiceError("No pending claim for that member in this giveaway.")
            status = str(row.get("status") or "")
            if status == self.CLAIM_CLAIMED:
                raise ServiceError("That prize was already claimed.")
            if status == self.CLAIM_SKIPPED:
                raise ServiceError("That claim was already verified by staff.")
            if status == self.CLAIM_REPLACED:
                raise ServiceError("That winner was replaced by a reroll.")
            raise ServiceError("That claim window has already closed.")
        row = self.db.query_one(
            "SELECT * FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
            " AND status = 'skipped' ORDER BY round DESC LIMIT 1",
            (giveaway_id, str(user_id)),
        )
        return dict(row or {})

    def expire_claim(
        self, giveaway_id: str, user_id: str, now: int | None = None
    ) -> tuple[Giveaway, list[str]]:
        """Expire one pending claim and draw its replacement. Atomic-ish.

        The guarded UPDATE is the concurrency gate: whoever flips pending ->
        expired owns the replacement draw, and a racing tick/claim/skip sees a
        non-pending row and stops. The replacement reuses _pick with every
        ever-drawn user excluded, so nobody is ever selected twice. Returns the
        giveaway and the (possibly empty) replacement list.
        """
        gw = self.get(giveaway_id)
        if gw.active:
            raise ServiceError("This giveaway is still running.")
        ts = now if now is not None else now_ms()
        cur = self.db.execute(
            "UPDATE simple_claims SET status = 'expired'"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending' AND deadline_ms <= ?",
            (giveaway_id, str(user_id), ts),
        )
        if not _changed(cur):
            raise ServiceError("That claim is no longer pending.")
        drawn = [
            str(r.get("user_id"))
            for r in self.db.query(
                "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (giveaway_id,)
            )
        ]
        fresh = self._pick(gw.id, 1, exclude=drawn)
        if not fresh:
            return self.get(gw.id), []
        # CAS append with retry: a racing additive reroll may rewrite
        # winners_json between our pick and write; the loser rebases instead
        # of overwriting the other draw.
        for _ in range(5):
            winners_now, raw_now = self._read_winners_raw(gw.id)
            use = [w for w in fresh if w not in winners_now]
            if not use:
                # Racing draw took the same entrant: pick another, still
                # excluding everyone ever drawn.
                redrawn: set[str] = set(winners_now)
                for r in self.db.query(
                    "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                    (giveaway_id,),
                ):
                    redrawn.add(str(r.get("user_id")))
                repick = self._pick(gw.id, 1, exclude=sorted(redrawn))
                if not repick:
                    return self.get(gw.id), []
                fresh = repick
                continue
            combined = winners_now + use
            cur2 = self.db.execute(
                "UPDATE simple_giveaways SET winners_json = ? WHERE id = ? AND winners_json = ?",
                (json.dumps(combined), gw.id, raw_now),
            )
            if not _changed(cur2):
                continue
            # Stored above; a crash before the inserts must not strand the
            # replacement. Idempotent inserts plus the repair sweep heal it,
            # and the stored draw is still returned for the announce.
            try:
                self.start_claims(self.get(gw.id), use, now=ts)
            except Exception:
                log.warning("expire claim insert failed for %s; repairing", gw.id)
                try:
                    self.repair_missing_claims(gw.id, now=ts)
                except Exception:
                    pass
            return self.get(gw.id), use
        raise ServiceError("Reroll collided with another draw. Try again.")

    def discard(self, giveaway_id: str) -> None:
        """Drop a giveaway that never reached Discord.

        Used when the announcement message could not be posted: the row would
        otherwise stay 'active' forever, auto-end on schedule, and try to
        announce winners into a channel that already refused the bot.
        """
        self.db.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", (giveaway_id,))
        self.db.execute("DELETE FROM simple_entries WHERE giveaway_id = ?", (giveaway_id,))
        self.db.execute("DELETE FROM simple_giveaways WHERE id = ?", (giveaway_id,))
        # The row is gone: a cached active list still holding it would serve a
        # giveaway that no longer exists. The guild-scoped list key is not
        # marked because this path has no guild id to build it from; it ages
        # out on its own short TTL.
        self._mark_stale("all:active", f"count:{giveaway_id}")

    def cancel(self, giveaway_id: str) -> Giveaway:
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("Only an active giveaway can be cancelled.")
        # Guarded like end(): an end() or a second cancel landing between the
        # read above and this write must not be overwritten.
        cur = self.db.execute(
            "UPDATE simple_giveaways SET status = 'cancelled', ended_at = ?,"
            " entrant_count = (SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?)"
            " WHERE id = ? AND status = 'active'",
            (now_ms(), gw.id, gw.id),
        )
        if not _changed(cur):
            raise ServiceError("Only an active giveaway can be cancelled.")
        self._mark_stale(
            f"gw:{gw.id}", f"guild:{gw.guild_id}:active", "all:active", f"count:{gw.id}"
        )
        return self.get(gw.id)

    def set_message(self, giveaway_id: str, message_id: str) -> None:
        self.db.execute(
            "UPDATE simple_giveaways SET message_id = ? WHERE id = ?", (message_id, giveaway_id)
        )

    def extend(self, giveaway_id: str, extra_seconds: int) -> Giveaway:
        """Push the deadline back. Only on a running giveaway."""
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("Only a running giveaway can be extended.")
        if extra_seconds < 60 or extra_seconds > 60 * 86400:
            raise ServiceError("Extend by 1 minute to 60 days at a time.")
        cur = self.db.execute(
            "UPDATE simple_giveaways SET ends_at = ends_at + ? WHERE id = ? AND status = 'active'",
            (extra_seconds * 1000, gw.id),
        )
        if not _changed(cur):
            raise ServiceError("Only a running giveaway can be extended.")
        self._mark_stale(f"gw:{gw.id}", f"guild:{gw.guild_id}:active", "all:active")
        return self.get(gw.id)

    def set_entrants_role(self, giveaway_id: str, role_id: str | None) -> None:
        self.db.execute(
            "UPDATE simple_giveaways SET entrants_role_id = ? WHERE id = ?",
            (role_id, giveaway_id),
        )

    def ended_with_roles(self, limit: int = 200) -> list[Giveaway]:
        """Finished giveaways whose entrants role was never deleted.

        Restart recovery for the delayed role cleanup: anything listed here
        is a leftover whose 5-minute delete never ran.
        """
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status != 'active'"
            " AND entrants_role_id IS NOT NULL AND entrants_role_id != ''"
            " ORDER BY ended_at DESC LIMIT ?",
            (limit,),
        )
        return [Giveaway.from_row(r) for r in rows]

    #: How long join data (who entered) survives after a giveaway finishes.
    #: The giveaway record itself (prize, winners, status) is kept; only the
    #: per-user entry rows are wiped.
    ENTRY_RETENTION_MS = 5 * 3600 * 1000

    def wipe_stale_entries(self, now: int | None = None) -> int:
        """Delete entry rows for giveaways finished over 5h ago. Returns count.

        Also zeroes the live counter on wiped giveaways: otherwise
        entry_count() would keep returning the stale pre-wipe bump.
        """
        cutoff = (now if now is not None else now_ms()) - self.ENTRY_RETENTION_MS
        try:
            self.db.execute(
                "UPDATE simple_giveaways SET entrant_count = 0 WHERE status != 'active'"
                " AND COALESCE(ended_at, created_at) <= ?"
                " AND NOT EXISTS (SELECT 1 FROM simple_claims"
                " WHERE simple_claims.giveaway_id = simple_giveaways.id"
                " AND simple_claims.status = 'pending')",
                (cutoff,),
            )
        except Exception:
            pass
        cur = self.db.execute(
            # ended_at was added to a table that already held data, so giveaways
            # that finished before that migration carry NULL forever; created_at
            # is the fallback that still lets their entry rows expire.
            "DELETE FROM simple_entries WHERE giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE status != 'active'"
            " AND COALESCE(ended_at, created_at) <= ?"
            # A pending claim still needs its entry pool for the replacement
            # draw, so its giveaway is exempt until the claim settles.
            " AND NOT EXISTS (SELECT 1 FROM simple_claims"
            " WHERE simple_claims.giveaway_id = simple_giveaways.id"
            " AND simple_claims.status = 'pending'))",
            (cutoff,),
        )
        try:
            return int(cur.rowcount or 0)
        except (TypeError, ValueError):
            return 0

    # -- blacklist (timed + permanent bans) ---------------------------------
    #: Max timed-ban duration in hours. 0 = permanent (the original
    #: blacklist_add() had no expiry, so the default stays permanent).
    BAN_MAX_HOURS = 720

    def _ban_now(self, now: int | None) -> int:
        return now if now is not None else now_ms()

    def ban_info(
        self, guild_id: str, user_id: str, now: int | None = None
    ) -> dict[str, object] | None:
        """Active ban row, or None when not banned / expired.

        Permanent = expires_at NULL. Timed = expires_at > now. Expired rows
        are treated as absent (lazy expiry, no extra write on the hot path).
        """
        if not guild_id or not user_id:
            return None
        ts = self._ban_now(now)
        row = self.db.query_one(
            "SELECT expires_at, reason, banned_by, created_at FROM simple_blacklist"
            " WHERE guild_id = ? AND user_id = ?"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (guild_id, user_id, ts),
        )
        if row is None:
            return None
        try:
            raw_exp = row.get("expires_at")
            expires_at = None if raw_exp is None else int(raw_exp)
        except (TypeError, ValueError):
            expires_at = None
        reason = str(row.get("reason") or "")
        banned_by = str(row.get("banned_by") or "")
        permanent = expires_at is None
        remaining = 0 if permanent else max(0, expires_at - ts)
        return {
            "expires_at": expires_at,
            "reason": reason,
            "banned_by": banned_by,
            "permanent": permanent,
            "remaining_ms": remaining,
        }

    def ban_message(self, guild_id: str, user_id: str, now: int | None = None) -> str:
        """Refusal text for a banned member. Always contains "blocked".

        Shown both to the refused member (join path) and reused by staff
        confirmations. Reason appears in both places when set.
        """
        base = "\U0001F6AB You are blocked from giveaways in this server."
        info = self.ban_info(guild_id, user_id, now)
        if info is None:
            return base
        parts = [base]
        reason = str(info.get("reason") or "").strip()
        if reason:
            parts.append(f"Reason: {reason[:256]}")
        if not info.get("permanent"):
            try:
                exp = int(info.get("expires_at") or 0)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                exp = 0
            if exp:
                parts.append(f"Ban expires <t:{exp // 1000}:R>.")
        else:
            parts.append("This ban is permanent.")
        return " ".join(parts)

    def is_blacklisted(
        self, guild_id: str, user_id: str, now: int | None = None
    ) -> bool:
        if not guild_id or not user_id:
            return False
        return self.ban_info(guild_id, user_id, now) is not None

    def blacklist_add(
        self,
        guild_id: str,
        user_id: str,
        *,
        hours: int = 0,
        reason: str = "",
        banned_by: str = "",
        now: int | None = None,
    ) -> dict[str, object]:
        """Block a user for N hours (0 = permanent), purge running entries.

        hours 0..720 enforced; reason trimmed to 256 chars. Re-ban overwrites
        the previous expiry/reason (upsert). Returns the active ban_info.
        """
        try:
            hours_int = int(hours)
        except (TypeError, ValueError):
            raise ServiceError("Ban duration must be 0-720 hours (0 = permanent).") from None
        if hours_int < 0 or hours_int > self.BAN_MAX_HOURS:
            raise ServiceError(
                f"Ban duration must be 0-{self.BAN_MAX_HOURS} hours (0 = permanent)."
            )
        clean_reason = (reason or "").strip()[:256]
        clean_by = (banned_by or "").strip()[:64]
        ts = self._ban_now(now)
        expires_at = None if hours_int == 0 else ts + hours_int * 3600 * 1000
        self.db.execute(
            "INSERT INTO simple_blacklist"
            " (guild_id, user_id, expires_at, reason, banned_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET"
            " expires_at = excluded.expires_at, reason = excluded.reason,"
            " banned_by = excluded.banned_by, created_at = excluded.created_at",
            (guild_id, user_id, expires_at, clean_reason, clean_by, ts),
        )
        self.db.execute(
            "DELETE FROM simple_entries WHERE user_id = ? AND giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE guild_id = ? AND status = 'active')",
            (user_id, guild_id),
        )
        for row in self.list_active(guild_id):
            self._sync_counter(row.id)
        info = self.ban_info(guild_id, user_id, ts)
        return info or {"permanent": hours_int == 0, "reason": clean_reason}

    def blacklist_remove(self, guild_id: str, user_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM simple_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        try:
            return (cur.rowcount or 0) > 0
        except Exception:
            return True

    def purge_expired_bans(self, now: int | None = None) -> int:
        """Delete timed bans already past expiry. Returns rows removed."""
        ts = self._ban_now(now)
        cur = self.db.execute(
            "DELETE FROM simple_blacklist WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (ts,),
        )
        try:
            return int(cur.rowcount or 0)
        except (TypeError, ValueError):
            return 0

    def count_blacklisted(self, guild_id: str, now: int | None = None) -> int:
        """How many members of this guild are blocked from giveaways.

        Counts active bans only (permanent + unexpired timed). The listing is
        capped, so this is what keeps a caller from reporting the size of the
        page set as if it were the whole blacklist.
        """
        ts = self._ban_now(now)
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_blacklist WHERE guild_id = ?"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (guild_id, ts),
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    def blacklist_list(self, guild_id: str, limit: int = 100, now: int | None = None) -> list[str]:
        ts = self._ban_now(now)
        rows = self.db.query(
            "SELECT user_id FROM simple_blacklist WHERE guild_id = ?"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY user_id ASC LIMIT ?",
            (guild_id, ts, limit),
        )
        return [str(r["user_id"]) for r in rows]

    def blacklist_list_detailed(
        self, guild_id: str, limit: int = 100, now: int | None = None
    ) -> list[dict[str, object]]:
        """Active bans with expiry + reason for the staff list."""
        ts = self._ban_now(now)
        rows = self.db.query(
            "SELECT user_id, expires_at, reason, banned_by, created_at"
            " FROM simple_blacklist WHERE guild_id = ?"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY user_id ASC LIMIT ?",
            (guild_id, ts, max(1, min(int(limit), 500))),
        )
        out: list[dict[str, object]] = []
        for r in rows:
            try:
                raw = r.get("expires_at")
                exp = None if raw is None else int(raw)
            except (TypeError, ValueError):
                exp = None
            out.append({
                "user_id": str(r.get("user_id")),
                "expires_at": exp,
                "permanent": exp is None,
                "reason": str(r.get("reason") or ""),
                "banned_by": str(r.get("banned_by") or ""),
            })
        return out

    # -- timed-out penalty (Discord's native /mute) ----------------------
    #: Giveaways a member sits out after being caught timed out while joining.
    #: Small on purpose: this is a cooldown, not a blacklist.
    TIMEOUT_PENALTY = 3

    def timeout_ban_remaining(self, guild_id: str, user_id: str) -> int:
        """Giveaways this member still has to sit out. 0 means they may enter."""
        if not guild_id or not user_id:
            return 0
        row = self.db.query_one(
            "SELECT giveaways_remaining FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is None:
            return 0
        try:
            return max(0, int(row["giveaways_remaining"]))
        except (TypeError, ValueError):
            return 0

    def apply_timeout_penalty(
        self,
        guild_id: str,
        user_id: str,
        *,
        giveaways: int = TIMEOUT_PENALTY,
        giveaway_id: str | None = None,
    ) -> int:
        """Start the penalty for a member caught timed out. Returns what is left.

        MAX() rather than a plain overwrite is the "never stack" rule made
        durable: somebody already sitting out three giveaways stays at three no
        matter how often they are caught timed out in the meantime. The
        giveaway they were caught in is remembered as the one already counted,
        so hammering the same Join button cannot burn the penalty down.
        """
        count = max(1, int(giveaways))
        ts = now_ms()
        # Hits from giveaways that are over can never refuse anyone again, so
        # each new penalty starts by dropping this member's stale ones.
        self.db.execute(
            "DELETE FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ?"
            " AND giveaway_id NOT IN (SELECT id FROM simple_giveaways WHERE status = 'active')",
            (guild_id, user_id),
        )
        if giveaway_id:
            # The trigger giveaway is recorded but never spends a unit, which is
            # what keeps hammering its Join button from burning the penalty.
            self._record_ban_hit(guild_id, user_id, giveaway_id)
        self.db.execute(
            "INSERT INTO simple_giveaway_bans"
            " (guild_id, user_id, giveaways_remaining, last_blocked_giveaway_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET"
            " giveaways_remaining = MAX(giveaways_remaining, excluded.giveaways_remaining),"
            " last_blocked_giveaway_id = excluded.last_blocked_giveaway_id,"
            " updated_at = excluded.updated_at",
            (guild_id, user_id, count, giveaway_id, ts, ts),
        )
        return self.timeout_ban_remaining(guild_id, user_id)

    def _record_ban_hit(self, guild_id: str, user_id: str, giveaway_id: str) -> bool:
        """Remember a giveaway this member was blocked from. True when new.

        The primary key makes it idempotent, and rowcount says whether this
        call was the one that inserted it: that is the "count once" test, safe
        against two clicks racing each other.
        """
        cur = self.db.execute(
            "INSERT OR IGNORE INTO simple_giveaway_ban_hits (guild_id, user_id, giveaway_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (guild_id, user_id, giveaway_id, now_ms()),
        )
        return _changed(cur)

    def _was_blocked_from(self, guild_id: str, user_id: str, giveaway_id: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 AS one FROM simple_giveaway_ban_hits"
                " WHERE guild_id = ? AND user_id = ? AND giveaway_id = ?",
                (guild_id, user_id, giveaway_id),
            )
            is not None
        )

    def consume_timeout_ban(self, guild_id: str, user_id: str, giveaway_id: str) -> int:
        """Spend one giveaway of the penalty. Returns what is left (0 = lifted).

        Every giveaway already held against this member is remembered (not just
        the last one), so only a giveaway never seen before spends a unit:
        alternating A, B, A cannot burn the penalty down. The penalty row is
        deleted as soon as the counter reaches zero; the remembered giveaways
        stay, so the ones they sat out keep refusing them while still running.
        """
        if self._record_ban_hit(guild_id, user_id, giveaway_id):
            # last_blocked_giveaway_id is still honoured for rows written before
            # the hits table existed, whose trigger giveaway has no hit row.
            self.db.execute(
                "UPDATE simple_giveaway_bans SET giveaways_remaining = giveaways_remaining - 1,"
                " last_blocked_giveaway_id = ?, updated_at = ?"
                " WHERE guild_id = ? AND user_id = ? AND giveaways_remaining > 0"
                " AND (last_blocked_giveaway_id IS NULL OR last_blocked_giveaway_id != ?)",
                (giveaway_id, now_ms(), guild_id, user_id, giveaway_id),
            )
        left = self.timeout_ban_remaining(guild_id, user_id)
        if left > 0:
            return left
        self.db.execute(
            "DELETE FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND user_id = ? AND giveaways_remaining <= 0",
            (guild_id, user_id),
        )
        return 0

    def count_timeout_bans(self, guild_id: str) -> int:
        """How many members of this guild are sitting out a penalty right now.

        The listing is capped, so the count is what lets a caller say how many
        it did not show instead of quietly reporting a short total.
        """
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND giveaways_remaining > 0",
            (guild_id,),
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    def list_timeout_bans(self, guild_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Members of this guild still sitting out a penalty, longest first.

        Only this table and only this guild: the dashboard, the entry list and
        the winner draw never read it. A zero row cannot normally exist (it is
        deleted on the way to zero) but is filtered out anyway, so a row written
        by hand cannot show a member who is actually free to enter. The limit is
        a ceiling on one response, so pair it with count_timeout_bans() when the
        caller has to say whether anything was left out.
        """
        rows = self.db.query(
            "SELECT user_id, giveaways_remaining, created_at FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND giveaways_remaining > 0"
            " ORDER BY giveaways_remaining DESC, user_id ASC LIMIT ?",
            (guild_id, max(1, min(int(limit), 500))),
        )
        banned: list[dict[str, Any]] = []
        for row in rows:
            try:
                remaining = max(0, int(row["giveaways_remaining"]))
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                continue
            banned.append(
                {
                    "user_id": str(row["user_id"]),
                    "giveaways_remaining": remaining,
                    "created_at": int(row.get("created_at") or 0),
                }
            )
        return banned

    def check_timeout_ban(
        self, gw: Giveaway, *, user_id: str, timed_out: bool = False,
        _snap_ban: int | None = None, _snap_hit: bool | None = None,
    ) -> None:
        """Refuse a timed-out member — or one still serving that penalty.

        Called by check_eligible() only once the giveaway is known to still be
        joinable, so a stale button never hands out a penalty.

        A member who is already sitting out the penalty spends one giveaway
        here: the giveaway they are blocked from *is* one of the ones they have
        to sit out. The timed-out test is skipped while a penalty is running,
        which is what stops a second penalty stacking on the first. Either way
        an entry they already had is dropped rather than left to win.
        """
        if not user_id:
            return
        # Snapshot staleness: the tuple was read in parallel with get(), before
        # this thread started. A penalty started/expired in between would make
        # a snapshot-based DELETE/consume wrong (false penalty or missed
        # spend). Happy path (snapshot says clean and member not timed out)
        # returns fast; the INSERT guard in join() still blocks a penalty that
        # landed after the snapshot (zero-insert path re-runs this gate live).
        # Anything the snapshot would refuse re-reads live before acting, so a
        # stale ban/hit can never cause a false DELETE/consume.
        if _snap_ban is not None or _snap_hit is not None:
            snap_blocked = (_snap_ban or 0) > 0 or bool(_snap_hit) or timed_out
            if not snap_blocked:
                return
            _snap_ban = None
            _snap_hit = None
        # Stale-giveaway precedence: gw was read before this thread started,
        # so end() may have flipped status since. Never hand out, spend, or
        # keep a penalty from a stale button on an ended giveaway -- the
        # caller must hear "ended", with no side effects. Happy-path joins
        # already returned above, so only refusal paths pay this trip.
        try:
            _live = self.get(gw.id)
        except ServiceError:
            raise ServiceError("This giveaway has ended.") from None
        if not _live.active or _live.ends_at <= now_ms():
            raise ServiceError("This giveaway has ended.")
        left = _snap_ban if _snap_ban is not None else self.timeout_ban_remaining(gw.guild_id, user_id)
        if left <= 0 and not timed_out:
            # The penalty is over, but a giveaway it was served on is still one
            # they sat out: rejoining it (say, the one that spent the last unit)
            # would hand the penalty straight back.
            if _snap_hit is not None:
                was_blocked = _snap_hit
            else:
                was_blocked = self._was_blocked_from(gw.guild_id, user_id, gw.id)
            if was_blocked:
                raise ServiceError(
                    "You sat out this giveaway as part of your timed-out penalty.",
                    kind=TIMEOUT_BAN_KIND,
                )
            return
        # Blocked, so they must not be in a giveaway at all: entries made before
        # Discord timed them out (or before the penalty started) are dropped from
        # every running giveaway in the server, exactly as blacklist_add does.
        # Dropping only the one they clicked would let them win the giveaway
        # they joined yesterday while sitting out this one.
        self.db.execute(
            "DELETE FROM simple_entries WHERE user_id = ? AND giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE guild_id = ? AND status = 'active')",
            (user_id, gw.guild_id),
        )
        for row in self.list_active(gw.guild_id):
            self._sync_counter(row.id)
        if left > 0:
            left = self.consume_timeout_ban(gw.guild_id, user_id, gw.id)
            if left > 0:
                raise ServiceError(
                    "You are sitting out a penalty for joining while timed out."
                    f" {left} giveaway(s) left.",
                    kind=TIMEOUT_BAN_KIND,
                )
            raise ServiceError(
                "That was the last giveaway of your timed-out penalty —"
                " you can enter the next one.",
                kind=TIMEOUT_BAN_KIND,
            )
        left = self.apply_timeout_penalty(gw.guild_id, user_id, giveaway_id=gw.id)
        raise ServiceError(
            "You are currently timed out in this server, so you cannot enter giveaways."
            f" Timeout penalty: sit out the next {left} giveaway(s).",
            kind=TIMEOUT_BAN_KIND,
        )

    def due(self, now: int | None = None) -> list[Giveaway]:
        ts = now if now is not None else now_ms()
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active' AND ends_at <= ?"
            " ORDER BY ends_at ASC LIMIT 25",
            (ts,),
        )
        return [Giveaway.from_row(r) for r in rows]
