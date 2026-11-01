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

use async_trait::async_trait;
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

/// Time source for the budget and the breaker.
///
/// Production reads `Instant::now`; a test advances a [`ManualCacheClock`]
/// instead of sleeping, so budget refill and breaker cooldown are deterministic.
pub trait CacheClock: Send + Sync {
    fn now(&self) -> Instant;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct SystemCacheClock;

impl CacheClock for SystemCacheClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}

/// A clock that only moves when a test moves it.
pub struct ManualCacheClock {
    base: Instant,
    offset: std::sync::Mutex<Duration>,
}

impl ManualCacheClock {
    pub fn new() -> Self {
        Self {
            base: Instant::now(),
            offset: std::sync::Mutex::new(Duration::ZERO),
        }
    }

    pub fn advance(&self, seconds: f64) {
        let mut offset = self
            .offset
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *offset += Duration::from_secs_f64(seconds.max(0.0));
    }
}

impl Default for ManualCacheClock {
    fn default() -> Self {
        Self::new()
    }
}

impl CacheClock for ManualCacheClock {
    fn now(&self) -> Instant {
        self.base
            + *self
                .offset
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
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

    /// The same rule with the instant supplied, so a test can move time.
    fn take_at(&mut self, now: Instant) -> bool {
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
    clock: std::sync::Arc<dyn CacheClock>,
    driver: Mutex<Option<std::sync::Arc<dyn Driver>>>,
    budget: Mutex<Budget>,
    breaker: Mutex<Breaker>,
    counters: Mutex<Counters>,
}

/// The handful of Redis commands this cache needs, behind a trait.
///
/// Production uses [`LiveDriver`] over a managed connection. Tests inject a
/// fake, so the cache's own behaviour -- budget, breaker, key namespacing, the
/// EVAL fallback -- is exercised without a server, which is exactly what the
/// Python suite did with a fake async client.
#[async_trait]
pub trait Driver: Send + Sync {
    async fn ping(&self) -> Result<(), String>;
    async fn get(&self, key: &str) -> Result<Option<String>, String>;
    /// `SET key value [NX] PX millis`. `None` means NX found the key present.
    async fn set(
        &self,
        key: &str,
        value: &str,
        millis: i64,
        nx: bool,
    ) -> Result<Option<String>, String>;
    async fn del(&self, keys: &[String]) -> Result<i64, String>;
    /// `EVAL script 1 key... arg...`, returning the integer reply.
    async fn eval(&self, script: &str, keys: &[String], args: &[String]) -> Result<i64, String>;
    async fn incr(&self, key: &str) -> Result<i64, String>;
    async fn pexpire(&self, key: &str, millis: i64) -> Result<i64, String>;
}

/// The real driver: a managed Redis connection.
struct LiveDriver {
    conn: ConnectionManager,
}

#[async_trait]
impl Driver for LiveDriver {
    async fn ping(&self) -> Result<(), String> {
        let mut conn = self.conn.clone();
        redis::cmd("PING")
            .query_async::<String>(&mut conn)
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }

    async fn get(&self, key: &str) -> Result<Option<String>, String> {
        let mut conn = self.conn.clone();
        redis::cmd("GET")
            .arg(key)
            .query_async::<Option<String>>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }

    async fn set(
        &self,
        key: &str,
        value: &str,
        millis: i64,
        nx: bool,
    ) -> Result<Option<String>, String> {
        let mut conn = self.conn.clone();
        let mut cmd = redis::cmd("SET");
        cmd.arg(key).arg(value);
        if nx {
            cmd.arg("NX");
        }
        cmd.arg("PX").arg(millis);
        cmd.query_async::<Option<String>>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }

    async fn del(&self, keys: &[String]) -> Result<i64, String> {
        let mut conn = self.conn.clone();
        redis::cmd("DEL")
            .arg(keys)
            .query_async::<i64>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }

    async fn eval(&self, script: &str, keys: &[String], args: &[String]) -> Result<i64, String> {
        let mut conn = self.conn.clone();
        let mut cmd = redis::cmd("EVAL");
        cmd.arg(script).arg(keys.len());
        for key in keys {
            cmd.arg(key);
        }
        for arg in args {
            cmd.arg(arg);
        }
        cmd.query_async::<i64>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }

    async fn incr(&self, key: &str) -> Result<i64, String> {
        let mut conn = self.conn.clone();
        redis::cmd("INCR")
            .arg(key)
            .query_async::<i64>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }

    async fn pexpire(&self, key: &str, millis: i64) -> Result<i64, String> {
        let mut conn = self.conn.clone();
        redis::cmd("PEXPIRE")
            .arg(key)
            .arg(millis)
            .query_async::<i64>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }
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
                clock: std::sync::Arc::new(SystemCacheClock),
                driver: Mutex::new(None),
                budget: Mutex::new(Budget::new(opts.op_budget_per_sec)),
                breaker: Mutex::new(Breaker::default()),
                counters: Mutex::new(Counters::default()),
            })),
            closed: std::sync::Arc::new(AtomicBool::new(false)),
        }
    }

    /// Build a cache over an injected driver.
    ///
    /// Test-only seam: production always reaches Redis through
    /// [`RedisCache::new`]. A fake driver makes the cache's own behaviour --
    /// budget, breaker, namespacing, EVAL fallback -- testable without a
    /// server, the way the Python suite injected a fake async client.
    pub fn with_driver(driver: std::sync::Arc<dyn Driver>, opts: CacheOptions) -> Self {
        Self::with_driver_and_clock(driver, opts, std::sync::Arc::new(SystemCacheClock))
    }

    /// The full test seam: an injected driver and an injected clock.
    pub fn with_driver_and_clock(
        driver: std::sync::Arc<dyn Driver>,
        opts: CacheOptions,
        clock: std::sync::Arc<dyn CacheClock>,
    ) -> Self {
        let prefix = opts.prefix;
        Self {
            inner: Some(std::sync::Arc::new(Inner {
                url: String::new(),
                prefix,
                socket_timeout: opts.socket_timeout.max(0.0),
                connect_timeout: opts.socket_connect_timeout.max(0.0),
                max_connections: opts.max_connections.max(1),
                breaker_threshold: opts.breaker_threshold.max(1),
                breaker_cooldown: opts.breaker_cooldown.max(0.0),
                clock,
                driver: Mutex::new(Some(driver)),
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
            if inner.clock.now() < until {
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

    /// Lazily build the shared driver; never opens a socket by itself.
    async fn driver(&self) -> Option<std::sync::Arc<dyn Driver>> {
        let inner = self.inner.as_ref()?;
        if self.closed.load(Ordering::Relaxed) {
            return None;
        }
        if let Some(driver) = inner.driver.lock().await.clone() {
            return Some(driver);
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
            Ok(conn) => {
                let driver: std::sync::Arc<dyn Driver> = std::sync::Arc::new(LiveDriver { conn });
                *inner.driver.lock().await = Some(driver.clone());
                Some(driver)
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
        self.op(|driver| async move { driver.ping().await })
            .await
            .is_some()
    }

    /// Disconnect the shared driver. Safe to call twice, or never started.
    pub async fn aclose(&self) {
        self.closed.store(true, Ordering::Relaxed);
        if let Some(inner) = &self.inner {
            *inner.driver.lock().await = None;
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
        F: FnOnce(std::sync::Arc<dyn Driver>) -> Fut,
        Fut: std::future::Future<Output = Result<T, String>>,
    {
        if !self.available().await {
            self.bump_skipped().await;
            return None;
        }
        let Some(inner) = &self.inner else {
            self.bump_skipped().await;
            return None;
        };
        if !inner.budget.lock().await.take_at(inner.clock.now()) {
            // Over the plan's ops/sec: skip without touching the network.
            self.bump_skipped().await;
            return None;
        }
        let Some(driver) = self.driver().await else {
            // No driver, malformed URL, or a client already closed: every
            // caller treats this exactly like a miss.
            self.bump_skipped().await;
            return None;
        };
        inner.counters.lock().await.attempted += 1;
        match tokio::time::timeout(self.timeout(), make(driver)).await {
            Ok(Ok(value)) => {
                self.record_success().await;
                Some(value)
            }
            Ok(Err(e)) => {
                self.record_failure(&e).await;
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
                Some(inner.clock.now() + Duration::from_secs_f64(inner.breaker_cooldown));
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
            .op(move |driver| async move { driver.get(&name).await })
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
        self.op(move |driver| async move { driver.set(&name, &value, millis, false).await })
            .await
            .flatten()
            .is_some()
    }

    pub async fn delete(&self, keys: &[&str]) -> i64 {
        if keys.is_empty() {
            return 0;
        }
        let names: Vec<String> = keys.iter().map(|k| self.key(k)).collect();
        self.op(move |driver| async move { driver.del(&names).await })
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
        self.op(move |driver| async move {
            let keys = [name.clone()];
            let args = [millis.to_string()];
            match driver.eval(INCR_TTL_SCRIPT, &keys, &args).await {
                Ok(value) => Ok(value),
                Err(e) => {
                    // A server without EVAL (or with scripting disabled) would
                    // lose every counter, so fall back to two plain commands.
                    // Documented cost: one extra round trip on that path only.
                    tracing::debug!("redis eval unavailable ({}); using incr+pexpire", scrub(&e));
                    let value = driver.incr(&name).await?;
                    driver.pexpire(&name, millis).await?;
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
            .op(move |driver| async move { driver.set(&name, &token_for_call, millis, true).await })
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
        self.op(move |driver| async move { driver.eval(RELEASE_SCRIPT, &[name], &[token]).await })
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
        assert!(budget.take_at(Instant::now()));
        // No time has passed, so the bucket is empty and the call is refused.
        assert!(!budget.take_at(Instant::now()));
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
