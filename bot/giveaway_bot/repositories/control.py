"""Control-plane repositories: command queue, audit log, events, rate limits, OAuth."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from ..db import Database, now_ms
from ..models import CommandRecord


# --------------------------------------------------------------------------- #
# Audit log (append only)
# --------------------------------------------------------------------------- #
def audit(
    db: Database,
    *,
    guild_id: str,
    action: str,
    actor_id: str | None = None,
    actor_name: str | None = None,
    source: str = "bot",
    giveaway_id: str | None = None,
    target_id: str | None = None,
    outcome: str = "success",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    ip_hash: str | None = None,
) -> int:
    """Append one audit row. There is deliberately no update/delete API."""
    return db.execute_write(
        """
        INSERT INTO audit_log (
            guild_id, giveaway_id, action, actor_id, actor_name, source, target_id,
            outcome, before_json, after_json, metadata_json, ip_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            giveaway_id,
            action,
            actor_id,
            actor_name,
            source,
            target_id,
            outcome,
            _json(before),
            _json(after),
            _json(metadata),
            ip_hash,
            now_ms(),
        ),
    )


def list_audit(
    db: Database,
    *,
    guild_id: str | None = None,
    giveaway_id: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if guild_id:
        clauses.append("guild_id = ?")
        params.append(guild_id)
    if giveaway_id:
        clauses.append("giveaway_id = ?")
        params.append(giveaway_id)
    if actor_id:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if action:
        clauses.append("action LIKE ?")
        params.append(f"{action}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([max(1, min(limit, 200)), max(0, offset)])
    rows = db.query(
        f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?", params
    )
    for row in rows:
        for field in ("before_json", "after_json", "metadata_json"):
            row[field.removesuffix("_json")] = _load(row.pop(field))
    return rows


# --------------------------------------------------------------------------- #
# Events (dashboard live feed)
# --------------------------------------------------------------------------- #
def emit(
    db: Database,
    *,
    guild_id: str,
    event_type: str,
    payload: dict[str, Any],
    giveaway_id: str | None = None,
) -> int:
    return db.execute_write(
        """
        INSERT INTO giveaway_events (giveaway_id, guild_id, type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (giveaway_id, guild_id, event_type, _json(payload), now_ms()),
    )


def events_since(
    db: Database, giveaway_id: str, *, since_id: int = 0, limit: int = 50
) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT id, giveaway_id, guild_id, type, payload_json, created_at
        FROM giveaway_events
        WHERE giveaway_id = ? AND id > ?
        ORDER BY id ASC LIMIT ?
        """,
        (giveaway_id, since_id, max(1, min(limit, 200))),
    )
    for row in rows:
        row["payload"] = _load(row.pop("payload_json"))
    return rows


def latest_event_id(db: Database, giveaway_id: str) -> int:
    return int(
        db.scalar("SELECT COALESCE(MAX(id), 0) FROM giveaway_events WHERE giveaway_id = ?", (giveaway_id,))
        or 0
    )


def prune_events(db: Database, *, older_than_ms: int) -> int:
    cursor = db.execute("DELETE FROM giveaway_events WHERE created_at < ?", (older_than_ms,))
    removed = getattr(cursor, "rowcount", 0) or 0
    cursor.close()
    return int(removed)


# --------------------------------------------------------------------------- #
# Temporary entrants role reconciliation
# --------------------------------------------------------------------------- #
def role_task(
    db: Database,
    *,
    giveaway_id: str,
    user_id: str,
    action: str,
    status: str,
    role_id: str | None = None,
    attempts: int = 0,
    last_error: str | None = None,
) -> bool:
    """Queue a role add/remove so it can be retried until it succeeds.

    Grants must survive transient Discord failures (5xx, timeouts), and revokes
    are mandatory rather than best-effort - otherwise a crashed bot would leave
    the role stuck on members forever. Idempotent via the unique index on
    (giveaway, user, action): a duplicate enqueue re-arms the existing row.
    """
    guild_id = db.scalar("SELECT guild_id FROM giveaways WHERE id = ?", (giveaway_id,))
    if not guild_id:
        return False
    try:
        db.execute_write(
            """
            INSERT INTO giveaway_role_tasks (
                giveaway_id, guild_id, user_id, role_id, action, status,
                attempts, last_error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                giveaway_id,
                guild_id,
                user_id,
                role_id,
                action,
                status,
                attempts,
                last_error,
                now_ms(),
            ),
        )
        return True
    except Exception:  # noqa: BLE001 - UNIQUE violation -> re-arm the pending row
        db.execute(
            """
            UPDATE giveaway_role_tasks
            SET status = 'pending', role_id = COALESCE(?, role_id), last_error = NULL
            WHERE giveaway_id = ? AND user_id = ? AND action = ?
            """,
            (role_id, giveaway_id, user_id, action),
        )
        return False


def claim_role_tasks(db: Database, *, limit: int = 25) -> list[dict[str, Any]]:
    """Atomically claim pending role tasks.

    Atomicity comes from the conditional UPDATE, not from an enclosing write
    transaction - see :func:`claim_batch` for why.
    """
    limit = max(1, min(limit, 100))
    claimed: list[dict[str, Any]] = []
    for row in db.query(
        """
        SELECT id FROM giveaway_role_tasks
        WHERE status = 'pending'
        ORDER BY id ASC LIMIT ?
        """,
        (limit,),
    ):
        cursor = db.execute(
            "UPDATE giveaway_role_tasks SET status = 'claimed', attempts = attempts + 1"
            " WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        won = int(getattr(cursor, "rowcount", 0) or 0) > 0
        cursor.close()
        if not won:
            # Lost the race for this row; another claimer owns it now.
            continue
        claimed.append(
            dict(db.query_one("SELECT * FROM giveaway_role_tasks WHERE id = ?", (row["id"],)) or {})
        )
        if len(claimed) >= limit:
            break
    return claimed


def complete_role_task(db: Database, task_id: int, *, ok: bool, error: str | None = None) -> str:
    """Mark a role task done, or return it to pending for a retry."""
    if ok:
        db.execute(
            "UPDATE giveaway_role_tasks SET status = 'done', last_error = NULL WHERE id = ?",
            (task_id,),
        )
        return "done"
    # Retry with backoff, then give up (recorded, never silently dropped).
    status = "failed"
    row = db.query_one("SELECT attempts FROM giveaway_role_tasks WHERE id = ?", (task_id,))
    if row and int(row["attempts"] or 0) < 8:
        status = "pending"
    db.execute(
        "UPDATE giveaway_role_tasks SET status = ?, last_error = ? WHERE id = ?",
        (status, (error or "")[:500], task_id),
    )
    return status


def pending_role_tasks(db: Database, giveaway_id: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM giveaway_role_tasks WHERE giveaway_id = ? AND status = 'pending'",
        (giveaway_id,),
    ) or 0)


# --------------------------------------------------------------------------- #
# Command queue
# --------------------------------------------------------------------------- #
def enqueue(
    db: Database,
    *,
    guild_id: str,
    kind: str,
    payload: dict[str, Any],
    requested_by: str,
    source: str = "dashboard",
    giveaway_id: str | None = None,
    requested_by_name: str | None = None,
    priority: int = 100,
) -> int:
    return db.execute_write(
        """
        INSERT INTO command_queue (
            guild_id, giveaway_id, kind, payload_json, requested_by, requested_by_name,
            source, status, priority, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            guild_id,
            giveaway_id,
            kind,
            _json(payload),
            requested_by,
            requested_by_name,
            source,
            priority,
            now_ms(),
        ),
    )


def claim_batch(db: Database, *, limit: int = 4) -> list[CommandRecord]:
    """Atomically move pending commands to ``claimed``.

    Atomicity comes from the conditional UPDATE, which is a compare-and-set:
    ``WHERE id = ? AND status = 'pending'`` only matches while the row is still
    unclaimed, so when two claimers race for the same row exactly one sees
    ``rowcount == 1`` and the other sees 0. Two bot instances sharing one
    database therefore cannot process the same command twice.

    Deliberately not wrapped in ``db.transaction()``. A write transaction would
    make every claimer serialise on the single writer lock, so on a remote
    database the losers of that race are turned away with SQLITE_BUSY instead of
    simply claiming different rows - and BEGIN is issued straight on the
    connection, outside the retry path, so that failure is not retried. Letting
    each row's UPDATE be its own atomic step means concurrent claimers overlap
    freely and neither blocks nor fails the other.
    """
    limit = max(1, min(limit, 20))
    claimed: list[CommandRecord] = []
    rows = db.query(
        """
        SELECT id FROM command_queue
        WHERE status = 'pending'
        ORDER BY priority ASC, id ASC
        LIMIT ?
        """,
        (limit,),
    )
    for row in rows:
        cursor = db.execute(
            """
            UPDATE command_queue
            SET status = 'claimed', claimed_at = ?, attempts = attempts + 1
            WHERE id = ? AND status = 'pending'
            """,
            (now_ms(), row["id"]),
        )
        won = int(getattr(cursor, "rowcount", 0) or 0) > 0
        cursor.close()
        if not won:
            # Lost the race for this row; another claimer owns it now.
            continue
        record = _command_from_id(db, int(row["id"]))
        if record is not None:
            claimed.append(record)
        if len(claimed) >= limit:
            break
    return claimed


def _command_from_id(db: Database, command_id: int) -> CommandRecord | None:
    row = db.query_one("SELECT * FROM command_queue WHERE id = ?", (command_id,))
    if not row:
        return None
    return CommandRecord(
        id=int(row["id"]),
        guild_id=str(row["guild_id"]),
        giveaway_id=row.get("giveaway_id"),
        kind=str(row["kind"]),
        payload=_load(row.get("payload_json")) or {},
        requested_by=str(row["requested_by"]),
        requested_by_name=row.get("requested_by_name"),
        source=str(row["source"]),
        status=str(row["status"]),
        attempts=int(row.get("attempts") or 0),
        created_at=int(row.get("created_at") or 0),
        last_error=row.get("last_error"),
    )


def get_command(db: Database, command_id: int) -> CommandRecord | None:
    return _command_from_id(db, command_id)


def complete_command(db: Database, command_id: int, result: dict[str, Any] | None = None) -> None:
    db.execute(
        """
        UPDATE command_queue
        SET status = 'succeeded', result_json = ?, processed_at = ?, last_error = NULL
        WHERE id = ?
        """,
        (_json(result or {}), now_ms(), command_id),
    )


def fail_command(
    db: Database, command_id: int, error: str, *, retryable: bool = True, max_attempts: int = 5
) -> str:
    record = _command_from_id(db, command_id)
    attempts = record.attempts if record else max_attempts
    status = "failed"
    if retryable and attempts < max_attempts:
        status = "pending"  # will be retried
    db.execute(
        """
        UPDATE command_queue
        SET status = ?, last_error = ?, processed_at = ?
        WHERE id = ?
        """,
        (status, error[:1000], now_ms(), command_id),
    )
    return status


def cancel_pending(db: Database, giveaway_id: str, *, requested_by: str) -> int:
    cursor = db.execute(
        """
        UPDATE command_queue SET status = 'cancelled', processed_at = ?
        WHERE giveaway_id = ? AND status = 'pending'
        """,
        (now_ms(), giveaway_id),
    )
    changed = getattr(cursor, "rowcount", 0) or 0
    cursor.close()
    return int(changed)


def pending_count(db: Database, guild_id: str | None = None) -> int:
    if guild_id:
        value = db.scalar(
            "SELECT COUNT(*) FROM command_queue WHERE status IN ('pending','claimed') AND guild_id = ?",
            (guild_id,),
        )
    else:
        value = db.scalar("SELECT COUNT(*) FROM command_queue WHERE status IN ('pending','claimed')")
    return int(value or 0)


def recent_commands(
    db: Database, *, guild_id: str | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, guild_id, giveaway_id, kind, status, source, requested_by, attempts,"
        " last_error, created_at, processed_at FROM command_queue"
    )
    params: list[Any] = []
    if guild_id:
        sql += " WHERE guild_id = ?"
        params.append(guild_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    return db.query(sql, params)


# --------------------------------------------------------------------------- #
# OAuth state (login CSRF)
# --------------------------------------------------------------------------- #
def create_oauth_state(db: Database, redirect_to: str | None, *, ttl_seconds: int = 600) -> str:
    state = secrets.token_urlsafe(32)
    timestamp = now_ms()
    db.execute(
        "INSERT INTO oauth_states (state, redirect_to, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (state, (redirect_to or "")[:512], timestamp, timestamp + ttl_seconds * 1000),
    )
    return state


def consume_oauth_state(db: Database, state: str) -> bool:
    """Single-use, expiring state check."""
    row = db.query_one("SELECT expires_at, consumed_at FROM oauth_states WHERE state = ?", (state,))
    if not row:
        return False
    if row.get("consumed_at"):
        return False
    if int(row.get("expires_at") or 0) < now_ms():
        return False
    db.execute("UPDATE oauth_states SET consumed_at = ? WHERE state = ?", (now_ms(), state))
    return True


def prune_oauth_states(db: Database) -> None:
    db.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now_ms() - 3_600_000,))


# --------------------------------------------------------------------------- #
# Rate limiting (shared across serverless instances)
# --------------------------------------------------------------------------- #
def rate_limit_hit(db: Database, bucket: str, *, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    """Fixed-window limiter. Returns ``(allowed, remaining, reset_after_seconds)``."""
    window_seconds = max(1, window_seconds)
    window_start = (now_ms() // 1000 // window_seconds) * window_seconds
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO rate_limits (bucket, window_start, hits) VALUES (?, ?, 1)"
            " ON CONFLICT(bucket, window_start) DO UPDATE SET hits = hits + 1",
            (bucket, window_start),
        )
        row = tx.query_one(
            "SELECT hits FROM rate_limits WHERE bucket = ? AND window_start = ?",
            (bucket, window_start),
        )
    hits = int((row or {}).get("hits") or 1)
    reset_after = max(0, int(((window_start + window_seconds) * 1000 - now_ms()) / 1000))
    return (hits <= limit, max(0, limit - hits), reset_after)


def rate_limit_peek(db: Database, bucket: str, *, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    """Non-mutating variant for cost-free checks (e.g. SSE connection budget)."""
    window_start = (now_ms() // 1000 // max(1, window_seconds)) * max(1, window_seconds)
    row = db.query_one(
        "SELECT hits FROM rate_limits WHERE bucket = ? AND window_start = ?",
        (bucket, window_start),
    )
    hits = int((row or {}).get("hits") or 0)
    reset_after = max(0, int(((window_start + window_seconds) * 1000 - now_ms()) / 1000))
    return (hits < limit, max(0, limit - hits), reset_after)


def prune_rate_limits(db: Database) -> None:
    cutoff = (now_ms() // 1000) - 3600
    db.execute("DELETE FROM rate_limits WHERE window_start < ?", (cutoff,))


def hash_ip(ip: str | None, salt: str = "") -> str | None:
    """IPs are never stored raw - only a salted digest for abuse correlation."""
    if not ip:
        return None
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# bot_state key/value
# --------------------------------------------------------------------------- #
def set_state(db: Database, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_ms()),
    )


def get_state(db: Database, key: str) -> str | None:
    row = db.query_one("SELECT value FROM bot_state WHERE key = ?", (key,))
    return row["value"] if row else None


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), default=str)


def _load(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None