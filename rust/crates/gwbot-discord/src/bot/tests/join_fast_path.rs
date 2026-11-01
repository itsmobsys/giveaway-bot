//! Port of the retired Python `JoinFastPathTests` cases.
//!
//! `handle_join` reuses the snapshot and the count it already holds instead of
//! re-reading rows. serenity 0.12 builds a `Context` only inside its own
//! dispatch and `Http` performs real requests, so the click is driven through
//! the same calls `handle_join` makes (`join_header` -> `join_and_refresh`) and
//! the post-ack halves are asserted on the pure seams that decide them
//! (`post_join`, `refresh_count`, `refresh_allowed`). Read budgets come from the
//! database's own statement log, which is what Python's `db.query` spies saw.

use super::*;

/// The eligibility read `join_header` still takes when the row is already warm.
///
/// Two spaces after the comma: `join_snapshot` folds the same four subqueries
/// into its own SELECT with one space, so this marker names only the standalone
/// `eligibility_snapshot` read.
const ELIGIBILITY_SQL: &str = "AS bl,  (SELECT";

/// Python: `JoinFastPathTests.test_handle_join_passes_snapshot_to_join`.
///
/// ADAPTED: Python spied `service.join` and asserted the `snapshot` kwarg was a
/// 4-tuple. Rust's `click_join_on` always takes the pair `join_header` returns
/// and hands that snapshot to `join_and_refresh`, so the pair itself is what is
/// pinned: four subqueries, clean, and the click that carries them still
/// commits exactly one entry.
#[tokio::test(flavor = "multi_thread")]
async fn handle_join_passes_snapshot_to_join() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "555555555555555555";

    let (header, snapshot) = join_header(&f.bot, &gw.id, GUILD, member)
        .await
        .expect("header + snapshot");
    assert_eq!(header.id, gw.id, "the pair is for this giveaway");
    let (blacklisted, ban, messages, hit) =
        snapshot.expect("fast-path must hand join() a snapshot tuple");
    assert!(!blacklisted, "a clean member is not blacklisted");
    assert_eq!(ban, 0, "no timeout penalty outstanding");
    assert_eq!(messages, 0, "no message count yet");
    assert!(!hit, "no penalty hit on this giveaway");

    let answer = f.click_join(&gw.id, member).await;
    assert!(answer.entered(), "{answer:?}");
    assert_eq!(answer.count(), 1);
    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1,
        "the snapshot the click handed in still commits one entry"
    );
    // ADAPTED: the "You're in!" ack is written by `handle_join` itself, which
    // needs a `Context`; the wording is pinned in the shipped source instead.
    assert!(
        body_of(BUTTONS_SOURCE, "async fn handle_join").contains("You're in!"),
        "the ack still says You're in!"
    );
}

/// Python: `JoinFastPathTests.test_a_cached_header_still_checks_eligibility_live`.
///
/// Second tap in a burst: the row is in hand, the rules are not. The hot-header
/// path skips the row read, but the eligibility snapshot still has to be taken:
/// it is what lets the service's timeout-ban gate skip its own live row read.
#[tokio::test(flavor = "multi_thread")]
async fn a_cached_header_still_checks_eligibility_live() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "557555555555555555";
    // Warm exactly the way a previous click in the burst leaves it.
    f.bot.gw_store(&gw.id, &gw);

    let db = f.bot.service().db();
    db.log_queries();
    let answer = f.click_join(&gw.id, member).await;
    let log = db.take_query_log();

    assert!(answer.entered(), "{answer:?}");
    let eligibility: Vec<&String> = log
        .iter()
        .filter(|sql| sql.contains(ELIGIBILITY_SQL))
        .collect();
    assert_eq!(
        eligibility.len(),
        1,
        "exactly one live eligibility read: {log:?}"
    );
    assert!(
        log.iter().all(|sql| !sql.contains(HEADER_SQL)),
        "the header is already in hand: {log:?}"
    );
    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1,
        "exactly one entry, read live"
    );
}

/// Python: `JoinFastPathTests.test_handle_join_reuses_count_for_refresh`.
///
/// ADAPTED: Python replaced `_refresh_embed` with a spy and read the
/// `entry_count` it was handed. The count is produced by `join_and_refresh` and
/// consumed by `refresh_count`, so both ends are asserted: the post-commit
/// number, and that reusing it costs no read at all.
#[tokio::test(flavor = "multi_thread")]
async fn handle_join_reuses_count_for_refresh() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "557555555555555555";

    let (header, snapshot) = join_header(&f.bot, &gw.id, GUILD, member)
        .await
        .expect("header + snapshot");
    let (status, count) = f
        .bot
        .service()
        .join_and_refresh(
            &header,
            member,
            "Tester",
            &[],
            Some(ACCOUNT_CREATED_TS),
            0,
            false,
            snapshot,
        )
        .await
        .expect("join");

    assert_eq!(count, 1, "the join hands back the post-commit count");
    assert_eq!(status.as_deref(), Some("active"));
    // Python also asserted the grant ran for this giveaway: `post_join` is what
    // selects that half of the ack.
    assert_eq!(
        post_join(status.as_deref()),
        PostJoin::GrantAndRefresh,
        "the grant half still runs"
    );

    let db = f.bot.service().db();
    db.log_queries();
    let reused = refresh_count(&f.bot, &header, Some(count)).await;
    let log = db.take_query_log();
    assert_eq!(reused, Some(1), "the count is reused, not re-queried");
    assert!(
        log.is_empty(),
        "a reused count must not cost a second COUNT query: {log:?}"
    );
}

/// Python: `JoinFastPathTests.test_refresh_embed_reuses_count_without_query`.
///
/// ADAPTED: Python replaced `service.entry_count` with one that raises and
/// asserted the edit still happened. The read is exactly what `refresh_count`
/// skips when it is handed a number, so that is asserted directly, together
/// with the render the edit would have written.
#[tokio::test(flavor = "multi_thread")]
async fn refresh_embed_reuses_count_without_query() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("message id");
    let fresh = f.bot.service().get(&gw.id).await.expect("row");
    assert!(
        refresh_allowed(&fresh),
        "a live giveaway with a message is editable"
    );

    let db = f.bot.service().db();
    db.log_queries();
    let count = refresh_count(&f.bot, &fresh, Some(5)).await;
    let log = db.take_query_log();
    assert_eq!(count, Some(5), "the caller's number is reused");
    assert!(
        log.is_empty(),
        "entry_count must not be queried when reused: {log:?}"
    );

    let embed = embeds::giveaway_embed(
        &fresh,
        count.expect("a count to render"),
        f.bot.state.settings.embed_color,
    );
    let rendered = format!("{:?}", embed.description);
    assert!(
        rendered.contains("Entries: **5**"),
        "the reused count is what the edit would show: {rendered}"
    );
}

/// Python: `JoinFastPathTests.test_post_join_race_with_end_takes_role_without_refresh`.
///
/// ADAPTED: the end lands between the INSERT and the status read that
/// `join_and_refresh` performs. Rust splits those into `join` and
/// `refresh_snapshot`, so the race is reproduced by running them in that order
/// and asking `post_join` what the ack would do with the answer.
#[tokio::test(flavor = "multi_thread")]
async fn post_join_race_with_end_takes_role_without_refresh() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("message id");
    let member = "558555555555555555";

    let (header, snapshot) = join_header(&f.bot, &gw.id, GUILD, member)
        .await
        .expect("header + snapshot");
    let committed = f
        .bot
        .service()
        .join(
            &header,
            member,
            "Tester",
            &[],
            Some(ACCOUNT_CREATED_TS),
            0,
            false,
            snapshot,
            false,
        )
        .await
        .expect("join");
    assert_eq!(committed, -1, "committed, the count left to the refresh");
    // The draw wins the race inside the window the status read covers.
    f.bot
        .service()
        .end(&gw.id, None, false, None)
        .await
        .expect("end");

    let (status, count) = f.bot.service().refresh_snapshot(&gw.id).await;
    assert_eq!(status.as_deref(), Some("ended"), "the draw won the race");
    assert_eq!(count, 1, "the entry still landed");

    assert_eq!(
        post_join(status.as_deref()),
        PostJoin::TakeRole,
        "an ended giveaway takes the role back instead of granting it"
    );
    assert_eq!(post_join(Some("active")), PostJoin::GrantAndRefresh);
    let ended = f.bot.service().get(&gw.id).await.expect("row");
    assert!(
        !refresh_allowed(&ended),
        "the ended race must not restore the live embed"
    );
    // The TakeRole half is what takes the role back, for this giveaway and this
    // member -- Python asserted `taken == [(gw.id, member.id)]`.
    let branch = body_of(BUTTONS_SOURCE, "PostJoin::TakeRole =>");
    assert!(branch.contains("take_entrants_role"), "{branch}");
    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1,
        "the entry stays even though it missed the draw"
    );
}
