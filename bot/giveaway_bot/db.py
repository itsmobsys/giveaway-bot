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
import dataclasses
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


#: Substrings that mean the connection's underlying stream is gone. Turso speaks
#: Hrana over HTTP, and the client holds a ``Connection`` that survives the server
#: dropping the stream, so these have to be detected from the message text: the
#: driver raises a bare ``ValueError`` and exports no exception hierarchy to match.
_DEAD_CONNECTION_MARKERS = (
    "stream not found",
    "stream was idle for too long",
    "no transaction is active",
    "connection closed",
    "channel closed",
    "not connected",
    "no current connection",
    "broken pipe",
    "connection reset",
    "connection aborted",
    "server closed",
    "unexpected eof",
    "socket is closed",
)


def _is_dead_connection(message: str) -> bool:
    return any(marker in message for marker in _DEAD_CONNECTION_MARKERS)


def _brief(exc: Exception, limit: int = 120) -> str:
    """Single-line, length-capped form of an error, safe to log."""
    text = " ".join(str(exc).split())
    return text[:limit]


@dataclasses.dataclass
class _TxState:
    """Transaction bookkeeping for exactly one connection.

    Held in the same thread-local as the connection it describes, so the depth
    can never refer to a different connection than the one statements go to.
    That identity is the whole point: the previous code tracked nothing, so a
    transaction left open on a connection was invisible, and the next statement
    issued on that connection failed with "cannot start a transaction within a
    transaction".
    """

    connection: Any
    depth: int = 0
    retired: bool = False

    @property
    def nested(self) -> bool:
        return self.depth > 0


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
        #: Transaction bookkeeping per live connection, keyed by id(conn).
        self._states: dict[int, _TxState] = {}
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
        """Return this thread's connection, creating it on first use.

        A connection is thread-confined: exactly one thread can reach it at a
        time. That is what makes the scheduler safe. Every job runs in
        ``asyncio.to_thread``, so each worker thread gets its own connection and
        no two threads can ever be inside a transaction on the same one. It also
        means a thread is reused for unrelated later work, which is why the
        connection has to be left in a clean state on the way out - see
        :meth:`transaction`.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect_factory()
            # Row access by name is handled in _to_dict from the cursor
            # description, not by asking the driver for sqlite3.Row: libSQL has no
            # row_factory attribute at all, so that assignment was a silent no-op
            # there and only SQLite ever produced named rows. Doing it in one place
            # means both backends take the identical path.
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA foreign_keys=ON")
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            state = _TxState(connection=conn)
            self._local.tx = state
            with self._lock:
                self._connections.append(conn)
                # Keyed by connection rather than held only in the owning thread's
                # local, so close_all() can mark another thread's state retired.
                # id() is a safe key because _connections holds a strong reference
                # for as long as the entry exists.
                self._states[id(conn)] = state
        return conn

    def _tx_state(self, conn: Any) -> _TxState:
        """The transaction state belonging to this connection."""
        with self._lock:
            state = self._states.get(id(conn))
            if state is None:
                state = _TxState(connection=conn)
                self._states[id(conn)] = state
        self._local.tx = state
        return state

    def in_transaction(self) -> bool:
        """True while this thread holds an open transaction on its connection."""
        state = getattr(self._local, "tx", None)
        return bool(state is not None and state.nested)

    def transaction_depth(self) -> int:
        """How many transaction blocks are open on this thread's connection."""
        state = getattr(self._local, "tx", None)
        return 0 if state is None else state.depth

    def _retire(self, conn: Any, reason: str, *, broken: bool) -> None:
        """Stop using this connection: close it and drop every reference to it.

        Called either on purpose (a normal close) or because the connection got
        into a state nobody can reason about - usually a transaction whose
        COMMIT/ROLLBACK itself failed. The broken case logs at ERROR and is the
        one that matters: the alternative is handing the next operation a
        connection that may still hold a transaction, so its own BEGIN fails with
        "cannot start a transaction within a transaction". That error surfaces
        against code which has nothing to do with whatever went wrong earlier,
        which is what made this so hard to trace.
        """
        verb = "discarding" if broken else "closing"
        (log.error if broken else log.debug)("%s database connection: %s", verb, reason)
        with self._lock:
            with contextlib.suppress(ValueError):
                self._connections.remove(conn)
            state = self._states.pop(id(conn), None)
        if state is not None:
            # Whatever transaction this state described died with the connection.
            state.retired = True
            state.depth = 0
        with contextlib.suppress(Exception):
            conn.close()
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
            self._local.tx = None

    def close(self) -> None:
        """Close this thread's connection and forget it.

        SQLite on Windows keeps the file locked until every connection object is
        released, so closing is explicit rather than relying on GC.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            state = getattr(self._local, "tx", None)
            if state is not None and state.nested:
                # Reaching here means a transaction block was abandoned without
                # being closed out. Undo it before the connection goes, so the
                # work cannot be left half-applied.
                log.error(
                    "closing a connection that still has an open transaction (depth %d)", state.depth
                )
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
            self._retire(conn, "close() called", broken=False)
            self._local.conn = None

    def close_all(self) -> None:
        """Close connections opened by every thread in this process."""
        for conn in list(self._connections):
            with contextlib.suppress(Exception):
                self._retire(conn, "close_all() called", broken=False)
        with self._lock:
            self._connections.clear()
            self._states.clear()
        self._local = threading.local()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close_all()

    # ------------------------------------------------------------- primitives
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Execute one statement and return the cursor.

        Two distinct failures are handled, and neither hides an error:

        * **A dead connection.** Turso closes an idle Hrana stream after a while,
          and closes it outright on a server restart or a network blip. The
          client keeps its ``Connection`` object, so with no intervention every
          later statement on that connection fails with
          ``ValueError: Hrana: ... stream not found`` - forever, until the
          process restarts. The connection is retired and replaced, once per
          statement. A dead stream means the statement never reached the server,
          so replaying it cannot double-apply anything, and if the *replacement*
          also fails then the error surfaces normally.
        * **Genuine writer contention** that busy_timeout did not absorb, which
          is retried with a short backoff exactly as before.
        """
        attempts = 5 if not self._is_turso else 6
        last_error: Exception | None = None
        reconnected = False
        for attempt in range(attempts):
            # Re-read per attempt so a retired connection is never reused. This
            # also fixes a pre-existing flaw: the transient retry re-ran the
            # statement on the very connection that had just failed it.
            conn = self.connection()
            try:
                return conn.execute(sql, tuple(params))
            except Exception as exc:  # noqa: BLE001 - re-raised unless recoverable
                message = str(exc).lower()
                if not reconnected and _is_dead_connection(message):
                    log.warning(
                        "database stream went away (%s); replacing the connection", _brief(exc)
                    )
                    self._retire(conn, f"hrana stream lost: {_brief(exc)}", broken=True)
                    reconnected = True
                    continue
                # Matching on sqlite3.OperationalError was SQLite-only: libSQL
                # raises its own exception types and exports no OperationalError,
                # so on Turso this retry could never fire and a transient
                # lock/conflict surfaced as a hard failure. The message is the only
                # portable signal, so it is matched directly and anything else is
                # re-raised untouched.
                message = str(exc).lower()
                transient = any(word in message for word in ("locked", "busy", "conflict"))
                if not transient:
                    raise
                last_error = exc
                time.sleep(0.15 * (attempt + 1))
        raise last_error  # type: ignore[misc]

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement per row.

        The row-at-a-time fallback is chosen by what the driver offers, not by
        catching an exception. Catching it and retrying every row re-applies the
        prefix executemany had already written before it failed, which silently
        double-writes instead of reporting the error.
        """
        if not rows:
            return
        conn = self.connection()
        params = [tuple(row) for row in rows]
        if hasattr(conn, "executemany"):
            conn.executemany(sql, params)
            return
        for row in params:  # pragma: no cover - drivers without executemany
            conn.execute(sql, row)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        columns = _cursor_columns(cur)
        cur.close()
        return [self._to_dict(row, columns) for row in rows]

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
    def _to_dict(row: Any, columns: Sequence[str] | None = None) -> dict[str, Any]:
        """Map one result row to a dict, whatever the driver hands back.

        This is the seam that differs between the two backends. SQLite can be
        asked for sqlite3.Row and then supports row.keys(); libSQL has no
        row_factory at all and returns plain tuples, so calling row.keys()
        unconditionally raised AttributeError: 'tuple' object has no attribute
        'keys' the first time a hosted database was queried. Both drivers do
        expose cursor.description, so the names are taken from there and zipped
        against the row. The keys() path is kept for any row that does offer it.
        """
        if isinstance(row, dict):
            return row
        keys = getattr(row, "keys", None)
        if callable(keys):
            return {key: row[key] for key in keys()}
        if columns:
            # strict=True so a driver that returns the wrong number of values
            # fails here, loudly, instead of silently dropping trailing columns
            # and producing a dict that is missing keys.
            return dict(zip(columns, row, strict=True))
        raise TypeError(
            f"cannot map a {type(row).__name__} row to column names: the cursor "
            "exposed no description"
        )

    # ------------------------------------------------------------ transaction
    @contextlib.contextmanager
    def transaction(self) -> Iterator[Database]:
        """Run a block inside a transaction, atomically.

        SQLite in WAL mode and libSQL both give real transactions, so every
        multi-statement mutation (join + stats + audit + event) is atomic.

        Two properties matter more than the SQL:

        **Nesting is legal.** A ``transaction()`` opened inside an already-open
        one joins the existing transaction as a SAVEPOINT instead of issuing a
        second BEGIN, which SQLite refuses with "cannot start a transaction
        within a transaction". The outermost block owns the real BEGIN and
        COMMIT; a nested block's failure discards only its own savepoint and
        leaves the enclosing work intact.

        **The transaction is always closed out.** Return, raise, or a
        BaseException such as ``asyncio.CancelledError``, ``KeyboardInterrupt``
        or ``SystemExit`` all reach the COMMIT/ROLLBACK path. The old code caught
        only ``Exception``, so a BaseException skipped both and left the
        transaction open - and since a connection is thread-confined and threads
        are pooled, the next unrelated job the pool scheduled onto that thread
        failed on its own BEGIN.
        """
        conn = self.connection()
        state = self._tx_state(conn)
        depth = state.depth
        savepoint = f"giveaway_bot_sp_{depth}"

        if depth == 0:
            # BEGIN IMMEDIATE takes the write lock up front so two writers cannot
            # both read-then-write; busy_timeout (set on connect) is what makes
            # that wait instead of failing.
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception as exc:  # noqa: BLE001 - inspected, then re-raised
                # Turso drops an idle Hrana stream server-side ("stream was idle
                # for too long", "stream not found"). The next BEGIN on that
                # stream then fails even though the connection object looks fine.
                # Nothing was applied, so retiring and retrying once on a fresh
                # stream is safe; anything else propagates.
                if _is_dead_connection(str(exc).lower()):
                    log.warning(
                        "database stream died before BEGIN (%s); reconnecting once",
                        _brief(exc),
                    )
                    self._retire(conn, f"dead stream at BEGIN: {_brief(exc)}", broken=True)
                    conn = self.connection()
                    state = self._tx_state(conn)
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    raise
        else:
            conn.execute(f'SAVEPOINT "{savepoint}"')

        state.depth = depth + 1
        try:
            yield self
        except BaseException:
            self._close_out(conn, state, depth=depth, savepoint=savepoint, commit=False)
            raise
        if not self._close_out(conn, state, depth=depth, savepoint=savepoint, commit=True):
            # A failed COMMIT means the server discarded the whole transaction
            # (Turso rolls back idle interactive transactions), so reporting
            # success would lie: join() would say "you're in" with no row saved.
            # Raising lets the caller answer "try again", and the retry is safe
            # because nothing from this block was applied.
            raise RuntimeError("database commit failed; retry the operation")

    def _close_out(
        self, conn: Any, state: _TxState, *, depth: int, savepoint: str, commit: bool
    ) -> bool:
        """COMMIT or ROLLBACK, and restore the nesting depth.

        Returns True when the close-out succeeded. A COMMIT that fails leaves
        the connection in a state nobody can reason about, so it is retired and
        the failure logged at ERROR, and False is returned so the caller can
        raise instead of reporting uncommitted work as done. A failed ROLLBACK
        only means "nothing was applied", which is already the safe state, so it
        stays silent. Never raises: raising from here on the exception path
        would replace the exception the caller was already propagating, and that
        one is the one worth reading.
        """
        if state.retired:
            # The connection was already closed - by close(), close_all(), or an
            # earlier failed close-out. There is nothing left to commit or roll
            # back, and asking a closed connection only raises "Cannot operate on
            # a closed database", which would be reported as though the commit
            # itself had failed.
            log.debug(
                "not closing out a transaction: its connection was already retired (depth %d)", depth
            )
            state.depth = depth
            if commit:
                # The transaction never reached COMMIT on any live connection:
                # either the connection died mid-block (later statements ran
                # unprotected on a replacement) or it was closed outright. The
                # caller must hear about it instead of assuming atomicity.
                log.error("transaction lost its connection before COMMIT; reporting failure")
            return not commit

        verb = "commit" if commit else "roll back"
        try:
            if depth == 0:
                conn.execute("COMMIT" if commit else "ROLLBACK")
            elif commit:
                conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
            else:
                conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')
                conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
        except Exception:  # noqa: BLE001 - reported and the connection retired
            log.error("could not %s the database transaction", verb, exc_info=True)
            self._retire(conn, f"could not {verb} its transaction", broken=True)
            return not commit
        finally:
            state.depth = depth
        return True

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


def _cursor_columns(cursor: Any) -> list[str] | None:
    """Column names from a DB-API cursor description, or None if unavailable.

    Description entries are 7-tuples per PEP 249, but this tolerates shorter ones
    so an unusual driver cannot break every read.
    """
    description = getattr(cursor, "description", None)
    if not description:
        return None
    names: list[str] = []
    for item in description:
        if isinstance(item, (tuple, list)) and item:
            names.append(str(item[0]))
        else:
            names.append(str(item))
    return names


def load_migrations(directory: Path) -> list[Migration]:
    """Load ``*.sql`` files from ``directory`` in filename order."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {directory}")
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        # utf-8-sig drops a leading BOM, and the replace() catches one anywhere
        # else in the file. A BOM is not whitespace, so it becomes part of the
        # first statement's text: SQLite happens to ignore it, but Turso's parser
        # rejects the statement outright, which made a hosted database fail on a
        # migration that had passed every local test.
        text = path.read_text(encoding="utf-8-sig")
        migrations.append((path.name, text.replace("\ufeff", "")))
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