//! Storage-contract tests against the real schema.
//!
//! These do not re-implement the DDL: they ask SQLite what was actually created
//! by `init_schema` and assert the shape the rest of the suite (and the
//! dashboard) relies on. A change here is a migration, not a refactor, so the
//! assertions are deliberately exact.

use std::collections::BTreeSet;

use gwbot_testkit::{Fixture, Harness};
use serde_json::{json, Value};

/// Every table `init_schema` is responsible for. `sqlite_%` internals are
/// filtered out before comparing, so this is the complete public surface.
const EXPECTED_TABLES: &[&str] = &[
    "simple_claims",
    "simple_entries",
    "simple_giveaway_ban_hits",
    "simple_giveaway_bans",
    "simple_giveaways",
    "simple_guild_settings",
    "simple_blacklist",
    "simple_message_counts",
];

/// One index per real access shape. The three redundant ones an earlier
/// revision declared are dropped by `SCHEMA_DROPS` and asserted absent below.
const EXPECTED_INDEXES: &[&str] = &[
    "idx_simple_claims_pending_due",
    "idx_simple_entries_gw_entered",
    "idx_simple_entries_user",
    "idx_simple_gw_guild_status_ended",
    "idx_simple_gw_guild_status_ends",
    "idx_simple_gw_status_ended",
    "idx_simple_gw_status_ends",
];

const GIVEAWAY_COLUMNS: &[&str] = &[
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
];

const ENTRY_COLUMNS: &[&str] = &["giveaway_id", "user_id", "username", "entered_at"];

/// The pre-v2 `simple_giveaways` shape: no `required_role_ids` and no
/// `claim_timeout_seconds`. Live databases from that revision must be migrated
/// in place rather than rebuilt, so this is recreated verbatim.
const LEGACY_GIVEAWAYS_DDL: &str = "
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
";

fn set(values: &[&str]) -> BTreeSet<String> {
    values.iter().map(|value| value.to_string()).collect()
}

fn names(rows: &Value, key: &str) -> BTreeSet<String> {
    rows.as_array()
        .expect("expected a row list")
        .iter()
        .filter_map(|row| row.get(key).and_then(Value::as_str))
        .map(str::to_string)
        .collect()
}

async fn tables(h: &Harness) -> BTreeSet<String> {
    let rows = h
        .sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        .await;
    names(&rows, "name")
        .into_iter()
        .filter(|name| !name.starts_with("sqlite_"))
        .collect()
}

async fn indexes(h: &Harness) -> BTreeSet<String> {
    let rows = h
        .sql("SELECT name FROM sqlite_master WHERE type = 'index'")
        .await;
    names(&rows, "name")
        .into_iter()
        .filter(|name| name.starts_with("idx_"))
        .collect()
}

async fn columns(h: &Harness, table: &str) -> BTreeSet<String> {
    let rows = h.sql(&format!("PRAGMA table_info({table})")).await;
    names(&rows, "name")
}

/// The whole created surface, so a second boot can be compared against the first.
async fn fingerprint(
    h: &Harness,
) -> (
    BTreeSet<String>,
    BTreeSet<String>,
    Vec<(String, BTreeSet<String>)>,
    i64,
) {
    let tables = tables(h).await;
    let mut shape = Vec::new();
    for table in &tables {
        shape.push((table.clone(), columns(h, table).await));
    }
    let version = h
        .scalar("PRAGMA user_version")
        .await
        .and_then(|value| value.as_i64())
        .unwrap_or(0);
    (tables, indexes(h).await, shape, version)
}

#[tokio::test(flavor = "multi_thread")]
async fn init_schema_is_idempotent() {
    let f = Fixture::new().await;
    let before = fingerprint(&f.h).await;
    let gw = f.gw(json!({})).await;

    // A second process boots on the same file. It runs init_schema too, and
    // that must be a no-op: same objects, same version, data untouched.
    let sibling = f.sibling().await;

    assert_eq!(before, fingerprint(&f.h).await);
    let seen = sibling.ok("get", json!({ "giveaway_id": gw["id"] })).await;
    assert_eq!(gw["id"], seen["id"]);
    assert_eq!(
        2,
        sibling
            .scalar("PRAGMA user_version")
            .await
            .and_then(|value| value.as_i64())
            .unwrap_or(0)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn indexes_exist_including_user_lookup() {
    let f = Fixture::new().await;
    let found = indexes(&f.h).await;
    assert_eq!(set(EXPECTED_INDEXES), found);
    // Redundant with the entries primary key: every write pays for it and no
    // read needs it. It is dropped rather than left behind by old installs.
    assert!(!found.contains("idx_simple_entries_giveaway"));
    assert!(!found.contains("idx_simple_msgcounts_pk"));
}

#[tokio::test(flavor = "multi_thread")]
async fn expected_tables_exist() {
    let f = Fixture::new().await;
    assert_eq!(set(EXPECTED_TABLES), tables(&f.h).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn giveaway_columns_match_schema() {
    let f = Fixture::new().await;
    assert_eq!(
        set(GIVEAWAY_COLUMNS),
        columns(&f.h, "simple_giveaways").await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn entry_columns_are_exactly_the_four() {
    let f = Fixture::new().await;
    assert_eq!(set(ENTRY_COLUMNS), columns(&f.h, "simple_entries").await);
}

#[tokio::test(flavor = "multi_thread")]
async fn schema_version_is_stamped() {
    let f = Fixture::new().await;
    assert_eq!(
        json!({ "user_version": 2 }),
        f.h.ok("schema_version", Value::Null).await
    );
    assert_eq!(
        2,
        f.h.scalar("PRAGMA user_version")
            .await
            .and_then(|value| value.as_i64())
            .unwrap_or(0)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn pragma_assignment_is_persisted() {
    let f = Fixture::new().await;
    // `PRAGMA x = y` is a write: it must be committed like any INSERT or a
    // restart would forget it. `application_id` is used instead of
    // `user_version` because init_schema intentionally restamps the latter.
    f.h.ok("exec", json!({ "sql": "PRAGMA user_version = 1" }))
        .await;
    assert_eq!(
        1,
        f.h.scalar("PRAGMA user_version")
            .await
            .and_then(|value| value.as_i64())
            .unwrap_or(0)
    );
    f.h.ok("exec", json!({ "sql": "PRAGMA application_id = 424242" }))
        .await;

    let sibling = f.sibling().await;
    assert_eq!(
        424242,
        sibling
            .scalar("PRAGMA application_id")
            .await
            .and_then(|value| value.as_i64())
            .unwrap_or(0)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn write_and_read_round_trip_persist_across_processes() {
    let f = Fixture::new().await;
    f.h.ok(
        "exec",
        json!({
            "sql": "INSERT INTO simple_guild_settings (guild_id, notify_role_id) VALUES (?, ?)",
            "params": ["g", "r"],
        }),
    )
    .await;
    let sibling = f.sibling().await;
    assert_eq!(
        json!("r"),
        sibling
            .scalar_params(
                "SELECT notify_role_id FROM simple_guild_settings WHERE guild_id = ?",
                json!(["g"]),
            )
            .await
            .unwrap_or(Value::Null)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn raw_giveaway_defaults() {
    let f = Fixture::new().await;
    // The service always names every column, so only a raw INSERT proves the
    // DDL defaults a hand-written or legacy row would fall back on.
    f.execute_params(
        "INSERT INTO simple_giveaways (id, guild_id, channel_id, prize, ends_at, created_by, created_at)\
         VALUES (?, ?, ?, ?, ?, ?, ?)",
        json!(["raw", "g", "c", "Prize", 1234, "u", 1000]),
    )
    .await;
    let row =
        f.h.one_params(
            "SELECT * FROM simple_giveaways WHERE id = ?",
            json!(["raw"]),
        )
        .await
        .expect("the raw row was inserted");

    assert_eq!(json!("active"), row["status"]);
    assert_eq!(json!("[]"), row["winners_json"]);
    assert_eq!(json!("[]"), row["required_role_ids"]);
    assert_eq!(json!(1), row["winner_count"]);
    assert_eq!(json!(0), row["min_messages"]);
    assert_eq!(json!(0), row["min_account_age_days"]);
    assert_eq!(json!(0), row["claim_timeout_seconds"]);
    assert!(row["entrant_count"].is_null());
    assert!(row["ended_at"].is_null());
}

#[tokio::test(flavor = "multi_thread")]
async fn legacy_schema_gains_missing_columns() {
    let f = Fixture::new().await;
    f.execute("DROP TABLE simple_giveaways").await;
    f.execute(LEGACY_GIVEAWAYS_DDL).await;
    f.execute("PRAGMA user_version = 0").await;

    let sibling = f.sibling().await;

    let found = columns(&f.h, "simple_giveaways").await;
    assert!(found.contains("required_role_ids"));
    assert!(found.contains("claim_timeout_seconds"));
    assert_eq!(
        2,
        sibling
            .scalar("PRAGMA user_version")
            .await
            .and_then(|value| value.as_i64())
            .unwrap_or(0)
    );
    // The full path also recreates the indexes the dropped table took with it.
    assert_eq!(set(EXPECTED_INDEXES), indexes(&f.h).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn redundant_indexes_are_dropped() {
    let f = Fixture::new().await;
    // An old install still carries these. Booting at the current version
    // short-circuits init_schema, so force the full path the way a real upgrade
    // would: drop the version stamp first.
    f.execute("CREATE INDEX idx_simple_entries_giveaway ON simple_entries(giveaway_id)")
        .await;
    f.execute("CREATE INDEX idx_simple_msgcounts_pk ON simple_message_counts(guild_id, user_id)")
        .await;
    f.execute("PRAGMA user_version = 0").await;

    f.sibling().await;

    let found = indexes(&f.h).await;
    assert!(!found.contains("idx_simple_entries_giveaway"));
    assert!(!found.contains("idx_simple_msgcounts_pk"));
}
