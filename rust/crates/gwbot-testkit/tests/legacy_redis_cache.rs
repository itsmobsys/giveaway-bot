//! Port of the retired Python `test_redis_cache.py`: the optional Redis cache, with no
//! network and no credentials.
//!
//! The Python suite injected a fake async client through `RedisCache(connect=)`
//! and a fake clock through `clock=`. The Rust cache exposes the same two seams
//! ([`RedisCache::with_driver`] / [`RedisCache::with_driver_and_clock`]), so
//! every case here exercises the cache's own behaviour -- budget, breaker, key
//! namespacing, the EVAL fallback -- against [`FakeRedis`] and a
//! [`ManualCacheClock`], exactly as before.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use gwbot_core::cache::{
    CacheClock, CacheOptions, Driver, ManualCacheClock, RedisCache, OP_CEILING_PER_SEC,
};
use gwbot_core::config::Settings;
use gwbot_testkit::fakes::FakeRedis;
use serde_json::{json, Value};

/// Python's `cache_for(fake, **kwargs)`: an injected fake on the default
/// budget, breaker and timeouts.
fn cache_for(fake: &Arc<FakeRedis>) -> RedisCache {
    let driver: Arc<dyn Driver> = fake.clone();
    RedisCache::with_driver(driver, CacheOptions::default())
}

/// The same, with tunables overridden.
fn cache_with(fake: &Arc<FakeRedis>, opts: CacheOptions) -> RedisCache {
    let driver: Arc<dyn Driver> = fake.clone();
    RedisCache::with_driver(driver, opts)
}

/// The same, with an injected clock so budget refill and cooldown are
/// deterministic instead of slept through.
fn cache_with_clock(
    fake: &Arc<FakeRedis>,
    opts: CacheOptions,
    clock: &Arc<ManualCacheClock>,
) -> RedisCache {
    let driver: Arc<dyn Driver> = fake.clone();
    let clock: Arc<dyn CacheClock> = clock.clone();
    RedisCache::with_driver_and_clock(driver, opts, clock)
}

/// One counter out of `stats()`.
async fn counter(cache: &RedisCache, name: &str) -> u64 {
    cache.stats().await["counters"][name]
        .as_u64()
        .unwrap_or_else(|| panic!("stats has no counter {name}"))
}

// -- degraded mode -----------------------------------------------------------

#[tokio::test]
async fn no_url_is_inert_and_never_raises() {
    let cache = RedisCache::new("", CacheOptions::default());
    assert!(!cache.configured());
    assert!(!cache.available().await);
    assert!(cache.get("k").await.is_none());
    assert!(!cache.set("k", "v", 5.0).await);
    assert_eq!(0, cache.delete(&["k"]).await);
    assert!(cache.get_json("k").await.is_none());
    assert!(!cache.set_json("k", &json!({"a": 1}), 5.0).await);
    assert!(cache.incr_with_ttl("k", 5.0).await.is_none());
    assert!(cache.acquire("k", 5.0).await.is_none());
    assert!(!cache.release("k", "tok").await);
    assert!(!cache.start().await);
    assert!(cache.ping_ms().await.is_none());
    cache.aclose().await;
}

#[tokio::test]
async fn disabled_switch_beats_a_valid_url() {
    let fake = Arc::new(FakeRedis::new());
    let settings = Settings {
        redis_url: "redis://example.invalid:6379/0".to_string(),
        redis_enabled: false,
        ..Settings::default()
    };
    let cache = RedisCache::from_settings(&settings);
    assert!(!cache.configured());
    assert!(cache.get("k").await.is_none());
    assert!(fake.calls().is_empty());
}

#[tokio::test]
async fn a_dead_server_degrades_then_opens_the_breaker() {
    let fake = Arc::new(FakeRedis::new());
    fake.fail_with("host 1.2.3.4:19722 refused");
    let clock = Arc::new(ManualCacheClock::new());
    let cache = cache_with_clock(
        &fake,
        CacheOptions {
            breaker_threshold: 3,
            breaker_cooldown: 30.0,
            ..CacheOptions::default()
        },
        &clock,
    );

    for _ in 0..3 {
        assert!(cache.get("k").await.is_none());
    }
    assert!(!cache.available().await);
    let attempts = fake.calls().len();
    // While the breaker is open nothing reaches the network at all.
    for _ in 0..5 {
        assert!(cache.get("k").await.is_none());
    }
    assert_eq!(attempts, fake.calls().len());
    assert!(counter(&cache, "skipped").await >= 5);
    assert_eq!(json!(true), cache.stats().await["breaker_open"]);

    // After the cooldown it tries again and a success closes the breaker.
    clock.advance(31.0);
    fake.clear_failure();
    fake.put("gwbot:v1:k", "v");
    assert_eq!(Some("v".to_string()), cache.get("k").await);
    assert!(cache.available().await);
    assert_eq!(json!(false), cache.stats().await["breaker_open"]);
}

#[tokio::test]
async fn malformed_json_is_a_miss() {
    let fake = Arc::new(FakeRedis::new());
    let cache = cache_for(&fake);
    fake.put("gwbot:v1:gw:gw_1", "{not json");
    assert!(cache.get_json("gw:gw_1").await.is_none());
}

#[tokio::test]
async fn a_driver_that_cannot_be_built_is_a_miss_not_a_crash() {
    // ADAPTED: Python injected a `connect` callable that raised
    // ModuleNotFoundError. Rust has no driver factory to replace, so the
    // equivalent unbuildable driver is a URL the redis client refuses to parse:
    // the cache stays `configured`, every call falls through, and nothing
    // panics.
    let cache = RedisCache::new("http://example.invalid:6379/0", CacheOptions::default());
    assert!(cache.configured());
    assert!(cache.get("k").await.is_none());
    assert!(!cache.start().await);
    assert!(counter(&cache, "skipped").await >= 1);
}

// -- budget ------------------------------------------------------------------

#[tokio::test]
async fn over_budget_ops_are_skipped_without_touching_redis() {
    let fake = Arc::new(FakeRedis::new());
    let clock = Arc::new(ManualCacheClock::new());
    let cache = cache_with_clock(
        &fake,
        CacheOptions {
            op_budget_per_sec: 2.0,
            ..CacheOptions::default()
        },
        &clock,
    );
    fake.put("gwbot:v1:k", "v");

    assert_eq!(Some("v".to_string()), cache.get("k").await);
    assert_eq!(Some("v".to_string()), cache.get("k").await);
    // Budget spent: the next calls must not reach the client at all.
    assert!(cache.get("k").await.is_none());
    assert!(cache.get("k").await.is_none());
    assert_eq!(2, fake.count("get"));
    assert_eq!(2, counter(&cache, "skipped").await);

    // The bucket refills over time.
    clock.advance(1.0);
    assert_eq!(Some("v".to_string()), cache.get("k").await);
}

#[tokio::test]
async fn a_sliding_second_stays_under_the_plan_ceiling() {
    // The plan's limit is OPS PER SECOND, so whole seconds must be checked. A
    // token bucket cannot promise anything about an interval: it can promise
    // that a burst of its capacity plus one second of refill fits in a second.
    // With capacity + rate held at the ceiling that sum never overshoots the
    // managed plan (measured 159 ops in one second when capacity was rate).
    let fake = Arc::new(FakeRedis::new());
    let clock = Arc::new(ManualCacheClock::new());
    let rate = 80.0;
    let cache = cache_with_clock(
        &fake,
        CacheOptions {
            op_budget_per_sec: rate,
            ..CacheOptions::default()
        },
        &clock,
    );
    fake.put("gwbot:v1:k", "v");

    let mut stamps: Vec<f64> = Vec::new();
    let mut now = 0.0_f64;
    // One instant: the bucket's whole allowance, with no time passing.
    for _ in 0..(rate as usize * 2) {
        if cache.get("k").await.is_some() {
            stamps.push(now);
        }
    }
    // Then exactly one interval between attempts, for 2.0s of virtual time.
    let step = 1.0 / rate;
    for _ in 0..(rate as usize * 2) {
        clock.advance(step);
        now += step;
        if cache.get("k").await.is_some() {
            stamps.push(now);
        }
    }
    assert!(
        stamps.len() as f64 > rate,
        "the bucket must still serve its allowance"
    );
    let worst = stamps
        .iter()
        .map(|start| {
            stamps
                .iter()
                .filter(|t| **t >= *start && **t < *start + 1.0)
                .count()
        })
        .max()
        .unwrap_or(0);
    assert!(
        worst as f64 <= OP_CEILING_PER_SEC,
        "{worst} ops inside one sliding second exceeds the plan ceiling"
    );
}

#[tokio::test]
async fn a_partial_interval_buys_nothing() {
    // A partly elapsed interval is not a token, however many callers ask.
    let fake = Arc::new(FakeRedis::new());
    let clock = Arc::new(ManualCacheClock::new());
    let cache = cache_with_clock(
        &fake,
        CacheOptions {
            op_budget_per_sec: 80.0,
            ..CacheOptions::default()
        },
        &clock,
    );
    fake.put("gwbot:v1:k", "v");

    let mut served = 0;
    while cache.get("k").await.is_some() {
        // Drain the whole burst allowance.
        served += 1;
    }
    assert!(served > 0);
    let gets = fake.count("get");
    clock.advance(0.005); // half an interval at 80/s (0.0125s)
    for _ in 0..40 {
        assert!(
            cache.get("k").await.is_none(),
            "half an interval must not pay for 40 ops"
        );
    }
    assert_eq!(gets, fake.count("get"));
    // One whole interval buys exactly one op -- not two.
    clock.advance(0.0125);
    assert!(cache.get("k").await.is_some());
    assert!(cache.get("k").await.is_none());
    assert_eq!(gets + 1, fake.count("get"), "one interval, one op");
}

#[tokio::test]
async fn the_sustained_rate_is_the_ceiling() {
    // Over a long window the bucket may send no more than rate + capacity.
    let fake = Arc::new(FakeRedis::new());
    let clock = Arc::new(ManualCacheClock::new());
    let rate = 10.0;
    let cache = cache_with_clock(
        &fake,
        CacheOptions {
            op_budget_per_sec: rate,
            ..CacheOptions::default()
        },
        &clock,
    );

    for _ in 0..500 {
        clock.advance(0.01); // one interval (0.1s) every ten calls: 5.0s
        cache.get("k").await;
    }
    let sends = fake.count("get") as f64;
    assert!(
        sends <= rate * 5.0 + rate,
        "capacity plus five seconds of refill"
    );
    assert!(sends >= rate, "the bucket must still serve its allowance");
}

// -- operations --------------------------------------------------------------

#[tokio::test]
async fn set_get_delete_round_trip_and_millis_ttl() {
    let fake = Arc::new(FakeRedis::new());
    let cache = cache_for(&fake);

    assert!(cache.set("count:gw_1", "7", 2.5).await);
    let set_call = fake
        .calls()
        .into_iter()
        .find(|c| c.name == "set")
        .expect("a SET call");
    assert_eq!(
        vec!["gwbot:v1:count:gw_1".to_string(), "7".to_string()],
        set_call.args
    );
    assert_eq!(Some(2500), set_call.px);
    assert!(!set_call.nx);

    assert_eq!(Some("7".to_string()), cache.get("count:gw_1").await);
    let last = fake.calls().pop().expect("a call");
    assert!(
        last.args[0].starts_with("gwbot:v1:"),
        "keys are namespaced: {:?}",
        last.args
    );

    assert!(cache.set_json("gw:gw_1", &json!({"id": "gw_1"}), 2.0).await);
    assert_eq!(Some(json!({"id": "gw_1"})), cache.get_json("gw:gw_1").await);
    assert_eq!(2, cache.delete(&["count:gw_1", "gw:gw_1"]).await);
}

#[tokio::test]
async fn incr_with_ttl_uses_one_round_trip_and_sets_the_ttl() {
    let fake = Arc::new(FakeRedis::new());
    let cache = cache_for(&fake);

    assert_eq!(Some(1), cache.incr_with_ttl("count:gw_1", 30.0).await);
    assert_eq!(1, fake.calls().len());
    assert_eq!("eval", fake.calls()[0].name);
    assert_eq!(Some(30_000), fake.ttl("gwbot:v1:count:gw_1"));

    assert_eq!(Some(2), cache.incr_with_ttl("count:gw_1", 30.0).await);
    assert_eq!(Some(30_000), fake.ttl("gwbot:v1:count:gw_1"));
}

#[tokio::test]
async fn incr_falls_back_when_scripting_is_unavailable() {
    let fake = Arc::new(FakeRedis::new());
    fake.fail_eval(true);
    let cache = cache_for(&fake);

    assert_eq!(Some(1), cache.incr_with_ttl("count:gw_1", 30.0).await);
    let names = fake.call_names();
    assert!(names.iter().any(|n| n == "incr"), "{names:?}");
    assert!(names.iter().any(|n| n == "pexpire"), "{names:?}");
    assert_eq!(Some(30_000), fake.ttl("gwbot:v1:count:gw_1"));
}

#[tokio::test]
async fn lock_is_only_released_by_its_owner() {
    let fake = Arc::new(FakeRedis::new());
    let cache = cache_for(&fake);

    let token = cache
        .acquire("lock:join:gw_1:42", 10.0)
        .await
        .expect("the first holder gets a token");
    let acquire_call = fake
        .calls()
        .into_iter()
        .find(|c| c.name == "set")
        .expect("a SET call");
    assert!(acquire_call.nx, "a lock is SET ... NX");
    assert_eq!(Some(10_000), acquire_call.px);

    // A second holder gets nothing while the lock is held.
    assert!(cache.acquire("lock:join:gw_1:42", 10.0).await.is_none());
    // A stale/wrong token must not delete the live lock.
    assert!(!cache.release("lock:join:gw_1:42", "someone-elses").await);
    assert!(fake.get_raw("gwbot:v1:lock:join:gw_1:42").is_some());
    assert!(cache.release("lock:join:gw_1:42", &token).await);
    assert!(fake.get_raw("gwbot:v1:lock:join:gw_1:42").is_none());
}

#[tokio::test]
async fn json_payload_is_compact_and_unicode_safe() {
    let fake = Arc::new(FakeRedis::new());
    let cache = cache_for(&fake);

    assert!(
        cache
            .set_json("gw:gw_1", &json!({"prize": "café ☕", "n": 2}), 5.0)
            .await
    );
    let stored = fake
        .get_raw("gwbot:v1:gw:gw_1")
        .expect("the payload was stored");
    // Compact separators: no space after ':' or ',' outside the values.
    assert!(!stored.contains("\": "), "{stored}");
    assert!(!stored.contains(", "), "{stored}");
    let parsed: Value = serde_json::from_str(&stored).expect("stored JSON parses");
    assert_eq!(json!({"prize": "café ☕", "n": 2}), parsed);
}

/// A driver whose reads never answer, to prove the cache times them out.
struct Hanging;

#[async_trait]
impl Driver for Hanging {
    async fn ping(&self) -> Result<(), String> {
        Ok(())
    }

    async fn get(&self, _key: &str) -> Result<Option<String>, String> {
        tokio::time::sleep(Duration::from_secs(5)).await;
        Ok(None)
    }

    async fn set(
        &self,
        _key: &str,
        _value: &str,
        _millis: i64,
        _nx: bool,
    ) -> Result<Option<String>, String> {
        Ok(None)
    }

    async fn del(&self, _keys: &[String]) -> Result<i64, String> {
        Ok(0)
    }

    async fn eval(&self, _script: &str, _keys: &[String], _args: &[String]) -> Result<i64, String> {
        Ok(0)
    }

    async fn incr(&self, _key: &str) -> Result<i64, String> {
        Ok(0)
    }

    async fn pexpire(&self, _key: &str, _millis: i64) -> Result<i64, String> {
        Ok(0)
    }
}

#[tokio::test]
async fn a_stalled_call_times_out_instead_of_parking_the_caller() {
    let hanging = RedisCache::with_driver(
        Arc::new(Hanging),
        CacheOptions {
            socket_timeout: 0.01,
            socket_connect_timeout: 0.01,
            ..CacheOptions::default()
        },
    );
    // Python wrapped the call in `asyncio.wait_for(..., 1.0)`; the cache's own
    // hard timeout is connect + socket + 0.5s, so the caller is released well
    // inside the outer second.
    let outcome = tokio::time::timeout(Duration::from_secs(1), hanging.get("k")).await;
    assert_eq!(Ok(None), outcome);
}

// -- hygiene -----------------------------------------------------------------

#[tokio::test]
async fn stats_reveal_no_endpoint_or_secret() {
    // ADAPTED: Python passed the credential-bearing URL alongside the fake and
    // asserted `stats()` never echoed it. The Rust test seam stores no URL, and
    // `stats()` carries no error text -- only one bucket per error kind -- so
    // the check is that the endpoint, port, user and password in the driver's
    // failure never reach the serialised counters.
    let fake = Arc::new(FakeRedis::new());
    fake.fail_with(
        "auth failed for user default at redis-1.example:19722 password=supersecretpassword",
    );
    let cache = cache_with(&fake, CacheOptions::default());
    for _ in 0..6 {
        cache.get("k").await;
    }
    let blob = serde_json::to_string(&cache.stats().await).expect("stats serialise");
    for secret in ["supersecretpassword", "redis-1.example", "19722", "default"] {
        assert!(!blob.contains(secret), "stats leaked {secret}: {blob}");
    }
}

#[tokio::test]
async fn close_is_idempotent_and_ends_the_cache() {
    let fake = Arc::new(FakeRedis::new());
    let cache = cache_for(&fake);
    assert!(cache.get("k").await.is_none());
    cache.aclose().await;
    cache.aclose().await;
    assert!(!cache.available().await);
    let calls = fake.calls().len();
    assert!(cache.get("k").await.is_none());
    // ADAPTED: the driver trait has no close hook, so the observable end of the
    // cache is that it stops issuing commands rather than that the fake was
    // told to shut down.
    assert_eq!(
        calls,
        fake.calls().len(),
        "a closed cache must not reach Redis"
    );
}
