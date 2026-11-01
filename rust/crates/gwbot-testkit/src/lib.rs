//! In-process test harness for the giveaway engine.
//!
//! One [`Harness`] is one bot process: a fresh database, a [`GiveawayService`]
//! over it, and a [`ManualClock`] it owns. Tests call operations by name through
//! [`Harness::call`], exactly as the Python suite called them over the old
//! JSON-line protocol, so every ported test reads the same way while the wire
//! format and the subprocess are gone.
//!
//! Three rules keep the suite honest:
//!
//! * every fixture starts a fresh database, so no test can be made green by
//!   another test's leftovers;
//! * the clock only moves when a test moves it, so an "expires in five minutes"
//!   assertion never races a slow CI box;
//! * the clock belongs to the harness, not to the process, so tests can run in
//!   parallel threads without moving each other's deadlines.
//!
//! Nothing here needs a Discord token, a network connection or a Turso account.

#![deny(unsafe_code)]

use std::fmt;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;

use anyhow::Result;
use gwbot_core::db::Database;
use gwbot_core::models::{system_now_ms, Giveaway, ManualClock, PartialFlush, ServiceError};
use gwbot_core::service::GiveawayService;
use serde_json::{json, Map, Value};
use tempfile::TempDir;

pub mod dispatch;
pub mod fakes;

/// The instant every fixture starts at: a fixed, round epoch so a test can name
/// an absolute deadline instead of measuring elapsed time.
pub const START_MS: i64 = 1_700_000_000_000;

/// A failure the harness surfaced, keeping the shape the Python client raised.
///
/// A refusal stays a [`ServiceError`] so a test can assert on the rule (and on
/// `kind`) rather than on a formatted message.
#[derive(Debug)]
pub enum CallError {
    /// The service refused, with its user-facing message and optional tag.
    Service(ServiceError),
    /// A batched write stopped part way, after `applied` rows landed.
    Partial { applied: usize, message: String },
    /// Anything else: a protocol mistake in the test, or a database failure.
    Other(anyhow::Error),
}

impl CallError {
    pub fn service(&self) -> Option<&ServiceError> {
        match self {
            Self::Service(err) => Some(err),
            _ => None,
        }
    }

    /// The refusal message, or `None` when this was not a refusal.
    pub fn message(&self) -> Option<&str> {
        match self {
            Self::Service(err) => Some(&err.message),
            _ => None,
        }
    }

    /// Rows a partial flush had already committed.
    pub fn applied(&self) -> Option<usize> {
        match self {
            Self::Partial { applied, .. } => Some(*applied),
            _ => None,
        }
    }
}

impl fmt::Display for CallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Service(err) => write!(f, "{}", err.message),
            Self::Partial { message, .. } => f.write_str(message),
            Self::Other(err) => write!(f, "{err:#}"),
        }
    }
}

impl std::error::Error for CallError {}

/// One bot process: a database, a service over it, and a clock.
///
/// The service is the real one; the database is real SQLite. Nothing is mocked
/// except the clock, which is the point.
pub struct Harness {
    db_path: PathBuf,
    /// Owned only when this harness created the directory, so a sibling or a
    /// reopen never deletes the file the fixture still needs.
    _tmp: Option<TempDir>,
    clock: std::sync::Arc<ManualClock>,
    /// Kept so a reopen keeps the same (possibly fake) cache.
    redis: Option<gwbot_core::cache::RedisCache>,
    service: GiveawayService,
}

impl Harness {
    /// A fresh database in its own temporary directory.
    pub async fn new() -> Self {
        let tmp = TempDir::new().expect("could not create a temporary directory");
        let db_path = tmp.path().join("test.db");
        let mut harness = Self::with_db(db_path).await;
        harness._tmp = Some(tmp);
        harness
    }

    /// A fresh database at `path`, for a fixture that owns the directory.
    pub async fn with_db(path: impl Into<PathBuf>) -> Self {
        Self::with_db_and_redis(path, None).await
    }

    /// A fresh database plus an injected read cache.
    ///
    /// The cache is the real [`RedisCache`] over a fake driver, so the service's
    /// cache integration is exercised without a server.
    pub async fn with_db_and_redis(
        path: impl Into<PathBuf>,
        redis: Option<gwbot_core::cache::RedisCache>,
    ) -> Self {
        let db_path = path.into();
        let clock = std::sync::Arc::new(ManualClock::new(START_MS));
        let service = Self::open(&db_path, clock.clone(), redis.clone()).await;
        Self {
            db_path,
            _tmp: None,
            clock,
            redis,
            service,
        }
    }

    async fn open(
        path: &Path,
        clock: std::sync::Arc<ManualClock>,
        redis: Option<gwbot_core::cache::RedisCache>,
    ) -> GiveawayService {
        let db = Database::open_local(&path.to_string_lossy())
            .unwrap_or_else(|err| panic!("could not open {}: {err}", path.display()));
        db.init_schema()
            .await
            .unwrap_or_else(|err| panic!("could not create the schema: {err:#}"));
        // No Redis by default: the suite must never need a network. The service
        // treats an absent cache exactly like an unreachable one, which the bot
        // also runs in production.
        GiveawayService::with_clock(db, redis, clock)
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn service(&self) -> &GiveawayService {
        &self.service
    }

    pub fn clock(&self) -> &ManualClock {
        &self.clock
    }

    // -- operations ------------------------------------------------------

    /// Run one operation and return its result, or its error.
    pub async fn call(&self, op: &str, args: Value) -> Result<Value, CallError> {
        let args = match args {
            Value::Null => Map::new(),
            Value::Object(map) => map,
            other => panic!("{op}: arguments must be an object, got {other}"),
        };
        match dispatch::dispatch(&self.service, &self.clock, op, &args).await {
            Ok(value) => Ok(value),
            Err(err) => Err(classify(err)),
        }
    }

    /// Run one operation, panicking with its name when it fails.
    ///
    /// Ported tests read better with this than with a bare `.unwrap()`, and a
    /// panic that names the operation is worth the extra helper.
    pub async fn ok(&self, op: &str, args: Value) -> Value {
        match self.call(op, args).await {
            Ok(value) => value,
            Err(err) => panic!("{op} failed: {err}"),
        }
    }

    /// Run one operation and return its refusal, panicking if it succeeded.
    pub async fn refuse(&self, op: &str, args: Value) -> ServiceError {
        match self.call(op, args).await {
            Ok(value) => panic!("{op} was expected to be refused, but returned {value}"),
            Err(CallError::Service(err)) => err,
            Err(other) => panic!("{op} was expected to be refused, but failed with {other}"),
        }
    }

    // -- storage ---------------------------------------------------------

    /// Run a statement straight against the test database.
    ///
    /// Used to assert storage state (row counts, guarded writes) without
    /// trusting the service to report on itself.
    pub async fn sql(&self, statement: &str) -> Value {
        self.ok("sql", json!({ "sql": statement })).await
    }

    pub async fn sql_params(&self, statement: &str, params: Value) -> Value {
        self.ok("sql", json!({ "sql": statement, "params": params }))
            .await
    }

    pub async fn one(&self, statement: &str) -> Option<Value> {
        self.one_params(statement, json!([])).await
    }

    pub async fn one_params(&self, statement: &str, params: Value) -> Option<Value> {
        self.sql_params(statement, params)
            .await
            .as_array()
            .and_then(|rows| rows.first().cloned())
    }

    pub async fn scalar(&self, statement: &str) -> Option<Value> {
        self.scalar_params(statement, json!([])).await
    }

    pub async fn scalar_params(&self, statement: &str, params: Value) -> Option<Value> {
        let row = self.one_params(statement, params).await?;
        row.as_object().and_then(|map| map.values().next().cloned())
    }

    pub async fn scalar_int(&self, statement: &str) -> Option<i64> {
        self.scalar(statement).await.and_then(|v| v.as_i64())
    }

    // -- clock -----------------------------------------------------------

    pub fn now(&self) -> i64 {
        self.clock.now()
    }

    /// Move the clock forward and return the new instant.
    pub fn advance(&self, milliseconds: i64) -> i64 {
        self.clock.advance(milliseconds)
    }

    pub fn set_time(&self, milliseconds: i64) -> i64 {
        self.clock.set(milliseconds)
    }

    // -- lifecycle -------------------------------------------------------

    /// A second bot process on the same file, warm and ready.
    ///
    /// The `ping` round-trip proves the service is serving before a test starts
    /// racing it.
    pub async fn sibling(&self) -> Self {
        let harness = Self::with_db(&self.db_path).await;
        assert_eq!(json!("pong"), harness.ok("ping", Value::Null).await);
        harness
    }

    /// Simulate a restart: close this process and reopen the same file.
    pub async fn reopen(&mut self) {
        let path = self.db_path.clone();
        let clock = self.clock.clone();
        let redis = self.redis.clone();
        self.service.db().close().await;
        self.service = Self::open(&path, clock, redis).await;
    }

    pub async fn close(self) {
        self.service.db().close().await;
    }
}

/// Walk the error chain so a refusal that travelled through `?` and gained
/// context still reports as a refusal, not as a generic failure.
fn classify(err: anyhow::Error) -> CallError {
    for cause in err.chain() {
        if let Some(service) = cause.downcast_ref::<ServiceError>() {
            return CallError::Service(service.clone());
        }
        if let Some(partial) = cause.downcast_ref::<PartialFlush>() {
            return CallError::Partial {
                applied: partial.applied,
                message: partial.to_string(),
            };
        }
    }
    CallError::Other(err)
}

// -- concurrency ---------------------------------------------------------

/// The value or failure one concurrent job produced.
pub type RaceResult = Result<Value, CallError>;

/// A job's future, boxed so jobs of different shapes fit in one list.
pub type RaceFuture<'a> = Pin<Box<dyn Future<Output = RaceResult> + Send + 'a>>;

/// One concurrent job: a closure that drives a harness and returns its value.
pub type RaceJob = Box<dyn for<'a> FnOnce(&'a Harness) -> RaceFuture<'a> + Send>;

/// Run every job at the same instant, each on its own harness.
///
/// The barrier makes the race real rather than sequential: without it the first
/// writer would finish long before the second started, and the test would pass
/// even if the guard it targets did not exist. Each job gets its own thread and
/// its own runtime -- the in-process stand-in for the separate bot processes the
/// Python suite started, sharing one SQLite file and its busy timeout.
pub fn run_concurrently(jobs: Vec<(&Harness, RaceJob)>) -> Vec<RaceResult> {
    let barrier = std::sync::Barrier::new(jobs.len());
    std::thread::scope(|scope| {
        let handles: Vec<_> = jobs
            .into_iter()
            .map(|(harness, job)| {
                let barrier = &barrier;
                scope.spawn(move || {
                    let runtime = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .expect("could not build a test runtime");
                    barrier.wait();
                    runtime.block_on(job(harness))
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| handle.join().expect("a concurrent job panicked"))
            .collect()
    })
}

/// `ok` / `refused` / `error`, the labels the Python race helper used.
pub fn race_status(result: &RaceResult) -> &'static str {
    match result {
        Ok(_) => "ok",
        Err(CallError::Service(_)) => "refused",
        Err(_) => "error",
    }
}

/// Box one service call as a race job.
///
/// The op name is `'static` because a job outlives the statement that built it;
/// the arguments are owned for the same reason.
pub fn call_job(op: &'static str, args: Value) -> RaceJob {
    Box::new(move |h: &Harness| Box::pin(async move { h.call(op, args).await }))
}

/// Box several calls as one race job, in order, aborting at the first refusal.
pub fn calls_job(calls: Vec<(&'static str, Value)>) -> RaceJob {
    Box::new(move |h: &Harness| {
        Box::pin(async move {
            let mut out = Vec::with_capacity(calls.len());
            for (op, args) in calls {
                out.push(h.call(op, args).await?);
            }
            Ok(Value::Array(out))
        })
    })
}

/// Join several members from one process, in order; returns their counts.
///
/// A refusal aborts the batch: a race test wants the job to report the failure
/// rather than a partial list that silently hides it.
pub fn join_batch_job(gw: Value, users: Vec<String>) -> RaceJob {
    Box::new(move |h: &Harness| {
        Box::pin(async move {
            let mut counts = Vec::with_capacity(users.len());
            for user in &users {
                counts.push(join(h, &gw, user, json!({})).await?);
            }
            Ok(Value::Array(counts))
        })
    })
}

/// One join with per-call overrides (roles, a stale snapshot, `timed_out`).
pub fn join_job(gw: Value, user_id: String, overrides: Value) -> RaceJob {
    Box::new(move |h: &Harness| Box::pin(async move { join(h, &gw, &user_id, overrides).await }))
}

/// Leave several entries from one process, in order; returns the flags.
pub fn leave_batch_job(giveaway_id: Value, users: Vec<String>) -> RaceJob {
    Box::new(move |h: &Harness| {
        Box::pin(async move {
            let mut flags = Vec::with_capacity(users.len());
            for user in &users {
                flags.push(
                    h.call(
                        "leave",
                        json!({"giveaway_id": giveaway_id, "user_id": user}),
                    )
                    .await?,
                );
            }
            Ok(Value::Array(flags))
        })
    })
}

// -- convenience builders ------------------------------------------------

/// Merge `overrides` into `defaults`, one level deep.
pub fn merge(defaults: Value, overrides: Value) -> Value {
    let (Value::Object(mut base), Value::Object(extra)) = (defaults, overrides) else {
        panic!("merge expects two objects");
    };
    for (key, value) in extra {
        base.insert(key, value);
    }
    Value::Object(base)
}

/// Create a giveaway with sane defaults, overridable per test.
pub async fn create(h: &Harness, overrides: Value) -> Value {
    let args = merge(
        json!({
            "guild_id": "100000000000000001",
            "channel_id": "100000000000000002",
            "prize": "Nitro",
            "winner_count": 1,
            "duration_seconds": 3600,
            "created_by": "100000000000000003",
        }),
        overrides,
    );
    h.ok("create", args).await
}

/// Join a giveaway with a member that passes every requirement.
pub async fn join(
    h: &Harness,
    gw: &Value,
    user_id: &str,
    overrides: Value,
) -> Result<Value, CallError> {
    let args = merge(
        json!({
            "gw": gw,
            "user_id": user_id,
            "username": format!("user{}", &user_id[user_id.len().saturating_sub(3)..]),
            "member_roles": [],
            "account_created_ts": Value::Null,
        }),
        overrides,
    );
    h.call("join", args).await
}

/// The id of a giveaway value, whether it is an object or a bare id.
pub fn gw_id(gw: &Value) -> String {
    match gw {
        Value::String(id) => id.clone(),
        Value::Object(map) => map
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        other => panic!("expected a giveaway or its id, got {other}"),
    }
}

/// Decode a `Giveaway` out of a JSON value.
pub fn giveaway(value: &Value) -> Giveaway {
    serde_json::from_value(value.clone()).expect("value is not a giveaway")
}

/// The `[giveaway, winners]` pair `end`/`reroll`/`expire_claim` return.
pub fn ended(value: &Value) -> (Giveaway, Vec<String>) {
    let pair = value.as_array().expect("expected [giveaway, winners]");
    let gw = giveaway(&pair[0]);
    let winners = pair[1]
        .as_array()
        .expect("expected a winner list")
        .iter()
        .map(|item| item.as_str().unwrap_or_default().to_string())
        .collect();
    (gw, winners)
}

/// A fixture: one database, the fixed ids every test uses, and the helpers the
/// Python base class provided.
///
/// It owns the temporary directory so a recovery test can close the process and
/// reopen the *same* file.
pub struct Fixture {
    pub h: Harness,
    tmp: TempDir,
    db_path: PathBuf,
    redis: Option<gwbot_core::cache::RedisCache>,
    pub guild: String,
    pub other_guild: String,
    pub channel: String,
}

impl Fixture {
    pub async fn new() -> Self {
        Self::build(None).await
    }

    /// A fixture whose service has an injected (fake-backed) cache.
    pub async fn with_redis(redis: gwbot_core::cache::RedisCache) -> Self {
        Self::build(Some(redis)).await
    }

    async fn build(redis: Option<gwbot_core::cache::RedisCache>) -> Self {
        let tmp = TempDir::new().expect("could not create a temporary directory");
        let db_path = tmp.path().join("test.db");
        let h = Harness::with_db_and_redis(&db_path, redis.clone()).await;
        Self {
            h,
            tmp,
            db_path,
            redis,
            guild: "111111111111111111".to_string(),
            other_guild: "222222222222222222".to_string(),
            channel: "333333333333333333".to_string(),
        }
    }

    /// Start another harness on this fixture's database.
    pub async fn make(&self) -> Harness {
        Harness::with_db_and_redis(&self.db_path, self.redis.clone()).await
    }

    /// A second bot process on the same file, warm and ready.
    pub async fn sibling(&self) -> Harness {
        let harness = self.make().await;
        assert_eq!(json!("pong"), harness.ok("ping", Value::Null).await);
        harness
    }

    /// Simulate a restart: close this process, reopen the same file.
    pub async fn reopen(&mut self) {
        self.h.reopen().await;
    }

    // -- giveaways -------------------------------------------------------

    pub async fn gw(&self, overrides: Value) -> Value {
        let overrides = merge(
            json!({ "guild_id": self.guild, "channel_id": self.channel }),
            overrides,
        );
        create(&self.h, overrides).await
    }

    /// Join and return the entry count the caller would print.
    pub async fn enter(&self, gw: &Value, user_id: &str) -> i64 {
        self.enter_with(gw, user_id, json!({})).await
    }

    pub async fn enter_with(&self, gw: &Value, user_id: &str, overrides: Value) -> i64 {
        join(&self.h, gw, user_id, overrides)
            .await
            .unwrap_or_else(|err| panic!("join failed: {err}"))
            .as_i64()
            .unwrap_or_else(|| panic!("join did not return a count"))
    }

    /// Join and return the refusal, failing the test if it succeeded.
    pub async fn join_error(&self, gw: &Value, user_id: &str) -> ServiceError {
        self.join_error_with(gw, user_id, json!({})).await
    }

    /// A refused join with per-call overrides: a stale snapshot, roles, age.
    pub async fn join_error_with(
        &self,
        gw: &Value,
        user_id: &str,
        overrides: Value,
    ) -> ServiceError {
        join(&self.h, gw, user_id, overrides)
            .await
            .err()
            .and_then(|err| err.service().cloned())
            .unwrap_or_else(|| panic!("join was expected to be refused"))
    }

    pub async fn timed_join_error(&self, gw: &Value, user_id: &str) -> ServiceError {
        self.timed_join_error_with(gw, user_id, json!({})).await
    }

    pub async fn timed_join_error_with(
        &self,
        gw: &Value,
        user_id: &str,
        overrides: Value,
    ) -> ServiceError {
        let overrides = merge(json!({ "timed_out": true }), overrides);
        self.join_error_with(gw, user_id, overrides).await
    }

    pub async fn end(&self, gw: &Value) -> (Giveaway, Vec<String>) {
        let value = self.h.ok("end", json!({ "giveaway_id": gw_id(gw) })).await;
        ended(&value)
    }

    pub async fn end_with(&self, gw: &Value, overrides: Value) -> (Giveaway, Vec<String>) {
        let args = merge(json!({ "giveaway_id": gw_id(gw) }), overrides);
        ended(&self.h.ok("end", args).await)
    }

    /// End and return the refusal, failing the test if it succeeded.
    pub async fn end_error(&self, gw: &Value) -> ServiceError {
        self.end_error_with(gw, json!({})).await
    }

    pub async fn end_error_with(&self, gw: &Value, overrides: Value) -> ServiceError {
        let args = merge(json!({ "giveaway_id": gw_id(gw) }), overrides);
        self.h
            .call("end", args)
            .await
            .err()
            .and_then(|err| err.service().cloned())
            .unwrap_or_else(|| panic!("end was expected to be refused"))
    }

    /// Create a giveaway, enter `entrants` members, and end it.
    pub async fn draw(&self, entrants: usize, overrides: Value) -> (Giveaway, Vec<String>) {
        let gw = self.gw(overrides).await;
        for index in 0..entrants {
            self.enter(&gw, &format!("20000000000000{index:04}")).await;
        }
        self.end(&gw).await
    }

    /// A Discord account creation timestamp that is `days` old right now.
    ///
    /// The service compares account age against the real wall clock, not the
    /// harness clock, so this cannot be derived from [`Harness::now`].
    pub fn account_age(&self, days: f64) -> f64 {
        system_now_ms() as f64 / 1000.0 - days * 86400.0
    }

    // -- storage ---------------------------------------------------------

    pub async fn execute(&self, statement: &str) -> i64 {
        self.h
            .ok("exec", json!({ "sql": statement }))
            .await
            .as_i64()
            .unwrap_or(0)
    }

    pub async fn execute_params(&self, statement: &str, params: Value) -> i64 {
        self.h
            .ok("exec", json!({ "sql": statement, "params": params }))
            .await
            .as_i64()
            .unwrap_or(0)
    }

    pub async fn trigger(&self, statement: &str) {
        self.execute(statement).await;
    }

    pub async fn drop_trigger(&self, name: &str) {
        self.execute(&format!("DROP TRIGGER IF EXISTS {name}"))
            .await;
    }

    pub async fn entry_rows(&self, giveaway_id: &str) -> i64 {
        self.h
            .scalar_params(
                "SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?",
                json!([giveaway_id]),
            )
            .await
            .and_then(|value| value.as_i64())
            .unwrap_or(0)
    }

    /// The fixture's directory, kept alive by `tmp`.
    pub fn dir(&self) -> &Path {
        self.tmp.path()
    }
}

/// Read `key` off a JSON object, panicking with a useful message when absent.
pub fn field<'a>(value: &'a Value, key: &str) -> &'a Value {
    value
        .get(key)
        .unwrap_or_else(|| panic!("missing field `{key}` in {value}"))
}

/// Read `key` off a JSON object as a string.
pub fn field_str<'a>(value: &'a Value, key: &str) -> &'a str {
    field(value, key)
        .as_str()
        .unwrap_or_else(|| panic!("field `{key}` is not a string"))
}

/// Read `key` off a JSON object as an integer.
pub fn field_int(value: &Value, key: &str) -> i64 {
    field(value, key)
        .as_i64()
        .unwrap_or_else(|| panic!("field `{key}` is not an integer"))
}

/// The string ids of a list value.
pub fn ids(value: &Value) -> Vec<String> {
    value
        .as_array()
        .unwrap_or_else(|| panic!("expected a list, got {value}"))
        .iter()
        .map(|item| item.as_str().unwrap_or_default().to_string())
        .collect()
}
