//! Port of the retired Python `OffLoopReadPoolTests` cases.
//!
//! Python's premise was a 64-lane pinned thread pool: the loop's default executor
//! was min(32, cpu+4) workers, every cold worker opened its own database stream,
//! and two call sites (the heartbeat's `/health` probe and the active-giveaway
//! lookup the request probe needs for its key) used to block on the default
//! executor instead. Python proved the wiring by monkeypatching `bot._db_call`
//! and looking at what the spy saw.
//!
//! Rust has no such pool and no `_db_call` to swap: every database call is a
//! future run under `db_call`'s 8s timeout, and `probe_health` is a raw TCP
//! `/health` request that never touches the database at all. Each case below is
//! ported to the honest equivalent — the statement log and the shipped source —
//! and the divergence is spelled out in an `// ADAPTED:` comment.

use super::*;

/// The active-giveaway read the probe makes for its cache key.
///
/// `list_active`'s own statement, and the one `list_active_cached` falls through
/// to on a miss. `list_all_active` selects from the same table with a different
/// tail, so the guild filter is part of the marker.
const ACTIVE_LIST_SQL: &str = "FROM simple_giveaways WHERE guild_id = ? AND status = 'active'";

/// The probe's own lookup really happens, on the same lane as every other call.
#[tokio::test(flavor = "multi_thread")]
async fn the_request_probe_lookup_rides_the_pinned_pool() {
    // ADAPTED: Python spied on `bot._db_call` and asserted `"list_active"` was in
    // the names it saw. There is no injectable `_db_call` here, so the evidence
    // is the database's own statement log: the read the probe needs for its key
    // must have been issued.
    let f = BotFixture::new().await;
    f.make().await;
    let db = f.bot.service().db();

    db.log_queries();
    let _measured = measure_probe(&f.bot, GUILD, CHANNEL, CREATED_BY).await;
    let log = db.take_query_log();

    assert!(
        log.iter().any(|sql| sql.contains(ACTIVE_LIST_SQL)),
        "the probe's own read went elsewhere: {log:?}"
    );
    // The probe's first database action is that lookup, so it leads the log.
    assert!(
        log.first().is_some_and(|sql| sql.contains(ACTIVE_LIST_SQL)),
        "the active-giveaway lookup must be the probe's first read: {log:?}"
    );
}

/// The health probe costs no database work at all.
#[tokio::test(flavor = "multi_thread")]
async fn the_health_probe_rides_the_pinned_pool() {
    // ADAPTED: Python asserted `"_ping"` showed up in the spy, because its
    // `/health` probe read the database through `_db_call`. Rust's `probe_health`
    // is a raw TCP request to our own server, so the premise is obsolete: the
    // honest assertion is that it issues no statement whatsoever, which is
    // strictly stronger than "it used the pinned lane".
    let f = BotFixture::with_port(1).await;
    let db = f.bot.service().db();

    db.log_queries();
    // Nothing listens on port 1, so this fails fast; whether it succeeded is not
    // the point, and no answer would change the statement log either way.
    let _ = f.bot.probe_health().await;
    let log = db.take_query_log();

    assert!(
        log.is_empty(),
        "the health probe went to the database: {log:?}"
    );
}

/// The service's cached readers use the bot's cache, and a warm read is free.
#[tokio::test(flavor = "multi_thread")]
async fn cached_reads_ride_the_pinned_pool_too() {
    // ADAPTED: Python asserted the injected runner *was* `GiveawayBot._db_call`
    // bound to this bot. There is no injected runner in Rust, so the equivalent
    // claim is made on the object that replaced it: the service holds the bot's
    // own cache (proved by reading, through the fixture's handle, the key the
    // service just wrote), and a warm cached read issues zero statements.
    let f = BotFixture::new().await;
    let gw = f.make().await;

    assert!(
        f.bot
            .service()
            .redis()
            .is_some_and(|cache| cache.configured()),
        "the bot must inject its cache into the service"
    );

    let warm = f
        .bot
        .service()
        .entry_count_cached(&gw.id)
        .await
        .expect("warm count");
    let key = format!("count:{}", gw.id);
    assert_eq!(
        f.cache().get_json(&key).await,
        Some(serde_json::json!(warm)),
        "the service must write into the cache object the bot owns"
    );

    let db = f.bot.service().db();
    db.log_queries();
    let again = f
        .bot
        .service()
        .entry_count_cached(&gw.id)
        .await
        .expect("warm read");
    let log = db.take_query_log();

    assert_eq!(again, warm, "the warm value must be served as-is");
    assert!(
        log.is_empty(),
        "a warm cached read must not reach the database: {log:?}"
    );
}

/// No blocking read is left behind: the database lane is a future, not a thread.
#[test]
fn no_blocking_read_is_left_on_the_default_executor() {
    // ADAPTED: Python scanned `bot.py` and `service.py` for `asyncio.to_thread(`,
    // the call that used to put a read on the loop's default executor. Rust's
    // equivalents are `spawn_blocking` and `block_in_place`; the scan is the same
    // whole-file scan, over the same two units of code.
    for (name, source) in [("mod.rs", BOT_SOURCE), ("service.rs", SERVICE_SOURCE)] {
        assert!(
            !source.contains("spawn_blocking"),
            "{name}: every off-loop database read must stay a future on the database lane"
        );
        assert!(
            !source.contains("tokio::task::block_in_place"),
            "{name}: a blocking read must never be parked on the runtime thread"
        );
    }
    // What replaced the pool: one awaited call under a timeout.
    let lane = body_of(BOT_SOURCE, "async fn db_call");
    assert!(
        lane.contains("tokio::time::timeout"),
        "the database lane must bound every call with a timeout: {lane}"
    );
}
