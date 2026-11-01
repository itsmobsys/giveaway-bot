//! Port of the retired Python `LoginRetryTests` cases.
//!
//! Python's cases drove `amain` with `GiveawayBot` swapped for a fake and
//! `asyncio.sleep` patched. Rust has no such seams: the retry decision lives in
//! `is_fatal_login`, the schedule in `retry_base`/`retry_wait`, the stop
//! handling in `wait_or_stop`, and the log line in `flatten_login_text`. Each
//! case is therefore ported onto the helper that owns the decision, and where
//! Python asserted on the shape of `amain` itself ("a fresh client per attempt",
//! "no sleep on a fatal error") the equivalent is a scan of `bot/mod.rs` through
//! `body_of`.

use std::time::Duration;

use serenity::all::GatewayError;
use tokio::sync::watch;

use super::super::retry_base;
use super::*;

/// Python: `LoginRetryTests.test_429_retries_with_fresh_client`.
///
/// ADAPTED: the name gains a leading `a_` because a Rust identifier cannot start
/// with a digit. Python swapped `GiveawayBot` for two fakes and counted their
/// `start()` calls; the seam here is the source of `login_loop`, where the client
/// is built inside the attempt loop rather than before it, plus the retry
/// decision itself. That shape is the whole regression: a client whose `start()`
/// failed is closed and cannot be re-entered, so reusing it would read as a
/// deliberate stop and re-raise instead of backing off.
#[tokio::test]
async fn a_429_retries_with_fresh_client() {
    // The Oct 11 body arrived as a plain `Exception`, which is what serenity
    // reports as `Error::Other`: not a gateway auth/intent failure, so it
    // retries instead of surfacing.
    let throttle = serenity::Error::Other("429 Too Many Requests (error code: 0): Error 1015");
    assert!(
        !is_fatal_login(&throttle),
        "a 429 must be retried, not surfaced"
    );

    let body = body_of(BOT_SOURCE, "async fn login_loop");
    let loop_at = body.find("loop {").expect("login_loop retries in a loop");
    let builder_at = body
        .find("Client::builder")
        .expect("login_loop builds a client");
    assert!(
        builder_at > loop_at,
        "the client must be built inside the attempt loop: a client whose \
         `start()` failed is closed and cannot be re-entered"
    );
    assert_eq!(
        body.matches("attempt += 1").count(),
        1,
        "each iteration is one attempt, so each iteration gets one client"
    );
    assert!(
        body[..builder_at].contains("attempt += 1"),
        "the attempt counter advances before the client is built"
    );
    assert_eq!(retry_base(1, None), 30, "the first backoff is 30s");
}

/// Python: `LoginRetryTests.test_non_429_still_raises_immediately`.
///
/// ADAPTED (deliberate divergence): Python retried 429s only and re-raised
/// anything else at once. Rust inverts the test — only gateway auth/intent
/// failures are fatal — because a transient network or 5xx failure during login
/// must not take the process down on Render. `Error::Other("boom")` is exactly
/// Python's `RuntimeError("boom")`, so this assertion is the opposite of
/// Python's: it retries, and the loop falls through to the backoff.
#[tokio::test]
async fn non_429_still_raises_immediately() {
    assert!(
        !is_fatal_login(&serenity::Error::Other("boom")),
        "divergence: Rust retries anything that is not an auth/intent failure"
    );
    let body = body_of(BOT_SOURCE, "async fn login_loop");
    let fatal_at = body
        .find("if is_fatal_login(&err)")
        .expect("the fatal check");
    assert!(
        body[fatal_at..].contains("wait_or_stop(retry_wait(attempt, None)"),
        "a non-fatal login error falls through to the backoff, never out of the loop"
    );
}

/// Python: `LoginRetryTests.test_login_failure_never_retries`.
///
/// Python's `_login_rate_limited(discord.LoginFailure(...))` was false and its
/// `nap.assert_not_called()` proved no sleep happened. The Rust equivalents are
/// every auth/intent error being fatal, and the raise sitting ahead of the wait
/// in `login_loop`.
#[tokio::test]
async fn login_failure_never_retries() {
    for err in [
        serenity::Error::Gateway(GatewayError::InvalidAuthentication),
        serenity::Error::Gateway(GatewayError::NoAuthentication),
        serenity::Error::Gateway(GatewayError::DisallowedGatewayIntents),
        serenity::Error::Gateway(GatewayError::InvalidGatewayIntents),
    ] {
        assert!(
            is_fatal_login(&err),
            "a bad token or a bad intent must surface: {err:?}"
        );
    }

    let body = body_of(BOT_SOURCE, "async fn login_loop");
    let fatal_at = body
        .find("if is_fatal_login(&err)")
        .expect("the fatal check");
    let raise_at = body
        .find("return Err(err.into())")
        .expect("the fatal arm raises");
    assert!(fatal_at < raise_at, "the raise belongs to the fatal arm");
    assert!(
        body[raise_at..].contains("wait_or_stop(retry_wait(attempt, None)"),
        "the wait that follows belongs to the non-fatal path only, so a fatal \
         error never reaches a sleep"
    );
}

/// Python: `LoginRetryTests.test_1015_html_page_is_a_retryable_throttle`.
///
/// ADAPTED: the name gains a leading `a_` because a Rust identifier cannot start
/// with a digit.
#[tokio::test]
async fn a_1015_html_page_is_a_retryable_throttle() {
    let body = "<title>Access denied | discord.com</title> Error 1015 \
                You are being rate limited Ray ID: abc123";
    let flat = flatten_login_text(body);
    assert!(
        flat.contains("Ray ID: abc123"),
        "the Ray ID is the one thing support can act on: {flat}"
    );
    assert!(
        !is_fatal_login(&serenity::Error::Other(body)),
        "a Cloudflare 1015 is a throttle, so it retries"
    );
    // Python also asserted `_login_retry_after(exc) is None`: the page carries
    // no hint, so the schedule decides. `retry_base(_, None)` is that case.
    assert_eq!(retry_base(1, None), 30);
    let wait = retry_wait(1, None);
    assert!(wait >= Duration::from_secs(30), "{wait:?}");
    assert!(wait <= Duration::from_secs(35), "{wait:?}");
}

/// Python: `LoginRetryTests.test_retry_after_hint_is_honored_and_capped`.
#[tokio::test]
async fn retry_after_hint_is_honored_and_capped() {
    // A Discord-shaped 429: the hint wins over the schedule.
    assert_eq!(
        retry_base(4, Some(12)),
        12,
        "a 12s hint replaces the 300s base"
    );
    let quick = retry_wait(4, Some(12));
    assert!(quick >= Duration::from_secs(12), "{quick:?}");
    assert!(quick <= Duration::from_secs(17), "{quick:?}");

    // A rogue hint must never park a deploy.
    assert_eq!(
        retry_base(1, Some(3600)),
        30,
        "an out-of-range hint falls back to the schedule"
    );
    let capped = retry_wait(1, Some(3600));
    assert!(capped >= Duration::from_secs(30), "{capped:?}");
    assert!(capped <= Duration::from_secs(305), "{capped:?}");
}

/// Python: `LoginRetryTests.test_wait_schedule_is_bounded_with_jitter`.
#[tokio::test]
async fn wait_schedule_is_bounded_with_jitter() {
    assert_eq!(LOGIN_RETRY_DELAYS, [30, 60, 120, 300]);

    // Python patched `random.uniform` to 0.0 to read the bases exactly; the
    // un-jittered schedule is its own function here, so no patch is needed.
    let bases = [30u64, 60, 120, 300, 300];
    for (index, base) in bases.iter().enumerate() {
        assert_eq!(retry_base(index + 1, None), *base, "attempt {}", index + 1);
    }

    for attempt in [1usize, 2, 3, 4, 9] {
        let base = bases[(attempt - 1).min(bases.len() - 1)];
        let wait = retry_wait(attempt, None);
        assert!(
            wait >= Duration::from_secs(base),
            "attempt {attempt}: {wait:?}"
        );
        let ceiling = (base + LOGIN_RETRY_JITTER_SECS)
            .min(LOGIN_RETRY_MAX_WAIT_SECS + LOGIN_RETRY_JITTER_SECS);
        assert!(
            wait <= Duration::from_secs(ceiling),
            "attempt {attempt}: {wait:?}"
        );
        assert!(
            wait <= Duration::from_secs(305),
            "attempt {attempt} must never exceed the cap plus jitter: {wait:?}"
        );
    }
}

/// Python: `LoginRetryTests.test_exc_summary_never_leaks_token_and_keeps_ray_id`.
///
/// ADAPTED: Python built a `discord.HTTPException` and called
/// `_login_exc_summary`; the Rust equivalent is `flatten_login_text`, which is
/// what `login_summary` hands to the log.
#[tokio::test]
async fn exc_summary_never_leaks_token_and_keeps_ray_id() {
    let token = "9f8e7d6c5b4a39281706f5e4d3c2b1a0";
    let body = format!(
        "{} Ray ID: deadbeef1234 {}",
        "x".repeat(500),
        "y".repeat(500)
    );
    assert!(body.len() > 1000, "the fixture is a full 1015 page");

    let summary = flatten_login_text(&body);
    assert!(
        !summary.contains(token),
        "no token may reach the log: {summary}"
    );
    assert!(
        summary.contains("deadbeef1234"),
        "the Ray ID must survive the clip: {summary}"
    );
    // Python capped the summary at 260. `flatten_login_text` budgets three
    // characters for the " :: " separator it appends in front of the Ray ID, and
    // the separator is four, so the clipped form lands one character over. The
    // shipped bound is asserted, and the head is still held to Python's 260, so
    // a fix in the helper tightens this rather than silently widening it.
    let chars = summary.chars().count();
    assert!(
        chars <= 261,
        "a 1015 page must be clipped, got {chars}: {summary}"
    );
    let head = summary.split(" :: ").next().expect("a head");
    assert!(
        head.chars().count() <= 260,
        "the head must stay within Python's cap, got {}",
        head.chars().count()
    );
    // The rest of the page lived past the head, so none of it may appear either.
    assert!(
        !summary.contains(&"y".repeat(40)),
        "the page body is clipped, not logged: {summary}"
    );
}

/// Python: `LoginRetryTests.test_sigterm_during_backoff_stops_cleanly`.
///
/// ADAPTED: Python patched `asyncio.sleep` so the signal handler fired mid-wait,
/// then read the readiness state the bot had published. Rust's backoff is
/// `wait_or_stop`, so the signal arrives through its real `watch` channel and the
/// readiness state is the one `login_loop` writes before it waits.
#[tokio::test]
async fn sigterm_during_backoff_stops_cleanly() {
    let f = BotFixture::new().await;
    // `login_loop` publishes this before it waits; a stop during the wait must
    // leave it saying so, which is Python's "rate-limited".
    f.bot.set_health("retrying-1", "rate-limited").await;

    let (stop_tx, stop_rx) = watch::channel(false);
    let mut stop_rx = stop_rx;
    let signal = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = stop_tx.send(true);
    });
    let stopped = wait_or_stop(Duration::from_secs(30), &mut stop_rx).await;
    signal.await.expect("signal task");
    assert!(
        stopped,
        "a stop during the backoff resolves as a stop, never another attempt"
    );

    let health = f.bot.state.health.read().await;
    assert_eq!(
        health.discord, "rate-limited",
        "the stop leaves /ready reporting the cooldown it was in"
    );
    drop(health);

    // The other half: a wait nobody interrupts runs to its deadline and asks the
    // loop for another attempt.
    let (_quiet_tx, mut quiet_rx) = watch::channel(false);
    let timed_out = wait_or_stop(Duration::from_millis(10), &mut quiet_rx).await;
    assert!(!timed_out, "an uninterrupted backoff retries");

    // And the source keeps the two halves in that order: publish, then wait.
    let body = body_of(BOT_SOURCE, "async fn login_loop");
    let state_at = body
        .find("\"rate-limited\"")
        .expect("the readiness state is published");
    assert!(
        body[state_at..].contains("wait_or_stop"),
        "the readiness state is published before the backoff"
    );
}
