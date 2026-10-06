"""Storage-layer tests: what the retry policy may replay, and how loudly it fails.

The rules layer runs against a real SQLite database in test_service.py. This file
covers the parts that exist only because Turso sits behind a network: which
failures are safe to replay, which are not, and what happens when the schema
cannot be read.
"""

from __future__ import annotations

import unittest

from giveaway_bot.config import Settings
from giveaway_bot.db import Database, _is_ambiguous_loss, _is_dead_stream


class ScriptedCursor:
    def __init__(self, rows: list[dict], error: str | None = None) -> None:
        self._rows = rows
        self._error = error
        self.description = [("n",)]

    def fetchall(self) -> list[dict]:
        if self._error is not None:
            raise ValueError(self._error)
        return list(self._rows)

    def close(self) -> None:
        pass


class ScriptedConnection:
    """A connection that fails exactly the statements it is told to fail."""

    def __init__(
        self,
        failures: dict[str, list[str]] | None = None,
        fetch_error: str | None = None,
    ) -> None:
        self.failures = failures or {}
        self.fetch_error = fetch_error
        self.statements: list[str] = []
        self.commits = 0
        self.closed = False

    def execute(self, sql, params=()) -> ScriptedCursor:
        self.statements.append(sql)
        for marker, pending in self.failures.items():
            if marker in sql and pending:
                raise ValueError(pending.pop(0))
        error = None
        if "SELECT" in sql:
            error, self.fetch_error = self.fetch_error, None
        return ScriptedCursor([{"n": 1}], error=error)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class ReplaySafetyTests(unittest.TestCase):
    """A statement may only be replayed when its effect is known."""

    def database(self, *connections: ScriptedConnection) -> Database:
        remaining = list(connections)

        def connect() -> ScriptedConnection:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        return Database(Settings(turso_url=""), connect=connect)

    @staticmethod
    def statements(conn: ScriptedConnection, needle: str) -> list[str]:
        return [sql for sql in conn.statements if needle in sql]

    def test_a_write_lost_mid_request_is_never_replayed(self) -> None:
        # "broken pipe" arrives while awaiting the response, so the server may
        # already have applied it: replaying count = count + 1 would double it.
        conn = ScriptedConnection({"simple_message_counts": ["broken pipe"]})
        with self.assertRaisesRegex(ValueError, "broken pipe"):
            self.database(conn).execute("UPDATE simple_message_counts SET count = count + 1")
        self.assertEqual(len(self.statements(conn, "simple_message_counts")), 1)
        self.assertTrue(conn.closed, "the dead connection is still recycled")

    def test_a_write_the_server_never_saw_is_replayed(self) -> None:
        dead = ScriptedConnection({"simple_message_counts": ["stream not found"]})
        live = ScriptedConnection({})
        self.database(dead, live).execute("UPDATE simple_message_counts SET count = count + 1")
        self.assertEqual(len(self.statements(dead, "simple_message_counts")), 1)
        self.assertEqual(len(self.statements(live, "simple_message_counts")), 1)
        self.assertEqual(live.commits, 1, "the replayed write still commits")

    def test_a_read_lost_mid_request_is_replayed(self) -> None:
        dead = ScriptedConnection({"simple_giveaways": ["connection reset"]})
        live = ScriptedConnection({})
        rows = self.database(dead, live).query("SELECT n FROM simple_giveaways")
        self.assertEqual(rows, [{"n": 1}])
        self.assertEqual(len(self.statements(dead, "simple_giveaways")), 1)
        self.assertEqual(len(self.statements(live, "simple_giveaways")), 1)

    def test_a_read_lost_while_fetching_rows_is_replayed(self) -> None:
        # The stream can die after the statement succeeded but before the rows
        # arrive. A read has no side effect, so replaying it is still safe.
        flaky = ScriptedConnection(fetch_error="connection reset")
        live = ScriptedConnection({})
        rows = self.database(flaky, live).query("SELECT n FROM simple_giveaways")
        self.assertEqual(rows, [{"n": 1}])
        self.assertEqual(len(self.statements(flaky, "simple_giveaways")), 1)
        self.assertEqual(len(self.statements(live, "simple_giveaways")), 1)

    def test_the_two_marker_sets_are_disjoint(self) -> None:
        for message in ("stream not found", "stream was idle for too long", "not connected"):
            self.assertTrue(_is_dead_stream(message), message)
            self.assertFalse(_is_ambiguous_loss(message), message)
        for message in ("broken pipe", "connection reset", "connection aborted", "unexpected eof"):
            self.assertTrue(_is_ambiguous_loss(message), message)
            self.assertFalse(_is_dead_stream(message), message)


class MigrationTests(unittest.TestCase):
    def test_a_column_check_that_fails_stops_startup_instead_of_being_skipped(self) -> None:
        conn = ScriptedConnection({"table_info": ["unable to open database file"]})
        db = Database(Settings(turso_url=""), connect=lambda: conn)
        with (
            self.assertLogs("giveaway_bot.db", level="ERROR") as captured,
            self.assertRaisesRegex(ValueError, "unable to open"),
        ):
            db.init_schema()
        self.assertIn("simple_giveaways", captured.output[0])


if __name__ == "__main__":
    unittest.main()
