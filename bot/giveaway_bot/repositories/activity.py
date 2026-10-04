"""Message activity repositories.

Counting strategy (the efficiency requirement in practice):

* **One row per (guild, user).** Counts are bumped with a single UPSERT, so a
  message costs one indexed write no matter how many users are tracked. There is
  no per-message row anywhere - a message table would grow without bound.
* **Per-channel high-water marks** (``message_channel_state``) detect gateway
  gaps. Discord reconnects can drop events; a high-water mark lets the bot
  notice and backfill, which is what makes counts *reliable across restarts*
  rather than silently drifting downward.
* **Idempotent ingestion.** Backfill passes carry message ids and skip anything
  at or below the stored high-water mark, so replaying a range cannot
  double-count.
"""

from __future__ import annotations

from typing import Any

from ..db import Database, now_ms

#: Confidence levels recorded alongside every count.
EXACT = "exact"
BACKFILLED = "backfilled"
ESTIMATED = "estimated"


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #
def record_message(
    db: Database,
    *,
    guild_id: str,
    user_id: str,
    channel_id: str,
    message_id: str,
    message_at: int,
    distinct_channel: bool = False,
    ignore_bots: bool = True,
    track_channels: list[str] | None = None,
) -> None:
    """Count one message from a human.

    ``ignore_bots`` is enforced here as well as at the gateway so that a bug
    upstream can never inflate someone's count. ``track_channels`` optionally
    maintains the per-channel breakdown used by channel-scoped requirements;
    empty/None keeps only the guild-wide counter, which is the cheap default.
    """
    if not user_id.isdigit() or not message_id.isdigit():
        return

    timestamp = now_ms()
    db.execute(
        """
        INSERT INTO message_counters (
            guild_id, user_id, message_count, distinct_channels,
            first_message_at, last_message_at, window_started_at, exactness, updated_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
          message_count = message_counters.message_count + 1,
          distinct_channels = message_counters.distinct_channels + ?,
          first_message_at = MIN(COALESCE(message_counters.first_message_at, ?), ?),
          last_message_at = MAX(COALESCE(message_counters.last_message_at, 0), ?),
          exactness = ?,
          updated_at = ?
        """,
        (
            guild_id,
            user_id,
            int(distinct_channel),  # distinct_channels (insert)
            message_at,              # first_message_at (insert)
            message_at,              # last_message_at (insert)
            message_at,              # window_started_at
            EXACT,                   # exactness (insert)
            timestamp,               # updated_at (insert)
            int(distinct_channel),   # distinct_channels (update)
            message_at,              # first_message_at MIN arg 1
            message_at,              # first_message_at MIN arg 2
            message_at,              # last_message_at MAX arg
            EXACT,                   # exactness (update)
            timestamp,               # updated_at (update)
        ),
    )

    if track_channels and channel_id in track_channels:
        db.execute(
            """
            INSERT INTO message_counter_channels (
                guild_id, user_id, channel_id, message_count, last_message_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(guild_id, user_id, channel_id) DO UPDATE SET
              message_count = message_counter_channels.message_count + 1,
              last_message_at = MAX(COALESCE(message_counter_channels.last_message_at, 0), ?),
              updated_at = ?
            """,
            (guild_id, user_id, channel_id, message_at, timestamp, message_at, timestamp),
        )

    advance_channel(
        db, guild_id=guild_id, channel_id=channel_id, message_id=message_id, message_at=message_at
    )


def get_count(db: Database, guild_id: str, user_id: str) -> int:
    value = db.scalar(
        "SELECT message_count FROM message_counters WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return int(value or 0)


def get_counts(db: Database, guild_id: str, user_ids: list[str]) -> dict[str, int]:
    """Batch read - one query for many users (autocomplete, dashboards)."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    rows = db.query(
        f"SELECT user_id, message_count FROM message_counters"
        f" WHERE guild_id = ? AND user_id IN ({placeholders})",
        (guild_id, *user_ids),
    )
    return {str(row["user_id"]): int(row["message_count"]) for row in rows}


def get_activity(db: Database, guild_id: str, user_id: str) -> dict[str, Any] | None:
    return db.query_one(
        """
        SELECT user_id, message_count, distinct_channels, first_message_at,
               last_message_at, window_started_at, exactness, updated_at
        FROM message_counters WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    )



def top_counters(db: Database, guild_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Leaderboard. Never rendered publicly by default - it is admin data."""
    return db.query(
        """
        SELECT user_id, message_count, distinct_channels, exactness, updated_at
        FROM message_counters
        WHERE guild_id = ? AND message_count > 0
        ORDER BY message_count DESC
        LIMIT ?
        """,
        (guild_id, max(1, min(limit, 200))),
    )


def reset_window(db: Database, guild_id: str, *, at: int | None = None) -> int:
    """Start a fresh counting window (all counts return to zero)."""
    timestamp = at if at is not None else now_ms()
    cursor = db.execute(
        """
        UPDATE message_counters
        SET message_count = 0, distinct_channels = 0, first_message_at = NULL,
            last_message_at = NULL, window_started_at = ?, exactness = ?, updated_at = ?
        WHERE guild_id = ?
        """,
        (timestamp, EXACT, timestamp, guild_id),
    )
    changed = int(getattr(cursor, "rowcount", 0) or 0)
    cursor.close()
    return changed


def prune_idle(db: Database, *, older_than_ms: int) -> int:
    """Drop counters untouched for a long time to bound table growth."""
    cursor = db.execute(
        "DELETE FROM message_counters WHERE last_message_at IS NOT NULL AND last_message_at < ?",
        (older_than_ms,),
    )
    removed = int(getattr(cursor, "rowcount", 0) or 0)
    cursor.close()
    return removed


def guild_coverage(db: Database, guild_id: str) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT COUNT(*) AS tracked_users,
               COALESCE(SUM(message_count), 0) AS total_messages,
               COALESCE(SUM(CASE WHEN exactness = ? THEN 1 ELSE 0 END), 0) AS exact_users,
               COALESCE(SUM(CASE WHEN exactness = ? THEN 1 ELSE 0 END), 0) AS backfilled_users,
               COALESCE(SUM(CASE WHEN exactness = ? THEN 1 ELSE 0 END), 0) AS estimated_users
        FROM message_counters WHERE guild_id = ?
        """,
        (EXACT, BACKFILLED, ESTIMATED, guild_id),
    ) or {}
    channels = db.query_one(
        "SELECT COUNT(*) AS channels, COALESCE(SUM(message_count), 0) AS messages"
        " FROM message_channel_state WHERE guild_id = ?",
        (guild_id,),
    ) or {}
    return {
        **{key: int(value or 0) for key, value in row.items()},
        "tracked_channels": int(channels.get("channels") or 0),
        "channel_messages": int(channels.get("messages") or 0),
    }


# --------------------------------------------------------------------------- #
# Channel high-water marks (gap detection + idempotent backfill)
# --------------------------------------------------------------------------- #
def advance_channel(
    db: Database, *, guild_id: str, channel_id: str, message_id: str, message_at: int
) -> None:
    """Move the channel high-water mark forward (never backwards)."""
    db.execute(
        """
        INSERT INTO message_channel_state (
            guild_id, channel_id, last_message_id, last_message_at, message_count, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET
          last_message_id = excluded.last_message_id,
          last_message_at = excluded.last_message_at,
          message_count = message_channel_state.message_count + 1,
          updated_at = excluded.updated_at
        WHERE excluded.last_message_id > message_channel_state.last_message_id
        """,
        (guild_id, channel_id, str(message_id), message_at, now_ms()),
    )


def counts_since_bulk(
    db: Database,
    *,
    guild_id: str,
    user_ids: list[str],
    since: int | None = None,
) -> dict[str, int]:
    """Guild-wide counts for many users (one query for the whole batch).

    ``since`` is honoured conservatively: a counter whose window began *after*
    the giveaway's ``since`` has no history we can attribute to that window, so it
    contributes 0 rather than an inflated number.
    """
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    rows = db.query(
        f"""
        SELECT user_id, message_count, window_started_at
        FROM message_counters
        WHERE guild_id = ? AND user_id IN ({placeholders})
        """,
        (guild_id, *user_ids),
    )
    counts: dict[str, int] = {}
    for row in rows:
        count = int(row["message_count"] or 0)
        window_start = int(row["window_started_at"] or 0)
        if since and window_start > since:
            count = 0
        counts[str(row["user_id"])] = count
    return counts


def count_since(db: Database, *, guild_id: str, user_id: str, since: int | None = None) -> int:
    """Guild-wide count for one user."""
    return counts_since_bulk(
        db, guild_id=guild_id, user_ids=[user_id], since=since
    ).get(str(user_id), 0)


def counts_in_channels_bulk(
    db: Database,
    *,
    guild_id: str,
    user_ids: list[str],
    channel_ids: list[str],
    since: int | None = None,
) -> dict[str, int]:
    """Counts summed over specific channels, for many users at once.

    Reads ``message_counter_channels``, which is only populated for channels a
    running giveaway is watching. Users with no rows in scope report 0 - never
    the guild-wide total, which would let activity elsewhere qualify them.
    """
    if not user_ids:
        return {}
    if not channel_ids:
        return dict.fromkeys(user_ids, 0)

    user_placeholders = ",".join("?" for _ in user_ids)
    channel_placeholders = ",".join("?" for _ in channel_ids)
    params: list[Any] = [guild_id, *user_ids, *channel_ids]
    time_clause = ""
    if since:
        time_clause = " AND (last_message_at IS NULL OR last_message_at >= ?)"
        params.append(since)

    rows = db.query(
        f"""
        SELECT user_id, COALESCE(SUM(message_count), 0) AS total
        FROM message_counter_channels
        WHERE guild_id = ? AND user_id IN ({user_placeholders})
          AND channel_id IN ({channel_placeholders}){time_clause}
        GROUP BY user_id
        """,
        params,
    )
    found = {str(row["user_id"]): int(row["total"] or 0) for row in rows}
    return {user_id: found.get(user_id, 0) for user_id in user_ids}


def counts_in_channels(
    db: Database,
    *,
    guild_id: str,
    user_id: str,
    channel_ids: list[str],
    since: int | None = None,
) -> int:
    """Channel-restricted count for one user."""
    return counts_in_channels_bulk(
        db,
        guild_id=guild_id,
        user_ids=[user_id],
        channel_ids=channel_ids,
        since=since,
    ).get(str(user_id), 0)


def record_channel_count(
    db: Database,
    *,
    guild_id: str,
    user_id: str,
    channel_id: str,
    message_at: int,
    tracked_channels: set[str] | frozenset[str] | list[str] | None = None,
) -> None:
    """Increment the per-(user, channel) breakdown for a watched channel.

    Used when backfilling history, where the guild-wide counter is already
    handled by :func:`backfill_messages` and only the channel row is missing.
    """
    if tracked_channels is not None and channel_id not in tracked_channels:
        return
    if not user_id.isdigit():
        return
    db.execute(
        """
        INSERT INTO message_counter_channels (
            guild_id, user_id, channel_id, message_count, last_message_at, updated_at
        ) VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(guild_id, user_id, channel_id) DO UPDATE SET
          message_count = message_counter_channels.message_count + 1,
          last_message_at = MAX(COALESCE(message_counter_channels.last_message_at, 0), ?),
          updated_at = ?
        """,
        (guild_id, user_id, channel_id, message_at, now_ms(), message_at, now_ms()),
    )


def prune_channel_counts(db: Database, *, older_than_ms: int) -> int:
    """Drop per-channel breakdown rows with no recent activity."""
    cursor = db.execute(
        "DELETE FROM message_counter_channels"
        " WHERE last_message_at IS NOT NULL AND last_message_at < ?",
        (older_than_ms,),
    )
    removed = int(getattr(cursor, "rowcount", 0) or 0)
    cursor.close()
    return removed


def watched_channels(db: Database, guild_id: str) -> list[str]:
    """Channels whose per-user breakdown is being maintained."""
    return [
        str(row["channel_id"])
        for row in db.query(
            "SELECT DISTINCT channel_id FROM message_counter_channels WHERE guild_id = ?",
            (guild_id,),
        )
    ]


def channel_state(db: Database, guild_id: str, channel_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM message_channel_state WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )


def channels_needing_backfill(db: Database, guild_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Channels whose high-water mark lags the newest known message.

    Called after a gateway resume, when events may have been missed.
    """
    return db.query(
        """
        SELECT * FROM message_channel_state
        WHERE guild_id = ?
        ORDER BY last_message_at DESC
        LIMIT ?
        """,
        (guild_id, max(1, min(limit, 100))),
    )


def backfill_messages(
    db: Database,
    *,
    guild_id: str,
    channel_id: str,
    messages: list[dict[str, Any]],
    ignore_bots: bool = True,
    window_started_at: int | None = None,
) -> dict[str, int]:
    """Apply a fetched page of messages idempotently.

    Anything at or below the stored high-water mark is skipped, so re-running a
    backfill range (after a crash, or an overlapping fetch) can never inflate a
    count. Returns a small stats dict for logging/auditing.
    """
    state = channel_state(db, guild_id, channel_id)
    high_water = str(state["last_message_id"]) if state else "0"
    high_water_at = int(state["last_message_at"] or 0) if state else 0

    added = 0
    skipped = 0
    bots_skipped = 0
    window_skipped = 0
    highest = high_water
    highest_at = high_water_at
    timestamp = now_ms()
    seen_authors: set[str] = set()

    for message in messages:
        message_id = str(message.get("id") or "")
        if not message_id.isdigit():
            continue
        # Idempotency: already counted.
        if int(message_id) <= int(high_water):
            skipped += 1
            continue
        author = message.get("author") or {}
        author_id = str(author.get("id") or "")
        message_at = int(message.get("timestamp_ms") or 0)

        if message_id > highest:
            highest, highest_at = message_id, message_at

        if ignore_bots and bool(author.get("bot")):
            bots_skipped += 1
            continue
        if not author_id.isdigit():
            bots_skipped += 1
            continue
        # Only count messages inside the active counting window.
        if window_started_at and message_at and message_at < window_started_at:
            window_skipped += 1
            continue

        # A user counts a new distinct channel only the first time we see them
        # post in it during this backfill window.
        already_counted_here = author_id in seen_authors
        db.execute(
            """
            INSERT INTO message_counters (
                guild_id, user_id, message_count, distinct_channels,
                first_message_at, last_message_at, window_started_at, exactness, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              message_count = message_counters.message_count + 1,
              distinct_channels = message_counters.distinct_channels + ?,
              first_message_at = MIN(COALESCE(message_counters.first_message_at, ?), ?),
              last_message_at = MAX(COALESCE(message_counters.last_message_at, 0), ?),
              exactness = ?,
              updated_at = ?
            """,
            (
                guild_id,
                author_id,
                int(not already_counted_here),   # distinct_channels (insert)
                message_at,                       # first_message_at
                message_at,                       # last_message_at
                window_started_at or message_at,  # window_started_at
                BACKFILLED,                      # exactness
                timestamp,                        # updated_at
                int(not already_counted_here),   # distinct_channels (update)
                message_at,                       # first_message_at (update)
                message_at,                       # first_message_at (update)
                message_at,                       # last_message_at (update)
                BACKFILLED,                      # exactness (update)
                timestamp,                        # updated_at (update)
            ),
        )
        seen_authors.add(author_id)
        added += 1

    if highest != high_water:
        db.execute(
            """
            INSERT INTO message_channel_state (
                guild_id, channel_id, last_message_id, last_message_at, message_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
              last_message_id = excluded.last_message_id,
              last_message_at = excluded.last_message_at,
              message_count = message_channel_state.message_count + excluded.message_count,
              updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, highest, highest_at, added, now_ms()),
        )

    return {
        "added": added,
        "skipped": skipped,
        "bots_skipped": bots_skipped,
        "window_skipped": window_skipped,
    }
