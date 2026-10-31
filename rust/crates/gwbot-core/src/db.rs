//! Turso (libSQL) storage, with the same failure semantics as the Python bot.
//!
//! Deliberately no local-file fallback in production: on Render a local file is
//! wiped on every redeploy, which silently "forgot" all giveaways. A missing
//! `TURSO_DATABASE_URL` is a hard error, so that becomes one loud line instead
//! of data loss. The only local backend is [`Database::in_memory`], used by
//! tests and the JSON harness.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Result};
use tokio::sync::Mutex;

use crate::config::Settings;

/// Substrings meaning the stream is gone and the statement never ran: the
/// server rejected a stream it no longer knows (idle timeout, restart), or the
/// client never had a usable connection to send on. Nothing was applied, so
/// replaying either a read or a write is safe.
const UNAPPLIED_MARKERS: &[&str] = &[
    "stream not found",
    "stream was idle for too long",
    "no transaction is active",
    "connection closed",
    "not connected",
];

/// Substrings meaning the socket died *mid-request*: the server may already
/// have applied the statement before the response was lost. A read can be
/// replayed; a write must not be, because replaying `count = count + 1` or
/// `ends_at = ends_at + ?` would apply it a second time.
const AMBIGUOUS_MARKERS: &[&str] = &[
    "broken pipe",
    "connection reset",
    "connection aborted",
    "server closed",
    "unexpected eof",
];

/// Schema revision recorded in the database's own `PRAGMA user_version`.
/// Bump it on any change to the DDL below; the next boot re-runs everything
/// (all of it is idempotent).
pub const SCHEMA_VERSION: i64 = 2;

pub const TABLE_GIVEAWAYS: &str = "simple_giveaways";
pub const TABLE_ENTRIES: &str = "simple_entries";

const SCHEMA_TABLES: &str = r#"
CREATE TABLE IF NOT EXISTS simple_giveaways (
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
CREATE TABLE IF NOT EXISTS simple_entries (
  giveaway_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  entered_at INTEGER NOT NULL,
  PRIMARY KEY (giveaway_id, user_id)
);
"#;

/// Indexes an earlier revision declared that only repeated a primary-key
/// prefix or were superseded by a wider one below. They are dropped rather
/// than left in place: a redundant index costs nothing to read through but
/// taxes every INSERT and DELETE on that table, and the entry and
/// message-count tables see the most writes.
const SCHEMA_DROPS: &str = r#"
DROP INDEX IF EXISTS idx_simple_entries_giveaway;
DROP INDEX IF EXISTS idx_simple_msgcounts_pk;
DROP INDEX IF EXISTS idx_simple_bans_cover;
DROP INDEX IF EXISTS idx_simple_banhits_cover;
DROP INDEX IF EXISTS idx_simple_bl_cover;
"#;

/// One index per real access shape, each ordered so the statement that uses it
/// needs no temporary sort.
const SCHEMA_INDEXES: &str = r#"
CREATE INDEX IF NOT EXISTS idx_simple_gw_status_ends ON simple_giveaways(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_status_ended ON simple_giveaways(status, ended_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_guild_status_ends
ON simple_giveaways(guild_id, status, ends_at);
CREATE INDEX IF NOT EXISTS idx_simple_gw_guild_status_ended
ON simple_giveaways(guild_id, status, ended_at);
CREATE INDEX IF NOT EXISTS idx_simple_entries_gw_entered ON simple_entries(giveaway_id, entered_at);
CREATE INDEX IF NOT EXISTS idx_simple_entries_user ON simple_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_simple_claims_pending_due
ON simple_claims(deadline_ms) WHERE status = 'pending';
"#;

const MESSAGE_COUNTS_DDL: &str = "CREATE TABLE IF NOT EXISTS simple_message_counts (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
)";

const GUILD_SETTINGS_DDL: &str = "CREATE TABLE IF NOT EXISTS simple_guild_settings (
  guild_id TEXT PRIMARY KEY,
  notify_role_id TEXT
)";

const BLACKLIST_DDL: &str = "CREATE TABLE IF NOT EXISTS simple_blacklist (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  expires_at INTEGER,
  reason TEXT NOT NULL DEFAULT '',
  banned_by TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
)";

const GIVEAWAY_BANS_DDL: &str = "CREATE TABLE IF NOT EXISTS simple_giveaway_bans (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  giveaways_remaining INTEGER NOT NULL DEFAULT 0,
  last_blocked_giveaway_id TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
)";

const GIVEAWAY_BAN_HITS_DDL: &str = "CREATE TABLE IF NOT EXISTS simple_giveaway_ban_hits (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  giveaway_id TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id, giveaway_id)
)";

const CLAIMS_DDL: &str = "CREATE TABLE IF NOT EXISTS simple_claims (
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
)";

/// True when the statement provably never reached the server.
pub fn is_dead_stream(message: &str) -> bool {
    UNAPPLIED_MARKERS.iter().any(|m| message.contains(m))
}

/// True when the connection dropped and the statement's fate is unknown.
pub fn is_ambiguous_loss(message: &str) -> bool {
    AMBIGUOUS_MARKERS.iter().any(|m| message.contains(m))
}

/// True for statements that change data and need a commit under libsql.
///
/// libsql opens an implicit transaction around writes and never auto-commits.
/// Forgetting the commit means the writing thread sees its own row while every
/// other connection sees nothing — the exact "created fine, join says not
/// found, restart forgets everything" failure. SELECTs/PRAGMAs commit nothing.
pub fn is_write(sql: &str) -> bool {
    let trimmed = sql.trim_start();
    let Some(head) = trimmed.split_whitespace().next() else {
        return false;
    };
    let head = head.to_ascii_uppercase();
    if head == "PRAGMA" {
        // `PRAGMA user_version = 2` assigns and needs the commit like any
        // other write; a bare pragma only reads.
        return sql.contains('=');
    }
    // WITH counts as a write on purpose: a CTE can head an INSERT/UPDATE/DELETE.
    // Committing a read costs nothing, while forgetting to commit a write
    // silently loses it.
    !matches!(head.as_str(), "SELECT" | "EXPLAIN" | "VALUES")
}

/// One bound parameter, or one column value.
#[derive(Debug, Clone, PartialEq)]
pub enum SqlValue {
    Null,
    Integer(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
}

impl SqlValue {
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            SqlValue::Integer(v) => Some(*v),
            SqlValue::Real(v) => Some(*v as i64),
            SqlValue::Text(s) => s.trim().parse().ok(),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            SqlValue::Integer(v) => Some(*v as f64),
            SqlValue::Real(v) => Some(*v),
            SqlValue::Text(s) => s.trim().parse().ok(),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            SqlValue::Text(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, SqlValue::Null)
    }
}

impl From<&str> for SqlValue {
    fn from(v: &str) -> Self {
        SqlValue::Text(v.to_string())
    }
}

impl From<String> for SqlValue {
    fn from(v: String) -> Self {
        SqlValue::Text(v)
    }
}

impl From<i64> for SqlValue {
    fn from(v: i64) -> Self {
        SqlValue::Integer(v)
    }
}

impl From<i32> for SqlValue {
    fn from(v: i32) -> Self {
        SqlValue::Integer(v as i64)
    }
}

impl From<f64> for SqlValue {
    fn from(v: f64) -> Self {
        SqlValue::Real(v)
    }
}

impl From<bool> for SqlValue {
    fn from(v: bool) -> Self {
        SqlValue::Integer(i64::from(v))
    }
}

impl<T: Into<SqlValue>> From<Option<T>> for SqlValue {
    fn from(v: Option<T>) -> Self {
        match v {
            Some(inner) => inner.into(),
            None => SqlValue::Null,
        }
    }
}

/// A result row addressed by column name, like the dict rows the Python
/// service works with.
#[derive(Debug, Clone, Default)]
pub struct Row {
    cols: Vec<String>,
    vals: Vec<SqlValue>,
}

impl Row {
    pub fn new(cols: Vec<String>, vals: Vec<SqlValue>) -> Self {
        Self { cols, vals }
    }

    pub fn get(&self, name: &str) -> Option<&SqlValue> {
        self.cols
            .iter()
            .position(|c| c == name)
            .and_then(|idx| self.vals.get(idx))
    }

    /// Text value, or `None` for a missing column or NULL.
    pub fn text(&self, name: &str) -> Option<String> {
        match self.get(name)? {
            SqlValue::Null => None,
            SqlValue::Text(s) => Some(s.clone()),
            SqlValue::Integer(v) => Some(v.to_string()),
            SqlValue::Real(v) => Some(v.to_string()),
            SqlValue::Blob(b) => Some(String::from_utf8_lossy(b).to_string()),
        }
    }

    /// Integer value, or `None` for a missing column or NULL.
    pub fn int(&self, name: &str) -> Option<i64> {
        self.get(name).and_then(SqlValue::as_i64)
    }

    /// Integer with a fallback, matching `int(row.get(x) or 0)`.
    pub fn int_or(&self, name: &str, default: i64) -> i64 {
        self.int(name).unwrap_or(default)
    }

    pub fn float(&self, name: &str) -> Option<f64> {
        self.get(name).and_then(SqlValue::as_f64)
    }

    pub fn is_null(&self, name: &str) -> bool {
        matches!(self.get(name), None | Some(SqlValue::Null))
    }

    pub fn columns(&self) -> &[String] {
        &self.cols
    }

    pub fn values(&self) -> &[SqlValue] {
        &self.vals
    }
}

/// DB-only attempt counters (Turso round-trips, not Discord/total).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DbStats {
    pub reads: u64,
    pub writes: u64,
    pub read_ms: u64,
    pub write_ms: u64,
}

#[derive(Default)]
struct Stats {
    reads: AtomicU64,
    writes: AtomicU64,
    read_us: AtomicU64,
    write_us: AtomicU64,
}

impl Stats {
    fn record(&self, write: bool, elapsed: Duration) {
        let us = elapsed.as_micros().min(u64::MAX as u128) as u64;
        if write {
            self.writes.fetch_add(1, Ordering::Relaxed);
            self.write_us.fetch_add(us, Ordering::Relaxed);
        } else {
            self.reads.fetch_add(1, Ordering::Relaxed);
            self.read_us.fetch_add(us, Ordering::Relaxed);
        }
    }

    fn snapshot(&self) -> DbStats {
        DbStats {
            reads: self.reads.load(Ordering::Relaxed),
            writes: self.writes.load(Ordering::Relaxed),
            read_ms: self.read_us.load(Ordering::Relaxed) / 1000,
            write_ms: self.write_us.load(Ordering::Relaxed) / 1000,
        }
    }

    fn reset(&self) {
        self.reads.store(0, Ordering::Relaxed);
        self.writes.store(0, Ordering::Relaxed);
        self.read_us.store(0, Ordering::Relaxed);
        self.write_us.store(0, Ordering::Relaxed);
    }
}

/// Where the bytes live. Remote is production; Local is tests only.
enum Backend {
    Remote {
        db: Arc<libsql::Database>,
        /// Idle connections, reused like the Python driver's per-thread ones.
        /// Cloning a [`libsql::Connection`] shares a stream, so this holds
        /// independent streams created by `Database::connect`.
        idle: Mutex<Vec<libsql::Connection>>,
    },
    Local {
        conn: Arc<StdMutex<rusqlite::Connection>>,
    },
}

/// Maximum idle remote connections kept for reuse. A burst above this opens
/// extra streams and drops them on return instead of pinning memory forever.
const IDLE_POOL_MAX: usize = 16;

pub struct Database {
    backend: Backend,
    stats: Stats,
    /// Kept for `doctor` and for the production-URL guard's error text.
    settings: Settings,
}

impl Database {
    /// Production constructor: remote Turso only.
    pub async fn connect(settings: &Settings) -> Result<Self> {
        if settings.turso_url.is_empty() {
            bail!(
                "TURSO_DATABASE_URL is not set. This bot stores everything in Turso so \
                 restarts never lose data — set TURSO_DATABASE_URL (and TURSO_AUTH_TOKEN) \
                 and restart."
            );
        }
        if settings.turso_url.starts_with("file:") {
            bail!(
                "TURSO_DATABASE_URL must be a remote libsql:// (or https://) URL: a file: \
                 database is wiped on every Render redeploy."
            );
        }
        let db =
            libsql::Builder::new_remote(settings.turso_url.clone(), settings.turso_token.clone())
                .build()
                .await
                .map_err(|e| anyhow!("could not open Turso database: {e}"))?;
        Ok(Self {
            backend: Backend::Remote {
                db: Arc::new(db),
                idle: Mutex::new(Vec::new()),
            },
            stats: Stats::default(),
            settings: settings.clone(),
        })
    }

    /// Test/harness constructor. Never used by the production CLI.
    pub fn in_memory() -> Result<Self> {
        let conn = rusqlite::Connection::open_in_memory()?;
        conn.execute_batch("PRAGMA busy_timeout=10000")?;
        Ok(Self {
            backend: Backend::Local {
                conn: Arc::new(StdMutex::new(conn)),
            },
            stats: Stats::default(),
            settings: Settings::default(),
        })
    }

    /// Wrap an existing connection (shared test oracle).
    pub fn from_connection(conn: rusqlite::Connection) -> Self {
        Self {
            backend: Backend::Local {
                conn: Arc::new(StdMutex::new(conn)),
            },
            stats: Stats::default(),
            settings: Settings::default(),
        }
    }

    /// Test/harness constructor against a real file.
    ///
    /// A file rather than `:memory:` so a test can restart the process against
    /// the same data (recovery tests) and so a crash leaves evidence behind.
    /// `busy_timeout` keeps two harnesses on one file from failing a write
    /// outright while the other holds the lock.
    pub fn open_local(path: &str) -> Result<Self> {
        let conn = rusqlite::Connection::open(path)?;
        conn.execute_batch("PRAGMA busy_timeout=10000; PRAGMA journal_mode=WAL")?;
        Ok(Self::from_connection(conn))
    }

    pub fn is_local(&self) -> bool {
        matches!(self.backend, Backend::Local { .. })
    }

    /// Short backend name for `doctor`, matching what the Python bot printed.
    pub fn backend(&self) -> &'static str {
        if self.is_local() {
            "sqlite"
        } else {
            "turso"
        }
    }

    pub fn settings(&self) -> &Settings {
        &self.settings
    }

    // -- schema ---------------------------------------------------------

    /// True when an earlier boot already applied this exact schema.
    ///
    /// The DDL is ~19 sequential round trips (8 CREATE TABLE, 3 column probes,
    /// 5 drops, 7 indexes). Against a remote database that is seconds of every
    /// cold start, paid to re-create objects that are already there. One pragma
    /// read answers the same question; any database from an earlier revision —
    /// or one the pragma cannot be read from — takes the full path, so this can
    /// only ever cost the old behaviour.
    pub async fn schema_is_current(&self) -> bool {
        match self.query("PRAGMA user_version", &[]).await {
            Ok(rows) => rows
                .first()
                .and_then(|r| r.int("user_version"))
                .map(|v| v >= SCHEMA_VERSION)
                .unwrap_or(false),
            Err(e) => {
                tracing::debug!(error = %e, "could not read the schema version");
                false
            }
        }
    }

    pub async fn init_schema(&self) -> Result<()> {
        if self.schema_is_current().await {
            return Ok(());
        }
        for stmt in split_statements(SCHEMA_TABLES) {
            self.execute(&stmt, &[]).await?;
        }
        // Full column set, so a table created by any earlier v2 revision gains
        // whatever it is missing (live databases are never rebuilt). Indexes
        // come last: they reference columns that may only just have been added.
        self.ensure_columns(
            TABLE_GIVEAWAYS,
            &[
                ("guild_id", "TEXT NOT NULL DEFAULT ''"),
                ("channel_id", "TEXT NOT NULL DEFAULT ''"),
                ("message_id", "TEXT"),
                ("prize", "TEXT NOT NULL DEFAULT ''"),
                ("winner_count", "INTEGER NOT NULL DEFAULT 1"),
                ("ends_at", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("required_role_id", "TEXT"),
                ("required_role_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ("blocked_role_id", "TEXT"),
                ("min_account_age_days", "INTEGER NOT NULL DEFAULT 0"),
                ("min_messages", "INTEGER NOT NULL DEFAULT 0"),
                ("image_url", "TEXT"),
                ("entrants_role_id", "TEXT"),
                ("created_by", "TEXT NOT NULL DEFAULT ''"),
                ("host_id", "TEXT"),
                ("host_name", "TEXT"),
                ("created_at", "INTEGER NOT NULL DEFAULT 0"),
                ("ended_at", "INTEGER"),
                ("winners_json", "TEXT NOT NULL DEFAULT '[]'"),
                // Live entry counter: incremented on every successful join,
                // decremented on leave/ban-drop, so refresh_snapshot reads 1
                // indexed row instead of COUNT(*) over all entries. NULL for rows
                // that ended before the counter existed; the read path COALESCEs
                // and backfills without a migration.
                ("entrant_count", "INTEGER"),
                // Optional winner-claim window in seconds. 0/NULL = disabled;
                // live databases gain it through this same migration map.
                ("claim_timeout_seconds", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        .await?;
        self.ensure_columns(
            TABLE_ENTRIES,
            &[
                ("giveaway_id", "TEXT NOT NULL DEFAULT ''"),
                ("user_id", "TEXT NOT NULL DEFAULT ''"),
                ("username", "TEXT NOT NULL DEFAULT ''"),
                ("entered_at", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        .await?;
        self.execute(MESSAGE_COUNTS_DDL, &[]).await?;
        // One-time per-server setup: the role pinged on every giveaway event.
        self.execute(GUILD_SETTINGS_DDL, &[]).await?;
        // Users blocked from giveaways in a server. Permanent until removed
        // (expires_at NULL) or timed (expires_at ms epoch) — the entry-wipe
        // sweep never touches this table.
        self.execute(BLACKLIST_DDL, &[]).await?;
        self.ensure_columns(
            "simple_blacklist",
            &[
                ("expires_at", "INTEGER"),
                ("reason", "TEXT NOT NULL DEFAULT ''"),
                ("banned_by", "TEXT NOT NULL DEFAULT ''"),
                ("created_at", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        .await?;
        // Members sitting out a fixed number of giveaways because Discord had
        // them timed out when they tried to join. Persisted, so a restart never
        // forgives a penalty. A row is deleted the moment its counter reaches
        // zero, so nothing needs to expire it.
        self.execute(GIVEAWAY_BANS_DDL, &[]).await?;
        // Every giveaway a penalty has been held against, per member. Only the
        // first block on a giveaway spends a unit, and a giveaway listed here
        // keeps refusing that member while it is still running.
        self.execute(GIVEAWAY_BAN_HITS_DDL, &[]).await?;
        // Optional winner-claim state, one row per drawn winner. Statuses:
        // pending -> claimed / expired / skipped / replaced.
        self.execute(CLAIMS_DDL, &[]).await?;
        for stmt in split_statements(SCHEMA_DROPS) {
            self.execute(&stmt, &[]).await?;
        }
        for stmt in split_statements(SCHEMA_INDEXES) {
            self.execute(&stmt, &[]).await?;
        }
        self.stamp_schema().await;
        Ok(())
    }

    async fn stamp_schema(&self) {
        if let Err(e) = self
            .execute(&format!("PRAGMA user_version = {SCHEMA_VERSION}"), &[])
            .await
        {
            // Worst case the next boot re-runs the DDL, which is idempotent.
            tracing::warn!(error = %e, "could not record the schema version");
        }
    }

    async fn ensure_columns(&self, table: &str, desired: &[(&str, &str)]) -> Result<()> {
        // The columns could not be read, so one may be missing. Failing here
        // beats running the whole process against an unmigrated table, where
        // every statement naming the column dies with "no such column" instead
        // of the problem showing up once, at startup.
        let rows = self
            .query(&format!("PRAGMA table_info({table})"), &[])
            .await?;
        let existing: Vec<String> = rows.iter().filter_map(|r| r.text("name")).collect();
        for (name, ddl) in desired {
            if !existing.iter().any(|c| c == name) {
                self.execute(&format!("ALTER TABLE {table} ADD COLUMN {name} {ddl}"), &[])
                    .await?;
            }
        }
        Ok(())
    }

    // -- statements -----------------------------------------------------

    /// Run one statement, surviving dead Turso streams and brief contention.
    ///
    /// * Stream the server never saw: the connection is dropped and the
    ///   statement replayed on a fresh one, on every hit up to the attempt cap.
    /// * Connection lost mid-request: a read is replayed, a write is not. The
    ///   server may have applied the write before the response was lost, and a
    ///   blind replay would apply it twice.
    /// * `locked`/`busy`/`conflict`: genuine writer contention, retried with a
    ///   short backoff. Anything else is returned untouched.
    pub async fn execute(&self, sql: &str, params: &[SqlValue]) -> Result<u64> {
        let write = is_write(sql);
        let mut last_error: Option<anyhow::Error> = None;
        for attempt in 0..6u32 {
            let start = Instant::now();
            let outcome = self.execute_once(sql, params).await;
            self.stats.record(write, start.elapsed());
            match outcome {
                Ok(changed) => return Ok(changed),
                Err(e) => {
                    let message = e.to_string().to_lowercase();
                    let ambiguous = is_ambiguous_loss(&message);
                    if ambiguous || is_dead_stream(&message) {
                        self.drop_connection().await;
                        if ambiguous && write {
                            tracing::warn!(
                                error = %brief(&e),
                                "connection lost mid-write; not replaying"
                            );
                            return Err(e);
                        }
                        tracing::warn!(
                            error = %brief(&e),
                            "database stream went away; reconnecting"
                        );
                        last_error = Some(e);
                        sleep_backoff(attempt).await;
                        continue;
                    }
                    if message.contains("locked")
                        || message.contains("busy")
                        || message.contains("conflict")
                    {
                        last_error = Some(e);
                        sleep_backoff(attempt).await;
                        continue;
                    }
                    return Err(e);
                }
            }
        }
        Err(last_error.unwrap_or_else(|| anyhow!("database statement failed")))
    }

    /// Read rows, replaying the whole read if the stream dies on the way back.
    ///
    /// Every statement `query` runs is a read, so replaying is safe whether the
    /// server never saw it or the rows were lost in transit.
    pub async fn query(&self, sql: &str, params: &[SqlValue]) -> Result<Vec<Row>> {
        let mut last_error: Option<anyhow::Error> = None;
        for _ in 0..3 {
            let start = Instant::now();
            let outcome = self.query_once(sql, params).await;
            // fetchall() transfer time counts toward read_ms: execute() counted
            // the trip, and this adds the rows' transfer, like the Python layer.
            self.stats.record(false, start.elapsed());
            match outcome {
                Ok(rows) => return Ok(rows),
                Err(e) => {
                    let message = e.to_string().to_lowercase();
                    if is_dead_stream(&message) || is_ambiguous_loss(&message) {
                        tracing::warn!(
                            error = %brief(&e),
                            "database stream died mid-read; retrying"
                        );
                        self.drop_connection().await;
                        last_error = Some(e);
                        continue;
                    }
                    return Err(e);
                }
            }
        }
        Err(last_error.unwrap_or_else(|| anyhow!("database read failed")))
    }

    pub async fn query_one(&self, sql: &str, params: &[SqlValue]) -> Result<Option<Row>> {
        Ok(self.query(sql, params).await?.into_iter().next())
    }

    // -- backends -------------------------------------------------------

    async fn remote_connection(&self) -> Result<libsql::Connection> {
        match &self.backend {
            Backend::Remote { db, idle } => {
                if let Some(conn) = idle.lock().await.pop() {
                    return Ok(conn);
                }
                db.connect()
                    .map_err(|e| anyhow!("could not open a Turso connection: {e}"))
            }
            Backend::Local { .. } => unreachable!("local backend has no remote connection"),
        }
    }

    async fn return_connection(&self, conn: libsql::Connection) {
        if let Backend::Remote { idle, .. } = &self.backend {
            let mut idle = idle.lock().await;
            if idle.len() < IDLE_POOL_MAX {
                idle.push(conn);
            }
            // Otherwise dropping the value closes the stream.
        }
    }

    /// Drop the calling operation's connection so a dead stream is not reused.
    ///
    /// Remote connections are per-operation here (the idle pool is shared), so
    /// the practical effect is the same as the Python per-thread close: the
    /// next statement gets a fresh stream.
    async fn drop_connection(&self) {
        if let Backend::Remote { idle, .. } = &self.backend {
            idle.lock().await.clear();
        }
    }

    async fn execute_once(&self, sql: &str, params: &[SqlValue]) -> Result<u64> {
        match &self.backend {
            Backend::Remote { .. } => {
                let conn = self.remote_connection().await?;
                let result = conn
                    .execute(sql, libsql_params(params))
                    .await
                    .map_err(|e| anyhow!("{e}"));
                match result {
                    Ok(changed) => {
                        self.return_connection(conn).await;
                        Ok(changed)
                    }
                    Err(e) => {
                        // A dead/ambiguous stream must not go back in the pool.
                        let message = e.to_string().to_lowercase();
                        if !(is_dead_stream(&message) || is_ambiguous_loss(&message)) {
                            self.return_connection(conn).await;
                        }
                        Err(e)
                    }
                }
            }
            Backend::Local { conn } => {
                let conn = Arc::clone(conn);
                let sql = sql.to_string();
                let params = params.to_vec();
                // rusqlite is blocking and this backend is tests-only, so
                // running it inline is simpler than a blocking-pool hop and
                // cannot stall a production event loop.
                let changed = conn
                    .lock()
                    .map_err(|_| anyhow!("database connection lock poisoned"))?
                    .execute(&sql, rusqlite::params_from_iter(params.iter()))
                    .map_err(|e| anyhow!("{e}"))?;
                Ok(changed as u64)
            }
        }
    }

    async fn query_once(&self, sql: &str, params: &[SqlValue]) -> Result<Vec<Row>> {
        match &self.backend {
            Backend::Remote { .. } => {
                let conn = self.remote_connection().await?;
                let result = async {
                    let mut rows = conn
                        .query(sql, libsql_params(params))
                        .await
                        .map_err(|e| anyhow!("{e}"))?;
                    let mut out = Vec::new();
                    while let Some(row) = rows.next().await.map_err(|e| anyhow!("{e}"))? {
                        out.push(libsql_row(&row)?);
                    }
                    Ok::<Vec<Row>, anyhow::Error>(out)
                }
                .await;
                match result {
                    Ok(rows) => {
                        self.return_connection(conn).await;
                        Ok(rows)
                    }
                    Err(e) => {
                        let message = e.to_string().to_lowercase();
                        if !(is_dead_stream(&message) || is_ambiguous_loss(&message)) {
                            self.return_connection(conn).await;
                        }
                        Err(e)
                    }
                }
            }
            Backend::Local { conn } => {
                let conn = Arc::clone(conn);
                let sql = sql.to_string();
                let params = params.to_vec();
                let guard = conn
                    .lock()
                    .map_err(|_| anyhow!("database connection lock poisoned"))?;
                let mut stmt = guard.prepare(&sql).map_err(|e| anyhow!("{e}"))?;
                let cols: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
                let mut rows = stmt
                    .query(rusqlite::params_from_iter(params.iter()))
                    .map_err(|e| anyhow!("{e}"))?;
                let mut out = Vec::new();
                while let Some(row) = rows.next().map_err(|e| anyhow!("{e}"))? {
                    let mut vals = Vec::with_capacity(cols.len());
                    for idx in 0..cols.len() {
                        let value: rusqlite::types::Value = row
                            .get(idx)
                            .map_err(|e| anyhow!("could not read column {idx}: {e}"))?;
                        vals.push(match value {
                            rusqlite::types::Value::Null => SqlValue::Null,
                            rusqlite::types::Value::Integer(v) => SqlValue::Integer(v),
                            rusqlite::types::Value::Real(v) => SqlValue::Real(v),
                            rusqlite::types::Value::Text(v) => SqlValue::Text(v),
                            rusqlite::types::Value::Blob(v) => SqlValue::Blob(v),
                        });
                    }
                    out.push(Row::new(cols.clone(), vals));
                }
                Ok(out)
            }
        }
    }

    // -- stats ----------------------------------------------------------

    pub fn stats(&self) -> DbStats {
        self.stats.snapshot()
    }

    pub fn reset_stats(&self) {
        self.stats.reset();
    }

    /// Close every pooled connection. For shutdown.
    pub async fn close(&self) {
        if let Backend::Remote { idle, .. } = &self.backend {
            idle.lock().await.clear();
        }
    }
}

fn libsql_params(params: &[SqlValue]) -> libsql::params::Params {
    if params.is_empty() {
        return libsql::params::Params::None;
    }
    libsql::params::Params::Positional(
        params
            .iter()
            .map(|v| match v {
                SqlValue::Null => libsql::Value::Null,
                SqlValue::Integer(i) => libsql::Value::Integer(*i),
                SqlValue::Real(f) => libsql::Value::Real(*f),
                SqlValue::Text(s) => libsql::Value::Text(s.clone()),
                SqlValue::Blob(b) => libsql::Value::Blob(b.clone()),
            })
            .collect(),
    )
}

fn libsql_row(row: &libsql::Row) -> Result<Row> {
    let count = row.column_count();
    let mut cols = Vec::with_capacity(count as usize);
    let mut vals = Vec::with_capacity(count as usize);
    for idx in 0..count {
        cols.push(row.column_name(idx).unwrap_or_default().to_string());
        let value = row
            .get_value(idx)
            .map_err(|e| anyhow!("could not read column {idx}: {e}"))?;
        vals.push(match value {
            libsql::Value::Null => SqlValue::Null,
            libsql::Value::Integer(v) => SqlValue::Integer(v),
            libsql::Value::Real(v) => SqlValue::Real(v),
            libsql::Value::Text(v) => SqlValue::Text(v),
            libsql::Value::Blob(v) => SqlValue::Blob(v),
        });
    }
    Ok(Row::new(cols, vals))
}

impl rusqlite::ToSql for SqlValue {
    fn to_sql(&self) -> rusqlite::Result<rusqlite::types::ToSqlOutput<'_>> {
        use rusqlite::types::{ToSqlOutput, Value as RV, ValueRef};
        Ok(match self {
            SqlValue::Null => ToSqlOutput::Borrowed(ValueRef::Null),
            SqlValue::Integer(v) => ToSqlOutput::Owned(RV::Integer(*v)),
            SqlValue::Real(v) => ToSqlOutput::Owned(RV::Real(*v)),
            SqlValue::Text(v) => ToSqlOutput::Borrowed(ValueRef::Text(v.as_bytes())),
            SqlValue::Blob(v) => ToSqlOutput::Borrowed(ValueRef::Blob(v)),
        })
    }
}

fn split_statements(sql: &str) -> Vec<String> {
    sql.split(';')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Mirrors the Python backoff: 150ms * attempt plus up to 50ms * attempt jitter.
async fn sleep_backoff(attempt: u32) {
    let base = 150u64 * u64::from(attempt + 1);
    let jitter = (rand::random::<f64>() * 50.0 * f64::from(attempt + 1)) as u64;
    tokio::time::sleep(Duration::from_millis(base + jitter)).await;
}

fn brief(error: &anyhow::Error) -> String {
    let flat = error
        .to_string()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    flat.chars().take(120).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_detection_matches_python() {
        assert!(is_write("INSERT INTO t VALUES (1)"));
        assert!(is_write("  update t set a=1"));
        assert!(is_write(
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
        ));
        assert!(is_write("PRAGMA user_version = 2"));
        assert!(!is_write("PRAGMA user_version"));
        assert!(!is_write("SELECT 1"));
        assert!(!is_write("EXPLAIN SELECT 1"));
        assert!(!is_write("VALUES (1)"));
        assert!(!is_write(""));
    }

    #[test]
    fn stream_markers_are_classified_like_python() {
        assert!(is_dead_stream("stream not found"));
        assert!(is_dead_stream("no transaction is active"));
        assert!(is_ambiguous_loss("broken pipe"));
        assert!(is_ambiguous_loss("connection reset by peer"));
        assert!(!is_dead_stream("syntax error"));
        assert!(!is_ambiguous_loss("syntax error"));
    }

    #[tokio::test]
    async fn local_backend_round_trips_rows_and_stats() {
        let db = Database::in_memory().unwrap();
        db.init_schema().await.unwrap();
        db.execute(
            "INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES (?, ?, ?)",
            &["g1".into(), "u1".into(), 3.into()],
        )
        .await
        .unwrap();
        let row = db
            .query_one(
                "SELECT count FROM simple_message_counts WHERE guild_id = ? AND user_id = ?",
                &["g1".into(), "u1".into()],
            )
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.int("count"), Some(3));
        let stats = db.stats();
        assert!(stats.writes >= 1);
        assert!(stats.reads >= 1);
    }

    #[tokio::test]
    async fn schema_is_idempotent_and_versioned() {
        let db = Database::in_memory().unwrap();
        db.init_schema().await.unwrap();
        assert!(db.schema_is_current().await);
        // A second call short-circuits on the version pragma.
        db.init_schema().await.unwrap();
        let row = db
            .query_one("PRAGMA user_version", &[])
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.int("user_version"), Some(SCHEMA_VERSION));
    }

    #[tokio::test]
    async fn migration_adds_missing_columns_without_rebuilding() {
        let db = Database::in_memory().unwrap();
        // Simulate a v2 table created before `claim_timeout_seconds` existed.
        db.execute(
            "CREATE TABLE simple_giveaways (id TEXT PRIMARY KEY, guild_id TEXT NOT NULL,
             channel_id TEXT NOT NULL, message_id TEXT, prize TEXT NOT NULL,
             winner_count INTEGER NOT NULL DEFAULT 1, ends_at INTEGER NOT NULL,
             status TEXT NOT NULL DEFAULT 'active', required_role_id TEXT,
             blocked_role_id TEXT, min_account_age_days INTEGER NOT NULL DEFAULT 0,
             min_messages INTEGER NOT NULL DEFAULT 0, image_url TEXT,
             entrants_role_id TEXT, created_by TEXT NOT NULL, host_id TEXT,
             host_name TEXT, created_at INTEGER NOT NULL, ended_at INTEGER,
             winners_json TEXT NOT NULL DEFAULT '[]', entrant_count INTEGER)",
            &[],
        )
        .await
        .unwrap();
        db.init_schema().await.unwrap();
        let rows = db
            .query("PRAGMA table_info(simple_giveaways)", &[])
            .await
            .unwrap();
        let names: Vec<String> = rows.iter().filter_map(|r| r.text("name")).collect();
        assert!(names.contains(&"claim_timeout_seconds".to_string()));
        assert!(names.contains(&"required_role_ids".to_string()));
    }
}
