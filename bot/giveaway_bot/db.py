"""Tiny DB layer: Turso (libSQL) only.

Deliberately no local/SQLite fallback: on a host like Render a local file is
wiped on every redeploy, which silently "forgot" all giveaways. Refusing to
start without TURSO_DATABASE_URL turns that into a loud one-line error instead
of data loss.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from .config import Settings, get_settings

log = logging.getLogger("giveaway_bot.db")

#: Substrings meaning the stream is gone and the statement never ran: the server
#: rejected a stream it no longer knows (idle timeout, restart), or the client
#: never had a usable connection to send on. The driver raises a bare ValueError
#: with no exception hierarchy, so the message text is the only signal. Nothing
#: was applied, so replaying either a read or a write is safe.
_UNAPPLIED_MARKERS = (
    "stream not found",
    "stream was idle for too long",
    "no transaction is active",
    "connection closed",
    "not connected",
)

#: Substrings meaning the socket died *mid-request*: the server may already have
#: applied the statement before the response was lost. A read can be replayed; a
#: write must not be, because replaying "count = count + 1" or
#: "ends_at = ends_at + ?" would apply it a second time.
_AMBIGUOUS_MARKERS = (
    "broken pipe",
    "connection reset",
    "connection aborted",
    "server closed",
    "unexpected eof",
)


def _is_dead_stream(message: str) -> bool:
    """True when the statement provably never reached the server."""
    return any(marker in message for marker in _UNAPPLIED_MARKERS)


def _is_ambiguous_loss(message: str) -> bool:
    """True when the connection dropped and the statement's fate is unknown."""
    return any(marker in message for marker in _AMBIGUOUS_MARKERS)


def _is_write(sql: str) -> bool:
    """True for statements that change data and need a COMMIT under libsql.

    libsql opens an implicit transaction around writes (like sqlite3's legacy
    default) and never auto-commits. Forgetting the commit means the writing
    thread sees its own row while every other connection sees nothing — the
    exact "created fine, join says not found, restart forgets everything"
    failure. SELECTs/PRAGMAs commit nothing.
    """
    verb = sql.lstrip().split(None, 1)
    if not verb:
        return False
    head = verb[0].upper()
    if head == "PRAGMA":
        # "PRAGMA user_version = 2" assigns and needs the commit like any other
        # write; a bare pragma ("PRAGMA table_info(x)") only reads.
        return "=" in sql
    # WITH counts as a write on purpose: a CTE can head an INSERT/UPDATE/DELETE.
    # Committing a read costs nothing, while forgetting to commit a write
    # silently loses it.
    return head not in ("SELECT", "EXPLAIN", "VALUES")


def _brief(exc: Exception, limit: int = 120) -> str:
    return " ".join(str(exc).split())[:limit]

#: v2 table names. The original v1 bot used `giveaways` / `giveaway_entries`
#: with a different shape. Reusing those names would need a migration of live
#: data; fresh names start clean and leave any old rows untouched.
TABLE_GIVEAWAYS = "simple_giveaways"
TABLE_ENTRIES = "simple_entries"

#: Schema revision, recorded in the database's own PRAGMA user_version. A boot
#: that finds this number already set skips the whole DDL block; bump it on any
#: change to SCHEMA_TABLES, SCHEMA_DROPS, SCHEMA_INDEXES or a column map below,
#: and the next boot re-runs everything (all of it is idempotent).
SCHEMA_VERSION = 3

SCHEMA_TABLES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_GIVEAWAYS} (
  id TEXT PRIMARY KEY,
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  message_id TEXT,
  prize TEXT NOT NULL,
  winner_count INTEGER NOT NULL DEFAULT 1,
  ends_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  required_role_id TEXT,
  blocked_role_id TEXT,
  min_account_age_days INTEGER NOT NULL DEFAULT 0,
  min_messages INTEGER NOT NULL DEFAULT 0,
  image_url TEXT,
  entrants_role_id TEXT,
  created_by TEXT NOT NULL,
  host_id TEXT,
  host_name TEXT,
  created_at INTEGER NOT NULL,
  ended_at INTEGER,
  winners_json TEXT NOT NULL DEFAULT '[]',
  entrant_count INTEGER,
  claim_timeout_seconds INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {TABLE_ENTRIES} (
  giveaway_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  entered_at INTEGER NOT NULL,
  PRIMARY KEY (giveaway_id, user_id)
);
"""

#: Indexes an earlier revision declared that only repeated a primary-key prefix
#: or were superseded by a wider one below. They are dropped rather than left in
#: place: a redundant index costs nothing to read through (SQLite prefers the
#: covering one) but taxes every INSERT and DELETE on that table, and the entry
#: and message-count tables see the most writes. Dropping is a no-op on a fresh
#: database.
SCHEMA_DROPS = """
DROP INDEX IF EXISTS idx_simple_entries_giveaway;
DROP INDEX IF EXISTS idx_simple_msgcounts_pk;
DROP INDEX IF EXISTS idx_simple_bans_cover;
DROP INDEX IF EXISTS idx_simple_banhits_cover;
DROP INDEX IF EXISTS idx_simple_bl_cover;
"""

#: One index per real access shape, each ordered so the statement that uses it
#: needs no temporary sort. Verified against EXPLAIN QUERY PLAN on a seeded
#: database (500 giveaways / 27k entries / 5k claims):
#:   - (status, ends_at)      due() every tick, and it is the only one that can
#:                            serve the due scan, so it stays as-is.
#:   - (status, ended_at)     list_all_ended, which otherwise sorts a temp B-tree.
#:   - (guild_id, status, *)  list_active / list_ended: guild_id led no index at
#:                            all, so both scanned and sorted.
#:   - (giveaway_id, ...)     every entries read sorts by entered_at; the same
#:                            index replaces the old giveaway_id-only one.
#:   - (COALESCE(ended_at, created_at) DESC, id DESC)
#:                            the dashboard's finished list orders exactly like
#:                            this after filtering status IN ('ended',
#:                            'cancelled'); that filter cannot use a status
#:                            prefix, so only this expression index lets the
#:                            page walk rows in order instead of scanning and
#:                            sorting the whole table.
#:   - simple_claims(deadline_ms) WHERE pending — due_claims runs every 30s over
#:                            a table nothing ever prunes, so it must not scan.
#:   - entries(user_id) stays: the blacklist purge looks members up by user.
SCHEMA_INDEXES = f"""
CREATE INDEX IF NOT EXISTS idx_simple_gw_status_ends ON {TABLE_GIVEAWAYS}(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_status_ended ON {TABLE_GIVEAWAYS}(status, ended_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_guild_status_ends
ON {TABLE_GIVEAWAYS}(guild_id, status, ends_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_guild_status_ended
ON {TABLE_GIVEAWAYS}(guild_id, status, ended_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_ended
ON {TABLE_GIVEAWAYS}(COALESCE(ended_at, created_at) DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_simple_entries_gw_entered ON {TABLE_ENTRIES}(giveaway_id, entered_at);
CREATE INDEX IF NOT EXISTS idx_simple_entries_user ON {TABLE_ENTRIES}(user_id);
CREATE INDEX IF NOT EXISTS idx_simple_claims_pending_due
ON simple_claims(deadline_ms) WHERE status = 'pending';
"""


class Database:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._local = threading.local()
        #: Every connection this process opened. close() recycles one thread's
        #: connection; close_all() is for shutdown.
        self._connections: list[Any] = []
        self._lock = threading.Lock()
        #: Lightweight latency instrumentation: attempts + total ms spent
        #: inside execute()/query(). Each attempt is counted (a dead-stream
        #: retry that succeeds on the second try counts 2, not 1), and ms
        #: includes failed attempts plus fetchall() transfer time. These are
        #: DB-only numbers (Turso round-trips); Discord API time is not
        #: tracked here. Timings measured against the sqlite test harness are
        #: not Turso/Render benchmarks. Read via get_stats()/reset_stats().
        #: Counters are updated under the connection lock so concurrent
        #: to_thread workers cannot lose increments.
        self._query_count = 0
        self._write_count = 0
        self._query_ms = 0.0
        self._write_ms = 0.0
        if connect is not None:
            # Injection point for tests: anything with sqlite3's
            # execute/commit/close surface works, so the rules can run against a
            # real (in-memory) database without a Turso account or the driver.
            self._factory = connect
            self.backend = "injected"
            return
        if not self.settings.turso_url:
            raise RuntimeError(
                "TURSO_DATABASE_URL is not set. This bot stores everything in Turso"
                " so restarts never lose data — set TURSO_DATABASE_URL (and"
                " TURSO_AUTH_TOKEN) and restart."
            )
        if self.settings.turso_url.startswith("file:"):
            # A local file works on a laptop and silently loses every giveaway on
            # Render, which wipes local files on each redeploy. The injected
            # `connect` above is the only sanctioned local path (tests), so a
            # file: URL through configuration is always a mistake.
            raise RuntimeError(
                "TURSO_DATABASE_URL must be a remote libsql:// (or https://) URL:"
                " a file: database is wiped on every Render redeploy."
            )
        try:
            import libsql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "The Turso driver is missing. Install it with: pip install -e \".[turso]\""
            ) from exc
        self._factory = lambda: libsql.connect(  # noqa: E731
            self.settings.turso_url,
            auth_token=self.settings.turso_token or None,
        )
        self.backend = "turso"

    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._factory()
            with contextlib.suppress(Exception):
                conn.isolation_level = None  # autocommit, where the driver offers it
            if getattr(self, "backend", "") != "turso":
                # Local-only: busy_timeout governs file locking, which a remote
                # Turso stream has none of. Running it there costs a full extra
                # round-trip (~40-100ms warm) on every cold thread for nothing.
                try:
                    conn.execute("PRAGMA busy_timeout=10000")
                except Exception:
                    pass
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def init_schema(self) -> None:
        if self._schema_is_current():
            return
        for stmt in [s.strip() for s in SCHEMA_TABLES.split(";") if s.strip()]:
            self.execute(stmt)
        # Full column set, so a table created by any earlier v2 revision
        # gains whatever it is missing (live databases are never rebuilt).
        # Indexes come last: they reference columns that may only just have
        # been added above.
        self._ensure_columns(
            TABLE_GIVEAWAYS,
            {
                "guild_id": "TEXT NOT NULL DEFAULT ''",
                "channel_id": "TEXT NOT NULL DEFAULT ''",
                "message_id": "TEXT",
                "prize": "TEXT NOT NULL DEFAULT ''",
                "winner_count": "INTEGER NOT NULL DEFAULT 1",
                "ends_at": "INTEGER NOT NULL DEFAULT 0",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "required_role_id": "TEXT",
                "required_role_ids": "TEXT NOT NULL DEFAULT '[]'",
                "blocked_role_id": "TEXT",
                "min_account_age_days": "INTEGER NOT NULL DEFAULT 0",
                "min_messages": "INTEGER NOT NULL DEFAULT 0",
                "image_url": "TEXT",
                "entrants_role_id": "TEXT",
                "created_by": "TEXT NOT NULL DEFAULT ''",
                "host_id": "TEXT",
                "host_name": "TEXT",
                "created_at": "INTEGER NOT NULL DEFAULT 0",
                "ended_at": "INTEGER",
                "winners_json": "TEXT NOT NULL DEFAULT '[]'",
                # Live entry counter (P2-2): incremented on every successful
                # join, decremented on leave/ban-drop, so refresh_snapshot
                # reads 1 indexed row instead of COUNT(*) over all entries.
                # NULL for rows that ended before the counter existed;
                # read path COALESCEs + backfills without a migration.
                "entrant_count": "INTEGER",
                # Optional winner-claim window in seconds. 0/NULL = disabled;
                # live databases gain it via this same migration map.
                "claim_timeout_seconds": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        self._ensure_columns(
            TABLE_ENTRIES,
            {
                "giveaway_id": "TEXT NOT NULL DEFAULT ''",
                "user_id": "TEXT NOT NULL DEFAULT ''",
                "username": "TEXT NOT NULL DEFAULT ''",
                "entered_at": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS simple_message_counts (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
)"""
        )
        # One-time per-server setup: the role pinged on every giveaway event.
        self.execute(
            """CREATE TABLE IF NOT EXISTS simple_guild_settings (
  guild_id TEXT PRIMARY KEY,
  notify_role_id TEXT
)"""
        )
        # Users blocked from giveaways in a server. Permanent until removed
        # (expires_at NULL) or timed (expires_at ms epoch) — the entry-wipe
        # sweep never touches this table. reason/banned_by are shown both to
        # the refused member and in the staff list.
        self.execute(
            """CREATE TABLE IF NOT EXISTS simple_blacklist (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  expires_at INTEGER,
  reason TEXT NOT NULL DEFAULT '',
  banned_by TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
)"""
        )
        self._ensure_columns(
            "simple_blacklist",
            {
                "expires_at": "INTEGER",
                "reason": "TEXT NOT NULL DEFAULT ''",
                "banned_by": "TEXT NOT NULL DEFAULT ''",
                "created_at": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        # Members sitting out a fixed number of giveaways because Discord had
        # them timed out (native /mute) when they tried to join. Persisted like
        # everything else, so a restart never forgives a penalty, and — like the
        # blacklist — the entry-wipe sweep never touches it. A row is deleted
        # the moment its counter reaches zero, so nothing needs to expire it.
        self.execute(
            """CREATE TABLE IF NOT EXISTS simple_giveaway_bans (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  giveaways_remaining INTEGER NOT NULL DEFAULT 0,
  last_blocked_giveaway_id TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
)"""
        )
        # Every giveaway a penalty has been held against, per member. Only the
        # first block on a giveaway spends a unit, and a giveaway listed here
        # keeps refusing that member while it is still running — even after
        # the penalty row above is gone. Not touched by the entry-wipe sweep.
        self.execute(
            """CREATE TABLE IF NOT EXISTS simple_giveaway_ban_hits (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  giveaway_id TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id, giveaway_id)
)"""
        )
        # Optional winner-claim state, one row per drawn winner. Survives
        # restarts like everything else; never touched by the entry-wipe sweep
        # (a pending claim keeps its giveaway's entries alive — see
        # wipe_stale_entries in service.py). Statuses: pending -> claimed /
        # expired / skipped. round distinguishes repeat draws of one slot.
        self.execute(
            """CREATE TABLE IF NOT EXISTS simple_claims (
  giveaway_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  round INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  deadline_ms INTEGER NOT NULL DEFAULT 0,
  claimed_at INTEGER,
  skipped_by TEXT,
  skipped_at INTEGER,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (giveaway_id, user_id, round)
)"""
        )
        for stmt in [s.strip() for s in SCHEMA_DROPS.split(";") if s.strip()]:
            self.execute(stmt)
        for stmt in [s.strip() for s in SCHEMA_INDEXES.split(";") if s.strip()]:
            self.execute(stmt)
        self._stamp_schema()

    def _schema_is_current(self) -> bool:
        """True when an earlier boot already applied this exact schema.

        The DDL above is ~19 sequential round trips (8 CREATE TABLE, 3 column
        probes, 5 drops, 7 indexes). Against a remote database that is seconds
        of every cold start, paid to re-create objects that are already there,
        and Render pays it again on every restart and free-tier spin-up. One
        pragma read answers the same question. Any database from an earlier
        revision -- or one the pragma cannot be read from -- reports not current
        and takes the full path, so this can only ever cost the old behaviour.
        """
        try:
            rows = self.query("PRAGMA user_version")
        except Exception:
            log.debug("could not read the schema version", exc_info=True)
            return False
        try:
            return int(rows[0].get("user_version") or 0) >= SCHEMA_VERSION
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return False

    def _stamp_schema(self) -> None:
        """Record that this revision's schema is fully applied."""
        try:
            self.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        except Exception:
            # Worst case the next boot re-runs the DDL, which is idempotent.
            log.warning("could not record the schema version", exc_info=True)

    def _ensure_columns(self, table: str, desired: dict[str, str]) -> None:
        try:
            rows = self.query(f"PRAGMA table_info({table})")
        except Exception:
            # The columns could not be read, so one may be missing. Failing here
            # beats running the whole process against an unmigrated table, where
            # every statement naming the column dies with "no such column"
            # instead of the problem showing up once, at startup.
            log.exception("could not read the columns of %s", table)
            raise
        existing = {str(r.get("name")) for r in rows}
        for name, ddl in desired.items():
            if name not in existing:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one statement, surviving dead Turso streams and brief contention.

        * Stream the server never saw: the connection is dropped and the
          statement is replayed on a fresh one, on *every* hit up to the attempt
          cap (streams can flap repeatedly during a Turso wobble). Safe,
          because nothing ran.
        * Connection lost mid-request: a read is replayed, a write is not. The
          server may have applied the write before the response was lost, and a
          blind replay would apply it twice. The error goes to the caller, which
          can retry the whole operation deliberately.
        * `locked`/`busy`/`conflict`: genuine writer contention, retried with a
          short backoff. Anything else raises untouched.
        """
        last_error: Exception | None = None
        is_write = _is_write(sql)
        for attempt in range(6):
            start = time.perf_counter()
            try:
                conn = self._conn()
                cur = conn.execute(sql, tuple(params))
                if is_write:
                    conn.commit()
                elapsed = (time.perf_counter() - start) * 1000.0
                try:
                    with self._lock:
                        if is_write:
                            self._write_count += 1
                            self._write_ms += elapsed
                        else:
                            self._query_count += 1
                            self._query_ms += elapsed
                except Exception:
                    pass
                return cur
            except Exception as exc:
                elapsed = (time.perf_counter() - start) * 1000.0
                try:
                    with self._lock:
                        if is_write:
                            self._write_count += 1
                            self._write_ms += elapsed
                        else:
                            self._query_count += 1
                            self._query_ms += elapsed
                except Exception:
                    pass
                message = str(exc).lower()
                ambiguous = _is_ambiguous_loss(message)
                if ambiguous or _is_dead_stream(message):
                    self.close()
                    if ambiguous and _is_write(sql):
                        log.warning(
                            "connection lost mid-write (%s); not replaying", _brief(exc)
                        )
                        raise
                    log.warning(
                        "database stream went away (%s); reconnecting", _brief(exc)
                    )
                    last_error = exc
                    time.sleep(0.15 * (attempt + 1) + random.uniform(0, 0.05 * (attempt + 1)))  # noqa: S311
                    continue
                if any(word in message for word in ("locked", "busy", "conflict")):
                    last_error = exc
                    time.sleep(0.15 * (attempt + 1) + random.uniform(0, 0.05 * (attempt + 1)))  # noqa: S311
                    continue
                raise
        raise last_error  # type: ignore[misc]

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        # fetchall() pulls rows over the network on Turso, so the stream can
        # die here too — after execute() already succeeded. Retry the whole
        # read on a fresh connection instead of surfacing a phantom failure.
        # fetchall() transfer time counts toward read_ms (DB time, not
        # Discord/total): execute() already counted the trip, so only the ms
        # are added here, under the same lock.
        last_error: Exception | None = None
        for _ in range(3):
            fetch_start = time.perf_counter()
            try:
                cur = self.execute(sql, params)
                rows = cur.fetchall()
                try:
                    with self._lock:
                        self._query_ms += (time.perf_counter() - fetch_start) * 1000.0
                except Exception:
                    pass
                break
            except Exception as exc:
                message = str(exc).lower()
                # Every statement query() runs is a read, so replaying is safe
                # whether the server never saw it or the rows were lost on the
                # way back. A write could not be replayed here.
                if _is_dead_stream(message) or _is_ambiguous_loss(message):
                    log.warning(
                        "database stream died mid-read (%s); retrying", _brief(exc)
                    )
                    self.close()
                    last_error = exc
                    continue
                raise
        else:
            raise last_error  # type: ignore[misc]
        desc = cur.description
        cols = [d[0] for d in desc] if desc else []
        try:
            cur.close()
        except Exception:
            pass
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            elif hasattr(row, "keys"):
                try:
                    out.append({k: row[k] for k in row})
                except Exception:
                    out.append(dict(zip(cols, list(row), strict=False)))
            else:
                out.append(dict(zip(cols, list(row), strict=False)))
        return out

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def get_stats(self) -> dict[str, float]:
        """DB-only attempt counters (Turso round-trips, not Discord/total).

        Counts every execute() attempt including dead-stream/locked retries, and
        query() fetchall() transfer time is included in read_ms. Timings from
        the sqlite test harness are not Turso/Render benchmarks."""
        try:
            with self._lock:
                return {
                    "reads": int(self._query_count),
                    "writes": int(self._write_count),
                    "read_ms": float(self._query_ms),
                    "write_ms": float(self._write_ms),
                }
        except Exception:
            return {"reads": 0, "writes": 0, "read_ms": 0.0, "write_ms": 0.0}

    def reset_stats(self) -> None:
        try:
            with self._lock:
                self._query_count = 0
                self._write_count = 0
                self._query_ms = 0.0
                self._write_ms = 0.0
        except Exception:
            pass

    def close(self) -> None:
        """Close the calling thread's connection (also used to drop a dead stream).

        Only this thread's: another thread may be mid-statement on its own
        connection, and killing that one would surface as a phantom failure.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._lock, contextlib.suppress(ValueError):
            self._connections.remove(conn)
        with contextlib.suppress(Exception):
            conn.close()

    def close_all(self) -> None:
        """Close every connection, including other threads'. For shutdown."""
        with self._lock:
            connections = self._connections[:]
            self._connections.clear()
        self._local.conn = None
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
