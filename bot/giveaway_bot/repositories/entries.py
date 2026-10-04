"""Entry repositories.

Fairness-critical rule enforced here: once a giveaway is ``locked_at`` (a draw
is in flight or done) rows may only be *flagged*, never deleted, and only when
``FAIRNESS_FREEZE_ENTRIES_ON_DRAW`` allows it.  Every flag is audited.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import Database, now_ms
from ..models import EntryStatus

#: Statuses that still count towards participation numbers.
COUNTED_STATUSES = (EntryStatus.VALID.value, EntryStatus.WINNER.value, EntryStatus.LOST.value)


def count_user_entries(
    db: Database,
    giveaway_id: str,
    user_id: str,
    *,
    max_entries_per_user: int,
    statuses: tuple[str, ...] = COUNTED_STATUSES,
) -> int:
    placeholders = ",".join("?" for _ in statuses)
    value = db.scalar(
        f"SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?"
        f" AND status IN ({placeholders})",
        (giveaway_id, user_id, *statuses),
    )
    return int(value or 0)


def add_entry(
    db: Database,
    giveaway_id: str,
    user_id: str,
    entry_seq: int,
    *,
    account_created_at: int | None = None,
    guild_joined_at: int | None = None,
    snapshot: dict[str, Any] | None = None,
    role_granted_at: int | None = None,
    grant_source: str = "none",
) -> int | None:
    """Insert one entry. Returns the new row id, or None if it already existed."""
    timestamp = now_ms()
    try:
        return db.execute_write(
            """
            INSERT INTO giveaway_entries (
                giveaway_id, user_id, entry_seq, account_created_at, guild_joined_at,
                status, snapshot_json, role_granted_at, grant_source, joined_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                giveaway_id,
                user_id,
                entry_seq,
                account_created_at,
                guild_joined_at,
                EntryStatus.VALID.value,
                json.dumps(snapshot or {}, separators=(",", ":")),
                role_granted_at,
                grant_source,
                timestamp,
                timestamp,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - UNIQUE violation is an expected outcome
        if "UNIQUE" in str(exc).upper() or "constraint" in str(exc).lower():
            return None
        raise


def list_entries_for_user(
    db: Database, giveaway_id: str, user_id: str
) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ? ORDER BY entry_seq",
        (giveaway_id, user_id),
    )


def list_participants(
    db: Database,
    giveaway_id: str,
    *,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = """
        SELECT user_id,
               COUNT(*)                AS entries,
               MIN(joined_at)          AS first_joined_at,
               MAX(joined_at)          AS last_joined_at,
               MIN(account_created_at) AS account_created_at,
               MIN(guild_joined_at)    AS guild_joined_at,
               MAX(CASE WHEN invalid_reason IS NOT NULL THEN invalid_reason END) AS invalid_reason,
               CASE
                 WHEN SUM(CASE WHEN status = 'winner' THEN 1 ELSE 0 END) > 0 THEN 'winner'
                 WHEN SUM(CASE WHEN status = 'disqualified' THEN 1 ELSE 0 END)
                      = COUNT(*) THEN 'disqualified'
                 WHEN SUM(CASE WHEN status = 'invalid' THEN 1 ELSE 0 END)
                      = COUNT(*) THEN 'invalid'
                 ELSE 'valid'
               END AS status
        FROM giveaway_entries
        WHERE giveaway_id = ?
    """
    params: list[Any] = [giveaway_id]
    if status:
        sql += " AND ( ? = 'all' OR status = ? )"
        params.extend([status, status])
    if search:
        sql += " AND user_id LIKE ?"
        params.append(f"%{search.strip()}%")
    sql += " GROUP BY user_id ORDER BY last_joined_at DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 200)), max(0, offset)])

    rows = db.query(sql, params)
    for row in rows:
        row["entries"] = int(row["entries"])
    return rows


def count_participants(db: Database, giveaway_id: str, *, search: str | None = None) -> int:
    if search:
        value = db.scalar(
            "SELECT COUNT(DISTINCT user_id) FROM giveaway_entries"
            " WHERE giveaway_id = ? AND user_id LIKE ?",
            (giveaway_id, f"%{search.strip()}%"),
        )
    else:
        value = db.scalar(
            "SELECT COUNT(DISTINCT user_id) FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway_id,),
        )
    return int(value or 0)


def list_user_entry_status(db: Database, giveaway_id: str, user_ids: list[str]) -> dict[str, int]:
    """Entry counts for a batch of users - used by Discord autocomplete/render."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    status_placeholders = ",".join("?" for _ in COUNTED_STATUSES)
    rows = db.query(
        f"""
        SELECT user_id, COUNT(*) AS entries
        FROM giveaway_entries
        WHERE giveaway_id = ? AND user_id IN ({placeholders})
          AND status IN ({status_placeholders})
        GROUP BY user_id
        """,
        (giveaway_id, *user_ids, *COUNTED_STATUSES),
    )
    return {str(row["user_id"]): int(row["entries"]) for row in rows}


def set_status(
    db: Database,
    giveaway_id: str,
    user_id: str,
    status: EntryStatus,
    *,
    reason: str | None = None,
    actor_id: str | None = None,
    only_valid: bool = True,
) -> int:
    """Flag a user's entries. Returns the number of rows changed.

    ``only_valid=True`` prevents a subsequent flag from rewriting an already
    ``winner``/``lost`` marker, so reroll bookkeeping stays consistent.
    """
    timestamp = now_ms()
    if reason:
        sql = """
            UPDATE giveaway_entries
            SET status = ?, invalid_reason = ?, updated_at = ?,
                removed_by = ?, removed_at = ?, removed_reason = ?
            WHERE giveaway_id = ? AND user_id = ?
        """
        params: list[Any] = [status.value, reason, timestamp, actor_id, timestamp, reason,
                             giveaway_id, user_id]
    else:
        sql = """
            UPDATE giveaway_entries
            SET status = ?, invalid_reason = ?, updated_at = ?
            WHERE giveaway_id = ? AND user_id = ?
        """
        params = [status.value, reason, timestamp, giveaway_id, user_id]

    if only_valid:
        sql += " AND status IN ('valid', 'invalid', 'lost')"
    cursor = db.execute(sql, params)
    changed = getattr(cursor, "rowcount", 0) or 0
    cursor.close()
    return int(changed)


def flag_entry_ids(
    db: Database, entry_ids: list[int], status: EntryStatus, *, reason: str, actor_id: str
) -> int:
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    cursor = db.execute(
        f"""
        UPDATE giveaway_entries
        SET status = ?, invalid_reason = ?, removed_by = ?, removed_at = ?, removed_reason = ?,
            updated_at = ?
        WHERE id IN ({placeholders}) AND status = 'valid'
        """,
        (status.value, reason, actor_id, now_ms(), reason, now_ms(), *entry_ids),
    )
    changed = getattr(cursor, "rowcount", 0) or 0
    cursor.close()
    return int(changed)


def delete_entries(db: Database, giveaway_id: str, user_id: str) -> int:
    """Hard delete. Refused for locked giveaways by the service layer."""
    cursor = db.execute(
        "DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
        (giveaway_id, user_id),
    )
    changed = getattr(cursor, "rowcount", 0) or 0
    cursor.close()
    return int(changed)


def frozen_entries(db: Database, giveaway_id: str) -> list[dict[str, Any]]:
    """The exact set that a draw will score - status must be ``valid``."""
    return db.query(
        """
        SELECT id, user_id, entry_seq, joined_at, account_created_at, guild_joined_at
        FROM giveaway_entries
        WHERE giveaway_id = ? AND status = 'valid'
        ORDER BY user_id ASC, entry_seq ASC
        """,
        (giveaway_id,),
    )


def active_entry_totals(db: Database, giveaway_id: str) -> tuple[int, int]:
    placeholders = ",".join("?" for _ in COUNTED_STATUSES)
    row = db.query_one(
        f"""
        SELECT COUNT(*) AS entries, COUNT(DISTINCT user_id) AS participants
        FROM giveaway_entries WHERE giveaway_id = ? AND status IN ({placeholders})
        """,
        (giveaway_id, *COUNTED_STATUSES),
    )
    if not row:
        return (0, 0)
    return (int(row["entries"] or 0), int(row["participants"] or 0))


def entry_totals_global(db: Database) -> dict[str, int]:
    row = db.query_one(
        """
        SELECT
          (SELECT COUNT(*) FROM giveaway_entries)                       AS entries,
          (SELECT COUNT(DISTINCT giveaway_id) FROM giveaway_entries)      AS giveaways_with_entries,
          (SELECT COUNT(*) FROM giveaway_winners)                        AS winners
        """
    )
    return {key: int(value or 0) for key, value in (row or {}).items()}


def users_with_bot_role(db: Database, giveaway_id: str) -> list[str]:
    """Distinct users the *bot* granted the temporary entrants role to.

    Used on giveaway end to schedule removals. Deliberately excludes
    ``grant_source='manual'``: a role a human assigned is not ours to remove.
    """
    rows = db.query(
        """
        SELECT DISTINCT user_id FROM giveaway_entries
        WHERE giveaway_id = ? AND grant_source = 'bot' AND role_granted_at IS NOT NULL
        ORDER BY user_id
        """,
        (giveaway_id,),
    )
    return [str(row["user_id"]) for row in rows]


def has_bot_grant(db: Database, giveaway_id: str, user_id: str) -> bool:
    """True when the bot (not a human) is responsible for this member's role."""
    row = db.query_one(
        """
        SELECT 1 AS present FROM giveaway_entries
        WHERE giveaway_id = ? AND user_id = ? AND grant_source = 'bot'
          AND role_granted_at IS NOT NULL
        LIMIT 1
        """,
        (giveaway_id, user_id),
    )
    return row is not None


def mark_grants_revoked(db: Database, giveaway_id: str, user_id: str) -> None:
    """Clear grant bookkeeping once the role has been removed."""
    db.execute(
        """
        UPDATE giveaway_entries
        SET role_granted_at = NULL, grant_source = 'revoked', updated_at = ?
        WHERE giveaway_id = ? AND user_id = ? AND grant_source = 'bot'
        """,
        (now_ms(), giveaway_id, user_id),
    )


def entries_for_user_joined(db: Database, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT e.giveaway_id, g.title, g.status, e.entries, MAX(e.joined_at) AS joined_at
        FROM giveaway_entries e
        JOIN giveaways g ON g.id = e.giveaway_id
        WHERE e.user_id = ? AND e.status IN ('valid','winner','lost')
        GROUP BY e.giveaway_id, g.title, g.status
        ORDER BY joined_at DESC
        LIMIT ?
        """,
        (user_id, max(1, min(limit, 100))),
    )