"""Tiny DB layer: same SQL for local SQLite and hosted Turso (libsql)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS giveaways (
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
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  ended_at INTEGER,
  winners_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS entries (
  giveaway_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  entered_at INTEGER NOT NULL,
  PRIMARY KEY (giveaway_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_giveaways_status_ends ON giveaways(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_entries_giveaway ON entries(giveaway_id);
"""


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._local = threading.local()
        self._is_turso = self.settings.uses_turso
        if self._is_turso:
            try:
                import libsql  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "TURSO_DATABASE_URL is set but libsql is missing. "
                    'Install with: pip install -e ".[turso]"'
                ) from exc
            self._factory = lambda: libsql.connect(  # noqa: E731
                self.settings.turso_url,
                auth_token=self.settings.turso_token or None,
            )
            self.backend = "turso"
        else:
            path = Path(self.settings.sqlite_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._factory = lambda: sqlite3.connect(  # noqa: E731
                str(path), timeout=30.0, isolation_level=None, check_same_thread=False
            )
            self.backend = "sqlite"

    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._factory()
            try:
                conn.execute("PRAGMA busy_timeout=10000")
            except Exception:
                pass
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        conn = self._conn()
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._conn().execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._conn().execute(sql, tuple(params))
        rows = cur.fetchall()
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
