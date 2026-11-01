//! Rule-level tests for the giveaway engine.
//!
//! Every test drives the real service through the harness. Nothing here
//! re-implements a rule: assertions are about what the engine stored, returned
//! or refused.
//!
//! Conventions that keep the port honest:
//!
//! * the manual clock is the only clock (`advance`/`set_time`), never `sleep`;
//! * a draw is never asserted to pick a *particular* member, only invariants;
//! * cross-process races live in `concurrency.rs` and restarts in `recovery.rs`,
//!   because one process cannot produce either.

use std::collections::{BTreeMap, BTreeSet};

use gwbot_testkit::{ended, join, merge, Fixture};
use serde_json::{json, Value};

const USER: &str = "100000000000000010";
const OTHER: &str = "100000000000000011";
const STAFF: &str = "100000000000000012";

/// The `create` defaults [`Fixture::gw`] applies, exposed for the tests that
/// expect the call itself to be refused (and so cannot use `gw`).
fn create_args(f: &Fixture, overrides: Value) -> Value {
    merge(
        json!({
            "guild_id": f.guild,
            "channel_id": f.channel,
            "prize": "Nitro",
            "winner_count": 1,
            "duration_seconds": 3600,
            "created_by": "100000000000000003",
        }),
        overrides,
    )
}

fn gw_ids(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("expected a giveaway list")
        .iter()
        .map(|gw| gw["id"].as_str().unwrap_or_default().to_string())
        .collect()
}

fn user_ids(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("expected a row list")
        .iter()
        .map(|row| row["user_id"].as_str().unwrap_or_default().to_string())
        .collect()
}

/// `[guild, user, count]` triples, the shape `add_message_counts` takes.
fn count_rows(rows: &[(&str, &str, i64)]) -> Value {
    Value::Array(
        rows.iter()
            .map(|(guild, user, count)| json!([guild, user, count]))
            .collect(),
    )
}

/// The `id` of a giveaway, for the ops that take a bare id.
fn id_of(gw: &Value) -> String {
    gw["id"].as_str().expect("giveaway has no id").to_string()
}

// -- create --------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn create_defaults_and_round_trip() {
    let f = Fixture::new().await;
    let gw = f
        .gw(json!({
            "prize": "  Steam key  ",
            "host_name": "Host",
            "required_role_id": "42",
        }))
        .await;

    assert_eq!(json!("Steam key"), gw["prize"]);
    assert_eq!(json!("42"), gw["required_role_id"]);
    assert_eq!(json!(["42"]), gw["required_role_ids"]);
    assert_eq!(json!("active"), gw["status"]);
    assert_eq!(json!(1), gw["winner_count"]);
    assert_eq!(json!(0), gw["entrant_count"]);
    assert_eq!(json!(0), gw["claim_timeout_seconds"]);
    assert_eq!(json!("Host"), gw["host_name"]);
    assert!(gw["message_id"].is_null());
    assert_eq!(json!([]), gw["winners"]);

    let row =
        f.h.one_params(
            "SELECT * FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("giveaway row");
    assert_eq!(json!("[]"), row["winners_json"]);
    assert_eq!(gw["ends_at"], row["ends_at"]);
    assert_eq!(
        3600 * 1000,
        row["ends_at"].as_i64().unwrap() - row["created_at"].as_i64().unwrap()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn create_validates_every_bounded_field() {
    let f = Fixture::new().await;
    let cases: Vec<(Value, &str)> = vec![
        (json!({"prize": ""}), "Prize must be 1-256 characters."),
        (
            json!({"prize": "x".repeat(257)}),
            "Prize must be 1-256 characters.",
        ),
        (json!({"winner_count": 0}), "Winner count must be 1-25."),
        (json!({"winner_count": 26}), "Winner count must be 1-25."),
        (
            json!({"duration_seconds": 29}),
            "Duration must be 30 seconds to 60 days.",
        ),
        (
            json!({"duration_seconds": 60 * 86400 + 1}),
            "Duration must be 30 seconds to 60 days.",
        ),
        (
            json!({"min_messages": -1}),
            "Minimum messages must be 0-100000.",
        ),
        (
            json!({"min_messages": 100001}),
            "Minimum messages must be 0-100000.",
        ),
        (
            json!({"min_account_age_days": -1}),
            "Minimum account age must be 0-3650 days.",
        ),
        (
            json!({"min_account_age_days": 3651}),
            "Minimum account age must be 0-3650 days.",
        ),
        (
            json!({"image_url": "ftp://example.test/a.png"}),
            "Image must be an http(s) URL.",
        ),
        (
            json!({"image_url": format!("https://example.test/{}", "a".repeat(500))}),
            "Image must be an http(s) URL.",
        ),
        (
            json!({"claim_timeout_seconds": -1}),
            "Claim window must be 0 (off) or up to 60 days in seconds.",
        ),
        (
            json!({"claim_timeout_seconds": 60 * 86400 + 1}),
            "Claim window must be 0 (off) or up to 60 days in seconds.",
        ),
    ];
    for (overrides, message) in cases {
        let err =
            f.h.refuse("create", create_args(&f, overrides.clone()))
                .await;
        assert_eq!(message, err.message, "for {overrides}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn create_role_list_is_capped_and_deduplicated() {
    let f = Fixture::new().await;
    let gw = f
        .gw(json!({"required_role_ids": ["1", "2", "3", "4", "5", "6", "7"]}))
        .await;
    // Trimmed and capped at five, in the order the caller gave them.
    assert_eq!(json!(["1", "2", "3", "4", "5"]), gw["required_role_ids"]);

    // The legacy single role is only appended when the list does not already
    // name it: "9" leads here, so it is not added twice.
    let gw2 = f
        .gw(json!({"required_role_id": "9", "required_role_ids": ["9", "8"]}))
        .await;
    assert_eq!(json!(["9", "8"]), gw2["required_role_ids"]);
    assert_eq!(json!("9"), gw2["required_role_id"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn create_merges_the_legacy_required_role_column() {
    let f = Fixture::new().await;
    let gw = f
        .gw(json!({"required_role_id": "legacy", "required_role_ids": ["a", "b"]}))
        .await;
    assert_eq!(json!(["a", "b", "legacy"]), gw["required_role_ids"]);

    let row =
        f.h.one_params(
            "SELECT required_role_ids FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("giveaway row");
    let stored: Vec<String> = serde_json::from_str(row["required_role_ids"].as_str().unwrap())
        .expect("required_role_ids is JSON");
    assert_eq!(vec!["a", "b", "legacy"], stored);
}

#[tokio::test(flavor = "multi_thread")]
async fn get_missing_giveaway_raises() {
    let f = Fixture::new().await;
    let err =
        f.h.refuse("get", json!({"giveaway_id": "does-not-exist"}))
            .await;
    assert_eq!("Giveaway not found.", err.message);
}

// -- lookup --------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn list_active_is_guild_scoped_and_ordered() {
    let f = Fixture::new().await;
    let later = f.gw(json!({"duration_seconds": 1200})).await;
    let sooner = f.gw(json!({"duration_seconds": 600})).await;
    f.gw(json!({"guild_id": f.other_guild, "duration_seconds": 300}))
        .await;
    let finished = f.gw(json!({"duration_seconds": 300})).await;
    f.end(&finished).await;

    let active = f.h.ok("list_active", json!({"guild_id": f.guild})).await;
    assert_eq!(vec![id_of(&sooner), id_of(&later)], gw_ids(&active));
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_refuses_another_guild() {
    let f = Fixture::new().await;
    let foreign = f.gw(json!({"guild_id": f.other_guild})).await;
    let err =
        f.h.refuse(
            "resolve",
            json!({"guild_id": f.guild, "raw_id": foreign["id"]}),
        )
        .await;
    assert_eq!("That giveaway belongs to another server.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_falls_back_to_the_only_live_one() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let found =
        f.h.ok("resolve", json!({"guild_id": f.guild, "raw_id": "typo"}))
            .await;
    assert_eq!(gw["id"], found["id"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_needs_an_id_when_several_are_live() {
    let f = Fixture::new().await;
    f.gw(json!({})).await;
    f.gw(json!({})).await;
    let err =
        f.h.refuse("resolve", json!({"guild_id": f.guild, "raw_id": ""}))
            .await;
    assert!(
        err.message.contains("That ID did not match."),
        "{}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_with_nothing_live() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;
    let err =
        f.h.refuse("resolve", json!({"guild_id": f.guild, "raw_id": ""}))
            .await;
    assert_eq!("No active giveaway in this server.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn get_by_message_finds_the_giveaway() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "set_message",
        json!({"giveaway_id": gw["id"], "message_id": "m-1"}),
    )
    .await;

    let found = f.h.ok("get_by_message", json!({"message_id": "m-1"})).await;
    assert_eq!(gw["id"], found["id"]);
    let missing = f.h.ok("get_by_message", json!({"message_id": "m-2"})).await;
    assert!(missing.is_null());
}

// -- join and leave ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn join_then_leave_round_trips() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;

    assert_eq!(1, f.enter(&gw, USER).await);
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );

    let left =
        f.h.ok(
            "leave_with_count",
            json!({"giveaway_id": gw["id"], "user_id": USER}),
        )
        .await;
    assert_eq!(json!([true, 0, "active"]), left);
    assert_eq!(
        json!(false),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(0),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    // A leave must undo the join bump, not leave the live counter behind.
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn leave_after_the_end_keeps_the_entry() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.end(&gw).await;

    let left =
        f.h.ok(
            "leave_with_count",
            json!({"giveaway_id": gw["id"], "user_id": USER}),
        )
        .await;
    assert_eq!(json!([false, 0, ""]), left);
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_non_duplicate_constraint_error_is_not_called_already_entered() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.trigger(
        "CREATE TRIGGER bad_entry BEFORE INSERT ON simple_entries \
         BEGIN SELECT RAISE(ABORT, 'NOT NULL constraint failed: simple_entries.username'); END",
    )
    .await;
    // Any constraint failure other than the duplicate-entry one is a real bug
    // and must surface, not be disguised as "you are already entered".
    let err = join(&f.h, &gw, USER, json!({}))
        .await
        .expect_err("join was expected to fail");
    assert!(!format!("{err}").contains("already entered"), "{err}");
    assert!(
        err.service().is_none(),
        "a raw failure must not be a refusal"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn double_join_is_a_service_error() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    assert_eq!(
        "You are already entered.",
        f.join_error(&gw, USER).await.message
    );
    assert_eq!(1, f.entry_rows(&id_of(&gw)).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn required_roles_need_one_match() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"required_role_ids": ["r1", "r2"]})).await;
    assert_eq!(
        "You need one of the required roles to enter.",
        f.join_error_with(&gw, USER, json!({"member_roles": ["other"]}))
            .await
            .message
    );
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"member_roles": ["r2"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn blocked_role_cannot_enter() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"blocked_role_id": "bad"})).await;
    assert_eq!(
        "Your role is not allowed to enter.",
        f.join_error_with(&gw, USER, json!({"member_roles": ["bad"]}))
            .await
            .message
    );
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"member_roles": ["good"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn account_age_is_enforced_and_fails_closed() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"min_account_age_days": 30})).await;

    // An unreadable age must refuse: waving it through silently disables the
    // alt-account filter.
    assert_eq!(
        "Could not check your account age — try again in a moment.",
        f.join_error_with(&gw, USER, json!({"account_created_ts": null}))
            .await
            .message
    );
    assert_eq!(
        "Account must be 30+ days old.",
        f.join_error_with(
            &gw,
            USER,
            json!({"account_created_ts": f.account_age(10.0)})
        )
        .await
        .message
    );
    assert_eq!(
        1,
        f.enter_with(
            &gw,
            USER,
            json!({"account_created_ts": f.account_age(40.0)})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn min_messages_uses_the_database_count() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"min_messages": 5})).await;
    for _ in 0..4 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": USER}),
        )
        .await;
    }
    assert_eq!(
        "You need 5+ messages in this server (4 counted).",
        f.join_error(&gw, USER).await.message
    );
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    assert_eq!(1, f.enter(&gw, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn min_messages_counts_the_unflushed_buffer() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"min_messages": 5})).await;
    f.h.ok(
        "add_message_counts",
        json!({"rows": count_rows(&[(&f.guild, USER, 3)])}),
    )
    .await;
    // Three flushed + two still in the in-memory buffer must clear a 5 gate.
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"pending_messages": 2}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn has_entry_is_a_targeted_lookup() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    assert_eq!(
        json!(false),
        f.h.ok("has_entry", json!({"giveaway_id": gw["id"], "user_id": ""}))
            .await
    );
    f.enter(&gw, USER).await;
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(false),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": OTHER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn entries_can_be_limited_for_a_display_path() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let members: Vec<String> = (0..3)
        .map(|index| format!("30000000000000{index:04}"))
        .collect();
    for member in &members {
        f.enter(&gw, member).await;
        f.h.advance(1); // entered_at is the only ordering key
    }

    let rows =
        f.h.ok("entries", json!({"giveaway_id": gw["id"], "limit": 2}))
            .await;
    assert_eq!(members[..2].to_vec(), user_ids(&rows));
    // limit.max(1): a display path never asks for zero rows by accident.
    let none =
        f.h.ok("entries", json!({"giveaway_id": gw["id"], "limit": 0}))
            .await;
    assert_eq!(1, none.as_array().unwrap().len());
}

#[tokio::test(flavor = "multi_thread")]
async fn ended_giveaway_refuses_joins() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;
    assert_eq!(
        "This giveaway has ended.",
        f.join_error(&gw, USER).await.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn expired_deadline_refuses_joins() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    f.h.advance(61_000);
    assert_eq!(
        "This giveaway has ended.",
        f.join_error(&gw, USER).await.message
    );
}

// -- message counts ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn record_message_increments_the_stored_count() {
    let f = Fixture::new().await;
    for _ in 0..3 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": USER}),
        )
        .await;
    }
    assert_eq!(
        json!(3),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(0),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": OTHER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn batch_flush_adds_deltas_per_key() {
    let f = Fixture::new().await;
    let result =
        f.h.ok(
            "add_message_counts",
            json!({"rows": count_rows(&[
                (&f.guild, USER, 2),
                (&f.guild, USER, 3),
                (&f.other_guild, USER, 4),
            ])}),
        )
        .await;
    // "before" counts the distinct keys snapshotted (two), not the rows sent.
    assert_eq!(json!({"applied": 3, "before": 2}), result);
    assert_eq!(
        json!(5),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(4),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.other_guild, "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn batch_flush_chunks_large_payloads() {
    let f = Fixture::new().await;
    let members: Vec<String> = (0..301)
        .map(|index| format!("40000000000000{index:04}"))
        .collect();
    let rows: Vec<(&str, &str, i64)> = members
        .iter()
        .map(|member| (f.guild.as_str(), member.as_str(), 1))
        .collect();
    let result =
        f.h.ok("add_message_counts", json!({"rows": count_rows(&rows)}))
            .await;
    // 301 rows is two statements: the chunk size is 300.
    assert_eq!(json!({"applied": 301, "before": 301}), result);
    for member in [members.first().unwrap(), members.last().unwrap()] {
        assert_eq!(
            json!(1),
            f.h.ok(
                "message_count",
                json!({"guild_id": f.guild, "user_id": member})
            )
            .await
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn ambiguous_chunk_failure_requeues_only_what_is_missing() {
    let f = Fixture::new().await;
    // Before the chunk ran the stored count was 3; the chunk carried +5; a
    // partial write landed +2 (stored 5). Only 3 are still missing.
    f.h.ok(
        "add_message_counts",
        json!({"rows": count_rows(&[(&f.guild, USER, 3)])}),
    )
    .await;
    f.h.ok(
        "add_message_counts",
        json!({"rows": count_rows(&[(&f.guild, USER, 2)])}),
    )
    .await;
    let shortfall =
        f.h.ok(
            "reconcile_message_counts",
            json!({
                "rows": count_rows(&[(&f.guild, USER, 5)]),
                "before": count_rows(&[(&f.guild, USER, 3)]),
            }),
        )
        .await;
    assert_eq!(json!([[f.guild, USER, 3]]), shortfall);
    f.h.ok("add_message_counts", json!({"rows": shortfall}))
        .await;
    assert_eq!(
        json!(8),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn deterministic_chunk_failure_requeues_the_whole_chunk() {
    let f = Fixture::new().await;
    // No pre-write snapshot means the stored value cannot be compared, so the
    // whole delta is requeued: over-counting beats losing messages.
    let shortfall =
        f.h.ok(
            "reconcile_message_counts",
            json!({
                "rows": count_rows(&[(&f.guild, USER, 4)]),
                "before": [],
            }),
        )
        .await;
    assert_eq!(json!([[f.guild, USER, 4]]), shortfall);
}

#[tokio::test(flavor = "multi_thread")]
async fn end_resets_counts_for_the_next_grind() {
    let f = Fixture::new().await;
    for _ in 0..4 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": USER}),
        )
        .await;
    }
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;

    assert_eq!(
        json!(0),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_keeps_counts_while_another_giveaway_needs_them() {
    let f = Fixture::new().await;
    let first = f.gw(json!({"min_messages": 5})).await;
    f.gw(json!({"min_messages": 5})).await;
    for _ in 0..4 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": USER}),
        )
        .await;
    }

    f.end(&first).await;

    // Zeroing now would silently reset the grind the other giveaway measures.
    assert_eq!(
        json!(4),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_resets_only_the_guild_whose_giveaway_ended() {
    let f = Fixture::new().await;
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    f.h.ok(
        "record_message",
        json!({"guild_id": f.other_guild, "user_id": USER}),
    )
    .await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;

    assert_eq!(
        json!(0),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(1),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.other_guild, "user_id": USER})
        )
        .await
    );
}

// -- draws ---------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn end_draws_once_and_refuses_a_second_call() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 1})).await;
    let entrants: BTreeSet<String> = (0..3)
        .map(|index| format!("50000000000000{index:04}"))
        .collect();
    for member in &entrants {
        f.enter(&gw, member).await;
    }

    let (ended_gw, winners) = f.end(&gw).await;

    assert_eq!("ended", ended_gw.status);
    assert_eq!(1, winners.len());
    assert!(entrants.contains(&winners[0]));
    // entrant_count records the pool the draw saw, not the winner count.
    assert_eq!(Some(3), ended_gw.entrant_count);
    assert_eq!(
        "This giveaway has already ended.",
        f.end_error(&gw).await.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_without_entries_is_safe() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let (ended_gw, winners) = f.end(&gw).await;
    assert!(winners.is_empty());
    assert_eq!(Some(0), ended_gw.entrant_count);
    assert!(ended_gw.winners.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn winners_never_exceed_the_pool() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 5})).await;
    let entrants: BTreeSet<String> = (0..3)
        .map(|index| format!("50000000000000{index:04}"))
        .collect();
    for member in &entrants {
        f.enter(&gw, member).await;
    }

    let (_, winners) = f.end(&gw).await;

    assert_eq!(3, winners.len());
    assert_eq!(3, winners.iter().collect::<BTreeSet<_>>().len());
    assert!(winners.iter().all(|winner| entrants.contains(winner)));
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_excludes_previous_winners() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 1})).await;
    let entrants: BTreeSet<String> = (0..4)
        .map(|index| format!("50000000000000{index:04}"))
        .collect();
    for member in &entrants {
        f.enter(&gw, member).await;
    }
    let (_, winners) = f.end(&gw).await;

    let (_, fresh) = ended(
        &f.h.ok("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await,
    );

    assert_eq!(1, fresh.len());
    assert!(!winners.contains(&fresh[0]));
    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let stored_winners: Vec<String> = stored["winners"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item.as_str().unwrap_or_default().to_string())
        .collect();
    assert_eq!(2, stored_winners.len());
    assert_eq!(
        stored_winners.len(),
        stored_winners.iter().collect::<BTreeSet<_>>().len()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_refuses_when_the_pool_is_exhausted() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.end(&gw).await;

    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert_eq!("No other entrants left to reroll", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_refuses_after_the_entries_were_wiped() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for index in 0..3 {
        f.enter(&gw, &format!("50000000000000{index:04}")).await;
    }
    f.end(&gw).await;
    f.execute_params(
        "DELETE FROM simple_entries WHERE giveaway_id = ?",
        json!([gw["id"]]),
    )
    .await;

    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert_eq!("No other entrants left to reroll", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn end_stores_the_entrant_count() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for index in 0..3 {
        f.enter(&gw, &format!("50000000000000{index:04}")).await;
    }

    let (ended_gw, _) = f.end(&gw).await;

    assert_eq!(Some(3), ended_gw.entrant_count);
    assert_eq!(
        json!(3),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("entrant_count")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_end_write_leaves_the_giveaway_active() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.trigger(
        "CREATE TRIGGER fail_end BEFORE UPDATE ON simple_giveaways WHEN NEW.status = 'ended' \
         BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    .await;

    let err =
        f.h.call(
            "end",
            json!({"giveaway_id": gw["id"], "require_due": false}),
        )
        .await
        .expect_err("end was expected to fail");
    assert!(err.service().is_none(), "a database failure must surface");

    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("active"), stored["status"]);
    assert_eq!(json!([]), stored["winners"]);
    assert_eq!(
        json!("[]"),
        f.h.scalar_params(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("winners_json")
    );

    // The guarded flip is the only step allowed to raise; once the failure is
    // gone the same call must still be able to end it.
    f.drop_trigger("fail_end").await;
    let (ended_gw, winners) = f.end(&gw).await;
    assert_eq!("ended", ended_gw.status);
    assert_eq!(1, winners.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn cancel_stores_the_entrant_count_and_loses_a_race_to_end() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;

    let cancelled = f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;

    assert_eq!(json!("cancelled"), cancelled["status"]);
    assert_eq!(json!(2), cancelled["entrant_count"]);
    assert_eq!(
        "This giveaway has already ended.",
        f.end_error(&gw).await.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_refuses_a_running_giveaway() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert_eq!("End the giveaway before rerolling.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_never_exceeds_the_winner_cap() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 25})).await;
    for index in 0..30 {
        f.enter(&gw, &format!("50000000000000{index:04}")).await;
    }
    f.end(&gw).await;

    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 5}))
            .await;
    assert!(err.message.contains("25 winners"), "{}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cancelled_giveaway_cannot_be_rerolled() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;

    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert_eq!("A cancelled giveaway cannot be rerolled.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn extend_and_cancel_follow_the_lifecycle() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 600})).await;

    let extended =
        f.h.ok(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": 60}),
        )
        .await;
    assert_eq!(
        gw["ends_at"].as_i64().unwrap() + 60_000,
        extended["ends_at"].as_i64().unwrap()
    );

    let err =
        f.h.refuse(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": 59}),
        )
        .await;
    assert_eq!("Extend by 1 minute to 60 days at a time.", err.message);

    let cancelled = f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("cancelled"), cancelled["status"]);

    let err = f.h.refuse("cancel", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!("Only an active giveaway can be cancelled.", err.message);

    let err =
        f.h.refuse(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": 60}),
        )
        .await;
    assert_eq!("Only a running giveaway can be extended.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn discard_removes_a_never_posted_giveaway() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "set_message",
        json!({"giveaway_id": gw["id"], "message_id": "m-1"}),
    )
    .await;
    f.enter(&gw, USER).await;

    f.h.ok("discard", json!({"giveaway_id": gw["id"]})).await;

    let err = f.h.refuse("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!("Giveaway not found.", err.message);
    assert_eq!(0, f.entry_rows(&id_of(&gw)).await);
    assert_eq!(
        Some(0),
        f.h.scalar("SELECT COUNT(*) FROM simple_claims")
            .await
            .and_then(|value| value.as_i64())
    );
}

// -- retention -----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn leftover_entrants_roles_are_reported() {
    let f = Fixture::new().await;
    let ended_gw = f.gw(json!({})).await;
    f.h.ok(
        "set_entrants_role",
        json!({"giveaway_id": ended_gw["id"], "role_id": "r-ended"}),
    )
    .await;
    f.end(&ended_gw).await;

    let running = f.gw(json!({})).await;
    f.h.ok(
        "set_entrants_role",
        json!({"giveaway_id": running["id"], "role_id": "r-running"}),
    )
    .await;

    let leftovers = f.h.ok("ended_with_roles", json!({"limit": 50})).await;
    assert_eq!(vec![id_of(&ended_gw)], gw_ids(&leftovers));
}

#[tokio::test(flavor = "multi_thread")]
async fn entry_rows_are_wiped_after_the_retention_window() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    f.end(&gw).await;

    f.h.advance(5 * 60 * 60 * 1000 + 1);
    let deleted = f.h.ok("wipe_stale_entries", Value::Null).await;

    assert_eq!(json!(2), deleted);
    assert_eq!(0, f.entry_rows(&id_of(&gw)).await);
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        json!(0),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("entrant_count")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn rows_without_an_ended_at_still_expire() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    // Giveaways that finished before ended_at existed carry NULL forever.
    f.execute_params(
        "UPDATE simple_giveaways SET status = 'ended', ended_at = NULL, created_at = ? WHERE id = ?",
        json!([f.h.now() - 6 * 60 * 60 * 1000, gw["id"]]),
    )
    .await;

    assert_eq!(json!(2), f.h.ok("wipe_stale_entries", Value::Null).await);
    assert_eq!(0, f.entry_rows(&id_of(&gw)).await);
}

// -- blacklist -----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn blacklist_add_blocks_and_purges_only_running_giveaways() {
    let f = Fixture::new().await;
    let running = f.gw(json!({})).await;
    let finished = f.gw(json!({})).await;
    f.enter(&running, USER).await;
    f.enter(&finished, USER).await;
    f.end(&finished).await;

    let info =
        f.h.ok(
            "blacklist_add",
            json!({
                "guild_id": f.guild,
                "user_id": USER,
                "hours": 0,
                "reason": "spam",
                "banned_by": STAFF,
            }),
        )
        .await;

    assert_eq!(json!(true), info["permanent"]);
    assert!(info["expires_at"].is_null());
    let message =
        f.h.ok("ban_message", json!({"guild_id": f.guild, "user_id": USER}))
            .await;
    assert!(message.as_str().unwrap().contains("blocked"));
    assert_eq!(0, f.entry_rows(&id_of(&running)).await);
    assert_eq!(1, f.entry_rows(&id_of(&finished)).await);
    assert!(f
        .join_error(&running, USER)
        .await
        .message
        .contains("blocked"));
}

#[tokio::test(flavor = "multi_thread")]
async fn blacklist_count_matches_the_list() {
    let f = Fixture::new().await;
    let members: Vec<String> = (0..3)
        .map(|index| format!("60000000000000{index:04}"))
        .collect();
    for member in &members {
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": member}),
        )
        .await;
    }

    assert_eq!(
        json!(3),
        f.h.ok("count_blacklisted", json!({"guild_id": f.guild}))
            .await
    );
    assert_eq!(
        json!(members),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild, "limit": 50}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn the_blacklist_purge_leaves_other_guilds_alone() {
    let f = Fixture::new().await;
    let here = f.gw(json!({})).await;
    let there = f.gw(json!({"guild_id": f.other_guild})).await;
    f.enter(&here, USER).await;
    f.enter(&there, USER).await;

    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;

    assert_eq!(0, f.entry_rows(&id_of(&here)).await);
    assert_eq!(1, f.entry_rows(&id_of(&there)).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_blacklist_landing_mid_join_still_refuses() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    // The snapshot says "clean": only the INSERT guard can catch the ban.
    let err = f
        .join_error_with(&gw, USER, json!({"snapshot": [false, 0, 0, false]}))
        .await;
    assert!(err.message.contains("blocked"), "{}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn blacklist_remove_and_list_round_trip() {
    let f = Fixture::new().await;
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild,
            "user_id": USER,
            "hours": 1,
            "reason": "  spam  ",
            "banned_by": STAFF,
        }),
    )
    .await;
    let detailed =
        f.h.ok("blacklist_list_detailed", json!({"guild_id": f.guild}))
            .await;
    let rows = detailed.as_array().unwrap();
    assert_eq!(1, rows.len());
    assert_eq!(json!("spam"), rows[0]["reason"]);
    assert_eq!(json!(STAFF), rows[0]["banned_by"]);
    assert_eq!(json!(false), rows[0]["permanent"]);

    assert_eq!(
        json!(true),
        f.h.ok(
            "blacklist_remove",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(false),
        f.h.ok(
            "blacklist_remove",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!([]),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild})).await
    );
    assert_eq!(
        json!(0),
        f.h.ok("count_blacklisted", json!({"guild_id": f.guild}))
            .await
    );
}

// -- timeout penalty -----------------------------------------------------

/// Start a penalty by attempting a timed-out join, and return the giveaway it
/// happened on.
async fn start_penalty(f: &Fixture, user_id: &str) -> Value {
    let gw = f.gw(json!({})).await;
    let error = f.timed_join_error(&gw, user_id).await;
    assert_eq!("timeout_ban", error.kind);
    gw
}

async fn remaining(f: &Fixture, user_id: &str) -> i64 {
    f.h.ok(
        "timeout_ban_remaining",
        json!({"guild_id": f.guild, "user_id": user_id}),
    )
    .await
    .as_i64()
    .expect("remaining is an integer")
}

#[tokio::test(flavor = "multi_thread")]
async fn a_timed_out_member_is_refused_and_starts_the_penalty() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let error = f.timed_join_error(&gw, USER).await;

    assert_eq!("timeout_ban", error.kind);
    assert!(
        error.message.contains("sit out the next 3"),
        "{}",
        error.message
    );
    assert_eq!(3, remaining(&f, USER).await);
    assert_eq!(0, f.entry_rows(&id_of(&gw)).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_timeout_refusal_is_tagged_for_the_caller() {
    let f = Fixture::new().await;
    // The bot branches on this tag to take back the entrant role it granted.
    let gw = f.gw(json!({})).await;
    assert_eq!("timeout_ban", f.timed_join_error(&gw, USER).await.kind);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_untimed_out_member_is_unaffected() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    assert_eq!(1, f.enter(&gw, USER).await);
    assert_eq!(0, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_role_is_never_mistaken_for_a_timeout() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"member_roles": ["some-role"]}))
            .await
    );
    assert_eq!(0, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn three_blocked_giveaways_spend_the_penalty_and_lift_it() {
    let f = Fixture::new().await;
    start_penalty(&f, USER).await;
    let mut left: Vec<i64> = Vec::new();
    for _ in 0..3 {
        let blocked = f.gw(json!({})).await;
        // No timed_out flag here: the penalty alone must keep refusing.
        let error = f.join_error(&blocked, USER).await;
        assert!(error.message.contains("penalty"), "{}", error.message);
        assert_eq!(0, f.entry_rows(&id_of(&blocked)).await);
        left.push(remaining(&f, USER).await);
    }

    assert_eq!(vec![2, 1, 0], left, "one giveaway per blocked attempt");
    // The zero row is deleted, not left behind to show up in a listing.
    assert_eq!(
        json!([]),
        f.h.ok("list_timeout_bans", json!({"guild_id": f.guild}))
            .await
    );
    let fresh = f.gw(json!({})).await;
    assert_eq!(1, f.enter(&fresh, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn repeated_attempts_never_stack_or_respend_the_penalty() {
    let f = Fixture::new().await;
    let gw = start_penalty(&f, USER).await;
    for _ in 0..5 {
        // Still timed out, same giveaway: neither a second penalty nor a second
        // giveaway spent on the one they were already caught in.
        let error = f.timed_join_error(&gw, USER).await;
        assert!(
            error.message.contains("3 giveaway(s) left"),
            "{}",
            error.message
        );
        assert_eq!(3, remaining(&f, USER).await);
    }
    assert_eq!(
        Some(1),
        f.h.scalar_params(
            "SELECT COUNT(*) FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?",
            json!([f.guild, USER]),
        )
        .await
        .and_then(|value| value.as_i64())
    );

    // A different giveaway while still timed out spends one and reports the
    // running penalty, never a fresh three.
    let other = f.gw(json!({})).await;
    let error = f.timed_join_error(&other, USER).await;
    assert!(
        error.message.contains("2 giveaway(s) left"),
        "{}",
        error.message
    );
    assert_eq!(2, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn alternating_giveaways_cannot_burn_the_penalty() {
    let f = Fixture::new().await;
    let first = start_penalty(&f, USER).await;
    let second = f.gw(json!({})).await;

    f.timed_join_error(&second, USER).await;
    assert_eq!(2, remaining(&f, USER).await);
    // Both giveaways already count as sat out, so neither may spend again.
    f.timed_join_error(&first, USER).await;
    f.timed_join_error(&second, USER).await;
    assert_eq!(2, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_giveaway_that_spent_the_last_unit_still_refuses() {
    let f = Fixture::new().await;
    start_penalty(&f, USER).await;
    let blocked: Vec<Value> = {
        let mut items = Vec::new();
        for _ in 0..3 {
            items.push(f.gw(json!({})).await);
        }
        items
    };
    for gw in &blocked {
        f.join_error(gw, USER).await;
    }
    assert_eq!(0, remaining(&f, USER).await);

    // The penalty is gone, but this giveaway is one they already sat out:
    // rejoining it would hand the penalty straight back, so it refuses.
    let last = blocked.last().unwrap();
    let error = f.join_error(last, USER).await;
    assert_eq!("timeout_ban", error.kind);
    assert!(
        error.message.contains("sat out this giveaway"),
        "{}",
        error.message
    );
    assert_eq!(0, f.entry_rows(&id_of(last)).await);
    let fresh = f.gw(json!({})).await;
    assert_eq!(1, f.enter(&fresh, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_entry_made_before_the_timeout_is_dropped() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.timed_join_error(&gw, USER).await;

    assert_eq!(0, f.entry_rows(&id_of(&gw)).await);
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_penalty_clears_entries_in_every_running_giveaway() {
    let f = Fixture::new().await;
    let first = f.gw(json!({})).await;
    let second = f.gw(json!({})).await;
    f.enter(&first, USER).await;
    f.enter(&second, USER).await;

    let trigger = f.gw(json!({})).await;
    f.timed_join_error(&trigger, USER).await;

    assert_eq!(0, f.entry_rows(&id_of(&first)).await);
    assert_eq!(0, f.entry_rows(&id_of(&second)).await);
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": first["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_blacklisted_member_is_refused_without_a_penalty() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;

    let error = f.timed_join_error(&gw, USER).await;
    assert!(error.message.contains("blocked"), "{}", error.message);
    assert_eq!(0, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_ended_giveaway_never_hands_out_a_penalty() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;

    assert_eq!(
        "This giveaway has ended.",
        f.timed_join_error(&gw, USER).await.message
    );
    assert_eq!(0, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_expired_deadline_never_hands_out_a_penalty() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    f.h.advance(61_000);

    assert_eq!(
        "This giveaway has ended.",
        f.timed_join_error(&gw, USER).await.message
    );
    assert_eq!(0, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_penalty_is_per_guild_and_per_user() {
    let f = Fixture::new().await;
    start_penalty(&f, USER).await;

    let other_guild = f.gw(json!({"guild_id": f.other_guild})).await;
    assert_eq!(1, f.enter(&other_guild, USER).await);
    let other_user = f.gw(json!({})).await;
    assert_eq!(1, f.enter(&other_user, OTHER).await);
    assert_eq!(3, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_second_penalty_never_shrinks_the_first() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for giveaways in [5, 3, 1] {
        let applied =
            f.h.ok(
                "apply_timeout_penalty",
                json!({
                    "guild_id": f.guild,
                    "user_id": USER,
                    "giveaway_id": gw["id"],
                    "giveaways": giveaways,
                }),
            )
            .await;
        assert_eq!(json!(5), applied);
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn one_penalty_does_not_block_anybody_else() {
    let f = Fixture::new().await;
    start_penalty(&f, USER).await;
    let gw = f.gw(json!({})).await;
    assert_eq!(1, f.enter(&gw, OTHER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_entry_sweep_never_touches_a_penalty() {
    let f = Fixture::new().await;
    start_penalty(&f, USER).await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, OTHER).await;
    f.end(&gw).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
        json!([f.h.now() - 6 * 60 * 60 * 1000, gw["id"]]),
    )
    .await;

    assert_eq!(json!(1), f.h.ok("wipe_stale_entries", Value::Null).await);

    assert_eq!(3, remaining(&f, USER).await);
    assert_eq!(
        Some(1),
        f.h.scalar_params(
            "SELECT COUNT(*) FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ?",
            json!([f.guild, USER]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn timeout_ban_counting_matches_the_listing() {
    let f = Fixture::new().await;
    for (user_id, left) in [
        ("700000000000000000", 2),
        ("700000000000000001", 5),
        ("700000000000000002", 3),
    ] {
        let gw = f.gw(json!({})).await;
        f.h.ok(
            "apply_timeout_penalty",
            json!({
                "guild_id": f.guild,
                "user_id": user_id,
                "giveaways": left,
                "giveaway_id": gw["id"],
            }),
        )
        .await;
    }

    assert_eq!(
        json!(3),
        f.h.ok("count_timeout_bans", json!({"guild_id": f.guild}))
            .await
    );
    let listed =
        f.h.ok(
            "list_timeout_bans",
            json!({"guild_id": f.guild, "limit": 50}),
        )
        .await;
    let remaining_units: Vec<i64> = listed
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["giveaways_remaining"].as_i64().unwrap_or_default())
        .collect();
    assert_eq!(vec![5, 3, 2], remaining_units);
}

#[tokio::test(flavor = "multi_thread")]
async fn timeout_ban_list_is_guild_scoped_ordered_and_skips_zero_rows() {
    let f = Fixture::new().await;
    f.execute_params(
        "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) VALUES (?, ?, 0)",
        json!([f.guild, "700000000000000099"]),
    )
    .await;
    f.execute_params(
        "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) VALUES (?, ?, 9)",
        json!([f.other_guild, "700000000000000098"]),
    )
    .await;
    for (user_id, giveaways) in [("a", 1), ("b", 4)] {
        f.h.ok(
            "apply_timeout_penalty",
            json!({"guild_id": f.guild, "user_id": user_id, "giveaways": giveaways}),
        )
        .await;
    }

    let listed =
        f.h.ok(
            "list_timeout_bans",
            json!({"guild_id": f.guild, "limit": 50}),
        )
        .await;
    assert_eq!(vec!["b".to_string(), "a".to_string()], user_ids(&listed));
    // limit is clamped to at least one and applied by the query.
    let one =
        f.h.ok(
            "list_timeout_bans",
            json!({"guild_id": f.guild, "limit": 1}),
        )
        .await;
    assert_eq!(1, one.as_array().unwrap().len());
}

// -- claims --------------------------------------------------------------

/// Draw `winner_count` winners out of `entrants`, with a live claim window.
async fn ended_with_claims(
    f: &Fixture,
    entrants: usize,
    winner_count: i64,
    claim_timeout_seconds: i64,
) -> (Value, Vec<String>, Vec<String>) {
    let gw = f
        .gw(json!({
            "winner_count": winner_count,
            "claim_timeout_seconds": claim_timeout_seconds,
        }))
        .await;
    let users: Vec<String> = (0..entrants)
        .map(|index| format!("80000000000000{index:04}"))
        .collect();
    for member in &users {
        f.enter(&gw, member).await;
    }
    let (_, winners) = f.end(&gw).await;
    assert_eq!(winner_count as usize, winners.len());
    assert!(winners.iter().all(|winner| users.contains(winner)));
    (gw, winners, users)
}

/// `user_id -> status` from `claim_status`.
async fn claim_statuses(f: &Fixture, giveaway_id: &Value) -> BTreeMap<String, String> {
    f.h.ok("claim_status", json!({"giveaway_id": giveaway_id}))
        .await
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["user_id"].as_str().unwrap_or_default().to_string(),
                row["status"].as_str().unwrap_or_default().to_string(),
            )
        })
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn create_validates_the_claim_window() {
    let f = Fixture::new().await;
    for seconds in [-1, 60 * 86400 + 1] {
        f.h.refuse(
            "create",
            create_args(&f, json!({"claim_timeout_seconds": seconds})),
        )
        .await;
    }
    // 0 is off, and a positive window round-trips.
    assert_eq!(
        json!(0),
        f.gw(json!({"claim_timeout_seconds": 0})).await["claim_timeout_seconds"]
    );
    assert_eq!(
        json!(60),
        f.gw(json!({"claim_timeout_seconds": 60})).await["claim_timeout_seconds"]
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_opens_claims_when_enabled() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 3, 1, 300).await;

    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    let ended_at =
        f.h.scalar_params(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
        .expect("ended_at");

    assert_eq!(vec![winners[0].clone()], user_ids(&pending));
    assert_eq!(json!("pending"), pending[0]["status"]);
    // The deadline is anchored to ended_at, so a restart can rebuild it.
    assert_eq!(json!(ended_at + 300_000), pending[0]["deadline_ms"]);

    // Restart recovery: a second process on the same file sees the same rows.
    let sibling = f.sibling().await;
    let again = sibling
        .ok("pending_claims", json!({"giveaway_id": gw["id"]}))
        .await;
    assert_eq!(vec![winners[0].clone()], user_ids(&again));
}

#[tokio::test(flavor = "multi_thread")]
async fn end_creates_no_claims_when_disabled() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 3, 1, 0).await;

    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        json!([]),
        f.h.ok("due_claims", json!({"now": f.h.now() + 10 * 86400 * 1000}))
            .await
    );
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await;
    assert_eq!("Only the drawn winner can claim this prize.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn claim_happy_path_and_double_claim_refused() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 3, 1, 300).await;

    let row =
        f.h.ok(
            "claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await;

    assert_eq!(json!(winners[0]), row["user_id"]);
    assert_eq!(json!("claimed"), row["status"]);
    assert!(!row["claimed_at"].is_null());
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await;
    assert_eq!("This prize was already claimed.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn claim_by_non_winner_refused() {
    let f = Fixture::new().await;
    let (gw, winners, users) = ended_with_claims(&f, 3, 1, 300).await;
    let loser = users
        .iter()
        .find(|user| **user != winners[0])
        .expect("a loser exists");

    // The loser never drew, so there is no claim row for them at all.
    let err =
        f.h.refuse("claim", json!({"giveaway_id": gw["id"], "user_id": loser}))
            .await;
    assert_eq!("Only the drawn winner can claim this prize.", err.message);
    f.h.refuse(
        "claim",
        json!({"giveaway_id": "missing", "user_id": winners[0]}),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn skip_claim_needs_staff_and_no_replacement() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 3, 1, 300).await;

    let err =
        f.h.refuse(
            "skip_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "staff_id": ""}),
        )
        .await;
    assert_eq!("A staff member must verify the claim.", err.message);

    let row =
        f.h.ok(
            "skip_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "staff_id": STAFF}),
        )
        .await;
    assert_eq!(json!("skipped"), row["status"]);
    assert_eq!(json!(STAFF), row["skipped_by"]);
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
    // No replacement is drawn: the winners list is untouched.
    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!(winners), stored["winners"]);

    let err =
        f.h.refuse(
            "skip_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "staff_id": STAFF}),
        )
        .await;
    assert_eq!("That claim was already verified by staff.", err.message);
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await;
    assert_eq!("This prize was already verified by staff.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_draws_replacement_never_repeats() {
    let f = Fixture::new().await;
    let (gw, winners, users) = ended_with_claims(&f, 3, 1, 300).await;
    let deadline =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await[0]["deadline_ms"]
            .as_i64()
            .expect("deadline_ms");

    // Too early: the guarded UPDATE matches nothing.
    f.h.refuse(
        "expire_claim",
        json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline - 1}),
    )
    .await;

    let (ended_gw, replacements) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline + 1}),
        )
        .await,
    );

    assert_eq!(1, replacements.len());
    assert!(users.contains(&replacements[0]));
    assert!(!winners.contains(&replacements[0]));
    // The expired winner stays named; the replacement is added alongside.
    assert!(ended_gw.winners.contains(&winners[0]));
    assert!(ended_gw.winners.contains(&replacements[0]));

    let statuses = claim_statuses(&f, &gw["id"]).await;
    assert_eq!("expired", statuses[&winners[0]]);
    assert_eq!("pending", statuses[&replacements[0]]);
    assert_eq!(
        vec![replacements[0].clone()],
        user_ids(
            &f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
                .await
        )
    );

    // The expired slot can no longer be claimed, but its replacement can.
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await;
    assert_eq!("This claim window has closed.", err.message);
    f.h.ok(
        "claim",
        json!({"giveaway_id": gw["id"], "user_id": replacements[0]}),
    )
    .await;
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_with_empty_pool_returns_no_replacement() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 1, 1, 300).await;
    let deadline =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await[0]["deadline_ms"]
            .as_i64()
            .expect("deadline_ms");

    let (ended_gw, replacements) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline + 1}),
        )
        .await,
    );

    assert!(replacements.is_empty());
    assert_eq!(winners, ended_gw.winners);
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn multi_winner_claims_are_independent() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 4, 2, 300).await;
    assert_eq!(2, winners.len());

    f.h.ok(
        "claim",
        json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
    )
    .await;
    let still = user_ids(
        &f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await,
    );
    assert_eq!(vec![winners[1].clone()], still);

    let deadline =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await[0]["deadline_ms"]
            .as_i64()
            .expect("deadline_ms");
    let (ended_gw, replacements) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[1], "now": deadline + 1}),
        )
        .await,
    );
    assert_eq!(1, replacements.len());
    assert!(!winners.contains(&replacements[0]));
    assert_eq!(3, ended_gw.winners.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn start_claims_is_idempotent() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 3, 1, 300).await;
    let before =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;

    f.h.ok(
        "start_claims",
        json!({"gw": gw, "winners": winners, "now": f.h.now()}),
    )
    .await;

    let after =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(
        before.as_array().unwrap().len(),
        after.as_array().unwrap().len()
    );
    assert_eq!(1, after.as_array().unwrap().len());
}

#[tokio::test(flavor = "multi_thread")]
async fn manual_reroll_opens_claims_and_keeps_old_pending() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 3, 1, 300).await;

    let (ended_gw, fresh) = ended(
        &f.h.ok("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await,
    );

    assert_eq!(1, fresh.len());
    assert!(!winners.contains(&fresh[0]));
    // Additive: both the old winner and the reroll winner stay named.
    assert!(ended_gw.winners.contains(&winners[0]));
    assert!(ended_gw.winners.contains(&fresh[0]));

    let pending: BTreeSet<String> = user_ids(
        &f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await,
    )
    .into_iter()
    .collect();
    assert_eq!(
        BTreeSet::from([winners[0].clone(), fresh[0].clone()]),
        pending
    );

    f.h.ok(
        "claim",
        json!({"giveaway_id": gw["id"], "user_id": fresh[0]}),
    )
    .await;
    let still = user_ids(
        &f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await,
    );
    assert_eq!(vec![winners[0].clone()], still);
}

#[tokio::test(flavor = "multi_thread")]
async fn manual_reroll_never_repeats_claim_draws() {
    let f = Fixture::new().await;
    let (gw, winners, users) = ended_with_claims(&f, 3, 1, 300).await;
    let deadline =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await[0]["deadline_ms"]
            .as_i64()
            .expect("deadline_ms");

    let (_, auto) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline + 1}),
        )
        .await,
    );
    assert_eq!(1, auto.len());

    let (_, fresh) = ended(
        &f.h.ok("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await,
    );
    assert_eq!(1, fresh.len());
    // Neither the expired winner nor the auto replacement may reappear.
    assert!(![winners[0].clone(), auto[0].clone()].contains(&fresh[0]));
    assert!(users.contains(&fresh[0]));
    let third = users
        .iter()
        .find(|user| **user != winners[0] && **user != auto[0])
        .expect("a third entrant exists");
    assert_eq!(third, &fresh[0]);

    // The pool is now exhausted: one more reroll must refuse.
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert_eq!("No other entrants left to reroll", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_reroll_replaces_one_slot_in_place() {
    let f = Fixture::new().await;
    let (gw, winners, users) = ended_with_claims(&f, 4, 3, 300).await;
    let target = winners[0].clone();
    let expected = users
        .iter()
        .find(|user| !winners.contains(user))
        .expect("an unused entrant exists")
        .clone();

    let (ended_gw, replacements) = ended(
        &f.h.ok(
            "reroll",
            json!({"giveaway_id": gw["id"], "count": 1, "replace_user_id": target}),
        )
        .await,
    );

    assert_eq!(vec![expected], replacements);
    // Same length, order preserved: only the picked slot swaps.
    assert_eq!(3, ended_gw.winners.len());
    assert_eq!(replacements[0], ended_gw.winners[0]);
    assert_eq!(winners[1..].to_vec(), ended_gw.winners[1..].to_vec());
    assert!(!ended_gw.winners.contains(&target));

    let statuses = claim_statuses(&f, &gw["id"]).await;
    assert_eq!("replaced", statuses[&target]);
    assert_eq!("pending", statuses[&replacements[0]]);
    let pending: BTreeSet<String> = user_ids(
        &f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await,
    )
    .into_iter()
    .collect();
    assert_eq!(
        ended_gw.winners.iter().cloned().collect::<BTreeSet<_>>(),
        pending
    );

    // The fresh winner can press Claim Prize; the old one gets a clear no.
    f.h.ok(
        "claim",
        json!({"giveaway_id": gw["id"], "user_id": replacements[0]}),
    )
    .await;
    let err =
        f.h.refuse("claim", json!({"giveaway_id": gw["id"], "user_id": target}))
            .await;
    assert_eq!("This winner was replaced by a reroll.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_reroll_rejects_non_winner_and_exhausted_pool() {
    let f = Fixture::new().await;
    let (gw, winners, _) = ended_with_claims(&f, 4, 3, 300).await;

    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": gw["id"], "replace_user_id": "899999999999999999"}),
        )
        .await;
    assert_eq!("That member is not a winner of this giveaway.", err.message);

    // One swap exhausts the 4-entrant pool: every user is now ever-drawn.
    f.h.ok(
        "reroll",
        json!({"giveaway_id": gw["id"], "count": 1, "replace_user_id": winners[0]}),
    )
    .await;
    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": gw["id"], "count": 1, "replace_user_id": winners[1]}),
        )
        .await;
    assert_eq!("No other entrants left to reroll", err.message);
    f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
        .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_reroll_without_claims_still_excludes() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 0})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    let (_, winners) = f.end(&gw).await;

    let (ended_gw, replacements) = ended(
        &f.h.ok(
            "reroll",
            json!({"giveaway_id": gw["id"], "replace_user_id": winners[0]}),
        )
        .await,
    );
    assert_eq!(vec![replacements[0].clone()], ended_gw.winners);

    // The swapped-out winner has no claim row, so a marker must keep them out
    // of every later draw.
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert_eq!("No other entrants left to reroll", err.message);
}

// -- resolve ended -------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_returns_ended_by_id() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;
    let found =
        f.h.ok(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": gw["id"]}),
        )
        .await;
    assert_eq!(gw["id"], found["id"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_refuses_live() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let err =
        f.h.refuse(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": gw["id"]}),
        )
        .await;
    assert_eq!("That giveaway has not ended yet.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_never_falls_back_to_live() {
    let f = Fixture::new().await;
    f.gw(json!({})).await;
    let err =
        f.h.refuse("resolve_ended", json!({"guild_id": f.guild, "raw_id": ""}))
            .await;
    assert_eq!("No ended giveaway in this server.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_single_fallback() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;
    let found =
        f.h.ok(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": "typo"}),
        )
        .await;
    assert_eq!(gw["id"], found["id"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_refuses_another_guild() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"guild_id": f.other_guild})).await;
    f.end(&gw).await;
    let err =
        f.h.refuse(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": gw["id"]}),
        )
        .await;
    assert_eq!("That giveaway belongs to another server.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_with_nothing_ended() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;
    let err =
        f.h.refuse(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": gw["id"]}),
        )
        .await;
    assert_eq!("That giveaway has not ended yet.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn list_ended_is_guild_scoped() {
    let f = Fixture::new().await;
    let first = f.gw(json!({})).await;
    f.end(&first).await;
    f.h.advance(10);
    let second = f.gw(json!({})).await;
    f.end(&second).await;
    let foreign = f.gw(json!({"guild_id": f.other_guild})).await;
    f.end(&foreign).await;

    let ids =
        f.h.ok("list_ended", json!({"guild_id": f.guild, "limit": 25}))
            .await;
    assert_eq!(vec![id_of(&second), id_of(&first)], gw_ids(&ids));
    let foreign_ids =
        f.h.ok(
            "list_ended",
            json!({"guild_id": f.other_guild, "limit": 25}),
        )
        .await;
    assert_eq!(vec![id_of(&foreign)], gw_ids(&foreign_ids));
}

// -- reroll CAS ----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn read_winners_raw_round_trip() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 2})).await;
    for index in 0..3 {
        f.enter(&gw, &format!("80000000000000{index:04}")).await;
    }
    let (_, winners) = f.end(&gw).await;

    let stored =
        f.h.scalar_params(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("winners_json");
    // The draw is persisted as JSON; reroll reads it back to exclude winners.
    let parsed: Vec<String> = serde_json::from_str(stored.as_str().unwrap()).expect("JSON winners");
    assert_eq!(winners, parsed);
}

// -- claim atomicity -----------------------------------------------------

/// Make every claim insert fail, so the draw is stored but its claim rows are
/// not.
async fn block_claim_inserts(f: &Fixture) {
    f.trigger(
        "CREATE TRIGGER no_claims BEFORE INSERT ON simple_claims \
         BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn end_insert_crash_still_announces_and_heals() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    block_claim_inserts(&f).await;

    let (ended_gw, winners) = f.end(&gw).await;

    // The draw is stored before the inserts, so the announcement still works.
    assert_eq!("ended", ended_gw.status);
    assert_eq!(1, winners.len());
    assert!([USER, OTHER].contains(&winners[0].as_str()));
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );

    // The next read of the claim window repairs the stranded draw.
    f.drop_trigger("no_claims").await;
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(vec![winners[0].clone()], user_ids(&pending));
}

#[tokio::test(flavor = "multi_thread")]
async fn stranded_draw_healed_by_pending_and_claim_retry() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    f.enter(&gw, USER).await;
    let (_, winners) = f.end(&gw).await;
    f.execute_params(
        "DELETE FROM simple_claims WHERE giveaway_id = ?",
        json!([gw["id"]]),
    )
    .await;

    let row =
        f.h.ok(
            "claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await;

    assert_eq!(json!("claimed"), row["status"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn stranded_draw_healed_by_tick_sweep() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    let (_, winners) = f.end(&gw).await;
    let deadline =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await[0]["deadline_ms"]
            .as_i64()
            .expect("deadline_ms");
    f.execute_params(
        "DELETE FROM simple_claims WHERE giveaway_id = ?",
        json!([gw["id"]]),
    )
    .await;

    // due_claims heals stranded windows before it answers "nothing due".
    let due = f.h.ok("due_claims", json!({"now": deadline + 1})).await;

    assert_eq!(winners, user_ids(&due));
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(winners, user_ids(&pending));
}

#[tokio::test(flavor = "multi_thread")]
async fn repair_is_idempotent_and_settles_stale_pending() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    f.end(&gw).await;
    f.execute_params(
        "INSERT INTO simple_claims (giveaway_id, user_id, round, status, deadline_ms, created_at) \
         VALUES (?, ?, 0, 'pending', 0, 0)",
        json!([gw["id"], "899999999999999999"]),
    )
    .await;

    assert_eq!(
        json!(1),
        f.h.ok("repair_missing_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        json!(0),
        f.h.ok("repair_missing_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
    let statuses = claim_statuses(&f, &gw["id"]).await;
    assert_eq!("replaced", statuses["899999999999999999"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_insert_crash_heals_fresh_window() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    for index in 0..3 {
        f.enter(&gw, &format!("80000000000000{index:04}")).await;
    }
    block_claim_inserts(&f).await;
    f.end(&gw).await;

    f.drop_trigger("no_claims").await;
    let (ended_gw, fresh) = ended(
        &f.h.ok("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await,
    );

    assert_eq!(1, fresh.len());
    assert_eq!(2, ended_gw.winners.len());
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(2, pending.as_array().unwrap().len());
    assert_eq!(
        ended_gw.winners.iter().cloned().collect::<BTreeSet<_>>(),
        user_ids(&pending).into_iter().collect::<BTreeSet<_>>()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_insert_crash_heals_replacement() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 60})).await;
    for index in 0..3 {
        f.enter(&gw, &format!("80000000000000{index:04}")).await;
    }
    let (_, winners) = f.end(&gw).await;
    f.h.advance(61_000);
    block_claim_inserts(&f).await;

    let (_, replacements) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
        )
        .await,
    );
    assert_eq!(1, replacements.len());

    f.drop_trigger("no_claims").await;
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(vec![replacements[0].clone()], user_ids(&pending));
}

// -- targeted settled guard ---------------------------------------------

/// Draw winners with a live claim window and `entrants` in the pool.
async fn draw_with_claims(f: &Fixture, entrants: usize, overrides: Value) -> (Value, Vec<String>) {
    let gw = f
        .gw(merge(json!({"claim_timeout_seconds": 300}), overrides))
        .await;
    for index in 0..entrants {
        f.enter(&gw, &format!("90000000000000{index:04}")).await;
    }
    let (_, winners) = f.end(&gw).await;
    (gw, winners)
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_replace_refuses_claimed() {
    let f = Fixture::new().await;
    let (gw, winners) = draw_with_claims(&f, 3, json!({})).await;
    f.h.ok(
        "claim",
        json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
    )
    .await;

    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": gw["id"], "replace_user_id": winners[0]}),
        )
        .await;
    assert_eq!("That prize was already claimed.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_replace_refuses_skipped() {
    let f = Fixture::new().await;
    let (gw, winners) = draw_with_claims(&f, 3, json!({})).await;
    f.h.ok(
        "skip_claim",
        json!({"giveaway_id": gw["id"], "user_id": winners[0], "staff_id": STAFF}),
    )
    .await;

    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": gw["id"], "replace_user_id": winners[0]}),
        )
        .await;
    assert_eq!("That claim was already verified by staff.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_replace_refuses_expired() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"claim_timeout_seconds": 60})).await;
    for index in 0..3 {
        f.enter(&gw, &format!("90000000000000{index:04}")).await;
    }
    let (_, winners) = f.end(&gw).await;
    f.h.advance(61_000);
    f.h.ok(
        "expire_claim",
        json!({"giveaway_id": gw["id"], "user_id": winners[0]}),
    )
    .await;

    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": gw["id"], "replace_user_id": winners[0]}),
        )
        .await;
    assert_eq!("That winner was already replaced.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn second_replace_of_same_slot_refused() {
    let f = Fixture::new().await;
    let (gw, winners) = draw_with_claims(&f, 3, json!({})).await;
    f.h.ok(
        "reroll",
        json!({"giveaway_id": gw["id"], "replace_user_id": winners[0]}),
    )
    .await;

    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": gw["id"], "replace_user_id": winners[0]}),
        )
        .await;
    let allowed = [
        "That member is not a winner of this giveaway.",
        "That winner was already replaced.",
    ];
    assert!(allowed.contains(&err.message.as_str()), "{}", err.message);
}

// -- guild settings ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn notify_role_upserts_and_clears() {
    let f = Fixture::new().await;
    assert!(f
        .h
        .ok("get_notify_role", json!({"guild_id": f.guild}))
        .await
        .is_null());

    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": "r1"}),
    )
    .await;
    assert_eq!(
        json!("r1"),
        f.h.ok("get_notify_role", json!({"guild_id": f.guild}))
            .await
    );

    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": "r2"}),
    )
    .await;
    assert_eq!(
        json!("r2"),
        f.h.ok("get_notify_role", json!({"guild_id": f.guild}))
            .await
    );

    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": null}),
    )
    .await;
    assert!(f
        .h
        .ok("get_notify_role", json!({"guild_id": f.guild}))
        .await
        .is_null());
}

// -- eligibility snapshot ------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_returns_all_four_fields() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for _ in 0..4 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": USER}),
        )
        .await;
    }
    f.h.ok(
        "apply_timeout_penalty",
        json!({
            "guild_id": f.guild,
            "user_id": USER,
            "giveaways": 2,
            "giveaway_id": gw["id"],
        }),
    )
    .await;

    let snapshot =
        f.h.ok(
            "eligibility_snapshot",
            json!({"guild_id": f.guild, "user_id": USER, "giveaway_id": gw["id"]}),
        )
        .await;
    assert_eq!(json!([false, 2, 4, true]), snapshot);
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_sees_blacklist_ban_and_hit() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    f.h.ok(
        "apply_timeout_penalty",
        json!({
            "guild_id": f.guild,
            "user_id": USER,
            "giveaways": 1,
            "giveaway_id": gw["id"],
        }),
    )
    .await;

    let snapshot =
        f.h.ok(
            "eligibility_snapshot",
            json!({"guild_id": f.guild, "user_id": USER, "giveaway_id": gw["id"]}),
        )
        .await;
    let fields = snapshot.as_array().unwrap();
    assert_eq!(json!(true), fields[0]);
    assert_eq!(json!(1), fields[1]);
    assert_eq!(json!(0), fields[2]);
    assert_eq!(json!(true), fields[3]);
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_empty_ids_fail_closed() {
    let f = Fixture::new().await;
    let snapshot =
        f.h.ok(
            "eligibility_snapshot",
            json!({"guild_id": "", "user_id": "", "giveaway_id": null}),
        )
        .await;
    assert_eq!(json!([false, 0, 0, false]), snapshot);
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_none_on_db_failure_forces_fallback() {
    let f = Fixture::new().await;
    // A missing snapshot is how a failed snapshot read is reported, and the
    // live checks must still refuse. Omitting the argument is the same path.
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    assert!(f.join_error(&gw, USER).await.message.contains("blocked"));
}

#[tokio::test(flavor = "multi_thread")]
async fn join_with_snapshot_honors_live_block() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    let err = f
        .join_error_with(&gw, USER, json!({"snapshot": [true, 0, 0, false]}))
        .await;
    assert!(err.message.contains("blocked"), "{}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_snapshot_block_does_not_falsely_refuse() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    // The snapshot predates a blacklist_remove: confirming live is what stops an
    // unbanned member from being refused for another minute.
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"snapshot": [true, 0, 0, false]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn join_with_snapshot_counts_buffered_messages() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"min_messages": 5})).await;
    assert_eq!(
        1,
        f.enter_with(
            &gw,
            USER,
            json!({"snapshot": [false, 0, 3, false], "pending_messages": 2})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn get_status_is_status_only() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    assert_eq!(
        json!("active"),
        f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await
    );
    f.end(&gw).await;
    assert_eq!(
        json!("ended"),
        f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await
    );
    assert!(f
        .h
        .ok("get_status", json!({"giveaway_id": "missing"}))
        .await
        .is_null());
}

// -- audit races ---------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_racing_blacklist() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    let err = f
        .join_error_with(&gw, USER, json!({"snapshot": [false, 0, 0, false]}))
        .await;
    assert!(err.message.contains("blocked"), "{}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_racing_deadline() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.end(&gw).await;
    // `gw` is the stale pre-end copy the button handler still holds. Every
    // pre-check passes; only the INSERT guard can refuse it.
    assert_eq!(
        "This giveaway has ended.",
        f.join_error(&gw, OTHER).await.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_penalty_race_and_consumes() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "apply_timeout_penalty",
        json!({
            "guild_id": f.guild,
            "user_id": USER,
            "giveaways": 3,
            // The penalty was already spent on another giveaway, so the join
            // below must consume a unit here (3 -> 2) instead of being a no-op.
            "giveaway_id": "some-other-giveaway",
        }),
    )
    .await;
    let error = f
        .join_error_with(&gw, USER, json!({"snapshot": [false, 0, 0, false]}))
        .await;

    assert_eq!("timeout_ban", error.kind);
    assert!(
        error.message.contains("2 giveaway(s) left"),
        "{}",
        error.message
    );
    assert_eq!(2, remaining(&f, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_ban_hit_race() {
    let f = Fixture::new().await;
    let trigger = f.gw(json!({})).await;
    f.timed_join_error(&trigger, USER).await; // starts a 3-giveaway penalty
    for _ in 0..3 {
        let gw = f.gw(json!({})).await;
        f.timed_join_error(&gw, USER).await;
    }
    assert_eq!(0, remaining(&f, USER).await);

    let error = f
        .join_error_with(&trigger, USER, json!({"snapshot": [false, 0, 0, false]}))
        .await;
    assert_eq!("timeout_ban", error.kind);
    assert!(
        error.message.contains("sat out this giveaway"),
        "{}",
        error.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_ban_snapshot_does_not_falsely_penalize() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    // Snapshot claims a 3-giveaway penalty that has already been lifted.
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"snapshot": [false, 3, 0, false]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_hit_snapshot_does_not_falsely_refuse() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"snapshot": [false, 0, 0, true]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_message_snapshot_live_confirm_allows() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"min_messages": 5})).await;
    f.h.ok(
        "add_message_counts",
        json!({"rows": count_rows(&[(&f.guild, USER, 6)])}),
    )
    .await;
    // The snapshot saw 1; a re-read before refusing sees the flush.
    assert_eq!(
        1,
        f.enter_with(&gw, USER, json!({"snapshot": [false, 0, 1, false]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_ended_never_penalizes_timed_out() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.end(&gw).await;
    assert_eq!(
        "This giveaway has ended.",
        f.timed_join_error(&gw, USER).await.message
    );
    assert_eq!(0, remaining(&f, USER).await);
}

// -- timed bans ----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn default_ban_add_is_permanent() {
    let f = Fixture::new().await;
    let info =
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": USER}),
        )
        .await;
    assert_eq!(json!(true), info["permanent"]);
    assert!(info["expires_at"].is_null());
    let message =
        f.h.ok("ban_message", json!({"guild_id": f.guild, "user_id": USER}))
            .await;
    assert!(message.as_str().unwrap().contains("This ban is permanent."));
}

#[tokio::test(flavor = "multi_thread")]
async fn timed_ban_blocks_then_expires() {
    let f = Fixture::new().await;
    let base = f.h.now();
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER, "hours": 1, "now": base}),
    )
    .await;
    assert_eq!(
        json!(true),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );

    f.h.advance(3600 * 1000 + 1);

    // Created after the jump so the giveaway itself is still running.
    let gw = f.gw(json!({})).await;
    assert_eq!(
        json!(false),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(1, f.enter(&gw, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn ban_hours_validation() {
    let f = Fixture::new().await;
    for hours in [-1, 721] {
        let err =
            f.h.refuse(
                "blacklist_add",
                json!({"guild_id": f.guild, "user_id": USER, "hours": hours}),
            )
            .await;
        assert_eq!(
            "Ban duration must be 0-720 hours (0 = permanent).",
            err.message
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn reban_overwrites_expiry_and_reason() {
    let f = Fixture::new().await;
    let first =
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": USER, "hours": 1, "reason": "first"}),
        )
        .await;
    let second =
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": USER, "hours": 2, "reason": "second"}),
        )
        .await;

    assert_eq!(json!("second"), second["reason"]);
    assert!(second["expires_at"].as_i64().unwrap() > first["expires_at"].as_i64().unwrap());
    assert_eq!(
        json!(1),
        f.h.ok("count_blacklisted", json!({"guild_id": f.guild}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn ban_reason_is_trimmed_to_256() {
    let f = Fixture::new().await;
    let reason = format!("  {}  ", "x".repeat(300));
    let info =
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": USER, "reason": reason, "hours": 0}),
        )
        .await;
    let stored = info["reason"].as_str().unwrap();
    assert_eq!(256, stored.chars().count());
    assert!(!stored.starts_with(' '));
}

#[tokio::test(flavor = "multi_thread")]
async fn ban_message_shows_reason_and_expiry() {
    let f = Fixture::new().await;
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild,
            "user_id": USER,
            "hours": 1,
            "reason": "spam",
            "banned_by": STAFF,
        }),
    )
    .await;
    let message =
        f.h.ok("ban_message", json!({"guild_id": f.guild, "user_id": USER}))
            .await;
    let message = message.as_str().unwrap();

    assert!(message.contains("blocked"), "{message}");
    assert!(message.contains("Reason: spam"), "{message}");
    assert!(message.contains("Ban expires <t:"), "{message}");
    assert!(!message.contains("permanent"), "{message}");
}

#[tokio::test(flavor = "multi_thread")]
async fn timed_ban_purges_running_entries() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER, "hours": 1}),
    )
    .await;

    assert_eq!(0, f.entry_rows(&id_of(&gw)).await);
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn count_and_list_hide_expired_bans() {
    let f = Fixture::new().await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": "a", "hours": 0}),
    )
    .await;
    for user_id in ["b", "c"] {
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": user_id, "hours": 1}),
        )
        .await;
    }

    f.h.advance(3600 * 1000 + 1);

    assert_eq!(
        json!(1),
        f.h.ok("count_blacklisted", json!({"guild_id": f.guild}))
            .await
    );
    assert_eq!(
        json!(["a"]),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild, "limit": 50}))
            .await
    );
    let detailed =
        f.h.ok(
            "blacklist_list_detailed",
            json!({"guild_id": f.guild, "limit": 50}),
        )
        .await;
    assert_eq!(vec!["a".to_string()], user_ids(&detailed));
}

#[tokio::test(flavor = "multi_thread")]
async fn purge_expired_bans_removes_only_expired() {
    let f = Fixture::new().await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": "a", "hours": 0}),
    )
    .await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": "b", "hours": 1}),
    )
    .await;
    f.h.advance(3600 * 1000 + 1);

    assert_eq!(json!(1), f.h.ok("purge_expired_bans", Value::Null).await);
    assert_eq!(
        json!(["a"]),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild, "limit": 50}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_is_expiry_aware() {
    let f = Fixture::new().await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER, "hours": 1}),
    )
    .await;
    let banned =
        f.h.ok(
            "eligibility_snapshot",
            json!({"guild_id": f.guild, "user_id": USER, "giveaway_id": null}),
        )
        .await;
    assert_eq!(json!(true), banned[0]);

    f.h.advance(3600 * 1000 + 1);

    let banned =
        f.h.ok(
            "eligibility_snapshot",
            json!({"guild_id": f.guild, "user_id": USER, "giveaway_id": null}),
        )
        .await;
    assert_eq!(json!(false), banned[0]);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_racing_timed_ban() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER, "hours": 1}),
    )
    .await;
    let err = f
        .join_error_with(&gw, USER, json!({"snapshot": [false, 0, 0, false]}))
        .await;
    assert!(err.message.contains("blocked"), "{}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn zero_insert_reports_ban_with_reason() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER, "hours": 1, "reason": "spam"}),
    )
    .await;
    let message = f
        .join_error_with(&gw, USER, json!({"snapshot": [false, 0, 0, false]}))
        .await
        .message;
    assert!(message.contains("blocked"), "{message}");
    assert!(message.contains("Reason: spam"), "{message}");
}

// -- due vs extend -------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn stale_due_loses_to_extend() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    let now = f.h.advance(61_000);
    let stale = f.h.ok("due", json!({"now": now})).await;
    assert_eq!(vec![id_of(&gw)], gw_ids(&stale));

    f.h.ok(
        "extend",
        json!({"giveaway_id": gw["id"], "extra_seconds": 3600}),
    )
    .await;

    let err = f
        .end_error_with(&stale[0], json!({"now": now, "require_due": true}))
        .await;
    assert_eq!("This giveaway is not due to end yet.", err.message);
    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("active"), stored["status"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn atomic_flip_rechecks_deadline() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 3600})).await;
    let mut stale = gw.clone();
    // What a very old cached row would claim.
    stale["ends_at"] = json!(0);
    let err = f
        .end_error_with(&stale, json!({"now": f.h.now(), "require_due": true}))
        .await;
    assert_eq!("This giveaway is not due to end yet.", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn tick_end_still_wins_when_still_due() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    f.enter(&gw, USER).await;
    let now = f.h.advance(61_000);

    let (ended_gw, winners) = f
        .end_with(&gw, json!({"now": now, "require_due": true}))
        .await;

    assert_eq!("ended", ended_gw.status);
    assert_eq!(vec![USER.to_string()], winners);
}

#[tokio::test(flavor = "multi_thread")]
async fn manual_end_keeps_early_end() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 3600})).await;
    let (ended_gw, _) = f.end(&gw).await;
    assert_eq!("ended", ended_gw.status);
    assert!(f.h.now() < gw["ends_at"].as_i64().unwrap());
}

// -- join burst ----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn fetch_count_false_skips_count_but_commits() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let result = join(&f.h, &gw, USER, json!({"fetch_count": false}))
        .await
        .expect("join succeeds");
    // -1 means "committed, count unknown": callers must not print it.
    assert_eq!(json!(-1), result);
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn join_snapshot_matches_split_reads() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;

    let pair =
        f.h.ok(
            "join_snapshot",
            json!({"giveaway_id": gw["id"], "guild_id": f.guild, "user_id": USER}),
        )
        .await;
    let (snapshot_gw, snapshot) = (&pair[0], &pair[1]);

    assert_eq!(gw["id"], snapshot_gw["id"]);
    let split =
        f.h.ok(
            "eligibility_snapshot",
            json!({"guild_id": f.guild, "user_id": USER, "giveaway_id": gw["id"]}),
        )
        .await;
    assert_eq!(&split, snapshot);
}

#[tokio::test(flavor = "multi_thread")]
async fn refresh_snapshot_returns_status_and_count() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    assert_eq!(
        json!(["active", 2]),
        f.h.ok("refresh_snapshot", json!({"giveaway_id": gw["id"]}))
            .await
    );

    f.end(&gw).await;
    assert_eq!(
        json!(["ended", 2]),
        f.h.ok("refresh_snapshot", json!({"giveaway_id": gw["id"]}))
            .await
    );
}
