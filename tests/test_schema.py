"""Storage-contract tests against the real schema.

These do not re-implement the DDL: they ask SQLite what was actually created by
``init_schema`` and assert the shape the rest of the suite (and the dashboard)
relies on. A change here is a migration, not a refactor, so the assertions are
deliberately exact.
"""

from __future__ import annotations

import unittest

from tests.harness import HarnessTestCase

#: Every table ``init_schema`` is responsible for. ``sqlite_%`` internals are
#: filtered out before comparing, so this is the complete public surface.
EXPECTED_TABLES = {
    "simple_claims",
    "simple_entries",
    "simple_giveaway_ban_hits",
    "simple_giveaway_bans",
    "simple_giveaways",
    "simple_guild_settings",
    "simple_blacklist",
    "simple_message_counts",
}

#: One index per real access shape. The three redundant ones an earlier
#: revision declared are dropped by ``SCHEMA_DROPS`` and asserted absent below.
EXPECTED_INDEXES = {
    "idx_simple_claims_pending_due",
    "idx_simple_entries_gw_entered",
    "idx_simple_entries_user",
    "idx_simple_gw_guild_status_ended",
    "idx_simple_gw_guild_status_ends",
    "idx_simple_gw_status_ended",
    "idx_simple_gw_status_ends",
}

GIVEAWAY_COLUMNS = {
    "id",
    "guild_id",
    "channel_id",
    "message_id",
    "prize",
    "winner_count",
    "ends_at",
    "status",
    "required_role_id",
    "required_role_ids",
    "blocked_role_id",
    "min_account_age_days",
    "min_messages",
    "image_url",
    "entrants_role_id",
    "created_by",
    "host_id",
    "host_name",
    "created_at",
    "ended_at",
    "winners_json",
    "entrant_count",
    "claim_timeout_seconds",
}

ENTRY_COLUMNS = {"giveaway_id", "user_id", "username", "entered_at"}

#: The pre-v2 ``simple_giveaways`` shape: no ``required_role_ids`` and no
#: ``claim_timeout_seconds``. Live databases from that revision must be migrated
#: in place rather than rebuilt, so this is recreated verbatim.
LEGACY_GIVEAWAYS_DDL = """
CREATE TABLE simple_giveaways (
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
  entrant_count INTEGER
)
"""


class SchemaTests(HarnessTestCase):
    def _tables(self) -> set[str]:
        rows = self.h.sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row["name"] for row in rows if not row["name"].startswith("sqlite_")}

    def _indexes(self) -> set[str]:
        rows = self.h.sql("SELECT name FROM sqlite_master WHERE type = 'index'")
        return {row["name"] for row in rows if row["name"].startswith("idx_")}

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.h.sql(f"PRAGMA table_info({table})")}

    def _fingerprint(self) -> tuple[set[str], set[str], dict[str, set[str]], int]:
        tables = self._tables()
        return (
            tables,
            self._indexes(),
            {table: self._columns(table) for table in sorted(tables)},
            int(self.h.scalar("PRAGMA user_version")),
        )

    def test_init_schema_is_idempotent(self) -> None:
        before = self._fingerprint()
        gw = self.gw()

        # A second process boots on the same file. It runs init_schema too, and
        # that must be a no-op: same objects, same version, data untouched.
        sibling = self.sibling()

        self.assertEqual(before, self._fingerprint())
        self.assertEqual(gw["id"], sibling.call("get", giveaway_id=gw["id"])["id"])
        self.assertEqual(2, int(sibling.scalar("PRAGMA user_version")))

    def test_indexes_exist_including_user_lookup(self) -> None:
        indexes = self._indexes()
        self.assertEqual(EXPECTED_INDEXES, indexes)
        # Redundant with the entries primary key: every write pays for it and no
        # read needs it. It is dropped rather than left behind by old installs.
        self.assertNotIn("idx_simple_entries_giveaway", indexes)
        self.assertNotIn("idx_simple_msgcounts_pk", indexes)

    def test_expected_tables_exist(self) -> None:
        self.assertEqual(EXPECTED_TABLES, self._tables())

    def test_giveaway_columns_match_schema(self) -> None:
        self.assertEqual(GIVEAWAY_COLUMNS, self._columns("simple_giveaways"))

    def test_entry_columns_are_exactly_the_four(self) -> None:
        self.assertEqual(ENTRY_COLUMNS, self._columns("simple_entries"))

    def test_schema_version_is_stamped(self) -> None:
        self.assertEqual({"user_version": 2}, self.h.call("schema_version"))
        self.assertEqual(2, int(self.h.scalar("PRAGMA user_version")))

    def test_pragma_assignment_is_persisted(self) -> None:
        # ``PRAGMA x = y`` is a write: it must be committed like any INSERT or a
        # restart would forget it. ``application_id`` is used instead of
        # ``user_version`` because init_schema intentionally restamps the latter.
        self.h.call("exec", sql="PRAGMA user_version = 1")
        self.assertEqual(1, int(self.h.scalar("PRAGMA user_version")))
        self.h.call("exec", sql="PRAGMA application_id = 424242")

        sibling = self.sibling()
        self.assertEqual(424242, int(sibling.scalar("PRAGMA application_id")))

    def test_write_and_read_round_trip_persist_across_processes(self) -> None:
        self.h.call(
            "exec",
            sql="INSERT INTO simple_guild_settings (guild_id, notify_role_id) VALUES (?, ?)",
            params=["g", "r"],
        )
        sibling = self.sibling()
        self.assertEqual(
            "r",
            sibling.scalar("SELECT notify_role_id FROM simple_guild_settings WHERE guild_id = ?", ["g"]),
        )

    def test_raw_giveaway_defaults(self) -> None:
        # The service always names every column, so only a raw INSERT proves the
        # DDL defaults a hand-written or legacy row would fall back on.
        self.execute(
            "INSERT INTO simple_giveaways (id, guild_id, channel_id, prize, ends_at, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["raw", "g", "c", "Prize", 1234, "u", 1000],
        )
        row = self.h.one("SELECT * FROM simple_giveaways WHERE id = ?", ["raw"])
        assert row is not None
        self.assertEqual("active", row["status"])
        self.assertEqual("[]", row["winners_json"])
        self.assertEqual("[]", row["required_role_ids"])
        self.assertEqual(1, row["winner_count"])
        self.assertEqual(0, row["min_messages"])
        self.assertEqual(0, row["min_account_age_days"])
        self.assertEqual(0, row["claim_timeout_seconds"])
        self.assertIsNone(row["entrant_count"])
        self.assertIsNone(row["ended_at"])

    def test_legacy_schema_gains_missing_columns(self) -> None:
        self.execute("DROP TABLE simple_giveaways")
        self.execute(LEGACY_GIVEAWAYS_DDL)
        self.execute("PRAGMA user_version = 0")

        sibling = self.sibling()

        columns = self._columns("simple_giveaways")
        self.assertIn("required_role_ids", columns)
        self.assertIn("claim_timeout_seconds", columns)
        self.assertEqual(2, int(sibling.scalar("PRAGMA user_version")))
        # The full path also recreates the indexes the dropped table took with it.
        self.assertEqual(EXPECTED_INDEXES, self._indexes())

    def test_redundant_indexes_are_dropped(self) -> None:
        # An old install still carries these. Booting at the current version
        # short-circuits init_schema, so force the full path the way a real
        # upgrade would: drop the version stamp first.
        self.execute("CREATE INDEX idx_simple_entries_giveaway ON simple_entries(giveaway_id)")
        self.execute("CREATE INDEX idx_simple_msgcounts_pk ON simple_message_counts(guild_id, user_id)")
        self.execute("PRAGMA user_version = 0")

        self.sibling()

        indexes = self._indexes()
        self.assertNotIn("idx_simple_entries_giveaway", indexes)
        self.assertNotIn("idx_simple_msgcounts_pk", indexes)


if __name__ == "__main__":  # pragma: no cover - convenience for a single module
    unittest.main()
