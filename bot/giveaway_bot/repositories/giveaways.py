"""Giveaway CRUD.

All mutating helpers bump ``version`` and ``updated_at`` so the dashboard's
optimistic concurrency check (If-Match style) can detect a lost update.
"""

from __future__ import annotations

from typing import Any

from ..db import Database, now_ms
from ..models import Giveaway, GiveawayStatus, dump_role_list, load_role_list

GIVEAWAY_SELECT = """
    SELECT g.*,
           COALESCE(s.participant_count, 0) AS participant_count,
           COALESCE(s.entry_count, 0)      AS entry_count,
           COALESCE(s.winner_count, 0)     AS winner_count_total
    FROM giveaways g
    LEFT JOIN giveaway_stats s ON s.giveaway_id = g.id
"""


def create_giveaway(
    db: Database,
    *,
    giveaway_id: str,
    guild_id: str,
    channel_id: str,
    title: str,
    description: str,
    prize: str,
    created_by: str,
    winner_count: int = 1,
    prize_count: int = 1,
    prize_image_url: str | None = None,
    max_entries_per_user: int = 1,
    entry_limit: int = 0,
    starts_at: int | None = None,
    ends_at: int | None = None,
    required_role_ids: list[str] | None = None,
    required_mode: str = "any",
    blacklist_role_ids: list[str] | None = None,
    allowed_channel_ids: list[str] | None = None,
    min_account_age_days: int = 0,
    min_guild_join_days: int = 0,
    entrants_require_membership: bool = True,
    min_messages: int = 0,
    message_count_channel_ids: list[str] | None = None,
    message_count_ignore_bots: bool = True,
    message_count_since: int | None = None,
    message_count_scope: str = "guild",
    seed_commitment: str | None = None,
) -> Giveaway:
    timestamp = now_ms()
    db.execute(
        """
        INSERT INTO giveaways (
            id, guild_id, channel_id, message_id, status, title, description,
            prize, prize_image_url, prize_count, winner_count,
            entry_limit, max_entries_per_user,
            starts_at, ends_at, original_ends_at,
            required_role_ids, required_mode, blacklist_role_ids, allowed_channel_ids,
            min_account_age_days, min_guild_join_days, entrants_require_membership,
            server_seed, seed_commitment, draw_round,
            min_messages, message_count_channel_ids, message_count_ignore_bots,
            message_count_since, message_count_scope,
            created_by, version, created_at, updated_at
        ) VALUES (
            ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            NULL, ?, 0, ?, ?, ?, ?, ?, ?, 1, ?, ?
        )
        """,
        (
            giveaway_id,
            guild_id,
            channel_id,
            GiveawayStatus.SCHEDULED.value,
            title,
            description,
            prize,
            prize_image_url,
            prize_count,
            winner_count,
            entry_limit,
            max_entries_per_user,
            starts_at,
            ends_at,
            ends_at,
            dump_role_list(required_role_ids or []),
            required_mode,
            dump_role_list(blacklist_role_ids or []),
            dump_role_list(allowed_channel_ids or []),
            min_account_age_days,
            min_guild_join_days,
            int(entrants_require_membership),
            seed_commitment,
            min_messages,
            dump_role_list(message_count_channel_ids or []),
            int(message_count_ignore_bots),
            message_count_since,
            message_count_scope,
            created_by,
            timestamp,
            timestamp,
        ),
    )
    db.execute(
        """
        INSERT INTO giveaway_stats (giveaway_id, participant_count, entry_count, winner_count, updated_at)
        VALUES (?, 0, 0, 0, ?)
        ON CONFLICT(giveaway_id) DO NOTHING
        """,
        (giveaway_id, timestamp),
    )
    created = get_giveaway(db, giveaway_id)
    assert created is not None  # just inserted
    return created


def get_giveaway(db: Database, giveaway_id: str) -> Giveaway | None:
    row = db.query_one(f"{GIVEAWAY_SELECT} WHERE g.id = ?", (giveaway_id,))
    return Giveaway.from_row(row) if row else None


def get_by_message(db: Database, guild_id: str, message_id: str) -> Giveaway | None:
    row = db.query_one(
        f"{GIVEAWAY_SELECT} WHERE g.guild_id = ? AND g.message_id = ?", (guild_id, message_id)
    )
    return Giveaway.from_row(row) if row else None


def list_for_guild(
    db: Database,
    guild_id: str,
    *,
    statuses: list[GiveawayStatus] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Giveaway]:
    sql = f"{GIVEAWAY_SELECT} WHERE g.guild_id = ?"
    params: list[Any] = [guild_id]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        sql += f" AND g.status IN ({placeholders})"
        params.extend(status.value for status in statuses)
    sql += " ORDER BY g.created_at DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 200)), max(0, offset)])
    return [Giveaway.from_row(row) for row in db.query(sql, params)]


def list_due(db: Database, *, now: int | None = None, limit: int = 100) -> list[Giveaway]:
    """Running giveaways whose deadline has passed (scheduler hot path)."""
    current = now if now is not None else now_ms()
    rows = db.query(
        f"{GIVEAWAY_SELECT} WHERE g.status = ? AND g.ends_at IS NOT NULL AND g.ends_at <= ?"
        " ORDER BY g.ends_at ASC LIMIT ?",
        (GiveawayStatus.RUNNING.value, current, limit),
    )
    return [Giveaway.from_row(row) for row in rows]


def list_live(db: Database, *, limit: int = 200) -> list[Giveaway]:
    """Everything with a live Discord message - used by the embed refresher."""
    rows = db.query(
        f"{GIVEAWAY_SELECT} WHERE g.status IN (?, ?) AND g.message_id IS NOT NULL"
        " ORDER BY g.ends_at ASC LIMIT ?",
        (GiveawayStatus.RUNNING.value, GiveawayStatus.PAUSED.value, limit),
    )
    return [Giveaway.from_row(row) for row in rows]


def list_public(db: Database, *, status: str | None = None, limit: int = 50) -> list[Giveaway]:
    """Public listing for the dashboard's unauthenticated browse page."""
    sql = f"{GIVEAWAY_SELECT} WHERE 1 = 1"
    params: list[Any] = []
    if status:
        sql += " AND g.status = ?"
        params.append(status)
    sql += " ORDER BY g.created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    return [Giveaway.from_row(row) for row in db.query(sql, params)]


def list_public_for_guild(db: Database, guild_id: str, *, limit: int = 50) -> list[Giveaway]:
    rows = db.query(
        f"{GIVEAWAY_SELECT} WHERE g.guild_id = ? AND g.status != ? ORDER BY g.created_at DESC LIMIT ?",
        (guild_id, GiveawayStatus.SCHEDULED.value, max(1, min(limit, 100))),
    )
    return [Giveaway.from_row(row) for row in rows]


def set_status(
    db: Database,
    giveaway_id: str,
    status: GiveawayStatus,
    *,
    actor_id: str | None = None,
    ended_reason: str | None = None,
) -> None:
    db.execute(
        """
        UPDATE giveaways
        SET status = ?, ended_reason = ?, updated_by = ?, updated_at = ?, version = version + 1
        WHERE id = ?
        """,
        (status.value, ended_reason, actor_id, now_ms(), giveaway_id),
    )


def set_participant_role(
    db: Database, giveaway_id: str, role_id: str | None, *, actor_id: str | None = None
) -> None:
    """Attach (or clear) the temporary entrants role for a giveaway."""
    db.execute(
        "UPDATE giveaways SET participant_role_id = ?, updated_by = ?, updated_at = ? WHERE id = ?",
        (role_id, actor_id, now_ms(), giveaway_id),
    )


def find_active(db: Database, guild_id: str) -> dict[str, Any] | None:
    """The one open giveaway in a guild (scheduled, running or paused).

    The product deliberately runs a single giveaway at a time per server. That
    keeps the temporary entrants role unambiguous and means "who is in the
    giveaway?" always has exactly one answer.
    """
    return db.query_one(
        """
        SELECT g.id, g.title, g.status, g.ends_at, g.message_id, g.participant_role_id,
               COALESCE(s.participant_count, 0) AS participant_count,
               COALESCE(s.entry_count, 0)      AS entry_count
        FROM giveaways g
        LEFT JOIN giveaway_stats s ON s.giveaway_id = g.id
        WHERE g.guild_id = ? AND g.status IN ('scheduled','running','paused')
        ORDER BY g.created_at DESC
        LIMIT 1
        """,
        (guild_id,),
    )


def set_message_id(db: Database, giveaway_id: str, message_id: str) -> None:
    db.execute(
        "UPDATE giveaways SET message_id = ?, updated_at = ? WHERE id = ?",
        (message_id, now_ms(), giveaway_id),
    )


def update_fields(
    db: Database,
    giveaway_id: str,
    fields: dict[str, Any],
    *,
    actor_id: str | None = None,
    expected_version: int | None = None,
) -> bool:
    """Patch a giveaway. Returns False when the version check fails.

    ``fields`` may contain JSON-encoded role/channel lists as ``list[str]``.
    """
    if not fields:
        return False

    assignments: list[str] = []
    params: list[Any] = []
    for column, value in fields.items():
        if column in {
            "required_role_ids",
            "blacklist_role_ids",
            "allowed_channel_ids",
            "message_count_channel_ids",
        }:
            value = dump_role_list(value if isinstance(value, list) else load_role_list(value))
        elif column == "status":
            value = value.value if isinstance(value, GiveawayStatus) else str(value)
        assignments.append(f"{column} = ?")
        params.append(value)

    assignments.append("updated_by = ?")
    params.append(actor_id)
    assignments.append("updated_at = ?")
    params.append(now_ms())
    assignments.append("version = version + 1")
    params.append(giveaway_id)

    sql = f"UPDATE giveaways SET {', '.join(assignments)} WHERE id = ?"
    if expected_version is not None:
        sql += " AND version = ?"
        params.append(expected_version)

    cursor = db.execute(sql, params)
    changed = getattr(cursor, "rowcount", 0)
    cursor.close()
    return bool(changed and changed > 0)


def seal_seed(db: Database, giveaway_id: str, seed_hex: str, commitment_hex: str, starts_at: int) -> None:
    """Publish the commitment and store the sealed seed **before entries open**.

    This is the structural half of the fairness guarantee: the commitment is
    visible in the Discord message while nobody can enter yet, and the seed is
    already fixed, so no operator can swap in a friendlier value later.
    """
    db.execute(
        """
        UPDATE giveaways
        SET status = ?, server_seed = ?, seed_commitment = ?, seed_sealed_at = ?,
            starts_at = COALESCE(starts_at, ?), updated_at = ?, version = version + 1
        WHERE id = ?
        """,
        (GiveawayStatus.RUNNING.value, seed_hex, commitment_hex, now_ms(), starts_at, now_ms(),
         giveaway_id),
    )


def read_sealed_seed(db: Database, giveaway_id: str) -> tuple[str, str]:
    """Phase 2 of a draw reads the seed back from storage, never from memory.

    Returns ``(seed, commitment)``.  Raises ``LookupError`` when the giveaway was
    never sealed, which aborts the draw instead of silently minting a fresh seed.
    """
    row = db.query_one(
        "SELECT server_seed, seed_commitment FROM giveaways WHERE id = ?", (giveaway_id,)
    )
    if not row or not row.get("server_seed") or not row.get("seed_commitment"):
        raise LookupError(f"giveaway {giveaway_id} has no sealed seed")
    return str(row["server_seed"]), str(row["seed_commitment"])


def reseal_seed(db: Database, giveaway_id: str, seed_hex: str, commitment_hex: str) -> None:
    """Publish a brand-new commitment before a reroll round."""
    db.execute(
        """
        UPDATE giveaways
        SET server_seed = ?, seed_commitment = ?, seed_sealed_at = ?,
            seed_revealed_at = NULL, updated_at = ?, version = version + 1
        WHERE id = ?
        """,
        (seed_hex, commitment_hex, now_ms(), now_ms(), giveaway_id),
    )


def mark_seed_revealed(db: Database, giveaway_id: str) -> None:
    db.execute(
        "UPDATE giveaways SET seed_revealed_at = ?, updated_at = ? WHERE id = ?",
        (now_ms(), now_ms(), giveaway_id),
    )


def renew_seed_commitment(db: Database, giveaway_id: str, commitment_hex: str) -> None:
    """Attach a freshly published commitment before a draw/reroll."""
    db.execute(
        "UPDATE giveaways SET seed_commitment = ?, updated_at = ? WHERE id = ?",
        (commitment_hex, now_ms(), giveaway_id),
    )


def mark_locked(db: Database, giveaway_id: str, round_number: int) -> None:
    db.execute(
        """
        UPDATE giveaways
        SET locked_at = COALESCE(locked_at, ?), draw_round = ?, updated_at = ?
        WHERE id = ?
        """,
        (now_ms(), round_number, now_ms(), giveaway_id),
    )


def count_for_guild(db: Database, guild_id: str, status: str) -> int:
    value = db.scalar(
        "SELECT COUNT(*) FROM giveaways WHERE guild_id = ? AND status = ?",
        (guild_id, status),
    )
    return int(value or 0)


def refresh_stats(db: Database, giveaway_id: str) -> dict[str, int]:
    """Recompute the denormalised counters from the entries table."""
    row = db.query_one(
        """
        SELECT
          COUNT(DISTINCT CASE WHEN status IN ('valid','winner','lost') THEN user_id END)
              AS participant_count,
          COUNT(CASE WHEN status IN ('valid','winner','lost') THEN 1 END) AS entry_count
        FROM giveaway_entries WHERE giveaway_id = ?
        """,
        (giveaway_id,),
    )
    participant_count = int((row or {}).get("participant_count") or 0)
    entry_count = int((row or {}).get("entry_count") or 0)
    winners = int(
        db.scalar("SELECT COUNT(*) FROM giveaway_winners WHERE giveaway_id = ?", (giveaway_id,))
        or 0
    )
    db.execute(
        """
        INSERT INTO giveaway_stats (giveaway_id, participant_count, entry_count, winner_count, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(giveaway_id) DO UPDATE SET
          participant_count = excluded.participant_count,
          entry_count = excluded.entry_count,
          winner_count = excluded.winner_count,
          updated_at = excluded.updated_at
        """,
        (giveaway_id, participant_count, entry_count, winners, now_ms()),
    )
    return {
        "participant_count": participant_count,
        "entry_count": entry_count,
        "winner_count": winners,
    }