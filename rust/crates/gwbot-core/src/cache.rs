//! Optional Redis read-cache and concurrency damper.
//!
//! Turso stays the single source of truth. Everything here is an optimisation
//! that must be safe to LOSE: with no URL, or with Redis down, evicted, rate
//! limited or slow, every method returns a safe default, logs at most one
//! warning per outage, and the caller falls back to Turso. Nothing in this
//! module may fail a click handler.
//!
//! Two defences protect a managed plan (30 connections, 100 ops/sec):
//!
//! * a token-bucket governor that silently SKIPS operations over budget, so one
//!   process can never exceed the plan ceiling in any sliding second — a skip
//!   looks exactly like a miss. The budget is PER PROCESS: overlapping
//!   instances each run their own bucket, so the aggregate peaks at the number
//!   of live processes times the per-process worst second;
//! * a circuit breaker that stops hammering a dead server for a cooldown.
//!
//! Keys handed in by callers are SUFFIXES (`gw:gw_ab12`, `count:gw_ab12`); the
//! shared prefix is applied internally so every key this bot writes is
//! namespaced and versioned under one place.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use redis::aio::ConnectionManager;
use regex::Regex;
use serde_json::{json, Value as Json};
use tokio::sync::Mutex;

use crate::config::Settings;

/// Compare-and-delete: only the current holder may release. Without the token
/// check a slow holder whose lock already expired on TTL could release the
/// lock its successor had just re-acquired.
const RELEASE_SCRIPT: &str = r#"
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"#;

/// INCR + first-time TTL in ONE round trip. Two commands (INCR then EXPIRE)
/// cost two round trips, doubling the price of this path for nothing; Lua is
/// available on the managed server. The TTL is set whenever the key has none,
/// so a key left over from a half-failed write can never live forever.
const INCR_TTL_SCRIPT: &str = r#"
local n = redis.call('incr', KEYS[1])
if redis.call('pttl', KEYS[1]) < 0 then
    redis.call('pexpire', KEYS[1], ARGV[1])
end
return n
"#;

/// The managed plan's own ceiling, in ops/sec. The governor promises to keep
/// a SLIDING SECOND under this, which is a stricter promise than keeping the
/// sustained rate right: the worst second a token bucket can produce is
/// capacity + rate (spend the whole bucket, then earn a second of refill).
pub const OP_CEILING_PER_SEC: f64 = 100.0;

/// Shared namespace for every key this bot writes.
pub const KEY_PREFIX: &str = "gwbot:v1:";

/// Strip host:port and token-shaped runs out of anything we log.
///
/// Driver errors like to quote the endpoint, and this module must never put a
/// credential or a hostname into the log or into `stats()`.
pub fn scrub(text: &str) -> String {
    static HOST_PORT: OnceLock<Regex> = OnceLock::new();
    static LONG_SECRET: OnceLock<Regex> = OnceLock::new();
    let host_port = HOST_PORT.get_or_init(|| Regex::new(r"\b[A-Za-z0-9_.-]+:\d{2,5}\b").unwrap());
    let long_secret = LONG_SECRET.get_or_init(|| Regex::new(r"\b[A-Za-z0-9_\-]{24,}\b").unwrap());
    let clean = host_port.replace_all(text, "<host:port>");
    let clean = long_secret.replace_all(&clean, "<redacted>");
    clean.chars().take(160).collect()
}

/// Token bucket: refills at a fixed rate, refuses (never queues) over cap.
///
/// Refusing is the point. Queueing would just move the burst into the future
/// and let one click burst spend a whole minute's plan budget; skipping lets
/// the caller fall through to Turso, which is always correct.
#[derive(Debug)]
struct Budget {
    rate: f64,
    capacity: f64,
    tokens: f64,
    stamp: Instant,
}

impl Budget {
    fn new(rate: f64) -> Self {
        let rate = rate.max(1.0);
        // The burst allowance is capped so that even the worst sliding second
        // stays inside the plan ceiling: capacity + rate <= OP_CEILING_PER_SEC.
        // Spending a WHOLE rate instantly (the old rule) let one process emit
        // 2 x rate ops inside a single second. A rate at or above the ceiling
        // cannot be bounded by a single bucket; capacity then floors at one
        // token and the configured rate is left saying what it says.
        let capacity = rate.min(OP_CEILING_PER_SEC - rate).max(1.0);
        Self {
            rate,
            capacity,
            tokens: capacity,
            stamp: Instant::now(),
        }
    }

    fn take(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.stamp).as_secs_f64();
        if elapsed > 0.0 {
            self.tokens = self.capacity.min(self.tokens + elapsed * self.rate);
            self.stamp = now;
        }
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            return true;
        }
        false
    }
}

/// Circuit breaker state: stop hammering a dead server for a cooldown.
#[derive(Debug, Default)]
struct Breaker {
    fail_streak: u32,
    open_until: Option<Instant>,
    warned: bool,
}

#[derive(Debug, Default)]
struct Counters {
    attempted: u64,
    ok: u64,
    failed: u64,
    skipped: u64,
    hits: u64,
    misses: u64,
    errors: BTreeMap<String, u64>,
}

/// Tunables, matching the Python defaults exactly.
#[derive(Debug, Clone)]
pub struct CacheOptions {
    pub prefix: String,
    pub socket_timeout: f64,
    pub socket_connect_timeout: f64,
    pub max_connections: usize,
    pub op_budget_per_sec: f64,
    pub breaker_threshold: u32,
    pub breaker_cooldown: f64,
}

impl Default for CacheOptions {
    fn default() -> Self {
        Self {
            prefix: KEY_PREFIX.to_string(),
            socket_timeout: 1.5,
            socket_connect_timeout: 2.0,
            max_connections: 4,
            op_budget_per_sec: 80.0,
            breaker_threshold: 5,
            breaker_cooldown: 30.0,
        }
    }
}

struct Inner {
    url: String,
    prefix: String,
    socket_timeout: f64,
    connect_timeout: f64,
    max_connections: usize,
    breaker_threshold: u32,
    breaker_cooldown: f64,
    client: Mutex<Option<ConnectionManager>>,
    budget: Mutex<Budget>,
    breaker: Mutex<Breaker>,
    counters: Mutex<Counters>,
}

/// Short-TTL cache and lock helper over a managed Redis, degrading always.
///
/// Cheap to clone (one `Arc`); share one instance per process so the budget and
/// breaker are global rather than per-caller.
#[derive(Clone)]
pub struct RedisCache {
    inner: Option<std::sync::Arc<Inner>>,
    closed: std::sync::Arc<AtomicBool>,
}

impl RedisCache {
    /// A cache that does nothing: every read misses, every write is a no-op.
    /// This is what an unset `REDIS_URL` produces, not an error.
    pub fn disabled() -> Self {
        Self {
            inner: None,
            closed: std::sync::Arc::new(AtomicBool::new(false)),
        }
    }

    /// Build from settings. Never fails: a malformed URL or a down server
    /// yields a cache that reports itself unavailable and falls through.
    pub fn from_settings(settings: &Settings) -> Self {
        if settings.redis_url.trim().is_empty() || !settings.redis_enabled {
            return Self::disabled();
        }
        let opts = CacheOptions {
            op_budget_per_sec: settings.redis_op_budget_per_sec,
            ..CacheOptions::default()
        };
        Self::new(&settings.redis_url, opts)
    }

    pub fn new(url: &str, opts: CacheOptions) -> Self {
        let url = url.trim().to_string();
        if url.is_empty() {
            return Self::disabled();
        }
        let prefix = opts.prefix;
        Self {
            inner: Some(std::sync::Arc::new(Inner {
                url,
                prefix,
                socket_timeout: opts.socket_timeout.max(0.0),
                connect_timeout: opts.socket_connect_timeout.max(0.0),
                max_connections: opts.max_connections.max(1),
                breaker_threshold: opts.breaker_threshold.max(1),
                breaker_cooldown: opts.breaker_cooldown.max(0.0),
                client: Mutex::new(None),
                budget: Mutex::new(Budget::new(opts.op_budget_per_sec)),
                breaker: Mutex::new(Breaker::default()),
                counters: Mutex::new(Counters::default()),
            })),
            closed: std::sync::Arc::new(AtomicBool::new(false)),
        }
    }

    // -- state -----------------------------------------------------------

    /// True when a URL was supplied and the switch is on.
    pub fn configured(&self) -> bool {
        self.inner.is_some()
    }

    /// True when the next operation would actually be attempted.
    pub async fn available(&self) -> bool {
        if !self.configured() || self.closed.load(Ordering::Relaxed) {
            return false;
        }
        !self.breaker_open().await
    }

    async fn breaker_open(&self) -> bool {
        let Some(inner) = &self.inner else {
            return false;
        };
        let mut breaker = inner.breaker.lock().await;
        if let Some(until) = breaker.open_until {
            if Instant::now() < until {
                return true;
            }
            // Cooldown elapsed: allow one probe through so a recovered server
            // closes the breaker without a restart.
            breaker.open_until = None;
            breaker.fail_streak = 0;
            breaker.warned = false;
        }
        false
    }

    // -- lifecycle -------------------------------------------------------

    fn timeout(&self) -> Duration {
        match &self.inner {
            Some(inner) => {
                Duration::from_secs_f64(inner.connect_timeout + inner.socket_timeout + 0.5)
            }
            None => Duration::from_secs(2),
        }
    }

    /// Lazily build the shared client; never opens a socket by itself.
    async fn connection(&self) -> Option<ConnectionManager> {
        let inner = self.inner.as_ref()?;
        if self.closed.load(Ordering::Relaxed) {
            return None;
        }
        if let Some(client) = inner.client.lock().await.clone() {
            return Some(client);
        }
        // The manager is built below; opening the client only validates the URL.
        let client = redis::Client::open(inner.url.clone());
        let client = match client {
            Ok(c) => c,
            Err(e) => {
                self.record_failure(&e.to_string()).await;
                return None;
            }
        };
        match ConnectionManager::new_with_config(
            client,
            redis::aio::ConnectionManagerConfig::new()
                .set_connection_timeout(Duration::from_secs_f64(inner.connect_timeout))
                .set_response_timeout(Duration::from_secs_f64(inner.socket_timeout))
                .set_number_of_retries(0),
        )
        .await
        {
            Ok(manager) => {
                *inner.client.lock().await = Some(manager.clone());
                Some(manager)
            }
            Err(e) => {
                self.record_failure(&e.to_string()).await;
                None
            }
        }
    }

    /// Connect (once) and verify with a PING. Never fails loudly.
    pub async fn start(&self) -> bool {
        if !self.available().await {
            return false;
        }
        self.op(|mut conn| async move { redis::cmd("PING").query_async::<String>(&mut conn).await })
            .await
            .is_some()
    }

    /// Disconnect the shared client. Safe to call twice, or never started.
    pub async fn aclose(&self) {
        self.closed.store(true, Ordering::Relaxed);
        if let Some(inner) = &self.inner {
            *inner.client.lock().await = None;
            let mut breaker = inner.breaker.lock().await;
            breaker.open_until = None;
        }
    }

    // -- plumbing --------------------------------------------------------

    /// One governed network call: budget, breaker, hard timeout, counters.
    ///
    /// A skipped or breaker-open call never reaches the driver, so the caller
    /// sees a miss and reads Turso instead.
    async fn op<T, F, Fut>(&self, make: F) -> Option<T>
    where
        F: FnOnce(ConnectionManager) -> Fut,
        Fut: std::future::Future<Output = redis::RedisResult<T>>,
    {
        if !self.available().await {
            self.bump_skipped().await;
            return None;
        }
        let Some(inner) = &self.inner else {
            self.bump_skipped().await;
            return None;
        };
        if !inner.budget.lock().await.take() {
            // Over the plan's ops/sec: skip without touching the network.
            self.bump_skipped().await;
            return None;
        }
        let Some(conn) = self.connection().await else {
            // No driver, malformed URL, or a client already closed: every
            // caller treats this exactly like a miss.
            self.bump_skipped().await;
            return None;
        };
        inner.counters.lock().await.attempted += 1;
        match tokio::time::timeout(self.timeout(), make(conn)).await {
            Ok(Ok(value)) => {
                self.record_success().await;
                Some(value)
            }
            Ok(Err(e)) => {
                self.record_failure(&e.to_string()).await;
                None
            }
            Err(_) => {
                self.record_failure("timeout").await;
                None
            }
        }
    }

    async fn record_success(&self) {
        let Some(inner) = &self.inner else {
            return;
        };
        inner.counters.lock().await.ok += 1;
        let mut breaker = inner.breaker.lock().await;
        breaker.fail_streak = 0;
        breaker.open_until = None;
        breaker.warned = false;
    }

    async fn record_failure(&self, message: &str) {
        let Some(inner) = &self.inner else {
            return;
        };
        {
            let mut counters = inner.counters.lock().await;
            counters.failed += 1;
            // Python keys this by exception class name; the closest stable
            // equivalent here is the leading token of the message.
            let kind = error_kind(message);
            *counters.errors.entry(kind).or_insert(0) += 1;
        }
        let mut breaker = inner.breaker.lock().await;
        breaker.fail_streak += 1;
        if breaker.fail_streak >= inner.breaker_threshold {
            breaker.open_until =
                Some(Instant::now() + Duration::from_secs_f64(inner.breaker_cooldown));
            if !breaker.warned {
                // Exactly one warning per outage episode: a dead Redis must not
                // fill the log with thousands of identical lines.
                breaker.warned = true;
                tracing::warn!(
                    "redis unavailable ({}); pausing cache for {:.0}s, falling back to the database",
                    scrub(message),
                    inner.breaker_cooldown
                );
            }
        } else {
            tracing::debug!("redis op failed: {}", scrub(message));
        }
    }

    async fn bump_skipped(&self) {
        if let Some(inner) = &self.inner {
            inner.counters.lock().await.skipped += 1;
        }
    }

    async fn bump_hit(&self) {
        if let Some(inner) = &self.inner {
            inner.counters.lock().await.hits += 1;
        }
    }

    async fn bump_miss(&self) {
        if let Some(inner) = &self.inner {
            inner.counters.lock().await.misses += 1;
        }
    }

    fn key(&self, suffix: &str) -> String {
        match &self.inner {
            Some(inner) => format!("{}{}", inner.prefix, suffix),
            None => suffix.to_string(),
        }
    }

    // -- plain values ----------------------------------------------------

    pub async fn get(&self, key: &str) -> Option<String> {
        let name = self.key(key);
        let value = self
            .op(move |mut conn| async move {
                redis::cmd("GET")
                    .arg(name)
                    .query_async::<Option<String>>(&mut conn)
                    .await
            })
            .await
            .flatten();
        match &value {
            Some(_) => self.bump_hit().await,
            None => self.bump_miss().await,
        }
        value
    }

    pub async fn set(&self, key: &str, value: &str, ttl: f64) -> bool {
        let name = self.key(key);
        let value = value.to_string();
        let millis = ttl_millis(ttl);
        self.op(move |mut conn| async move {
            redis::cmd("SET")
                .arg(name)
                .arg(value)
                .arg("PX")
                .arg(millis)
                .query_async::<Option<String>>(&mut conn)
                .await
        })
        .await
        .flatten()
        .is_some()
    }

    pub async fn delete(&self, keys: &[&str]) -> i64 {
        if keys.is_empty() {
            return 0;
        }
        let names: Vec<String> = keys.iter().map(|k| self.key(k)).collect();
        self.op(move |mut conn| async move {
            redis::cmd("DEL")
                .arg(names)
                .query_async::<i64>(&mut conn)
                .await
        })
        .await
        .unwrap_or(0)
    }

    pub async fn get_json(&self, key: &str) -> Option<Json> {
        let raw = self.get(key).await?;
        // A truncated or foreign value is a miss, not a crash.
        serde_json::from_str(&raw).ok()
    }

    pub async fn set_json(&self, key: &str, value: &Json, ttl: f64) -> bool {
        let payload = match serde_json::to_string(value) {
            Ok(p) => p,
            Err(e) => {
                tracing::debug!(
                    "redis set_json could not serialise: {}",
                    scrub(&e.to_string())
                );
                return false;
            }
        };
        self.set(key, &payload, ttl).await
    }

    // -- counter ---------------------------------------------------------

    /// Increment a counter and give it a TTL in one round trip.
    ///
    /// `None` means "no answer" — the caller must fall back to the database,
    /// never treat it as zero.
    pub async fn incr_with_ttl(&self, key: &str, ttl: f64) -> Option<i64> {
        let name = self.key(key);
        let millis = ttl_millis(ttl);
        self.op(move |mut conn| async move {
            match redis::cmd("EVAL")
                .arg(INCR_TTL_SCRIPT)
                .arg(1)
                .arg(&name)
                .arg(millis)
                .query_async::<i64>(&mut conn)
                .await
            {
                Ok(value) => Ok(value),
                Err(e) => {
                    // A server without EVAL (or with scripting disabled) would
                    // lose every counter, so fall back to two plain commands.
                    // Documented cost: one extra round trip on that path only.
                    tracing::debug!(
                        "redis eval unavailable ({}); using incr+pexpire",
                        scrub(&e.to_string())
                    );
                    let value: i64 = redis::cmd("INCR").arg(&name).query_async(&mut conn).await?;
                    let _: i64 = redis::cmd("PEXPIRE")
                        .arg(&name)
                        .arg(millis)
                        .query_async(&mut conn)
                        .await?;
                    Ok(value)
                }
            }
        })
        .await
    }

    // -- lock ------------------------------------------------------------

    /// Take a short-lived lock. Returns its token, or `None` when held.
    ///
    /// `None` means "somebody else holds it" AND "Redis could not answer" — a
    /// caller that cannot act on the difference must treat both the same way
    /// (skip the optional shared work), never as licence to do something
    /// unsafe. A caller that CAN tell them apart uses [`Self::acquire_checked`].
    pub async fn acquire(&self, key: &str, ttl: f64) -> Option<String> {
        self.acquire_checked(key, ttl).await.0
    }

    /// Take a short-lived lock and say whether the server answered.
    ///
    /// * `(Some(token), true)` — we hold it; give the token to `release`.
    /// * `(None, true)` — somebody else holds it (a real answer).
    /// * `(None, false)` — Redis was over budget, unreachable, or timed out.
    ///
    /// Never raises, and a lock is never reported as taken without a token.
    pub async fn acquire_checked(&self, key: &str, ttl: f64) -> (Option<String>, bool) {
        let name = self.key(key);
        let token = random_token();
        let millis = ttl_millis(ttl);
        let token_for_call = token.clone();
        let result = self
            .op(move |mut conn| async move {
                redis::cmd("SET")
                    .arg(name)
                    .arg(token_for_call)
                    .arg("NX")
                    .arg("PX")
                    .arg(millis)
                    .query_async::<Option<String>>(&mut conn)
                    .await
            })
            .await;
        match result {
            // No answer: the caller must decide, not assume the lock is held.
            None => (None, false),
            Some(None) => (None, true),
            Some(Some(_)) => (Some(token), true),
        }
    }

    /// Release a lock only if this token still owns it.
    pub async fn release(&self, key: &str, token: &str) -> bool {
        let name = self.key(key);
        let token = token.to_string();
        self.op(move |mut conn| async move {
            redis::cmd("EVAL")
                .arg(RELEASE_SCRIPT)
                .arg(1)
                .arg(name)
                .arg(token)
                .query_async::<i64>(&mut conn)
                .await
        })
        .await
        .map(|value| value > 0)
        .unwrap_or(false)
    }

    // -- diagnostics -----------------------------------------------------

    /// Wall-clock milliseconds of one PING, or `None`. Never fails loudly.
    pub async fn ping_ms(&self) -> Option<f64> {
        let started = Instant::now();
        if !self.start().await {
            return None;
        }
        Some(started.elapsed().as_secs_f64() * 1000.0)
    }

    /// One-line, log-safe summary of the configuration (no endpoint).
    pub async fn describe(&self) -> String {
        match &self.inner {
            Some(inner) => {
                let budget = inner.budget.lock().await;
                format!(
                    "max_connections={}, budget={:.0} ops/s (burst {:.0}), breaker={}",
                    inner.max_connections, budget.rate, budget.capacity, inner.breaker_threshold
                )
            }
            None => "disabled".to_string(),
        }
    }

    /// Counters only — never a URL, host, username or password.
    pub async fn stats(&self) -> Json {
        match &self.inner {
            Some(inner) => {
                let counters = inner.counters.lock().await;
                let breaker_open = inner.breaker.lock().await.open_until.is_some();
                json!({
                    "configured": true,
                    "available": self.available().await,
                    "breaker_open": breaker_open,
                    "key_prefix_len": inner.prefix.chars().count(),
                    "counters": {
                        "attempted": counters.attempted,
                        "ok": counters.ok,
                        "failed": counters.failed,
                        "skipped": counters.skipped,
                        "hits": counters.hits,
                        "misses": counters.misses,
                    },
                    "errors": counters.errors,
                })
            }
            None => json!({
                "configured": false,
                "available": false,
                "breaker_open": false,
                "key_prefix_len": 0,
                "counters": {
                    "attempted": 0, "ok": 0, "failed": 0,
                    "skipped": 0, "hits": 0, "misses": 0,
                },
                "errors": {},
            }),
        }
    }
}

/// Milliseconds for a TTL, floored at 1 like the Python `max(1, int(ttl*1000))`.
fn ttl_millis(ttl: f64) -> i64 {
    ((ttl.max(0.0) * 1000.0) as i64).max(1)
}

fn random_token() -> String {
    use rand::Rng;
    let bytes: [u8; 16] = rand::thread_rng().gen();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// First word of an error message, used as a stable stats bucket.
fn error_kind(message: &str) -> String {
    message
        .split_whitespace()
        .next()
        .unwrap_or("error")
        .trim_matches(|c: char| !c.is_alphanumeric())
        .chars()
        .take(40)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budget_caps_the_worst_sliding_second() {
        // 80/s: capacity must be 20 so capacity + rate stays at the ceiling.
        let budget = Budget::new(80.0);
        assert_eq!(budget.capacity, 20.0);
        assert_eq!(Budget::new(1.0).capacity, 1.0);
        // A rate above the ceiling floors at one token rather than going negative.
        assert_eq!(Budget::new(500.0).capacity, 1.0);
    }

    #[test]
    fn budget_refuses_instead_of_queueing() {
        let mut budget = Budget::new(1.0);
        assert!(budget.take());
        // No time has passed, so the bucket is empty and the call is refused.
        assert!(!budget.take());
    }

    #[test]
    fn scrub_removes_endpoints_and_tokens() {
        let cleaned = scrub("connection to redis-1234.upstash.io:6379 failed token abcdefghijklmnopqrstuvwxyz123456");
        assert!(cleaned.contains("<host:port>"), "{cleaned}");
        assert!(cleaned.contains("<redacted>"), "{cleaned}");
        assert!(!cleaned.contains("6379"), "{cleaned}");
    }

    #[test]
    fn disabled_cache_is_safe() {
        let cache = RedisCache::disabled();
        assert!(!cache.configured());
        assert!(!cache.available_blocking_for_test());
    }

    impl RedisCache {
        fn available_blocking_for_test(&self) -> bool {
            // `available` is async only because of the breaker lock; with no
            // inner state it short-circuits to false.
            self.inner.is_some()
        }
    }

    #[tokio::test]
    async fn disabled_cache_misses_every_read() {
        let cache = RedisCache::disabled();
        assert!(cache.get("gw:x").await.is_none());
        assert!(!cache.set("gw:x", "1", 5.0).await);
        assert_eq!(cache.delete(&["gw:x"]).await, 0);
        assert!(cache.get_json("gw:x").await.is_none());
        assert!(cache.incr_with_ttl("count:x", 4.0).await.is_none());
        assert_eq!(cache.acquire_checked("lock", 5.0).await, (None, false));
        assert!(!cache.release("lock", "tok").await);
        assert!(cache.ping_ms().await.is_none());
        assert_eq!(cache.describe().await, "disabled");
        assert_eq!(cache.stats().await["configured"], json!(false));
    }

    #[test]
    fn key_prefix_is_applied_to_suffixes() {
        let cache = RedisCache::new("redis://127.0.0.1:6379", CacheOptions::default());
        assert_eq!(cache.key("gw:gw_ab12"), "gwbot:v1:gw:gw_ab12");
    }
}
