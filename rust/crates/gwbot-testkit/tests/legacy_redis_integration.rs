//! Port of the retired Python `test_redis_integration.py`, service half: Redis wired
//! into the giveaway engine, with no server and no network.
//!
//! `legacy_redis_cache.rs` drives the cache primitive on its own; these tests
//! drive the places the service actually uses it -- the cached display count,
//! the active-list key, the notify-role marker, the invalidation queue -- plus
//! the "Redis is not there" cases that must behave exactly like the pre-Redis
//! service.
//!
//! Every case here is one of the failures the integration has to survive: a
//! committed write that makes a cached key wrong, a Redis outage mid-join, a
//! flush bigger than one DEL. The database stays the only source of truth in
//! all of them.
//!
//! The bot half of the Python file (the sweep lease, the background flush, the
//! click read budget) needs the Discord adapter's private state and lives in
//! `gwbot-discord`'s own test module.

use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use gwbot_core::cache::{CacheOptions, Driver, RedisCache};
use gwbot_testkit::fakes::FakeRedis;
use gwbot_testkit::{gw_id, ids, Fixture, Harness};
use serde_json::{json, Value};

// -- fixtures ----------------------------------------------------------------

/// Python's `cache_for(fake)`: the real cache, a fake client.
fn cache_for(fake: &Arc<FakeRedis>) -> RedisCache {
    let driver: Arc<dyn Driver> = fake.clone();
    RedisCache::with_driver(driver, CacheOptions::default())
}

/// A fake-backed cache plus the handle that records its calls.
fn fake_cache() -> (RedisCache, Arc<FakeRedis>) {
    let fake = Arc::new(FakeRedis::new());
    (cache_for(&fake), fake)
}

/// A cache whose client cannot answer anything.
///
/// Not a degraded stub of the cache: the real breaker, the real op budget.
/// Only the client is hostile, which is what an unreachable or over-quota
/// server looks like from in here.
fn failing_cache() -> (RedisCache, Arc<FakeRedis>) {
    let (cache, fake) = fake_cache();
    fake.fail_with("connection refused");
    (cache, fake)
}

/// The giveaway every cached-count test starts from.
async fn make(f: &Fixture) -> Value {
    f.gw(json!({
        "prize": "Steam key",
        "duration_seconds": 600,
        "created_by": "9".repeat(18),
    }))
    .await
}

/// Join as a member whose account is old enough to pass every check.
async fn join(f: &Fixture, gw: &Value, user_id: &str) {
    f.enter_with(
        gw,
        user_id,
        json!({ "account_created_ts": f.account_age(400.0) }),
    )
    .await;
}

/// How many SQL reads the service's database has served so far.
///
/// Python wrapped `db.query` to see the exact statements. Rust has no
/// monkeypatching, but every read goes through the same counters, so a cache
/// hit is provable as "the read counter did not move".
async fn read_count(h: &Harness) -> u64 {
    h.ok("db_stats", Value::Null).await["reads"]
        .as_u64()
        .unwrap_or(0)
}

async fn reset_stats(h: &Harness) {
    h.ok("reset_stats", Value::Null).await;
}

async fn drain(f: &Fixture) -> Vec<String> {
    ids(&f.h.ok("drain_invalidations", Value::Null).await)
}

async fn flush(f: &Fixture) -> i64 {
    f.h.ok("flush_invalidations", Value::Null)
        .await
        .as_i64()
        .unwrap_or(0)
}

// -- the display count -------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn a_zero_count_is_cached_like_any_other() {
    // Zero entrants is an answer, not a miss. Leaving it uncached meant a
    // giveaway with no entrants yet could never be served from the cache:
    // every display read paid the counter fallback, which is a second query
    // and a second round trip on a remote database.
    let (cache, fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    assert_eq!(
        0,
        f.h.service().entry_count_cached(&id).await.expect("count")
    );
    assert!(!fake.is_empty(), "a zero count is a real answer");

    reset_stats(&f.h).await;
    assert_eq!(
        0,
        f.h.service().entry_count_cached(&id).await.expect("count")
    );
    assert_eq!(0, read_count(&f.h).await, "the second read is a cache hit");
}

#[tokio::test(flavor = "multi_thread")]
async fn cached_count_heals_after_a_committed_join() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    assert_eq!(
        0,
        f.h.service().entry_count_cached(&id).await.expect("count")
    );
    join(&f, &gw, &"5".repeat(18)).await;
    assert_eq!(1, flush(&f).await);

    assert_eq!(
        1,
        f.h.service().entry_count_cached(&id).await.expect("count"),
        "the flush must have dropped the key the join invalidated"
    );

    reset_stats(&f.h).await;
    assert_eq!(
        1,
        f.h.service().entry_count_cached(&id).await.expect("count")
    );
    assert_eq!(
        0,
        read_count(&f.h).await,
        "the healed count is served from the cache"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_committed_join_marks_the_count_key() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    join(&f, &gw, &"6".repeat(18)).await;
    assert!(
        drain(&f).await.contains(&format!("count:{id}")),
        "a committed join must invalidate the display count"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_leave_marks_the_count_key() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);
    let user = "7".repeat(18);

    join(&f, &gw, &user).await;
    drain(&f).await;
    assert_eq!(
        json!(true),
        f.h.ok("leave", json!({ "giveaway_id": id, "user_id": user }))
            .await
    );
    assert!(drain(&f).await.contains(&format!("count:{id}")));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_bulk_drop_marks_the_count_key() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);
    let user = "8".repeat(18);

    join(&f, &gw, &user).await;
    drain(&f).await;
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild,
            "user_id": user,
            "hours": 0,
            "reason": "test",
        }),
    )
    .await;
    assert!(
        drain(&f).await.contains(&format!("count:{id}")),
        "a ban purges entries, so the count key is wrong too"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_discarded_giveaway_leaves_the_active_list() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    f.h.ok("discard", json!({ "giveaway_id": id })).await;
    let spilled = drain(&f).await;
    assert!(spilled.contains(&"all:active".to_string()), "{spilled:?}");
    assert!(spilled.contains(&format!("count:{id}")), "{spilled:?}");
    assert_eq!(0, flush(&f).await, "nothing cached yet");
}

#[tokio::test(flavor = "multi_thread")]
async fn flush_sends_every_stale_key_in_one_call() {
    let (cache, fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    join(&f, &gw, &"9".repeat(18)).await;
    flush(&f).await;

    let deletes: Vec<_> = fake
        .calls()
        .into_iter()
        .filter(|call| call.name == "del")
        .collect();
    assert_eq!(1, deletes.len(), "one DEL for the whole batch");
    assert!(
        deletes[0].args.contains(&format!("gwbot:v1:count:{id}")),
        "{:?}",
        deletes[0].args
    );
    assert!(drain(&f).await.is_empty(), "the queue is emptied");
}

// -- other cached reads ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn a_notify_role_is_marked_for_guilds_that_never_set_one() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let svc = f.h.service();

    assert_eq!(None, svc.get_notify_role_cached(&f.guild).await);
    reset_stats(&f.h).await;
    assert_eq!(None, svc.get_notify_role_cached(&f.guild).await);
    assert_eq!(0, read_count(&f.h).await, "the 'none' marker must be a hit");

    svc.set_notify_role(&f.guild, Some("4242"))
        .await
        .expect("set role");
    svc.invalidate_notify_role(&f.guild).await;
    assert_eq!(
        Some("4242".to_string()),
        svc.get_notify_role_cached(&f.guild).await
    );
}

// -- the fallback must not park the runtime ---------------------------------

/// A driver whose every command spends `delay` waiting.
///
/// It is the stand-in for the slow Turso read the Python suite injected by
/// replacing `svc.entry_count` with a `time.sleep` spy.
struct Slow {
    delay: Duration,
}

#[async_trait]
impl Driver for Slow {
    async fn ping(&self) -> Result<(), String> {
        tokio::time::sleep(self.delay).await;
        Ok(())
    }

    async fn get(&self, _key: &str) -> Result<Option<String>, String> {
        tokio::time::sleep(self.delay).await;
        Ok(None)
    }

    async fn set(
        &self,
        _key: &str,
        _value: &str,
        _millis: i64,
        _nx: bool,
    ) -> Result<Option<String>, String> {
        tokio::time::sleep(self.delay).await;
        Ok(Some("OK".to_string()))
    }

    async fn del(&self, _keys: &[String]) -> Result<i64, String> {
        tokio::time::sleep(self.delay).await;
        Ok(0)
    }

    async fn eval(&self, _script: &str, _keys: &[String], _args: &[String]) -> Result<i64, String> {
        tokio::time::sleep(self.delay).await;
        Ok(0)
    }

    async fn incr(&self, _key: &str) -> Result<i64, String> {
        tokio::time::sleep(self.delay).await;
        Ok(0)
    }

    async fn pexpire(&self, _key: &str, _millis: i64) -> Result<i64, String> {
        tokio::time::sleep(self.delay).await;
        Ok(0)
    }
}

fn slow_cache() -> RedisCache {
    RedisCache::with_driver(
        Arc::new(Slow {
            delay: Duration::from_millis(150),
        }),
        CacheOptions::default(),
    )
}

/// Run `call` while a concurrent ticker counts its own 5ms steps.
///
/// ADAPTED: Python replaced the *synchronous* fallback with a `time.sleep`
/// spy and asserted it ran on a worker thread while the event loop kept
/// ticking. Rust has no monkeypatching, and every fallback here is an `async
/// fn` over an async database driver, so there is no blocking call to move off
/// the loop. The guarantee that still matters -- and that these tests pin -- is
/// that a cached reader *awaits* its legs: on a single-threaded runtime a
/// ticker must keep making progress while a 150ms-per-leg cache read is in
/// flight. A future change that made the reader block its thread would freeze
/// the ticker, exactly like the Python regression did.
async fn ticks_during<F, T>(call: F) -> (T, u64)
where
    F: Future<Output = T>,
{
    let ticks = Arc::new(AtomicU64::new(0));
    let counter = Arc::clone(&ticks);
    let ticker = tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_millis(5)).await;
            counter.fetch_add(1, Ordering::Relaxed);
        }
    });
    let value = call.await;
    ticker.abort();
    (value, ticks.load(Ordering::Relaxed))
}

#[tokio::test(flavor = "current_thread")]
async fn the_count_fallback_leaves_the_event_loop() {
    let f = Fixture::with_redis(slow_cache()).await;
    let gw = make(&f).await;
    join(&f, &gw, &"1".repeat(18)).await;
    let id = gw_id(&gw);

    let (value, ticks) = ticks_during(f.h.service().entry_count_cached(&id)).await;
    assert_eq!(
        1,
        value.expect("count"),
        "the fallback still answers correctly"
    );
    assert!(
        ticks > 0,
        "the runtime kept running while the read was in flight"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn the_notify_role_fallback_leaves_the_event_loop() {
    let f = Fixture::with_redis(slow_cache()).await;
    let svc = f.h.service();
    svc.set_notify_role(&f.guild, Some("4242"))
        .await
        .expect("set role");

    let (value, ticks) = ticks_during(svc.get_notify_role_cached(&f.guild)).await;
    assert_eq!(Some("4242".to_string()), value);
    assert!(
        ticks > 0,
        "the runtime kept running while the read was in flight"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn the_active_list_fallback_leaves_the_event_loop() {
    let f = Fixture::with_redis(slow_cache()).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    let (value, ticks) = ticks_during(f.h.service().list_active_cached(&f.guild)).await;
    let listed: Vec<String> = value
        .expect("list")
        .iter()
        .map(|gw| gw.id.clone())
        .collect();
    assert_eq!(
        vec![id],
        listed,
        "the fallback still lists the live giveaway"
    );
    assert!(
        ticks > 0,
        "the runtime kept running while the read was in flight"
    );
}

// -- Redis being wrong never changes a decision -----------------------------

#[tokio::test(flavor = "multi_thread")]
async fn every_read_of_a_dead_server_degrades_to_no_value() {
    let (cache, _fake) = failing_cache();

    assert!(cache.get("count:gw_1").await.is_none());
    assert!(!cache.set("count:gw_1", "3", 4.0).await);
    assert_eq!(0, cache.delete(&["count:gw_1"]).await);
    assert!(cache.incr_with_ttl("count:gw_1", 4.0).await.is_none());
    assert!(cache.ping_ms().await.is_none());
    let (token, answered) = cache.acquire_checked("sweep:live", 5.0).await;
    assert!(token.is_none());
    assert!(
        !answered,
        "no answer is not the same as somebody else holding it"
    );
    assert!(!cache.release("sweep:live", &"0".repeat(32)).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_breaker_stops_hammering_a_dead_server() {
    let (cache, _fake) = failing_cache();
    for _ in 0..6 {
        cache.get("count:gw_1").await;
    }
    let skipped = cache.stats().await["counters"]["skipped"]
        .as_u64()
        .expect("skipped counter");
    for _ in 0..20 {
        cache.get("count:gw_1").await;
    }
    assert_eq!(
        skipped + 20,
        cache.stats().await["counters"]["skipped"]
            .as_u64()
            .expect("skipped counter")
    );
    assert!(!cache.available().await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_dead_cache_leaves_the_service_working() {
    let (cache, _fake) = failing_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({ "giveaway_id": id })).await
    );
    join(&f, &gw, &"5".repeat(18)).await;
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({ "giveaway_id": id })).await
    );
    assert_eq!(0, flush(&f).await, "a failed DEL is reported, not raised");

    let active =
        f.h.ok("list_active_cached", json!({ "guild_id": f.guild }))
            .await;
    let listed: Vec<String> = active
        .as_array()
        .expect("a list of giveaways")
        .iter()
        .map(gw_id)
        .collect();
    assert_eq!(vec![id], listed);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_raising_client_never_breaks_a_write_path() {
    // Python built the cache over a client that raised on every command; the
    // fake's failure switch is the same client, seen from the Rust seam.
    let (cache, _fake) = failing_cache();
    let f = Fixture::with_redis(cache).await;
    let gw = make(&f).await;
    let id = gw_id(&gw);

    let count = f
        .enter_with(
            &gw,
            &"6".repeat(18),
            json!({ "account_created_ts": f.account_age(400.0) }),
        )
        .await;
    assert_eq!(1, count, "the row landed even though the cache was broken");
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({ "giveaway_id": id })).await
    );
    assert_eq!(0, flush(&f).await);
}

// -- the invalidation queue --------------------------------------------------

fn sent_batches(fake: &FakeRedis) -> Vec<Vec<String>> {
    fake.calls()
        .into_iter()
        .filter(|call| call.name == "del")
        .map(|call| call.args)
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_full_batch_is_sent_and_the_overflow_stays_queued() {
    let (cache, fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let queued: Vec<String> = (0..80).map(|index| format!("suffix:{index}")).collect();

    f.h.ok("mark_stale", json!({ "suffixes": queued })).await;
    flush(&f).await;
    let batches = sent_batches(&fake);
    assert_eq!(1, batches.len(), "one DEL per flush");
    assert_eq!(
        64,
        batches[0].len(),
        "the batch is capped at FLUSH_BATCH_MAX"
    );
    assert_eq!(format!("gwbot:v1:{}", queued[0]), batches[0][0]);

    // ADAPTED: Python peeked at `_pending_invalidations` here so that reading
    // the overflow did not consume it. The Rust service only exposes
    // `drain_invalidations`, which clears the queue, so the overflow is proven
    // by flushing again: the second DEL must carry exactly the 16 suffixes the
    // first one left behind, in order.
    flush(&f).await;
    let batches = sent_batches(&fake);
    let sizes: Vec<usize> = batches.iter().map(Vec::len).collect();
    assert_eq!(vec![64, 16], sizes);
    assert_eq!(format!("gwbot:v1:{}", queued[64]), batches[1][0]);
    assert!(drain(&f).await.is_empty(), "the queue ends empty");

    let sent: Vec<String> = batches.into_iter().flatten().collect();
    let unique: std::collections::HashSet<&String> = sent.iter().collect();
    assert_eq!(
        80,
        unique.len(),
        "every queued suffix went out exactly once"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn the_pending_queue_is_bounded_and_keeps_the_newest() {
    let (cache, _fake) = fake_cache();
    let f = Fixture::with_redis(cache).await;
    let queued: Vec<String> = (0..3000).map(|index| format!("queue:{index}")).collect();

    f.h.ok("mark_stale", json!({ "suffixes": queued })).await;
    let pending = drain(&f).await;
    assert_eq!(
        1024,
        pending.len(),
        "PENDING_INVALIDATIONS_MAX caps the queue"
    );
    assert_eq!("queue:2999", pending[1023], "the newest suffix is kept");
    assert_eq!("queue:1976", pending[0], "the oldest suffixes made room");
}
