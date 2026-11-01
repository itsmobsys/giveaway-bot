//! Port of the retired Python `test_db.py`: what the retry policy may replay, and how
//! loudly it fails.
//!
//! The rules layer runs against a real SQLite database in the service suites.
//! This file covers the parts that exist only because Turso sits behind a
//! network: which failures are safe to replay, which are not, and what happens
//! when the schema cannot be migrated.
//!
//! Where Python injected a `ScriptedConnection`, these tests provoke the same
//! failure from a real SQLite connection: a trigger that raises the transport
//! error's text for writes, and a user-defined function that raises it for
//! reads. The assertion is always the observable policy -- how many attempts
//! the database layer actually made -- not a mock's bookkeeping.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use gwbot_core::config::Settings;
use gwbot_core::db::{is_ambiguous_loss, is_dead_stream, Database};
use rusqlite::functions::FunctionFlags;

/// A trigger that makes every entry INSERT fail with `message`.
///
/// `RAISE(ABORT, ...)` surfaces the text verbatim, so the retry policy sees the
/// same string a dead Turso stream would produce.
fn failing_insert_trigger(name: &str, message: &str) -> String {
    format!(
        "CREATE TRIGGER {name} BEFORE INSERT ON simple_entries \
         BEGIN SELECT RAISE(ABORT, '{message}'); END"
    )
}

const INSERT_ENTRY: &str = "INSERT INTO simple_entries \
     (giveaway_id, user_id, username, entered_at) VALUES ('gw_1', 'u_1', 'u', 1)";

#[tokio::test(flavor = "multi_thread")]
async fn a_write_lost_mid_request_is_never_replayed() {
    // "broken pipe" arrives while awaiting the response, so the server may
    // already have applied it: replaying count = count + 1 would double it.
    let db = Database::in_memory().expect("in-memory database");
    db.init_schema().await.expect("schema");
    db.execute(&failing_insert_trigger("boom", "broken pipe"), &[])
        .await
        .expect("trigger");
    db.reset_stats();

    let err = db
        .execute(INSERT_ENTRY, &[])
        .await
        .expect_err("the ambiguous write must surface");
    assert!(
        err.to_string().contains("broken pipe"),
        "unexpected error: {err}"
    );
    assert_eq!(
        1,
        db.stats().writes,
        "an ambiguous write must never be replayed"
    );
    let rows = db
        .query_one("SELECT COUNT(*) AS n FROM simple_entries", &[])
        .await
        .expect("count")
        .and_then(|row| row.int("n"));
    assert_eq!(Some(0), rows, "nothing may be left behind");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_write_the_server_never_saw_is_replayed() {
    // ADAPTED: Python counted `execute` calls on two scripted connections. A
    // real SQLite connection cannot be swapped mid-flight, so the trigger keeps
    // failing: the point is that the policy keeps *replaying* (six attempts)
    // instead of giving up after the first, and that the next real write still
    // commits where another connection can see it.
    let dir = tempfile::tempdir().expect("temp dir");
    let path = dir.path().join("replay.db");
    let path = path.to_str().expect("utf-8 path").to_string();
    let db = Database::open_local(&path).expect("local database");
    db.init_schema().await.expect("schema");
    db.execute(&failing_insert_trigger("dead", "stream not found"), &[])
        .await
        .expect("trigger");
    db.reset_stats();

    let err = db
        .execute(INSERT_ENTRY, &[])
        .await
        .expect_err("the replay must eventually give up");
    assert!(
        err.to_string().contains("stream not found"),
        "unexpected error: {err}"
    );
    assert_eq!(
        6,
        db.stats().writes,
        "a write the server never saw is replayed to the attempt cap"
    );

    db.execute("DROP TRIGGER dead", &[])
        .await
        .expect("drop trigger");
    let changed = db.execute(INSERT_ENTRY, &[]).await.expect("insert");
    assert_eq!(1, changed, "the write reports one row");
    // Another connection only sees the row if the write was committed, which is
    // the failure the Turso driver punishes: "created fine, restart forgets it".
    let other = Database::open_local(&path).expect("second connection");
    let visible = other
        .query_one("SELECT COUNT(*) AS n FROM simple_entries", &[])
        .await
        .expect("count")
        .and_then(|row| row.int("n"));
    assert_eq!(Some(1), visible, "the write must be committed, not pending");
}

/// A connection whose `flaky()` function raises `message` on the calls listed
/// in `fail_on` and returns `1` on every other call.
///
/// This is the read-side twin of the failing trigger: the statement itself is
/// fine, but the stream dies on the way back.
fn flaky_reads(
    fail_on: &'static [usize],
    message: &'static str,
) -> (rusqlite::Connection, Arc<AtomicUsize>) {
    let conn = rusqlite::Connection::open_in_memory().expect("in-memory connection");
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&calls);
    conn.create_scalar_function("flaky", 0, FunctionFlags::SQLITE_UTF8, move |_| {
        let call = counter.fetch_add(1, Ordering::SeqCst);
        if fail_on.contains(&call) {
            Err(rusqlite::Error::UserFunctionError(Box::new(
                std::io::Error::other(message),
            )))
        } else {
            Ok(1_i64)
        }
    })
    .expect("scalar function");
    (conn, calls)
}

#[tokio::test(flavor = "multi_thread")]
async fn a_read_lost_mid_request_is_replayed() {
    // The server may or may not have seen the statement; a read has no side
    // effect, so replaying it is always safe.
    let (conn, calls) = flaky_reads(&[0], "connection reset");
    let db = Database::from_connection(conn);
    db.reset_stats();

    let rows = db
        .query("SELECT flaky() AS n", &[])
        .await
        .expect("a lost read is replayed");
    assert_eq!(1, rows.len());
    assert_eq!(Some(1), rows[0].int("n"));
    assert_eq!(
        2,
        db.stats().reads,
        "the read must be attempted twice, not zero or six times"
    );
    assert_eq!(2, calls.load(Ordering::SeqCst));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_read_lost_while_fetching_rows_is_replayed() {
    // The stream can die after the statement succeeded but before all the rows
    // arrive. The first attempt yields one row and then dies, so the whole read
    // has to be replayed for the caller to see both.
    let (conn, calls) = flaky_reads(&[1], "connection reset");
    conn.execute_batch("CREATE TABLE t (n INTEGER); INSERT INTO t VALUES (1), (2);")
        .expect("seed rows");
    let db = Database::from_connection(conn);
    db.reset_stats();

    let rows = db
        .query("SELECT flaky() AS n FROM t", &[])
        .await
        .expect("a read lost mid-fetch is replayed");
    assert_eq!(2, rows.len(), "both rows must arrive after the replay");
    assert_eq!(2, db.stats().reads);
    assert_eq!(
        4,
        calls.load(Ordering::SeqCst),
        "two calls per attempt: the first row, then the one that died"
    );
}

#[test]
fn the_two_marker_sets_are_disjoint() {
    // A message in both sets would make the write policy ambiguous: "never
    // replayed" and "always replayed" cannot both be right.
    for message in [
        "stream not found",
        "stream was idle for too long",
        "not connected",
    ] {
        assert!(is_dead_stream(message), "{message}");
        assert!(!is_ambiguous_loss(message), "{message}");
    }
    for message in [
        "broken pipe",
        "connection reset",
        "connection aborted",
        "unexpected eof",
    ] {
        assert!(is_ambiguous_loss(message), "{message}");
        assert!(!is_dead_stream(message), "{message}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_file_url_is_refused_loudly() {
    // No injected connection here: this is the production path, where a file:
    // URL would silently lose data on Render.
    for url in ["file:./data.db", "file::memory:"] {
        let settings = Settings {
            turso_url: url.to_string(),
            ..Settings::default()
        };
        let err = Database::connect(&settings)
            .await
            .err()
            .unwrap_or_else(|| panic!("{url} must be refused"));
        assert!(
            err.to_string().contains("libsql://"),
            "{url}: unexpected error: {err}"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn injected_connections_still_work_for_tests() {
    // ADAPTED: Python reported `backend == "injected"`. Rust has one local
    // backend and reports it as "sqlite", so the assertion is that the injected
    // connection is recognised as local and answers queries regardless of any
    // URL, which is what the whole suite depends on.
    let conn = rusqlite::Connection::open_in_memory().expect("in-memory connection");
    let db = Database::from_connection(conn);
    assert!(db.is_local());
    assert_eq!("sqlite", db.backend());
    let row = db
        .query_one("SELECT 1 AS n", &[])
        .await
        .expect("query on the injected connection")
        .expect("one row");
    assert_eq!(Some(1), row.int("n"));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_column_check_that_fails_stops_startup_instead_of_being_skipped() {
    // ADAPTED: Python injected a connection whose `PRAGMA table_info` raised.
    // Rust has no injection seam for the migration path, so the table is
    // presented as a view instead: the column probe finds it, the ALTER cannot
    // touch it, and startup must stop rather than run on against an unmigrated
    // schema. SQLite's message for that ALTER does not repeat the table name,
    // so the assertions are the failure itself and the un-stamped version.
    let db = Database::in_memory().expect("in-memory database");
    db.execute("CREATE VIEW simple_giveaways AS SELECT 1 AS id", &[])
        .await
        .expect("stand-in view");
    let err = db
        .init_schema()
        .await
        .expect_err("startup must stop instead of skipping the migration");
    let message = err.to_string().to_lowercase();
    assert!(
        message.contains("column"),
        "the error must name what the migration could not do: {message}"
    );
    // A skipped migration would have stamped the version and never retried;
    // stopping means the next boot pays the full path again.
    let version = db
        .query_one("PRAGMA user_version", &[])
        .await
        .expect("version")
        .and_then(|row| row.int("user_version"));
    assert_eq!(
        Some(0),
        version,
        "an incomplete migration must not be recorded as done"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_existing_table_gains_entrant_count_and_keeps_its_rows() {
    // ADAPTED: Python used a shared-cache in-memory URI; a temp file is the
    // same thing with the extra guarantee that the row survives the reopen.
    let dir = tempfile::tempdir().expect("temp dir");
    let path = dir.path().join("migrate.db");
    let path = path.to_str().expect("utf-8 path").to_string();
    let db = Database::open_local(&path).expect("local database");
    // A table from before the column existed, holding a finished giveaway.
    db.execute(
        "CREATE TABLE simple_giveaways (id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, \
         channel_id TEXT NOT NULL, prize TEXT NOT NULL, ends_at INTEGER NOT NULL, \
         status TEXT NOT NULL DEFAULT 'active', created_by TEXT NOT NULL, \
         created_at INTEGER NOT NULL, ended_at INTEGER)",
        &[],
    )
    .await
    .expect("legacy table");
    db.execute(
        "INSERT INTO simple_giveaways VALUES ('gw_old', 'g', 'c', 'p', 1, 'ended', 'u', 1, 2)",
        &[],
    )
    .await
    .expect("legacy row");

    db.init_schema().await.expect("first migration");
    db.init_schema()
        .await
        .expect("the migration must be idempotent");

    let cols: Vec<String> = db
        .query("PRAGMA table_info(simple_giveaways)", &[])
        .await
        .expect("columns")
        .iter()
        .filter_map(|row| row.text("name"))
        .collect();
    assert!(
        cols.iter().any(|name| name == "entrant_count"),
        "entrant_count missing: {cols:?}"
    );
    let row = db
        .query_one(
            "SELECT entrant_count FROM simple_giveaways WHERE id = 'gw_old'",
            &[],
        )
        .await
        .expect("row")
        .expect("the finished giveaway survived");
    assert!(
        row.is_null("entrant_count"),
        "unknown for giveaways that ended before the counter existed"
    );
    let tables: Vec<String> = db
        .query("SELECT name FROM sqlite_master WHERE type = 'table'", &[])
        .await
        .expect("tables")
        .iter()
        .filter_map(|row| row.text("name"))
        .collect();
    assert!(
        tables.iter().any(|name| name == "simple_giveaway_ban_hits"),
        "ban hits table missing: {tables:?}"
    );
}
