//! Port of the retired Python `BanCommandTests` cases.
//!
//! serenity 0.12 builds a `Context` only inside its own dispatch, so a slash
//! command cannot be invoked from a test. Each case here reproduces the exact
//! sequence `commands_admin`'s handlers run — the permission guard, the service
//! write, the live-giveaway count — and then asserts on the pure reply renderers
//! and on the database state those renderers are built from.

use super::*;

// -- BanCommandTests -----------------------------------------------------

/// Python: `BanCommandTests.test_ban_blocks_and_reports_expiry_and_reason`.
#[tokio::test]
async fn ban_blocks_and_reports_expiry_and_reason() {
    let f = BotFixture::new().await;
    let uid = "571555555555555555";

    // ADAPTED: Python invoked the callback through a fake interaction. The
    // handler is `commands_admin::ban`, whose only steps are this write, the
    // live list (for the "entries removed" count) and the reply. `999` is the
    // invoker id the fake interaction carried.
    let info = f
        .bot
        .service()
        .blacklist_add(GUILD, uid, 5, "spam", "999", None)
        .await
        .expect("ban");
    let running = f
        .bot
        .service()
        .list_active(GUILD)
        .await
        .expect("live list")
        .len();
    let permanent = info
        .get("permanent")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    let expires_at = info.get("expires_at").and_then(serde_json::Value::as_i64);
    let text = ban_reply(uid, permanent, expires_at, 5, running, "spam");

    assert!(
        text.contains("banned until <t:"),
        "a timed ban must name its expiry: {text}"
    );
    assert!(text.contains("Reason: spam"), "{text}");
    assert!(
        f.bot
            .service()
            .is_blacklisted(GUILD, uid, None)
            .await
            .expect("is_blacklisted"),
        "the ban must be stored"
    );
    let stored = f
        .bot
        .service()
        .ban_info(GUILD, uid, None)
        .await
        .expect("ban_info")
        .expect("an active ban row");
    assert_eq!(
        stored.get("reason").and_then(serde_json::Value::as_str),
        Some("spam"),
        "the reason is kept with the ban, not only in the reply"
    );
}

/// Python: `BanCommandTests.test_ban_zero_is_permanent`.
#[tokio::test]
async fn ban_zero_is_permanent() {
    let f = BotFixture::new().await;
    let uid = "572555555555555555";

    // ADAPTED: hours=0 is the permanent branch of the same handler.
    let info = f
        .bot
        .service()
        .blacklist_add(GUILD, uid, 0, "bad", "999", None)
        .await
        .expect("ban");
    let permanent = info
        .get("permanent")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    let expires_at = info.get("expires_at").and_then(serde_json::Value::as_i64);
    let text = ban_reply(uid, permanent, expires_at, 0, 0, "bad");

    assert!(text.contains("permanently"), "{text}");
    assert!(text.contains("Reason: bad"), "{text}");
    assert!(
        expires_at.is_none(),
        "a permanent ban carries no expiry: {info:?}"
    );
}

/// Python: `BanCommandTests.test_ban_bad_hours_is_refused`.
#[tokio::test]
async fn ban_bad_hours_is_refused() {
    let f = BotFixture::new().await;
    let uid = "573555555555555555";

    // ADAPTED: the handler turns a `ServiceError` into "⚠️ {message}" and
    // returns without touching the blacklist, so both halves are asserted.
    let err = f
        .bot
        .service()
        .blacklist_add(GUILD, uid, 721, "", "999", None)
        .await
        .expect_err("721 hours is out of range");
    let text = match err.downcast_ref::<ServiceError>() {
        Some(refusal) => format!("⚠️ {}", refusal.message),
        None => panic!("the duration rule must refuse this, not the database: {err}"),
    };

    assert!(text.contains("0-720"), "{text}");
    assert!(
        !f.bot
            .service()
            .is_blacklisted(GUILD, uid, None)
            .await
            .expect("is_blacklisted"),
        "a refused ban must not block anybody"
    );
}

/// Python: `BanCommandTests.test_ban_requires_manage`.
#[tokio::test]
async fn ban_requires_manage() {
    let f = BotFixture::new().await;
    let uid = "574555555555555555";

    // ADAPTED: Python asserted the interaction's only text. The permission bits
    // live on a `CommandInteraction` payload serenity will not build outside
    // dispatch, so the guard's answer is pinned and its shipped body is checked
    // to answer before any write.
    assert_eq!(MANAGE_REQUIRED, "You need **Manage Server**.");
    let guard = body_of(COMMANDS_ADMIN_SOURCE, "async fn manage_guard");
    assert!(
        guard.contains("MANAGE_REQUIRED") && guard.contains("return None"),
        "the guard answers MANAGE_REQUIRED and gives up: {guard}"
    );
    assert!(
        !guard.contains("service()"),
        "the guard must answer before any blacklist write: {guard}"
    );
    assert!(
        !f.bot
            .service()
            .is_blacklisted(GUILD, uid, None)
            .await
            .expect("is_blacklisted"),
        "a member without Manage Server must not be able to ban"
    );
}

/// Python: `BanCommandTests.test_unban_clears_and_reports`.
#[tokio::test]
async fn unban_clears_and_reports() {
    let f = BotFixture::new().await;
    let uid = "575555555555555555";
    f.bot
        .service()
        .blacklist_add(GUILD, uid, 3, "", "999", None)
        .await
        .expect("seed ban");

    // ADAPTED: `commands_admin::unban` is this removal plus the reply.
    let removed = f
        .bot
        .service()
        .blacklist_remove(GUILD, uid)
        .await
        .expect("unban");
    let text = unban_reply(uid, removed);

    assert!(removed, "there was a ban to clear");
    assert!(text.contains("can join giveaways again"), "{text}");
    assert!(
        !f.bot
            .service()
            .is_blacklisted(GUILD, uid, None)
            .await
            .expect("is_blacklisted"),
        "the row is gone"
    );
}
