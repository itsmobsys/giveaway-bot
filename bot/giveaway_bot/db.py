"""Tiny DB layer: Turso (libSQL) only.

Deliberately no local/SQLite fallback: on a host like Render a local file is
wiped on every redeploy, which silently "forgot" all giveaways. Refusing to
start without TURSO_DATABASE_URL turns that into a loud one-line error instead
of data loss.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

from .config import Settings, get_settings

log = logging.getLogger("giveaway_bot.db")

#: Substrings meaning the Turso Hrana stream behind this connection is gone
#: (server-side idle timeout, restart, network blip). The driver raises a bare
#: ValueError with no exception hierarchy, so the message text is the signal.
#: A dead stream means the statement never reached the server, so dropping the
#: connection and replaying the statement once cannot double-apply anything.
_DEAD_STREAM_MARKERS = (
    "stream not found",
    "stream was idle for too long",
    "no transaction is active",
    "connection closed",
    "not connected",
    "broken pipe",
    "connection reset",
    "connection aborted",
    "server closed",
    "unexpected eof",
)


def _is_dead_stream(message: str) -> bool:
    return any(marker in message for marker in _DEAD_STREAM_MARKERS)


def _is_write(sql: str) -> bool:
    """True for statements that change data and need a COMMIT under libsql.

    libsql opens an implicit transaction around writes (like sqlite3's legacy
    default) and never auto-commits. Forgetting the commit means the writing
    thread sees its own row while every other connection sees nothing — the
    exact "created fine, join says not found, restart forgets everything"
    failure. SELECTs/PRAGMAs commit nothing.
    """
    verb = sql.strip().split(None, 1)
    return bool(verb) and verb[0].upper() not in (
        "SELECT", "PRAGMA", "EXPLAIN", "WITH", "VALUES",
    )


def _brief(exc: Exception, limit: int = 120) -> str:
    return " ".join(str(exc).split())[:limit]

#: v2 table names. The v1 bot used `giveaways` / `giveaway_entries` with a
#: different shape (NOT NULL title, status CHECK constraint, ...). Reusing
#: those names would need a migration of live data; fresh names start clean
#: and leave the old rows (and the dashboard reading them) untouched.
TABLE_GIVEAWAYS = "simple_giveaways"
TABLE_ENTRIES = "simple_entries"

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
  winners_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS {TABLE_ENTRIES} (
  giveaway_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  entered_at INTEGER NOT NULL,
  PRIMARY KEY (giveaway_id, user_id)
);
"""

SCHEMA_INDEXES = f"""
CREATE INDEX IF NOT EXISTS idx_simple_gw_status_ends ON {TABLE_GIVEAWAYS}(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_simple_entries_giveaway ON {TABLE_ENTRIES}(giveaway_id);
"""


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._local = threading.local()
        if not self.settings.turso_url:
            raise RuntimeError(
                "TURSO_DATABASE_URL is not set. This bot stores everything in Turso"
                " so restarts never lose data — set TURSO_DATABASE_URL (and"
                " TURSO_AUTH_TOKEN) and restart."
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
            try:
                conn.execute("PRAGMA busy_timeout=10000")
            except Exception:
                pass
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
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
        for stmt in [s.strip() for s in SCHEMA_INDEXES.split(";") if s.strip()]:
            self.execute(stmt)

    def _ensure_columns(self, table: str, desired: dict[str, str]) -> None:
        try:
            rows = self.query(f"PRAGMA table_info({table})")
        except Exception:
            return
        existing = {str(r.get("name")) for r in rows}
        for name, ddl in desired.items():
            if name not in existing:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one statement, surviving dead Turso streams and brief contention.

        * Dead/idle Hrana stream: the connection is dropped and the statement
          is replayed on a fresh connection, on *every* hit up to the attempt
          cap (streams can flap repeatedly during a Turso wobble). Safe
          because a dead stream never applied anything.
        * `locked`/`busy`/`conflict`: genuine writer contention, retried with a
          short backoff. Anything else raises untouched.
        """
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                conn = self._conn()
                cur = conn.execute(sql, tuple(params))
                if _is_write(sql):
                    conn.commit()
                return cur
            except Exception as exc:
                message = str(exc).lower()
                if _is_dead_stream(message):
                    log.warning(
                        "database stream went away (%s); reconnecting", _brief(exc)
                    )
                    self.close()
                    last_error = exc
                    time.sleep(0.15 * (attempt + 1))
                    continue
                if any(word in message for word in ("locked", "busy", "conflict")):
                    last_error = exc
                    time.sleep(0.15 * (attempt + 1))
                    continue
                raise
        raise last_error  # type: ignore[misc]

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        # fetchall() pulls rows over the network on Turso, so the stream can
        # die here too — after execute() already succeeded. Retry the whole
        # read on a fresh connection instead of surfacing a phantom failure.
        last_error: Exception | None = None
        for _ in range(3):
            try:
                cur = self.execute(sql, params)
                rows = cur.fetchall()
                break
            except Exception as exc:
                if _is_dead_stream(str(exc).lower()):
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

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database()
        _database.init_schema()
    return _database
