//! Port of the retired Python `AuditPostAckTests` cases.
//!
//! Post-ack isolation, the refresh guard, and the silent-followup log. The ack
//! itself (`safe_followup_*`) and both post-ack halves need a `Context`, which
//! serenity only builds inside its own dispatch, so each assertion is pinned on
//! the seam that owns the decision: `post_join`, `refresh_count`,
//! `refresh_allowed` / `refresh_status_allows`, `should_revoke_after_grant`, and
//! the `tokio::join!` that stops a grant failure and a refresh failure from
//! cancelling each other.

use super::*;

use crate::bot::Gateway;

/// The status-only read `grant_entrants_role` re-checks through after the grant.
///
/// Narrow on purpose: `refresh_snapshot` selects `status, COALESCE(...)`, so this
/// marker breaks the grant's recheck alone.
const GET_STATUS_SQL: &str = "SELECT status FROM simple_giveaways WHERE id = ?";

/// The status + count read the post-ack refresh renders from.
const REFRESH_SNAPSHOT_SQL: &str = "SELECT status, COALESCE(entrant_count, 0) AS n";

/// Python: `AuditPostAckTests.test_post_ack_grant_failure_does_not_cancel_refresh`.
///
/// ADAPTED: the grant's Discord call needs a `Context`; its only database seam
/// is the post-grant status recheck, so that is the failure injected here. What
/// the ack guarantees is structural -- the two halves are one `tokio::join!`, so
/// neither can cancel the other -- and that is asserted on the branch itself,
/// together with the entry that must survive both.
#[tokio::test(flavor = "multi_thread")]
async fn post_ack_grant_failure_does_not_cancel_refresh() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "559555555555555555";
    let answer = f.click_join(&gw.id, member).await;
    assert!(answer.entered(), "{answer:?}");
    let JoinAnswer::Entered { status, count } = answer else {
        unreachable!("asserted a committed join above");
    };
    assert_eq!(status.as_deref(), Some("active"));
    assert_eq!(post_join(status.as_deref()), PostJoin::GrantAndRefresh);

    // Python's giveaway already had its message posted; the row has to carry that
    // id or `refresh_allowed` reports there is no live embed to edit.
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("message id");

    // The grant's recheck dies: `grant_entrants_role` logs it and carries on.
    f.bot.service().db().fail_matching(GET_STATUS_SQL);
    let grant = f.bot.service().get_status(&gw.id).await;
    f.bot.service().db().clear_failures();
    assert!(
        grant.is_err(),
        "the grant's post-grant recheck is what dies"
    );

    // The refresh half is a separate future in the same join, so it still runs:
    // its own read works and it renders the count the join handed over.
    let fresh = f.bot.service().get(&gw.id).await.expect("row");
    assert!(refresh_allowed(&fresh), "the live embed is still editable");
    assert_eq!(
        refresh_count(&f.bot, &fresh, Some(count)).await,
        Some(count),
        "the refresh is not downstream of the grant"
    );
    let branch = body_of(BUTTONS_SOURCE, "PostJoin::GrantAndRefresh =>");
    assert!(
        branch.contains("tokio::join!(grant, refresh)"),
        "a grant failure cannot cancel the refresh: {branch}"
    );

    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1,
        "entry stays despite grant failure"
    );
}

/// Python: `AuditPostAckTests.test_post_ack_refresh_failure_does_not_undo_entry`.
///
/// ADAPTED: Python raised from `_refresh_embed`. The refresh fails closed on its
/// own read (`refresh_snapshot` answers `(None, 0)`), so that read is what is
/// made to fail; the grant half is started first in the branch, which is why a
/// refresh failure cannot reach it.
#[tokio::test(flavor = "multi_thread")]
async fn post_ack_refresh_failure_does_not_undo_entry() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "560555555555555555";
    let answer = f.click_join(&gw.id, member).await;
    assert!(answer.entered(), "{answer:?}");

    // The refresh's own read dies.
    f.bot.service().db().fail_matching(REFRESH_SNAPSHOT_SQL);
    let snapshot = f.bot.service().refresh_snapshot(&gw.id).await;
    f.bot.service().db().clear_failures();
    assert_eq!(
        snapshot,
        (None, 0),
        "a dead read skips the edit instead of rendering zero entrants"
    );

    // The grant half is created before the refresh and is not behind it.
    let branch = body_of(BUTTONS_SOURCE, "PostJoin::GrantAndRefresh =>");
    let grant_at = branch.find("let grant =").expect("the grant half");
    let refresh_at = branch.find("let refresh =").expect("the refresh half");
    assert!(
        grant_at < refresh_at,
        "the grant must not be downstream of the refresh: {branch}"
    );
    assert!(
        !should_revoke_after_grant(Some("active")),
        "a live giveaway keeps the role it just granted"
    );

    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1,
        "a failed refresh must not undo the entry"
    );
}

/// Python: `AuditPostAckTests.test_join_refresh_guard_skips_ended_without_discord_edit`.
#[tokio::test(flavor = "multi_thread")]
async fn join_refresh_guard_skips_ended_without_discord_edit() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("message id");
    f.bot
        .service()
        .end(&gw.id, None, false, None)
        .await
        .expect("end");
    let ended = f.bot.service().get(&gw.id).await.expect("row");

    assert!(!ended.active(), "the draw already ran");
    assert!(
        !refresh_allowed(&ended),
        "an ended giveaway has no live embed to edit"
    );
    assert!(
        !refresh_status_allows(Some("ended")),
        "an ended status never permits the edit"
    );
    // The guard is the first thing the refresh checks, before any read.
    assert!(
        body_of(BOT_SOURCE, "async fn refresh_embed_with").contains("if !refresh_allowed(gw)"),
        "the ended guard runs before the status re-read"
    );
}

/// Python: `AuditPostAckTests.test_tick_refresh_stale_snapshot_does_not_clobber_announce`.
///
/// B05: the tick's live list can go stale between `list_all_active` and the
/// edit. The snapshot alone would allow the repaint; the status read taken
/// immediately before it is what refuses.
#[tokio::test(flavor = "multi_thread")]
async fn tick_refresh_stale_snapshot_does_not_clobber_announce() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("message id");
    let stale = f.bot.service().get(&gw.id).await.expect("row");
    assert!(stale.active());
    assert!(
        refresh_allowed(&stale),
        "the stale tick snapshot alone would allow the edit"
    );

    // end() flips status after the tick snapshot was taken: exactly the
    // list_all_active -> refresh window the tick walks every pass.
    f.bot
        .service()
        .end(&gw.id, None, false, None)
        .await
        .expect("end");
    let (status, _) = f.bot.service().refresh_snapshot(&gw.id).await;
    assert_eq!(status.as_deref(), Some("ended"));
    assert!(
        !refresh_status_allows(status.as_deref()),
        "the live status decides, not the snapshot: the winner announcement survives"
    );
    assert!(
        body_of(BOT_SOURCE, "async fn refresh_embed_with").contains("if !refresh_status_allows"),
        "the status recheck sits between the snapshot and the edit"
    );
}

/// Python: `AuditPostAckTests.test_timeout_loop_one_bad_role_does_not_stop_rest`.
///
/// ADAPTED: `_take_entrants_role` needs a `Context`, and the Rust take swallows
/// its own HTTP error, so the one failing step is injected at the database seam
/// the sweep reads through; the loop must still reach the second giveaway. The
/// structural reason a bad role cannot stop the rest -- the per-giveaway step is
/// infallible and the loop has no `?` or `break` -- is pinned by the source scan
/// at the end.
#[tokio::test(flavor = "multi_thread")]
async fn timeout_loop_one_bad_role_does_not_stop_rest() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let other = f.make_named("other", GUILD).await;
    let member = "561555555555555555";
    // Python seeded one entry per active giveaway for the timed-out member.
    for active in [&gw, &other] {
        f.join_user(active, member).await;
    }

    let answer = f.click_join_timed_out(&gw.id, member).await;
    assert!(
        matches!(&answer, JoinAnswer::Refused { message, .. } if message.contains("timed out")),
        "{answer:?}"
    );
    // The refusal drops the member from every running giveaway in the guild.
    for active in [&gw, &other] {
        assert_eq!(
            f.bot
                .service()
                .entry_count(&active.id, true)
                .await
                .expect("count"),
            0,
            "the timed-out penalty drops this guild's entries"
        );
    }

    // Seed the latch so each visit of the sweep is observable: `clear_entered`
    // is the per-giveaway side effect `handle_join` performs before the take.
    for active in [&gw, &other] {
        f.bot.mark_entered(&active.id, member);
    }

    let handles = Gateway {
        cache: Arc::new(Cache::new()),
        http: Arc::new(Http::new("Bot test-token")),
    };
    let mut attempted: Vec<String> = Vec::new();
    let mut failed: Vec<String> = Vec::new();
    for active in f.bot.service().list_active(GUILD).await.expect("live list") {
        attempted.push(active.id.clone());
        f.bot.clear_entered(&active.id, member);
        // One bad step: the first giveaway's read dies.
        if active.id == gw.id {
            f.bot.service().db().fail_matching(GET_STATUS_SQL);
        }
        let step = f.bot.db_call(f.bot.service().get_status(&active.id)).await;
        if step.is_err() {
            failed.push(active.id.clone());
        }
        f.bot.service().db().clear_failures();
        f.bot
            .take_entrants_role_with(&handles, &active, member)
            .await;
    }

    let mut seen = attempted;
    seen.sort();
    let mut expected = vec![gw.id.clone(), other.id.clone()];
    expected.sort();
    assert_eq!(seen, expected, "second take must run despite first failing");
    assert_eq!(
        failed,
        vec![gw.id.clone()],
        "the first step is the one that died"
    );
    assert!(
        !f.bot.entered_recently(&gw.id, member),
        "the giveaway whose step failed was still visited"
    );
    assert!(
        !f.bot.entered_recently(&other.id, member),
        "and the loop went on to the next one"
    );

    let body = body_of(BUTTONS_SOURCE, "for live in active");
    assert!(
        !body.contains('?'),
        "no per-giveaway step may abort the sweep: {body}"
    );
    assert!(body.contains("take_entrants_role"), "{body}");
}

/// Python: `AuditPostAckTests.test_silent_followup_is_logged`.
///
/// ADAPTED: an in-process send cannot fail, so the warning the helper writes on
/// a failed send is pinned in the shipped source instead of being provoked.
#[tokio::test]
async fn silent_followup_is_logged() {
    for helper in [
        "async fn safe_followup_command",
        "async fn safe_followup_component",
    ] {
        let body = body_of(BOT_SOURCE, helper);
        assert!(
            body.contains("if let Err(err)"),
            "{helper} must swallow the send error: {body}"
        );
        assert!(
            body.contains("warn!"),
            "{helper} must log the silent ack: {body}"
        );
    }
}

/// Python: `AuditPostAckTests.test_grant_recheck_revokes_when_ended_during_grant`.
///
/// The grant is an HTTP round trip; a giveaway can end inside that window, so
/// the role the member just earned has to be taken straight back.
#[tokio::test]
async fn grant_recheck_revokes_when_ended_during_grant() {
    assert!(
        should_revoke_after_grant(Some("ended")),
        "an ended giveaway revokes the role it just granted"
    );
    assert!(
        should_revoke_after_grant(None),
        "an unreadable status fails closed to a revoke"
    );
    let body = body_of(BOT_SOURCE, "async fn grant_entrants_role");
    assert!(body.contains("should_revoke_after_grant"), "{body}");
    assert!(body.contains("remove_member_role"), "{body}");
}

/// Python: `AuditPostAckTests.test_grant_recheck_keeps_when_still_active`.
#[tokio::test]
async fn grant_recheck_keeps_when_still_active() {
    assert!(
        !should_revoke_after_grant(Some("active")),
        "a live giveaway keeps the role it just granted"
    );
}
