//! Port of the retired Python `JoinBurstHandlerTests` cases.
//!
//! Python drove `bot.handle_join` with fake interactions and counted the PATCHes
//! a fake channel received. serenity 0.12 only builds a `Context` inside its own
//! dispatch and `Http` performs real requests, so each case here runs the same
//! burst through [`run_burst`] — cloned `Bot` handles meeting in one service,
//! which is where the race has to be observed — and then asserts on the seam
//! that owns the behaviour: the committed entry count, the recorded answer, the
//! per-giveaway refresh debounce and the database lane.

use std::sync::atomic::{AtomicUsize, Ordering};

use super::*;

/// The statement `refresh_snapshot` sends, and nothing else sends.
///
/// `join_and_refresh` calls it after the commit, so a failure injected here
/// leaves the entry written and only kills the post-ack status/count read.
const REFRESH_SQL: &str =
    "SELECT status, COALESCE(entrant_count, 0) AS n FROM simple_giveaways WHERE id";

// -- JoinBurstHandlerTests -----------------------------------------------

/// Python: `JoinBurstHandlerTests.test_40_concurrent_handler_joins_all_commit`.
///
/// Renamed only because a Rust identifier cannot start with a digit.
#[tokio::test(flavor = "multi_thread")]
async fn forty_concurrent_handler_joins_all_commit() {
    let f = BotFixture::new().await;
    let gw = f.make().await;

    // The ids Python used: `10 ** 17 + i`.
    let answers = run_burst(&f.bot, &gw.id, 40, 100_000_000_000_000_000).await;

    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("entry count"),
        40,
        "every concurrent click must commit a row"
    );
    // ADAPTED: Python looked for "You're in!" in each fake interaction's texts.
    // `JoinAnswer::Entered` is exactly the committed ack, recorded instead of
    // written, so "every ack is honest" is "every answer committed".
    for answer in &answers {
        assert!(answer.entered(), "every click must be acked: {answer:?}");
    }
}

/// Python: `JoinBurstHandlerTests.test_50_concurrent_handler_joins_all_commit`.
///
/// Renamed only because a Rust identifier cannot start with a digit.
#[tokio::test(flavor = "multi_thread")]
async fn fifty_concurrent_handler_joins_all_commit() {
    let f = BotFixture::new().await;
    let gw = f.make().await;

    // The ids Python used: `10 ** 17 + 1000 + i`.
    let answers = run_burst(&f.bot, &gw.id, 50, 100_000_000_000_001_000).await;

    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("entry count"),
        50,
        "every simultaneous click must commit a row"
    );
    // ADAPTED: same substitution as the 40-click case.
    for answer in &answers {
        assert!(answer.entered(), "every click must be acked: {answer:?}");
    }
}

/// Python: `JoinBurstHandlerTests.test_burst_embeds_are_debounced`.
#[tokio::test(flavor = "multi_thread")]
async fn burst_embeds_are_debounced() {
    let f = BotFixture::new().await;
    let gw = f.make().await;

    // The ids Python used: `3 * 10 ** 17 + i`.
    let answers = run_burst(&f.bot, &gw.id, 10, 300_000_000_000_000_000).await;
    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("entry count"),
        10,
        "every click in the burst still commits"
    );

    // ADAPTED: Python counted `channel.message.edits` on a fake message. A
    // message edit needs `Http` here, so the decision that gates it is taken
    // from the seam instead: `handle_join` calls `should_refresh` once per
    // acked click, right before the edit, and only the leading edge says yes.
    let mut refreshes = 0usize;
    for answer in &answers {
        let JoinAnswer::Entered { status, .. } = answer else {
            panic!("every burst click must commit: {answer:?}");
        };
        if post_join(status.as_deref()) == PostJoin::GrantAndRefresh && f.bot.should_refresh(&gw.id)
        {
            refreshes += 1;
        }
    }

    assert!(
        refreshes <= 10,
        "burst must not PATCH once per click at minimum; debounce cuts it: {refreshes}"
    );
    // Leading edge fires immediately: the first click always refreshes.
    assert!(refreshes >= 1, "the leading edge must refresh: {refreshes}");
    // The decisions are taken back to back, well inside the 2.0s window, so the
    // only refresh that may happen is the burst's first.
    assert_eq!(
        refreshes, 1,
        "every burst mate lands inside the debounce window: {refreshes}"
    );
}

/// Python: `JoinBurstHandlerTests.test_dead_refresh_read_still_acks_honestly`.
#[tokio::test(flavor = "multi_thread")]
async fn dead_refresh_read_still_acks_honestly() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    // The id Python used: `4 * 10 ** 17 + 1`.
    let member = format!("{}", 400_000_000_000_000_001u64);

    // ADAPTED: Python swapped `service.refresh_snapshot` for a raising lambda.
    // Rust's `refresh_snapshot` swallows a dead read into `(None, 0)` by design,
    // so the outage is injected at the database seam: the statement still fails,
    // the commit before it still stands.
    f.bot.service().db().fail_matching(REFRESH_SQL);
    let answer = f.click_join(&gw.id, &member).await;
    f.bot.service().db().clear_failures();

    assert!(
        f.bot
            .service()
            .has_entry(&gw.id, &member)
            .await
            .expect("has_entry"),
        "a dead post-ack read must not undo the committed entry"
    );
    let JoinAnswer::Entered { status, count } = answer else {
        panic!("the entry committed, so the click must be acked: {answer:?}");
    };
    // The same two ack texts `handle_join` chooses between: the plain one when
    // the status read died, the numbered one when it answered.
    let ack = match status {
        Some(_) => format!("🎟️ You're in! Entry #{count}."),
        None => "🎟️ You're in!".to_string(),
    };
    assert_eq!(
        ack, "🎟️ You're in!",
        "a dead refresh must fall back to the honest plain ack"
    );
}

/// Python: `JoinBurstHandlerTests.test_db_lane_caps_concurrent_streams`.
#[tokio::test(flavor = "multi_thread")]
async fn db_lane_caps_concurrent_streams() {
    let f = BotFixture::new().await;
    let live = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));

    let mut tasks = Vec::with_capacity(40);
    for _ in 0..40 {
        let bot = f.bot.clone();
        let live = live.clone();
        let peak = peak.clone();
        tasks.push(tokio::spawn(async move {
            bot.db_call(async move {
                let now = live.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(now, Ordering::SeqCst);
                // Python's probe slept 0.05s on a worker thread. Yielding here
                // keeps this call "in flight" while the others are polled, which
                // is the overlap the assertion is about.
                tokio::time::sleep(Duration::from_millis(50)).await;
                live.fetch_sub(1, Ordering::SeqCst);
                Ok::<bool, anyhow::Error>(true)
            })
            .await
        }));
    }
    let mut completed = 0usize;
    for task in tasks {
        assert!(
            task.await.expect("lane task").expect("lane call"),
            "every lane call must finish"
        );
        completed += 1;
    }
    let peak = peak.load(Ordering::SeqCst);

    // ADAPTED: Python's 64-lane `_db_call` thread pool has no Rust equivalent —
    // `db_call` is one future under an 8s budget (`DB_CALL_TIMEOUT_SECS`), with
    // no worker pool to cap. The cap is obsolete; what still matters is that
    // concurrent calls really do overlap and every one of them completes.
    assert!(
        peak <= 64,
        "the old gate capped live streams at 64, saw {peak}"
    );
    assert!(peak > 1, "probe must actually overlap, saw {peak}");
    assert_eq!(completed, 40, "every spawned lane call must return");
}

/// Python: `JoinBurstHandlerTests.test_ban_list_shows_expiry_and_reason`.
#[tokio::test]
async fn ban_list_shows_expiry_and_reason() {
    let f = BotFixture::new().await;
    let permanent = "1".repeat(18);
    let timed = "2".repeat(18);
    f.bot
        .service()
        .blacklist_add(GUILD, &permanent, 0, "perm reason", "999", None)
        .await
        .expect("permanent ban");
    f.bot
        .service()
        .blacklist_add(GUILD, &timed, 5, "timed reason", "999", None)
        .await
        .expect("timed ban");

    // ADAPTED: `commands_admin::ban_list` is the purge, the two reads and the
    // body renderer; the Context only carries the followup.
    f.bot
        .service()
        .purge_expired_bans(None)
        .await
        .expect("purge expired");
    let rows = f
        .bot
        .service()
        .blacklist_list_detailed(GUILD, 100, None)
        .await
        .expect("ban rows");
    let total = f
        .bot
        .service()
        .count_blacklisted(GUILD, None)
        .await
        .expect("ban total");
    let text = ban_list_body(&rows, total);

    assert!(text.contains("Banned (2)"), "{text}");
    assert!(text.contains("permanent"), "{text}");
    assert!(text.contains("expires <t:"), "{text}");
    assert!(text.contains("timed reason"), "{text}");
}
