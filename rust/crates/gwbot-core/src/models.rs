//! Domain types shared by the service, the Discord adapter and the harness.

use std::sync::atomic::{AtomicI64, Ordering};

use serde::{Deserialize, Serialize};

use crate::db::Row;

/// Hard bound on winners per giveaway: the announcement has to fit in one
/// Discord message. `create` and `reroll` both enforce it.
pub const MAX_WINNERS: i64 = 25;

/// Members timed out when they tried to join sit out this many giveaways.
pub const TIMEOUT_PENALTY: i64 = 3;

/// Rows still considered for the live entry count: 5 hours of entries is more
/// than any giveaway window the bot offers.
pub const ENTRY_RETENTION_MS: i64 = 5 * 60 * 60 * 1000;

/// One message-count batch writes at most this many rows per statement.
pub const MESSAGE_FLUSH_CHUNK: usize = 300;

/// Tag carried by the refusals a caller has to treat differently from a plain
/// "no": the member was stopped by the timed-out rule and their entry was
/// dropped, so anything granted on joining has to go with it.
pub const TIMEOUT_BAN_KIND: &str = "timeout_ban";

/// A giveaway row, decoded.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Giveaway {
    pub id: String,
    pub guild_id: String,
    pub channel_id: String,
    pub message_id: Option<String>,
    pub prize: String,
    pub winner_count: i64,
    pub ends_at: i64,
    pub status: String,
    pub required_role_id: Option<String>,
    pub required_role_ids: Vec<String>,
    pub blocked_role_id: Option<String>,
    pub min_account_age_days: i64,
    pub min_messages: i64,
    pub image_url: Option<String>,
    pub entrants_role_id: Option<String>,
    pub host_id: Option<String>,
    pub host_name: Option<String>,
    pub winners: Vec<String>,
    /// Optional winner-claim window in seconds. 0 = disabled (legacy default).
    pub claim_timeout_seconds: i64,
    /// Entry counter when the caller already knows it (a drawn giveaway knows
    /// its own pool size). `None` means "not loaded" — readers must fall back
    /// to a database read rather than print a zero that was never measured.
    pub entrant_count: Option<i64>,
}

impl Giveaway {
    pub fn active(&self) -> bool {
        self.status == "active"
    }

    pub fn claim_enabled(&self) -> bool {
        self.claim_timeout_seconds > 0
    }

    /// Decode a `simple_giveaways` row.
    ///
    /// Mirrors `Giveaway.from_row`: malformed JSON is treated as empty rather
    /// than raising, because one hand-edited row must not take down a command.
    pub fn from_row(row: &Row) -> Self {
        let winners = serde_json::from_str::<serde_json::Value>(
            &row.text("winners_json").unwrap_or_else(|| "[]".into()),
        )
        .ok()
        .and_then(|v| v.as_array().cloned())
        .map(|items| items.iter().map(json_to_string).collect())
        .unwrap_or_default();

        let mut required: Vec<String> = Vec::new();
        if let Some(raw) = row.text("required_role_ids").filter(|s| !s.is_empty()) {
            if let Ok(serde_json::Value::Array(items)) =
                serde_json::from_str::<serde_json::Value>(&raw)
            {
                for item in items {
                    let value = json_to_string(&item);
                    if !value.trim().is_empty() {
                        required.push(value);
                    }
                }
            }
        }
        let legacy = row.text("required_role_id").unwrap_or_default();
        let legacy = legacy.trim();
        if !legacy.is_empty() && !required.iter().any(|r| r == legacy) {
            required.push(legacy.to_string());
        }

        Self {
            id: row.text("id").unwrap_or_default(),
            guild_id: row.text("guild_id").unwrap_or_default(),
            channel_id: row.text("channel_id").unwrap_or_default(),
            message_id: row.text("message_id"),
            prize: row.text("prize").unwrap_or_default(),
            winner_count: row.int_or("winner_count", 0),
            ends_at: row.int_or("ends_at", 0),
            status: row.text("status").unwrap_or_default(),
            required_role_id: row.text("required_role_id"),
            required_role_ids: required,
            blocked_role_id: row.text("blocked_role_id"),
            min_account_age_days: row.int_or("min_account_age_days", 0),
            min_messages: row.int_or("min_messages", 0),
            image_url: row.text("image_url"),
            entrants_role_id: row.text("entrants_role_id"),
            host_id: row.text("host_id"),
            host_name: row.text("host_name"),
            winners,
            claim_timeout_seconds: row.int_or("claim_timeout_seconds", 0),
            entrant_count: row.int("entrant_count"),
        }
    }
}

fn json_to_string(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// A refusal a user should read, with an optional machine-readable tag.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceError {
    pub message: String,
    pub kind: String,
}

impl ServiceError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            kind: String::new(),
        }
    }

    pub fn with_kind(message: impl Into<String>, kind: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            kind: kind.into(),
        }
    }
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ServiceError {}

/// A batched write stopped part way, after applied rows had landed.
///
/// A batch is written in chunks that commit individually, so the rows before
/// the failure are already stored. Retrying the whole batch would add them a
/// second time; the caller hands back only the tail.
#[derive(Debug)]
pub struct PartialFlush {
    pub applied: usize,
    pub error: anyhow::Error,
}

impl std::fmt::Display for PartialFlush {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "batch stopped after {} applied rows: {}",
            self.applied, self.error
        )
    }
}

impl std::error::Error for PartialFlush {}

/// Where the service reads the time.
///
/// Milliseconds since the Unix epoch is the bot's only time unit. Production
/// uses [`SystemClock`]; a test injects a [`ManualClock`] so an "expires in five
/// minutes" assertion never races a slow CI box, and a timeout-penalty test does
/// not have to wait out five real hours.
///
/// This is a value the service owns rather than a process-global, because Rust
/// tests run in parallel threads inside one process: a global clock would let
/// one test's `advance` move another test's deadline.
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
}

/// The real clock. What production runs on.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        system_now_ms()
    }
}

/// Milliseconds since the Unix epoch, straight from the OS.
pub fn system_now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// A clock that only moves when a test moves it.
///
/// Starts at whatever instant the test picks, so assertions can name absolute
/// deadlines instead of measuring elapsed time.
#[derive(Debug)]
pub struct ManualClock {
    now: AtomicI64,
}

impl ManualClock {
    pub fn new(ms: i64) -> Self {
        Self {
            now: AtomicI64::new(ms),
        }
    }

    pub fn now(&self) -> i64 {
        self.now.load(Ordering::Relaxed)
    }

    /// Jump to `ms` and return it.
    pub fn set(&self, ms: i64) -> i64 {
        self.now.store(ms, Ordering::Relaxed);
        ms
    }

    /// Move forward by `delta_ms` and return the new instant. Negative deltas
    /// are allowed so a test can pin a deadline in the past.
    pub fn advance(&self, delta_ms: i64) -> i64 {
        let next = self.now().saturating_add(delta_ms);
        self.now.store(next, Ordering::Relaxed);
        next
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> i64 {
        self.now()
    }
}

/// True when a statement that must match a row actually matched one.
///
/// A missing count reads as "did not match": for a status flip that means
/// refusing to draw rather than drawing a second time.
pub fn changed(rows_affected: u64) -> bool {
    rows_affected > 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::SqlValue;

    fn row(pairs: &[(&str, SqlValue)]) -> Row {
        Row::new(
            pairs.iter().map(|(k, _)| k.to_string()).collect(),
            pairs.iter().map(|(_, v)| v.clone()).collect(),
        )
    }

    #[test]
    fn from_row_merges_legacy_role_into_list() {
        let gw = Giveaway::from_row(&row(&[
            ("id", "gw_1".into()),
            ("guild_id", "g".into()),
            ("channel_id", "c".into()),
            ("prize", "Nitro".into()),
            ("winner_count", 2.into()),
            ("ends_at", 99.into()),
            ("status", "active".into()),
            ("required_role_ids", r#"["111","222"]"#.into()),
            ("required_role_id", "222".into()),
            ("winners_json", r#"["u1"]"#.into()),
        ]));
        assert_eq!(gw.required_role_ids, vec!["111", "222"]);
        assert_eq!(gw.winners, vec!["u1"]);
        assert!(gw.active());
        assert_eq!(gw.winner_count, 2);
    }

    #[test]
    fn malformed_json_does_not_panic() {
        let gw = Giveaway::from_row(&row(&[
            ("id", "gw_1".into()),
            ("required_role_ids", "{not json".into()),
            ("winners_json", "nope".into()),
        ]));
        assert!(gw.required_role_ids.is_empty());
        assert!(gw.winners.is_empty());
    }

    #[test]
    fn claim_window_gates_claim_enabled() {
        let mut gw = Giveaway::default();
        assert!(!gw.claim_enabled());
        gw.claim_timeout_seconds = 60;
        assert!(gw.claim_enabled());
    }

    #[test]
    fn the_clock_only_moves_when_a_test_moves_it() {
        // Pinning freezes time: two reads with real work between them must
        // return the same instant, which is what makes an expiry assertion
        // deterministic.
        let clock = ManualClock::new(1_700_000_000_000);
        let pinned = clock.now_ms();
        assert_eq!(clock.now_ms(), pinned);
        assert_eq!(clock.now_ms(), pinned);

        // Advancing is the only thing that moves it, by exactly the delta.
        assert_eq!(clock.advance(1_500), pinned + 1_500);
        assert_eq!(clock.now_ms(), pinned + 1_500);
    }

    #[test]
    fn two_clocks_do_not_share_time() {
        // The reason the clock is owned by the service: parallel tests each get
        // their own, so one test's advance cannot move another's deadline.
        let a = ManualClock::new(1_000);
        let b = ManualClock::new(2_000);
        a.advance(500);
        assert_eq!(a.now_ms(), 1_500);
        assert_eq!(b.now_ms(), 2_000);
    }

    #[test]
    fn the_system_clock_moves_on_its_own() {
        // Only asserts the epoch is sane: the point is that production reads a
        // real clock rather than a frozen one.
        let now = SystemClock.now_ms();
        assert!(now > 1_600_000_000_000, "system clock looks unset: {now}");
    }
}
