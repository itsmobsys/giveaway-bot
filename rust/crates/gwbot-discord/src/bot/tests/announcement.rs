//! Port of the retired Python `AnnouncementTests` cases.
//!
//! Python watched a fake channel (`message.edits`, `channel.sent`) and a fake
//! interaction's texts. Serenity 0.12 only builds a `Context` inside its own
//! dispatch and `Http` performs real requests, so every case here is mapped onto
//! the pure pieces that own the behaviour: the announcement step plan, the
//! mention policy, the embed/title renderers and the service's DB state.

use gwbot_core::mentions::split_mentions;

use super::commands::ping_body;
use super::*;

/// Python: `AnnouncementTests.test_a_failed_reply_after_edit_does_not_duplicate_winner_embed`.
///
/// ADAPTED: Python counted `channel.message.edits` and `channel.sent`. The
/// sequence is `announce_with`'s step plan, and the property that a failed reply
/// cannot reach the fallback post is the shape of the edit branch in its source:
/// the reply's result is discarded (`let _ =`) and the branch returns either way.
#[tokio::test]
async fn a_failed_reply_after_edit_does_not_duplicate_winner_embed() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    // `channel_for` pointed the bot at a live message; the same precondition
    // here is a giveaway that knows its message id.
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("live message");
    let gw = f.bot.service().get(&gw.id).await.expect("fresh row");
    assert!(gw.message_id.is_some(), "the original is live");

    let steps = announce_steps(gw.message_id.is_some(), false);
    assert_eq!(
        steps[0],
        AnnounceStep::Edit,
        "the live panel is edited first"
    );
    assert!(
        steps.contains(&AnnounceStep::Reply),
        "the ping threads under the giveaway: {steps:?}"
    );
    assert_eq!(
        steps
            .iter()
            .filter(|step| **step == AnnounceStep::Post)
            .count(),
        1,
        "exactly one fallback post exists: {steps:?}"
    );

    // A failed reply must not fall through to the fallback post: the reply is
    // awaited for its effect only, and the edit branch returns right after it.
    let body = body_of(BOT_SOURCE, "async fn announce_with");
    assert!(
        body.contains("let _ = channel_id"),
        "a failed reply is ignored, not escalated: {body}"
    );
    let fallback = body
        .find("let mut message = CreateMessage::new()")
        .expect("the fallback post is in the body");
    let last_return = body.rfind("return;").expect("the edit branch returns");
    assert!(
        last_return < fallback,
        "the edited path must return before the fallback post: {body}"
    );
}

/// Python: `AnnouncementTests.test_a_failed_edit_falls_back_once_with_explicit_mention_policy`.
///
/// ADAPTED: the fake channel's `sent[0]["allowed_mentions"]` is the tuple
/// `announce_with` builds from `announce_mentions()`; the "one post" count is
/// the single `Post` step the failed edit falls through to.
#[tokio::test]
async fn a_failed_edit_falls_back_once_with_explicit_mention_policy() {
    let f = BotFixture::new().await;
    let gw = f.make_named("@everyone", GUILD).await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("live message");
    let gw = f.bot.service().get(&gw.id).await.expect("fresh row");

    let steps = announce_steps(gw.message_id.is_some(), false);
    assert_eq!(steps[0], AnnounceStep::Edit);
    assert_eq!(
        steps
            .iter()
            .filter(|step| **step == AnnounceStep::Post)
            .count(),
        1,
        "a failed edit falls back exactly once: {steps:?}"
    );

    // The fallback's mentions are explicit: `@everyone` in the prize is text,
    // never a server-wide ping, while winners and roles are still mentionable.
    let (everyone, users, roles) = announce_mentions();
    assert!(!everyone, "the announcement must never ping @everyone");
    assert!(users, "winners are mentioned by id");
    assert!(roles, "the notify/entrants roles are pinged explicitly");

    // And the fallback is only reachable when the edit did not land.
    let body = body_of(BOT_SOURCE, "async fn announce_with");
    let edit_branch = body
        .find("if edited.is_ok()")
        .expect("the edit success branch is in the body");
    let fallback = body
        .find("let mut message = CreateMessage::new()")
        .expect("the fallback post is in the body");
    assert!(
        edit_branch < fallback,
        "the fallback post must sit outside the successful edit: {body}"
    );
}

/// Python: `AnnouncementTests.test_roleless_ping_is_capped_and_says_what_it_missed`.
///
/// ADAPTED: Python counted `channel.sent` and read `interaction.texts[-1]`. The
/// send loop is `split_mentions(..).take(MAX_MENTION_MESSAGES)` and the followup
/// is `ping_summary`, so the cap arithmetic and that exact text are asserted.
#[tokio::test(flavor = "multi_thread")]
async fn roleless_ping_is_capped_and_says_what_it_missed() {
    let f = BotFixture::new().await;
    let gw = f.make_named("big", GUILD).await;
    for index in 0..600u64 {
        f.join_user(&gw, &format!("{:0>18}", index + 1)).await;
    }
    let rows = f
        .bot
        .service()
        .entries(&gw.id, None)
        .await
        .expect("entries");
    let ids: Vec<String> = rows.iter().filter_map(json_id).collect();
    assert_eq!(ids.len(), 600, "the fixture seeded every entrant");

    // `_role_for` answered None, so the per-member fallback runs: members are
    // mentioned, the role and @everyone are not.
    let (everyone, users, roles) = mention_policy(false);
    assert!(!everyone && users && !roles);

    let body = ping_body("big", "");
    let pairs = split_mentions(&ids, &body);
    let messages_sent = pairs.len().min(MAX_MENTION_MESSAGES);
    assert_eq!(
        messages_sent, MAX_MENTION_MESSAGES,
        "the cap is what bounds the send loop"
    );
    assert!(
        pairs.len() > messages_sent,
        "600 entrants need more messages than the cap allows: {}",
        pairs.len()
    );
    let sent: usize = pairs
        .iter()
        .take(messages_sent)
        .map(|(_, count)| *count)
        .sum();
    assert!(sent < ids.len(), "the cap must leave members unreached");

    let summary = ping_summary(sent, ids.len(), pairs.len() > MAX_MENTION_MESSAGES);
    assert!(summary.contains("capped"), "{summary}");
    assert!(summary.contains("600"), "{summary}");

    // A small giveaway still pings everybody with no cap notice.
    let small = f.make_named("small", GUILD).await;
    f.join_user(&small, &"4".repeat(18)).await;
    let rows = f
        .bot
        .service()
        .entries(&small.id, None)
        .await
        .expect("small entries");
    let ids: Vec<String> = rows.iter().filter_map(json_id).collect();
    let pairs = split_mentions(&ids, &ping_body("small", ""));
    let small_summary = ping_summary(ids.len(), ids.len(), pairs.len() > MAX_MENTION_MESSAGES);
    assert!(
        small_summary.contains("Pinged 1 entrant"),
        "{small_summary}"
    );
    assert!(!small_summary.contains("capped"), "{small_summary}");
}

/// Python: `AnnouncementTests.test_reroll_posts_fresh_winner_without_editing_original_result`.
///
/// ADAPTED: `channel.message.edits == []` and the single `channel.sent` are the
/// post-only step plan for a reroll; the post's embed is the same
/// `winner_embed` + `announce_title` pair `announce_with` builds.
#[tokio::test]
async fn reroll_posts_fresh_winner_without_editing_original_result() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("live message");
    for uid in ["4".repeat(18), "5".repeat(18)] {
        f.join_user(&gw, &uid).await;
    }
    let (_ended, originals) = f
        .bot
        .service()
        .end(&gw.id, None, false, None)
        .await
        .expect("end");
    assert_eq!(originals.len(), 1);

    // A reroll never edits the original result: post-only, even though the
    // giveaway still knows its message id.
    let ended_row = f.bot.service().get(&gw.id).await.expect("ended row");
    assert!(ended_row.message_id.is_some());
    let steps = announce_steps(ended_row.message_id.is_some(), true);
    assert_eq!(steps.len(), 1, "no edits, one post: {steps:?}");
    assert_eq!(steps[0], AnnounceStep::Post);
    assert_eq!(announce_title(true), "Giveaway Rerolled");

    let (ended, fresh) = f
        .bot
        .service()
        .reroll(&gw.id, 1, None)
        .await
        .expect("reroll");
    assert_eq!(fresh.len(), 1);
    assert!(
        !originals.contains(&fresh[0]),
        "a reroll picks somebody new: {fresh:?} vs {originals:?}"
    );

    let entries = ended.entrant_count.unwrap_or(0);
    let mut embed = embeds::winner_embed(
        &ended,
        &fresh,
        entries,
        f.bot.state.settings.embed_color,
        None,
        None,
    );
    embed.title = announce_title(true).to_string();
    assert_eq!(embed.title, "Giveaway Rerolled");
    assert!(
        !embed.description.contains(&format!("<@{}>", originals[0])),
        "the original winner must not be re-announced: {}",
        embed.description
    );
    assert!(
        embed.description.contains(&format!("<@{}>", fresh[0])),
        "the fresh winner is named: {}",
        embed.description
    );
}

/// Python: `AnnouncementTests.test_cancel_replaces_live_embed_and_removes_buttons`.
///
/// ADAPTED: `edits[0]["view"] is None` and `edits[0]["embed"].title` are read off
/// `cancel_edit`, the builder the handler sends; the cancel notice's mention
/// policy is `create_mentions`, and "not active" is the service row.
#[tokio::test]
async fn cancel_replaces_live_embed_and_removes_buttons() {
    let f = BotFixture::new().await;
    let gw = f.make_named("@everyone", GUILD).await;
    f.bot
        .service()
        .set_message(&gw.id, "333333333333333333")
        .await
        .expect("live message");
    f.join_user(&gw, &"4".repeat(18)).await;

    let ended = f.bot.service().cancel(&gw.id).await.expect("cancel");
    assert!(!f
        .bot
        .service()
        .get(&gw.id)
        .await
        .expect("cancelled row")
        .active());

    let panel = cancelled_embed(&ended, f.bot.state.settings.embed_color);
    assert_eq!(panel.title, "Giveaway Cancelled");
    let edit = serde_json::to_value(cancel_edit(&panel)).expect("edit serialises");
    assert_eq!(
        edit["components"],
        serde_json::json!([]),
        "the stale Join/Leave buttons must be removed"
    );
    assert_eq!(edit["embeds"][0]["title"], "Giveaway Cancelled");
    // The cancel notice uses the create policy: no @everyone, even with
    // "@everyone" as the prize.
    assert!(
        !create_mentions().0,
        "the cancel notice must not ping @everyone"
    );
}

/// Python: `AnnouncementTests.test_ping_and_extend_disable_everyone_in_both_mention_modes`.
///
/// ADAPTED: `channel.sent[-1]["allowed_mentions"]` is `mention_policy`, which
/// both handlers build their send from; the two bodies carry the literal prize.
#[tokio::test]
async fn ping_and_extend_disable_everyone_in_both_mention_modes() {
    let f = BotFixture::new().await;
    let gw = f.make_named("@everyone", GUILD).await;
    f.join_user(&gw, &"4".repeat(18)).await;

    // Both mention modes: an entrants role exists, and it does not.
    for has_role in [true, false] {
        let (everyone, users, roles) = mention_policy(has_role);
        assert!(
            !everyone,
            "a prize reading @everyone must never become a server-wide ping"
        );
        assert_eq!(roles, has_role, "a role ping mentions only the role");
        assert_eq!(users, !has_role, "the fallback mentions only the members");
        // The bodies carry the literal prize text, so the policy is the only
        // thing between it and a server-wide ping.
        assert!(ping_body(&gw.prize, "@everyone").contains("@everyone"));
        assert!(extend_notice(&gw.prize, 60).contains("@everyone"));
    }
}
