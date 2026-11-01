//! Port of the retired Python `GracefulShutdownTests` cases.
//!
//! Python ran `amain` with a fake `GiveawayBot` whose `close()` appended
//! "buffer flushed", and covered both the `loop.add_signal_handler` path and the
//! Windows `signal.signal` fallback. Rust has one signal path (`shutdown_signal`)
//! and one closer (`shutdown`), so the case is ported onto `shutdown` itself: the
//! buffered counts are drained before the pools close, and exactly one shutdown
//! runs per process.

use super::*;

/// Python: `GracefulShutdownTests.test_sigterm_closes_bot_and_awaits_its_buffer_flush`.
///
/// ADAPTED: Python asserted the same `["buffer flushed"]` event list twice, once
/// with `add_signal_handler` raising `NotImplementedError` (the Windows
/// fallback) and once without. Rust has a single `shutdown_signal` — Ctrl-C
/// everywhere, plus SIGTERM on unix — so there is no second path to cover. What
/// the two runs really proved, one close after the buffer was flushed, is
/// asserted here directly, and "exactly one close" is read off `run_bot`, which
/// calls `shutdown` once after `login_loop` returns.
#[tokio::test]
async fn sigterm_closes_bot_and_awaits_its_buffer_flush() {
    let f = BotFixture::new().await;
    let (a, b) = ("333333333333333333", "444444444444444444");
    for _ in 0..3 {
        f.bot.buffer_message(GUILD, a);
    }
    f.bot.buffer_message(GUILD, b);

    // Nothing has reached the database yet: the counts are the buffer's.
    assert_eq!(f.bot.pending_messages(GUILD, a), 3);
    assert_eq!(
        f.bot
            .service()
            .message_count(GUILD, a)
            .await
            .expect("count"),
        0,
        "the buffer holds the counts, as Python's fake bot did"
    );
    assert!(
        f.cache().available().await,
        "the cache is open before the stop"
    );

    shutdown(&f.bot).await;

    // The flush is awaited, not dropped: the counts are in the database by the
    // time `shutdown` returns, which is Python's "buffer flushed".
    assert_eq!(
        f.bot
            .service()
            .message_count(GUILD, a)
            .await
            .expect("count"),
        3
    );
    assert_eq!(
        f.bot
            .service()
            .message_count(GUILD, b)
            .await
            .expect("count"),
        1
    );
    assert_eq!(
        f.bot.pending_messages(GUILD, a),
        0,
        "the buffer is empty once the final flush has run"
    );

    // `aclose()` leaves the cache configured but unavailable: the client is
    // closed, which is Python's `close()` on the bot.
    assert!(f.cache().configured());
    assert!(
        !f.cache().available().await,
        "shutdown must close the cache client"
    );

    // One close per process: no path can flush twice, and none can skip it.
    let body = body_of(BOT_SOURCE, "pub async fn run_bot");
    assert_eq!(
        body.matches("shutdown(&bot).await").count(),
        1,
        "run_bot shuts down exactly once"
    );
    let loop_at = body.find("bot.login_loop()").expect("the login loop");
    let close_at = body.find("shutdown(&bot).await").expect("the shutdown");
    assert!(
        close_at > loop_at,
        "the shutdown runs after the gateway loop has returned"
    );
}
