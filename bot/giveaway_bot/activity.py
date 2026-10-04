"""Message-activity tracking at runtime.

Why this class exists instead of writing to the database in ``on_message``:

* **One write per message is too expensive.** A busy channel produces hundreds
  of events per second; a single UPSERT per event would hammer SQLite/libSQL.
  Instead counts are accumulated in memory and flushed in batches.
* **Only count when it matters.** If no running giveaway in the guild has the
  requirement enabled, the message is discarded after a dict lookup.
* **Restarts must not lose or double-count.** A per-channel high-water mark is
  persisted on flush, so a crash replays at most the un-flushed tail, and the
  replay is idempotent (see ``activity.backfill_messages``).

The tracker is deliberately transport-agnostic: it accepts plain
``(user_id, channel_id, message_id, timestamp, is_bot)`` tuples so it can be
unit tested without a Discord connection.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .db import Database, now_ms
from .models import GiveawayStatus
from .repositories import activity as activity_repo
from .repositories import giveaways as gw_repo

from .repositories import activity as activity_repo
from .repositories import giveaways as gw_repo
from .repositories import guilds as guilds_repo

log = logging.getLogger("giveaway_bot.activity")

#: Flush at least this often, even when quiet, so a crash loses very little.
MAX_PENDING_MS = 5_000
#: Flush once this many events are buffered.
MAX_PENDING_EVENTS = 500


@dataclass(slots=True)
class PendingEvent:
    user_id: str
    channel_id: str
    message_id: str
    message_at: int


@dataclass(slots=True)
class _GuildState:
    """Per-guild buffer plus the channel set we must maintain breakdowns for."""

    buffer: list[PendingEvent] = field(default_factory=list)
    last_flush_ms: int = field(default_factory=now_ms)
    #: Channels needing per-user rows (union of all active requirements).
    tracked_channels: frozenset[str] = frozenset()
    #: True when at least one running giveaway has min_messages > 0.
    any_requirement: bool = False


class MessageActivityTracker:
    """Buffers message events and flushes them to the database in batches."""

    def __init__(self, db: Database, service: Any) -> None:
        self.db = db
        self.service = service
        self._states: dict[str, _GuildState] = {}
        self._lock = threading.Lock()
        # Flushes run here so gateway threads never block on the database.
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="activity-flush"
        )
        self.stats = {"seen": 0, "counted": 0, "skipped_no_requirement": 0,
                      "skipped_bot": 0, "skipped_invalid": 0, "flushed": 0, "batches": 0}

    # ------------------------------------------------------------------ setup
    def refresh_requirements(self) -> None:
        """Recompute which guilds/channels need counting.

        Called on startup and whenever giveaways are created/edited, so a guild
        only pays the storage cost once a giveaway actually asks for it.
        """
        with self._lock:
            states = self._states

        from .repositories import guilds as guilds_repo

        guild_ids: set[str] = {guild.id for guild in guilds_repo.list_guilds(self.db)}
        for giveaway in gw_repo.list_public(self.db, limit=500):
            guild_ids.add(giveaway.guild_id)

        for guild_id in guild_ids:
            required_channels: set[str] = set()
            any_requirement = False
            for giveaway in gw_repo.list_for_guild(
                self.db, guild_id, statuses=[GiveawayStatus.RUNNING], limit=200
            ):
                if giveaway.min_messages <= 0:
                    continue
                any_requirement = True
                if giveaway.message_count_scope == "channel":
                    required_channels.update(giveaway.message_count_channel_ids)

            with self._lock:
                state = states.setdefault(guild_id, _GuildState())
                state.any_requirement = any_requirement
                state.tracked_channels = frozenset(required_channels)

    # ---------------------------------------------------------------- capture
    def record(self, message: Any) -> None:
        """Handle a ``discord.Message`` (or a plain object with the same shape)."""
        guild = getattr(message, "guild", None)
        if guild is None:
            return
        author = getattr(message, "author", None)
        if author is None:
            return
        channel = getattr(message, "channel", None)
        if channel is None:
            return

        self.record_raw(
            guild_id=str(guild.id),
            user_id=str(author.id),
            channel_id=str(channel.id),
            message_id=str(getattr(message, "id", "") or ""),
            message_at=int(
                getattr(getattr(message, "created_at", None), "timestamp", 0) * 1000
            ) or now_ms(),
            is_bot=bool(getattr(author, "bot", False))
            or bool(getattr(message, "webhook_id", None)),
            content_length=len(getattr(message, "content", "") or ""),
        )

    def record_raw(
        self,
        *,
        guild_id: str,
        user_id: str,
        channel_id: str,
        message_id: str,
        message_at: int,
        is_bot: bool = False,
        content_length: int = 0,
    ) -> None:
        """Transport-agnostic entry point (also used by tests)."""
        self.stats["seen"] += 1

        if not user_id.isdigit() or not message_id.isdigit():
            self.stats["skipped_invalid"] += 1
            return
        # Ignore bots/webhooks and empty messages: counting them would let a
        # giveaway be farmed by an automated account.
        if is_bot or content_length == 0:
            self.stats["skipped_bot"] += 1
            return

        with self._lock:
            state = self._states.get(guild_id)
            if state is None or not state.any_requirement:
                self.stats["skipped_no_requirement"] += 1
                return
            state.buffer.append(PendingEvent(user_id, channel_id, message_id, message_at))
            due = (
                len(state.buffer) >= MAX_PENDING_EVENTS
                or now_ms() - state.last_flush_ms >= MAX_PENDING_MS
            )
            events = state.buffer if due else []

        if due:
            # Flush off the gateway thread: a DB write must never delay a
            # message event and risk Discord dropping the connection.
            executor = getattr(self, "_executor", None)
            if executor is not None:
                executor.submit(self.flush_guild, guild_id)
            else:  # pragma: no cover - executor attached in __post_init__
                self.flush_guild(guild_id)

    def flush_guild_async(self, guild_id: str) -> None:
        """Schedule a flush on the tracker executor."""
        self._executor.submit(self.flush_guild, guild_id)  # type: ignore[union-attr]

    # ----------------------------------------------------------------- flush
    def flush_guild(self, guild_id: str) -> int:
        """Persist one guild's buffer. Safe to call from a worker thread."""
        with self._lock:
            state = self._states.get(guild_id)
            if state is None or not state.buffer:
                return 0
            events, state.buffer = state.buffer, []
            state.last_flush_ms = now_ms()
            tracked = state.tracked_channels

        written = 0
        try:
            # One transaction for the whole batch: an all-or-nothing flush means a
            # crash cannot leave half the batch applied, and the replayed tail is
            # idempotent because counting is keyed on the message high-water mark.
            with self.db.transaction() as tx:
                # Only the first message per (user, channel) in this batch counts
                # as a newly-active channel.
                seen_pairs: set[tuple[str, str]] = set()
                for event in events:
                    pair = (event.user_id, event.channel_id)
                    first_in_batch = pair not in seen_pairs
                    seen_pairs.add(pair)
                    activity_repo.record_message(
                        tx,
                        guild_id=guild_id,
                        user_id=event.user_id,
                        channel_id=event.channel_id,
                        message_id=event.message_id,
                        message_at=event.message_at,
                        distinct_channel=first_in_batch,
                        track_channels=list(tracked) or None,
                    )
                    written += 1
        except Exception:  # noqa: BLE001 - never lose events to a transient DB error
            log.exception("failed to flush message activity for guild %s", guild_id)
            # Re-queue so counts are not silently lost.
            with self._lock:
                state = self._states.get(guild_id)
                if state is not None:
                    state.buffer = events + state.buffer
            return 0

        self.stats["counted"] += written
        self.stats["flushed"] += written
        self.stats["batches"] += 1
        return written

    def flush_all(self) -> int:
        """Flush every guild. Called by the scheduler and on shutdown."""
        with self._lock:
            guild_ids = list(self._states)
        return sum(self.flush_guild(guild_id) for guild_id in guild_ids)

    def pending(self) -> int:
        with self._lock:
            return sum(len(state.buffer) for state in self._states.values())

    # -------------------------------------------------------------- backfill
    async def backfill_channel(self, bot: Any, guild_id: str, channel_id: str) -> dict[str, int]:
        """Repair a channel's counts after a gateway gap.

        Fetches recent history and applies it idempotently, so overlapping or
        repeated calls cannot inflate anyone's count.
        """
        channel = bot.get_channel(int(channel_id))
        if channel is None or not hasattr(channel, "history"):
            return {"added": 0, "skipped": 0, "bots_skipped": 0, "window_skipped": 0}

        state = activity_repo.channel_state(self.db, guild_id, channel_id)
        high_water = int(state["last_message_id"]) if state else 0

        messages: list[dict[str, Any]] = []
        try:
            async for message in channel.history(limit=200, oldest=high_water or None):
                messages.append(
                    {
                        "id": str(message.id),
                        "author": {"id": str(message.author.id), "bot": bool(message.author.bot)},
                        "timestamp_ms": int(message.created_at.timestamp() * 1000),
                    }
                )
        except Exception:  # noqa: BLE001 - missing permissions / rate limits
            log.warning("cannot backfill channel %s (insufficient history access)", channel_id)
            return {"added": 0, "skipped": 0, "bots_skipped": 0, "window_skipped": 0}

        tracked = set(self._states.get(guild_id, _GuildState()).tracked_channels)
        result = activity_repo.backfill_messages(
            self.db,
            guild_id=guild_id,
            channel_id=channel_id,
            messages=messages,
            ignore_bots=True,
        )
        if tracked:
            for message in messages:
                if message["author"]["bot"]:
                    continue
                activity_repo.record_channel_count(
                    self.db,
                    guild_id=guild_id,
                    user_id=str(message["author"]["id"]),
                    channel_id=channel_id,
                    message_at=int(message["timestamp_ms"]),
                    tracked_channels=tracked,
                )
        log.info(
            "backfilled channel %s: +%d counted (%d already known, %d bots)",
            channel_id,
            result["added"],
            result["skipped"],
            result["bots_skipped"],
        )
        return result