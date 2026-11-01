//! Port of the retired Python `CreatePersistenceTests` cases.
//!
//! Python made the bookkeeping writes raise and read the fake interaction's
//! followup. Here the same statements fail at the database seam, and the reply
//! is `started_reply` — the exact string the handler sends.

use super::*;

/// Python: `CreatePersistenceTests.test_posted_giveaway_still_reports_success_when_message_or_role_save_fails`.
///
/// ADAPTED: `service.set_message` / `service.set_entrants_role` are made to fail
/// by marker at the database, which is the `OSError` Python injected. The fake
/// channel's `sent` list becomes the active row count — the invariant Python
/// asserted (`len(list_active) == len(channel.sent)`) — and its
/// `allowed_mentions` is `create_mentions`.
#[tokio::test]
async fn posted_giveaway_still_reports_success_when_message_or_role_save_fails() {
    for failing_step in ["set_message", "set_entrants_role"] {
        let f = BotFixture::new().await;
        let gw = f.create("@everyone", GUILD, 600).await;
        let db = f.bot.service().db();

        // The giveaway is already posted; only the bookkeeping write dies.
        let (message_note, role_note) = match failing_step {
            "set_message" => {
                db.fail_matching("UPDATE simple_giveaways SET message_id");
                let failed = f
                    .bot
                    .service()
                    .set_message(&gw.id, "333333333333333333")
                    .await;
                db.clear_failures();
                assert!(failed.is_err(), "the message id write must fail");
                (MESSAGE_NOTE_FAILED, "")
            }
            _ => {
                db.fail_matching("UPDATE simple_giveaways SET entrants_role_id");
                let failed = f
                    .bot
                    .service()
                    .set_entrants_role(&gw.id, Some("777777777777777777"))
                    .await;
                db.clear_failures();
                assert!(failed.is_err(), "the entrants role write must fail");
                ("", ROLE_NOTE_FAILED)
            }
        };

        // The reply still claims success and only says what could not be saved.
        let reply = started_reply("https://discord.test/gw", message_note, role_note);
        assert!(reply.contains("Giveaway started:"), "{reply}");
        assert!(
            reply.contains(message_note) && reply.contains(role_note),
            "{reply}"
        );
        assert!(
            !reply.to_lowercase().contains("failed"),
            "a bookkeeping blip is not a failed create: {reply}"
        );

        // One posted giveaway, one active row: the failed write changed nothing.
        let active = f
            .bot
            .service()
            .list_active(GUILD)
            .await
            .expect("active list");
        assert_eq!(active.len(), 1, "the giveaway must still be live");
        assert_eq!(active[0].id, gw.id);

        // The create post never pings members or @everyone, whatever the prize.
        let (everyone, users, roles) = create_mentions();
        assert!(!everyone, "creating a giveaway must not ping @everyone");
        assert!(!users, "creating a giveaway must not ping members");
        assert!(roles, "required roles are still pinged by id");

        // The handler defers before any database work, so the member gets an ack
        // even when a later write is slow or dead.
        let body = body_of(COMMANDS_SOURCE, "async fn giveaway_create");
        let defer = body
            .find("defer_command")
            .expect("the create handler defers");
        let create = body.find("service().create").expect("...then creates");
        assert!(
            defer < create,
            "the defer must precede the database work: {body}"
        );
    }
}
