"""Database access layer.

The bot speaks plain DB-API 2.0 so the exact same code runs against:

* a local SQLite file (zero-config development), and
* a hosted Turso/libSQL database (production), via the ``libsql`` DB-API driver.

There is no ORM and no dialect-specific SQL.  Every statement in this project is
written in portable SQLite so an auditor can read the data access layer without
knowing an ORM.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

log = logging.getLogger("giveaway_bot.db")

Migration = tuple[str, str]  # (filename, sql)


class Database:
    """Thin DB-API wrapper with retries, transactions and instrumentation."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._local = threading.local()
        self._is_turso = self.settings.uses_turso
        self._lock = threading.Lock()
        #: Every connection handed out, so close_all() can release file handles
        #: created by worker threads (matters for SQLite on Windows).
        self._connections: list[Any] = []
        self._init_pool()

    # ------------------------------------------------------------------ setup
    def _init_pool(self) -> None:
        if self._is_turso:
            try:
                import libsql  # type: ignore[import-not-found]  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise RuntimeError(
                    "TURSO_DATABASE_URL is set but the Turso driver is missing. "
                    'Install it with: pip install -e ".[turso]"'
                ) from exc
            self._connect_factory = lambda: libsql.connect(  # noqa: PLC0415
                self.settings.turso_database_url,
                auth_token=self.settings.turso_auth_token or None,
            )
            self._backend = "turso"
        else:
            path = Path(self.settings.sqlite_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connect_factory = lambda: sqlite3.connect(  # noqa: PLC0415
                str(path), timeout=30.0, isolation_level=None
            )
            self._backend = "sqlite"

    @property
    def backend(self) -> str:
        return self._backend

    def connection(self) -> Any:
        """Return this thread's connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect_factory()
            # Row access by name, and enforce FKs where the backend supports it.
            with contextlib.suppress(Exception):
                conn.row_factory = sqlite3.Row
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA foreign_keys=ON")
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close this thread's connection and forget it.

        SQLite on Windows keeps the file locked until every connection object is
        released, so closing is explicit rather than relying on GC.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                log.debug("error closing database connection", exc_info=True)
            self._local.conn = None

    def close_all(self) -> None:
        """Close connections opened by every thread in this process."""
        for conn in list(self._connections):
            with contextlib.suppress(Exception):
                conn.close()
        self._connections.clear()
        self._local = threading.local()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close_all()

    # ------------------------------------------------------------- primitives
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Execute one statement and return the cursor."""
        conn = self.connection()
        attempts = 5 if not self._is_turso else 6
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                cur = conn.execute(sql, tuple(params))
                return cur
            except sqlite3.OperationalError as exc:  # pragma: no cover - transient
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message and "conflict" not in message:
                    raise
                last_error = exc
                time.sleep(0.15 * (attempt + 1))
        raise last_error  # type: ignore[misc]

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        conn = self.connection()
        with contextlib.suppress(Exception):
            conn.executemany(sql, [tuple(row) for row in rows])
            return
        for row in rows:  # pragma: no cover - fallback for drivers without executemany
            conn.execute(sql, tuple(row))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [self._to_dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return None
        return next(iter(row.values()), None)

    def execute_write(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a write statement and return ``lastrowid``/rowcount."""
        cur = self.execute(sql, params)
        rowid = getattr(cur, "lastrowid", 0) or 0
        cur.close()
        return int(rowid)

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return row
        return {key: row[key] for key in row.keys()}

    # ------------------------------------------------------------ transaction
    @contextlib.contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Explicit transaction.

        SQLite in WAL mode and libSQL both give us real transactions, so every
        multi-statement mutation (join + stats + audit + event) is atomic.
        """
        conn = self.connection()
        if self._is_turso:
            # libSQL has no client-controlled BEGIN; wrap in a write transaction
            # by issuing BEGIN IMMEDIATE / COMMIT on the same connection.
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except Exception:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # -------------------------------------------------------------- migrations
    def pending_migrations(self) -> list[Migration]:
        return load_migrations(self.settings.migrations_dir)

    def migrate(self, *, verbose: bool = True) -> list[str]:
        """Apply every pending migration.  Returns the filenames applied."""
        applied: list[str] = []
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename    TEXT PRIMARY KEY,
              checksum    TEXT NOT NULL,
              applied_at  INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        known = {
            row["filename"]: row["checksum"]
            for row in self.query("SELECT filename, checksum FROM schema_migrations")
        }
        for filename, sql in self.pending_migrations():
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:32]
            if filename in known:
                if known[filename] != checksum:
                    raise RuntimeError(
                        f"Migration {filename} was modified after being applied "
                        f"(expected {known[filename]}, got {checksum}). "
                        "Migrations are immutable - add a new file instead."
                    )
                continue
            started = time.perf_counter()
            statements, problems = _split_statements_checked(sql)
            if problems:
                raise RuntimeError(
                    f"Migration {filename} is malformed: " + "; ".join(problems)
                )
            with self.transaction() as db:
                for statement in statements:
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations (filename, checksum, applied_at, duration_ms)"
                    " VALUES (?, ?, ?, ?)",
                    (filename, checksum, int(time.time()), int((time.perf_counter() - started) * 1000)),
                )
            applied.append(filename)
            if verbose:
                log.info("applied migration %s", filename)
        return applied


def split_statements(sql: str) -> list[str]:
    """Split a migration file on ``; statement-breakpoint`` markers.

    Both the Python and the Node runner use this convention so a migration is a
    single portable artifact.
    """
    chunks, _ = _split_statements_checked(sql)
    return chunks


def _split_statements_checked(sql: str) -> tuple[list[str], list[str]]:
    """Split, and also report any chunk that still holds >1 statement.

    A missing separator is otherwise a confusing "You can only execute one
    statement at a time" error hours later, so it is surfaced explicitly.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        if line.strip().lower().startswith("; statement-breakpoint"):
            statement = "\n".join(current).strip()
            if statement:
                chunks.append(statement)
            current = []
            continue
        current.append(line)
    tail = "\n".join(current).strip()
    if tail:
        chunks.append(tail)
    chunks = [chunk for chunk in chunks if chunk and not _is_comment_only(chunk)]

    # Guard: count statements by semicolons that are not inside quotes. Comments
    # and trailing separators are ignored so `CREATE INDEX ...;` counts as one.
    problems: list[str] = []
    for index, chunk in enumerate(chunks):
        statements = _count_statements(chunk)
        if statements > 1:
            problems.append(
                f"chunk {index} contains {statements} statements; "
                "a '; statement-breakpoint' line is missing"
            )
    return chunks, problems


def _count_statements(sql: str) -> int:
    """Count top-level statements in a chunk, ignoring comments and quotes."""
    text = re.sub(r"--[^\n]*", "", sql)
    count = 0
    in_string = False
    previous = ""
    for char in text:
        if char == "'":
            if not in_string or previous != "\\":
                in_string = not in_string
        elif char == ";" and not in_string:
            count += 1
        previous = char
    return count


def _is_comment_only(statement: str) -> bool:
    body = [
        line.strip()
        for line in statement.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    return not body


def load_migrations(directory: Path) -> list[Migration]:
    """Load ``*.sql`` files from ``directory`` in filename order."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {directory}")
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        migrations.append((path.name, path.read_text(encoding="utf-8")))
    if not migrations:
        raise FileNotFoundError(f"No .sql migrations found in {directory}")
    return migrations


_database: Database | None = None


def get_database() -> Database:
    """Process-wide database singleton."""
    global _database
    if _database is None:
        _database = Database()
    return _database


def now_ms() -> int:
    return int(time.time() * 1000)