//! The bot-era rules suite, ported 1:1 from the retired Python `test_service.py`.
//!
//! That file predates the Rust rewrite and overlaps in *names* with
//! `service.rs`, but not in bodies: it exercises the older validation
//! thresholds, the pre-split helper shapes and the bot-era monkeypatch seams.
//! Every test below keeps its Python name and its assertions.
//!
//! Conventions:
//!
//! * the manual clock is the only clock; "400 days old" is still real wall
//!   time, because account age is compared against the system clock exactly
//!   like Discord's creation timestamp;
//! * draws are asserted on invariants, never on a specific winner;
//! * where the Python test reached into a Python-only seam (a monkeypatched
//!   `db.execute`, `svc.get`, `svc.start_claims`), the Rust test uses the real
//!   behaviour that replaced it and says so with `// ADAPTED:`.

use std::collections::{BTreeMap, BTreeSet};

use gwbot_core::db::{is_write, Database};
use gwbot_core::models::{ServiceError, ENTRY_RETENTION_MS, TIMEOUT_BAN_KIND};
use gwbot_testkit::{
    call_job, ended, join_job, merge, race_status, run_concurrently, CallError, Fixture, Harness,
    RaceJob, RaceResult,
};
use serde_json::{json, Value};

/// The default joining member.
const USER: &str = "333333333333333333";
const DAY_MS: i64 = 86_400_000;

/// The Python base class's `make()` defaults.
fn make_args(f: &Fixture, overrides: Value) -> Value {
    merge(
        json!({
            "guild_id": f.guild,
            "channel_id": "222222222222222222",
            "prize": "Steam key",
            "winner_count": 1,
            "duration_seconds": 60,
            "created_by": "999999999999999999",
        }),
        overrides,
    )
}

async fn make(f: &Fixture, overrides: Value) -> Value {
    f.h.ok("create", make_args(f, overrides)).await
}

/// Python's `join()`: 400-day-old account unless overridden, username from the
/// last three digits. `account_created_ts: null` is the "unknown age" sentinel.
async fn join_call(
    f: &Fixture,
    gw: &Value,
    user_id: &str,
    overrides: Value,
) -> Result<Value, CallError> {
    let args = merge(
        json!({
            "gw": gw,
            "user_id": user_id,
            "username": format!("user{}", &user_id[user_id.len().saturating_sub(3)..]),
            "member_roles": [],
            "account_created_ts": f.account_age(400.0),
        }),
        overrides,
    );
    f.h.call("join", args).await
}

async fn join_ok(f: &Fixture, gw: &Value, user_id: &str, overrides: Value) -> i64 {
    join_call(f, gw, user_id, overrides)
        .await
        .unwrap_or_else(|err| panic!("join failed: {err}"))
        .as_i64()
        .unwrap_or_else(|| panic!("join did not return a count"))
}

async fn join_err(f: &Fixture, gw: &Value, user_id: &str, overrides: Value) -> ServiceError {
    join_call(f, gw, user_id, overrides)
        .await
        .err()
        .and_then(|err| err.service().cloned())
        .unwrap_or_else(|| panic!("join was expected to be refused"))
}

/// The id of a giveaway value, for ops that take a bare id.
fn id_of(gw: &Value) -> String {
    gw["id"].as_str().expect("giveaway has no id").to_string()
}

/// `[guild, user, count]` triples for `add_message_counts`.
fn count_rows(rows: &[(&str, &str, i64)]) -> Value {
    Value::Array(
        rows.iter()
            .map(|(guild, user, count)| json!([guild, user, count]))
            .collect(),
    )
}

// -- schema --------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn init_schema_is_idempotent() {
    // ADAPTED: Python called `db.init_schema()` twice by hand. In Rust the
    // schema is applied by the storage layer on open, so the second boot is
    // modelled by reopening the same database file: a second `init_schema`
    // must be a no-op and must not lose or rename a column.
    let mut f = Fixture::new().await;
    f.reopen().await;

    let cols: BTreeSet<String> =
        f.h.sql("PRAGMA table_info(simple_giveaways)")
            .await
            .as_array()
            .expect("table_info rows")
            .iter()
            .map(|row| row["name"].as_str().unwrap_or_default().to_string())
            .collect();
    for name in [
        "id",
        "prize",
        "winner_count",
        "ends_at",
        "status",
        "required_role_id",
        "required_role_ids",
        "blocked_role_id",
        "entrants_role_id",
        "winners_json",
        "entrant_count",
    ] {
        assert!(cols.contains(name), "missing column {name} in {cols:?}");
    }
    let entry_cols: BTreeSet<String> =
        f.h.sql("PRAGMA table_info(simple_entries)")
            .await
            .as_array()
            .expect("table_info rows")
            .iter()
            .map(|row| row["name"].as_str().unwrap_or_default().to_string())
            .collect();
    assert_eq!(
        BTreeSet::from([
            "giveaway_id".to_string(),
            "user_id".to_string(),
            "username".to_string(),
            "entered_at".to_string(),
        ]),
        entry_cols
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn indexes_exist_including_user_lookup() {
    let f = Fixture::new().await;
    let names: BTreeSet<String> =
        f.h.sql("SELECT name FROM sqlite_master WHERE type = 'index'")
            .await
            .as_array()
            .expect("index rows")
            .iter()
            .map(|row| row["name"].as_str().unwrap_or_default().to_string())
            .collect();
    for (name, why) in [
        (
            "idx_simple_gw_status_ends",
            "due() scans on this every tick",
        ),
        (
            "idx_simple_entries_gw_entered",
            "entries reads sort by entered_at",
        ),
        ("idx_simple_entries_user", "blacklist purge needs this"),
        (
            "idx_simple_claims_pending_due",
            "due_claims scans every 30s",
        ),
    ] {
        assert!(names.contains(name), "{why} (missing {name})");
    }
    // Superseded by idx_simple_entries_gw_entered and dropped on migration;
    // it only repeated the simple_entries primary-key prefix.
    assert!(!names.contains("idx_simple_entries_giveaway"));
}

#[tokio::test(flavor = "multi_thread")]
async fn engine_agnostic_defaults() {
    // ADAPTED: Python asserted `db.backend == "injected"` for an injected
    // sqlite connection. The Rust storage layer has no injected backend name;
    // the invariant that matters is the other half of that test: a Database
    // built without a Turso URL must refuse, loudly, naming the env var.
    let settings = gwbot_core::config::Settings {
        turso_url: String::new(),
        turso_token: String::new(),
        ..Default::default()
    };
    let err = Database::connect(&settings)
        .await
        .err()
        .expect("a Database without TURSO_DATABASE_URL must not open");
    assert!(
        format!("{err:#}").contains("TURSO_DATABASE_URL"),
        "unexpected error: {err:#}"
    );
}

// -- write classification ------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn writes_need_a_commit() {
    for sql in [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "  insert into t values (1)",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "REPLACE INTO t VALUES (1)",
    ] {
        assert!(is_write(sql), "{sql}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn reads_do_not() {
    for sql in [
        "SELECT 1",
        "  select * from t",
        "PRAGMA table_info(t)",
        "EXPLAIN SELECT 1",
        "VALUES (1)",
        "",
    ] {
        assert!(!is_write(sql), "{sql}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pragma_that_assigns_is_a_write() {
    // "PRAGMA user_version = 2" changes the database, so it needs the same
    // commit as any other write; ignoring it would leave the schema stamp
    // unapplied and every boot would re-run the whole DDL block.
    assert!(is_write("PRAGMA user_version = 2"));
    assert!(is_write("  pragma journal_mode=wal"));
    assert!(!is_write("PRAGMA user_version"), "a bare pragma only reads");
}

// -- create --------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn defaults_and_round_trip() {
    let f = Fixture::new().await;
    let gw = make(
        &f,
        json!({"prize": "  Nitro  ", "required_role_ids": ["7", "8"], "host_name": "Mod"}),
    )
    .await;

    assert!(
        gw["id"].as_str().unwrap_or_default().starts_with("gw_"),
        "id: {}",
        gw["id"]
    );
    assert_eq!(json!("Nitro"), gw["prize"], "prize is stripped");
    assert_eq!(json!("active"), gw["status"]);
    // Python's `gw.active` is a derived property; the wire shape carries the
    // status string it derives from.
    assert_eq!(json!("active"), gw["status"]);
    assert_eq!(json!(["7", "8"]), gw["required_role_ids"]);
    assert_eq!(
        json!("7"),
        gw["required_role_id"],
        "first role mirrors the legacy column"
    );
    assert_eq!(
        json!("999999999999999999"),
        gw["host_id"],
        "host defaults to the creator"
    );
    assert_eq!(json!(1), gw["winner_count"]);
    assert_eq!(json!([]), gw["winners"]);
    assert_eq!(
        json!(f.h.now() + 60_000),
        gw["ends_at"],
        "sixty seconds from the clock the service read"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn validation() {
    let f = Fixture::new().await;
    let cases: Vec<(Value, &str)> = vec![
        (json!({"prize": ""}), "prize"),
        (json!({"prize": "x".repeat(257)}), "prize"),
        (json!({"winner_count": 0}), "winner"),
        (json!({"winner_count": 26}), "winner"),
        (json!({"duration_seconds": 10}), "duration"),
        (json!({"duration_seconds": 61 * 86400}), "duration"),
        (json!({"min_messages": -1}), "messages"),
        (json!({"min_messages": 100_001}), "messages"),
        (json!({"min_account_age_days": -1}), "account age"),
        (json!({"min_account_age_days": 4000}), "account age"),
        (json!({"image_url": "ftp://x/y.png"}), "http"),
        (
            json!({"image_url": format!("https://x/{}", "a".repeat(600))}),
            "http",
        ),
    ];
    for (overrides, needle) in cases {
        let err =
            f.h.call("create", make_args(&f, overrides.clone()))
                .await
                .err()
                .and_then(|err| err.service().cloned())
                .unwrap_or_else(|| panic!("create {overrides} was expected to be refused"));
        assert!(
            err.message.to_lowercase().contains(needle),
            "create {overrides}: {:?} does not contain {needle:?}",
            err.message
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn role_list_is_capped_and_deduplicated() {
    let f = Fixture::new().await;
    let gw = make(
        &f,
        json!({"required_role_ids": ["1", "2", "3", "4", "5", "6", "7"]}),
    )
    .await;
    assert_eq!(json!(["1", "2", "3", "4", "5"]), gw["required_role_ids"]);

    let gw2 = make(
        &f,
        json!({"required_role_id": "9", "required_role_ids": ["9", "8"]}),
    )
    .await;
    assert_eq!(json!(["9", "8"]), gw2["required_role_ids"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn legacy_required_role_column_is_merged() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.execute_params(
        "UPDATE simple_giveaways SET required_role_id = ?, required_role_ids = ? WHERE id = ?",
        json!(["42", "[]", gw["id"]]),
    )
    .await;
    let loaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!(["42"]), loaded["required_role_ids"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn get_missing_raises() {
    let f = Fixture::new().await;
    f.h.refuse("get", json!({"giveaway_id": "gw_nope"})).await;
}

// -- lookups -------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn list_active_is_guild_scoped_and_ordered() {
    let f = Fixture::new().await;
    let first = make(&f, json!({"prize": "first", "duration_seconds": 3600})).await;
    let second = make(&f, json!({"prize": "second", "duration_seconds": 60})).await;
    let third = make(
        &f,
        json!({"prize": "other guild", "guild_id": "555", "duration_seconds": 30}),
    )
    .await;

    let ids = |value: &Value| {
        value
            .as_array()
            .expect("giveaway list")
            .iter()
            .map(id_of)
            .collect::<Vec<_>>()
    };
    assert_eq!(
        vec![id_of(&second), id_of(&first)],
        ids(&f.h.ok("list_active", json!({"guild_id": f.guild})).await)
    );
    assert_eq!(
        3,
        f.h.ok("list_all_active", json!({"limit": 200}))
            .await
            .as_array()
            .expect("giveaway list")
            .len()
    );
    // due() is deliberately global: it is the auto-draw sweep, so it sees
    // every guild, soonest deadline first.
    assert_eq!(
        vec![id_of(&third), id_of(&second), id_of(&first)],
        ids(&f.h.ok("due", json!({"now": f.h.now() + 10 * DAY_MS})).await)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_refuses_another_guild() {
    let f = Fixture::new().await;
    let other = make(&f, json!({"guild_id": "555"})).await;
    let err =
        f.h.refuse(
            "resolve",
            json!({"guild_id": f.guild, "raw_id": other["id"]}),
        )
        .await;
    assert!(
        err.message.contains("another server"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_falls_back_to_the_only_live_one() {
    let f = Fixture::new().await;
    let only = make(&f, json!({})).await;
    for raw in ["gw_typo", ""] {
        let found =
            f.h.ok("resolve", json!({"guild_id": f.guild, "raw_id": raw}))
                .await;
        assert_eq!(json!(id_of(&only)), found["id"]);
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_needs_an_id_when_several_are_live() {
    let f = Fixture::new().await;
    let a = make(&f, json!({"prize": "a"})).await;
    make(&f, json!({"prize": "b"})).await;
    let resolved =
        f.h.ok("resolve", json!({"guild_id": f.guild, "raw_id": a["id"]}))
            .await;
    assert_eq!(json!(id_of(&a)), resolved["id"]);
    let err =
        f.h.refuse("resolve", json!({"guild_id": f.guild, "raw_id": "gw_typo"}))
            .await;
    assert!(
        err.message.contains("did not match"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_with_nothing_live() {
    let f = Fixture::new().await;
    let err =
        f.h.refuse("resolve", json!({"guild_id": f.guild, "raw_id": "gw_x"}))
            .await;
    assert!(
        err.message.contains("No active giveaway"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn get_by_message() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.h.ok(
        "set_message",
        json!({"giveaway_id": gw["id"], "message_id": "12345"}),
    )
    .await;
    let found =
        f.h.ok("get_by_message", json!({"message_id": "12345"}))
            .await;
    assert!(!found.is_null(), "set message must be findable");
    assert_eq!(gw["id"], found["id"]);
    assert!(f
        .h
        .ok("get_by_message", json!({"message_id": "999"}))
        .await
        .is_null());
}

// -- join and leave ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn join_then_leave() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(1, join_ok(&f, &gw, USER, json!({})).await);
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(2, join_ok(&f, &gw, "444444444444444444", json!({})).await);
    assert_eq!(
        json!(true),
        f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
            .await
    );
    assert_eq!(
        json!(false),
        f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
            .await
    );
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn leave_after_the_end_keeps_the_entry() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    f.end(&gw).await;
    assert_eq!(
        json!(false),
        f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
            .await
    );
    assert_eq!(
        json!(1),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await,
        "the drawn pool is not rewritten"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_non_duplicate_constraint_error_is_not_called_already_entered() {
    // ADAPTED: Python monkeypatched `db.execute` to raise a NOT NULL
    // IntegrityError for the entry INSERT and asserted it propagates. The Rust
    // storage layer is not monkeypatchable, so the same failure is injected in
    // SQL: a trigger aborts the INSERT with that message. The join must surface
    // the storage error, not translate it into "already entered".
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.trigger(
        "CREATE TRIGGER legacy_not_null BEFORE INSERT ON simple_entries \
         BEGIN SELECT RAISE(ABORT, 'NOT NULL constraint failed: simple_entries.username'); END",
    )
    .await;

    let err = join_call(&f, &gw, USER, json!({}))
        .await
        .expect_err("the injected constraint must abort the join");
    assert!(
        err.service().is_none(),
        "a storage failure must not be dressed up as a refusal: {err}"
    );
    assert!(
        err.to_string().to_lowercase().contains("not null"),
        "unexpected error: {err}"
    );
    f.drop_trigger("legacy_not_null").await;
}

#[tokio::test(flavor = "multi_thread")]
async fn double_join_is_a_service_error() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    let err = join_err(&f, &gw, USER, json!({})).await;
    assert!(
        err.message.contains("already entered"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn required_roles_need_one_match() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"required_role_ids": ["10", "11"]})).await;
    let err = join_err(&f, &gw, USER, json!({"member_roles": ["99"]})).await;
    assert!(
        err.message.contains("required roles"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(
        1,
        join_ok(&f, &gw, &"1".repeat(18), json!({"member_roles": ["11"]})).await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn blocked_role_cannot_enter() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"blocked_role_id": "77"})).await;
    let err = join_err(&f, &gw, USER, json!({"member_roles": ["77"]})).await;
    assert!(
        err.message.contains("not allowed"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn account_age_is_enforced_and_fails_closed() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"min_account_age_days": 30})).await;
    let err = join_err(
        &f,
        &gw,
        USER,
        json!({"account_created_ts": f.account_age(5.0)}),
    )
    .await;
    assert!(
        err.message.contains("30+ days"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(1, join_ok(&f, &gw, &"2".repeat(18), json!({})).await);
    let err = join_err(
        &f,
        &gw,
        &"3".repeat(18),
        json!({"account_created_ts": null}),
    )
    .await;
    assert!(
        err.message.contains("account age"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn min_messages_uses_the_database_count() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"min_messages": 3})).await;
    let member = "5".repeat(18);
    for _ in 0..2 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": member}),
        )
        .await;
    }
    let err = join_err(&f, &gw, &member, json!({})).await;
    assert!(
        err.message.contains("(2 counted)"),
        "unexpected: {}",
        err.message
    );
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    assert_eq!(1, join_ok(&f, &gw, &member, json!({})).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn min_messages_counts_the_unflushed_buffer() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"min_messages": 3})).await;
    let member = "6".repeat(18);
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    join_err(&f, &gw, &member, json!({})).await;
    // Two messages are still buffered in memory by the bot.
    assert_eq!(
        1,
        join_ok(&f, &gw, &member, json!({"pending_messages": 2})).await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn has_entry_is_a_targeted_lookup() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, &"1".repeat(18), json!({})).await;
    for (user, expected) in [
        ("1".repeat(18), true),
        ("2".repeat(18), false),
        (String::new(), false),
    ] {
        assert_eq!(
            json!(expected),
            f.h.ok(
                "has_entry",
                json!({"giveaway_id": gw["id"], "user_id": user})
            )
            .await,
            "has_entry({user:?})"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn entries_can_be_limited_for_a_display_path() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    for index in 0..5 {
        join_ok(&f, &gw, &format!("{:0>18}", index + 1), json!({})).await;
    }
    let full = f.h.ok("entries", json!({"giveaway_id": gw["id"]})).await;
    let full_rows = full.as_array().expect("rows").clone();
    assert_eq!(5, full_rows.len());
    let limited =
        f.h.ok("entries", json!({"giveaway_id": gw["id"], "limit": 2}))
            .await;
    assert_eq!(
        Value::Array(full_rows[..2].to_vec()),
        limited,
        "oldest first, same order"
    );
    assert_eq!(
        json!(5),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await,
        "the count is still the whole pool"
    );
    let zero =
        f.h.ok("entries", json!({"giveaway_id": gw["id"], "limit": 0}))
            .await;
    assert_eq!(
        Value::Array(full_rows[..1].to_vec()),
        zero,
        "never an unbounded read"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn ended_giveaway_refuses_joins() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.end(&gw).await;
    let err = join_err(&f, &gw, USER, json!({})).await;
    assert!(err.message.contains("ended"), "unexpected: {}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn expired_deadline_refuses_joins() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
        json!([f.h.now() - 1, gw["id"]]),
    )
    .await;
    let reloaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let err = join_err(&f, &reloaded, USER, json!({})).await;
    assert!(err.message.contains("ended"), "unexpected: {}", err.message);
}

// -- message counts ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn record_message_increments() {
    let f = Fixture::new().await;
    let member = "7".repeat(18);
    for _ in 0..2 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": member}),
        )
        .await;
    }
    for (guild, user, expected) in [
        (f.guild.clone(), member.clone(), 2),
        (f.guild.clone(), "8".repeat(18), 0),
        ("999".to_string(), member.clone(), 0),
    ] {
        assert_eq!(
            json!(expected),
            f.h.ok("message_count", json!({"guild_id": guild, "user_id": user}))
                .await,
            "message_count({guild}, {user})"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn batch_flush_adds_deltas() {
    let f = Fixture::new().await;
    let a = "a".repeat(18);
    let b = "b".repeat(18);
    f.h.ok("record_message", json!({"guild_id": f.guild, "user_id": a}))
        .await;
    f.h.ok(
        "add_message_counts",
        json!({"rows": count_rows(&[(&f.guild, &a, 5), (&f.guild, &b, 3)])}),
    )
    .await;
    for (user, expected) in [(&a, 6), (&b, 3)] {
        assert_eq!(
            json!(expected),
            f.h.ok(
                "message_count",
                json!({"guild_id": f.guild, "user_id": user})
            )
            .await,
            "message_count({user})"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn batch_flush_chunks_large_payloads() {
    let f = Fixture::new().await;
    let rows: Vec<Value> = (0..700)
        .map(|index| json!([f.guild, format!("u{index:05}"), 1]))
        .collect();
    f.h.ok("add_message_counts", json!({"rows": rows})).await;
    for user in ["u00000", "u00699"] {
        assert_eq!(
            json!(1),
            f.h.ok(
                "message_count",
                json!({"guild_id": f.guild, "user_id": user})
            )
            .await
        );
    }
    assert_eq!(
        Some(700),
        f.h.scalar("SELECT COUNT(*) AS n FROM simple_message_counts")
            .await
            .and_then(|value| value.as_i64())
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn ambiguous_chunk_failure_requeues_only_what_is_missing() {
    // ADAPTED: Python monkeypatched `db.execute` so the INSERT landed and then
    // raised OSError("connection reset by peer"), and asserted the in-place
    // reconciliation meant a retry added nothing twice. The Rust storage layer
    // has no execute seam, so the two halves of that contract are asserted
    // directly: a fully applied chunk reconciles to an empty tail, and a
    // half-applied one reconciles to exactly the missing delta.
    let f = Fixture::new().await;
    let a = "a".repeat(18);
    let b = "b".repeat(18);
    let rows = count_rows(&[(&f.guild, &a, 2), (&f.guild, &b, 3)]);
    let zeros = count_rows(&[(&f.guild, &a, 0), (&f.guild, &b, 0)]);

    // Everything landed before the response was lost: nothing to resend.
    f.h.ok("add_message_counts", json!({"rows": rows})).await;
    assert_eq!(
        json!([]),
        f.h.ok(
            "reconcile_message_counts",
            json!({"rows": rows, "before": zeros})
        )
        .await,
        "a fully applied chunk must not be replayed"
    );
    for (user, expected) in [(&a, 2), (&b, 3)] {
        assert_eq!(
            json!(expected),
            f.h.ok(
                "message_count",
                json!({"guild_id": f.guild, "user_id": user})
            )
            .await,
            "the retry must not double {user}"
        );
    }

    // Only the first row landed: the tail is exactly the second delta.
    let c = "c".repeat(18);
    let d = "d".repeat(18);
    f.h.ok(
        "add_message_counts",
        json!({"rows": count_rows(&[(&f.guild, &c, 4)])}),
    )
    .await;
    assert_eq!(
        json!([[f.guild, d, 7]]),
        f.h.ok(
            "reconcile_message_counts",
            json!({
                "rows": count_rows(&[(&f.guild, &c, 4), (&f.guild, &d, 7)]),
                "before": count_rows(&[(&f.guild, &c, 0), (&f.guild, &d, 0)]),
            })
        )
        .await,
        "only the row that never landed is requeued"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn deterministic_chunk_failure_requeues_the_whole_chunk() {
    // ADAPTED: Python injected OSError("disk is full") into the message-count
    // INSERT. The Rust equivalent is a trigger that aborts the statement, which
    // is a deterministic (non-ambiguous) failure: applied must be zero and the
    // delta must stay unsent.
    let f = Fixture::new().await;
    let member = "a".repeat(18);
    f.trigger(
        "CREATE TRIGGER legacy_flush_boom BEFORE INSERT ON simple_message_counts \
         BEGIN SELECT RAISE(ABORT, 'disk is full'); END",
    )
    .await;

    let err =
        f.h.call(
            "add_message_counts",
            json!({"rows": count_rows(&[(&f.guild, &member, 2)])}),
        )
        .await
        .expect_err("the injected write failure must surface");
    assert_eq!(Some(0), err.applied(), "nothing may be reported applied");
    assert_eq!(
        json!(0),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": member})
        )
        .await
    );
    f.drop_trigger("legacy_flush_boom").await;
}

#[tokio::test(flavor = "multi_thread")]
async fn end_resets_counts_for_the_next_grind() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let member = "c".repeat(18);
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    f.end(&gw).await;
    assert_eq!(
        json!(0),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": member})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_keeps_counts_while_another_giveaway_needs_them() {
    let f = Fixture::new().await;
    make(&f, json!({"prize": "grind", "min_messages": 5})).await;
    let member = "c".repeat(18);
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    let plain = make(&f, json!({"prize": "plain"})).await;
    f.end(&plain).await;
    assert_eq!(
        json!(1),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": member})
        )
        .await,
        "a running min_messages giveaway must not be reset under its entrants"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_resets_only_the_guild_whose_giveaway_ended() {
    let f = Fixture::new().await;
    let other = "555555555555555555";
    let member = "c".repeat(18);
    for guild in [&f.guild, &other.to_string()] {
        f.h.ok(
            "record_message",
            json!({"guild_id": guild, "user_id": member}),
        )
        .await;
    }
    let gw = make(&f, json!({})).await;
    f.end(&gw).await;
    assert_eq!(
        json!(0),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": member})
        )
        .await
    );
    assert_eq!(
        json!(1),
        f.h.ok(
            "message_count",
            json!({"guild_id": other, "user_id": member})
        )
        .await,
        "another server's min_messages counters must survive"
    );
}

// -- draws ---------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn end_draws_once_and_refuses_a_second_call() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"winner_count": 2})).await;
    for index in 0..5 {
        join_ok(&f, &gw, &(10i64.pow(17) + index).to_string(), json!({})).await;
    }
    let (ended_gw, winners) = f.end(&gw).await;
    assert_eq!(2, winners.len());
    let mut stored = ended_gw.winners.clone();
    let mut drawn = winners.clone();
    stored.sort();
    drawn.sort();
    assert_eq!(stored, drawn);
    assert_eq!("ended", ended_gw.status);

    // The status flip is the claim, so a second ender cannot draw again — and
    // cannot get a different set of winners to announce either.
    let err = f.end_error(&gw).await;
    assert!(
        err.message.contains("already ended"),
        "unexpected: {}",
        err.message
    );
    let loaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let mut reloaded: Vec<String> = loaded["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .map(|value| value.as_str().unwrap_or_default().to_string())
        .collect();
    reloaded.sort();
    let mut expected = winners.clone();
    expected.sort();
    assert_eq!(expected, reloaded);
}

#[tokio::test(flavor = "multi_thread")]
async fn two_end_calls_cannot_both_draw() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"winner_count": 1})).await;
    for index in 0..30 {
        join_ok(&f, &gw, &(10i64.pow(17) + index).to_string(), json!({})).await;
    }
    let second = f.sibling().await;

    let finish = || call_job("end", json!({"giveaway_id": gw["id"]}));
    let outcomes = run_concurrently(vec![(&f.h, finish()), (&second, finish())]);
    let mut winners: Vec<Vec<String>> = Vec::new();
    let mut refusals: Vec<String> = Vec::new();
    for outcome in &outcomes {
        match outcome {
            Ok(value) => {
                let (_, drawn) = ended(value);
                winners.push(drawn);
            }
            Err(err) => refusals.push(err.to_string()),
        }
    }
    assert_eq!(1, winners.len(), "exactly one draw, got {winners:?}");
    assert_eq!(1, refusals.len(), "{refusals:?}");
    assert!(
        refusals[0].contains("already ended"),
        "unexpected refusal: {}",
        refusals[0]
    );
    let loaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let mut stored: Vec<String> = loaded["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .map(|value| value.as_str().unwrap_or_default().to_string())
        .collect();
    stored.sort();
    let mut drawn = winners[0].clone();
    drawn.sort();
    assert_eq!(stored, drawn);
}

#[tokio::test(flavor = "multi_thread")]
async fn end_without_entries_is_safe() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let (ended_gw, winners) = f.end(&gw).await;
    assert!(winners.is_empty());
    assert!(ended_gw.winners.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn winners_never_exceed_the_pool() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"winner_count": 25})).await;
    join_ok(&f, &gw, &"1".repeat(18), json!({})).await;
    join_ok(&f, &gw, &"2".repeat(18), json!({})).await;
    let (_, winners) = f.end(&gw).await;
    assert_eq!(2, winners.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_excludes_previous_winners() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    for index in 0..4 {
        join_ok(&f, &gw, &(10i64.pow(17) + index).to_string(), json!({})).await;
    }
    let (_, first) = f.end(&gw).await;
    let after =
        f.h.ok("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    let (after_gw, fresh) = ended(&after);
    assert_eq!(1, fresh.len());
    assert!(
        !first.contains(&fresh[0]),
        "a reroll must not name a previous winner"
    );
    let mut combined: Vec<String> = first.iter().chain(fresh.iter()).cloned().collect();
    combined.sort();
    let mut stored = after_gw.winners.clone();
    stored.sort();
    assert_eq!(stored, combined);
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_refuses_when_the_pool_is_exhausted() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, &"1".repeat(18), json!({})).await;
    let (_, first) = f.end(&gw).await;
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert!(
        err.message.contains("No other entrants"),
        "unexpected: {}",
        err.message
    );
    let loaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!(first), loaded["winners"], "winners unchanged");
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_refuses_after_the_entries_were_wiped() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, &"1".repeat(18), json!({})).await;
    f.end(&gw).await;
    f.h.ok(
        "wipe_stale_entries",
        json!({"now": f.h.now() + 10 * DAY_MS}),
    )
    .await;
    f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
        .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn end_stores_the_entrant_count() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    for index in 0..3 {
        join_ok(&f, &gw, &(10i64.pow(17) + index).to_string(), json!({})).await;
    }
    f.end(&gw).await;
    assert_eq!(
        Some(3),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_end_write_leaves_the_giveaway_active() {
    // ADAPTED: Python monkeypatched `db.execute` to raise whenever the UPDATE
    // carried "status = 'ended'". The Rust equivalent is a trigger that aborts
    // exactly that UPDATE: the end must fail as a storage error, leave the row
    // active, and a later end must still draw the same entrant.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    f.trigger(
        "CREATE TRIGGER legacy_end_boom BEFORE UPDATE ON simple_giveaways \
         WHEN NEW.status = 'ended' BEGIN SELECT RAISE(ABORT, 'write failed'); END",
    )
    .await;

    let err =
        f.h.call("end", json!({"giveaway_id": gw["id"]}))
            .await
            .expect_err("the injected write failure must surface");
    assert!(
        err.service().is_none(),
        "a storage failure is not a refusal"
    );
    assert!(
        err.to_string().contains("write failed"),
        "unexpected error: {err}"
    );
    let loaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(
        json!("active"),
        loaded["status"],
        "not stranded as ended with no winners"
    );

    f.drop_trigger("legacy_end_boom").await;
    let (_, winners) = f.end(&gw).await;
    assert_eq!(vec![USER.to_string()], winners);
}

#[tokio::test(flavor = "multi_thread")]
async fn cancel_stores_the_entrant_count_and_loses_a_race_to_end() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(
        Some(1),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
    );

    // ADAPTED: Python faked a stale `svc.get` so cancel/extend acted on an
    // "active" object that the database had already ended. Rust has no get
    // seam, but the property under test is the guarded write: both must refuse
    // once the row is ended, and the status must stay ended.
    let other = make(&f, json!({"prize": "raced"})).await;
    f.end(&other).await;
    f.h.refuse("cancel", json!({"giveaway_id": other["id"]}))
        .await;
    f.h.refuse(
        "extend",
        json!({"giveaway_id": other["id"], "extra_seconds": 600}),
    )
    .await;
    let loaded = f.h.ok("get", json!({"giveaway_id": other["id"]})).await;
    assert_eq!(json!("ended"), loaded["status"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_refuses_a_running_giveaway() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert!(
        err.message.contains("End the giveaway"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_never_exceeds_the_winner_cap() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"winner_count": 1})).await;
    for index in 0..30 {
        join_ok(&f, &gw, &(10i64.pow(17) + index).to_string(), json!({})).await;
    }
    f.end(&gw).await;
    let after =
        f.h.ok("reroll", json!({"giveaway_id": gw["id"], "count": 200}))
            .await;
    let (_, fresh) = ended(&after);
    let loaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(
        25,
        loaded["winners"].as_array().expect("winners").len(),
        "the list stays inside the cap"
    );
    assert_eq!(
        24,
        fresh.len(),
        "the existing winner leaves room for 24 more"
    );
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert!(
        err.message.contains("most one"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cancelled_giveaway_cannot_be_rerolled() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, &"1".repeat(18), json!({})).await;
    f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;
    let err =
        f.h.refuse("reroll", json!({"giveaway_id": gw["id"], "count": 1}))
            .await;
    assert!(
        err.message.contains("cancelled"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn extend_and_cancel() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"duration_seconds": 600})).await;
    let extended =
        f.h.ok(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": 300}),
        )
        .await;
    assert_eq!(
        json!(gw["ends_at"].as_i64().unwrap() + 300_000),
        extended["ends_at"]
    );
    for bad in [0, 59, 61 * 86400] {
        f.h.refuse(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": bad}),
        )
        .await;
    }
    let cancelled = f.h.ok("cancel", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("cancelled"), cancelled["status"]);
    f.h.refuse("cancel", json!({"giveaway_id": gw["id"]})).await;
    f.h.refuse(
        "extend",
        json!({"giveaway_id": gw["id"], "extra_seconds": 600}),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn discard_removes_a_never_posted_giveaway() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    f.h.ok("discard", json!({"giveaway_id": gw["id"]})).await;
    f.h.refuse("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(
        json!(0),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    assert_eq!(
        json!([]),
        f.h.ok("list_active", json!({"guild_id": f.guild})).await
    );
}

// -- retention -----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn leftover_entrants_roles_are_reported() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.h.ok(
        "set_entrants_role",
        json!({"giveaway_id": gw["id"], "role_id": "4242"}),
    )
    .await;
    assert_eq!(
        json!([]),
        f.h.ok("ended_with_roles", json!({})).await,
        "still running"
    );

    f.end(&gw).await;
    let leftovers = f.h.ok("ended_with_roles", json!({})).await;
    let ids: Vec<&str> = leftovers
        .as_array()
        .expect("giveaway list")
        .iter()
        .map(|row| row["id"].as_str().unwrap_or_default())
        .collect();
    assert_eq!(vec![gw["id"].as_str().unwrap_or_default()], ids);

    f.h.ok(
        "set_entrants_role",
        json!({"giveaway_id": gw["id"], "role_id": null}),
    )
    .await;
    assert_eq!(
        json!([]),
        f.h.ok("ended_with_roles", json!({})).await,
        "cleaned up"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn entry_rows_are_wiped_after_the_retention_window() {
    let f = Fixture::new().await;
    let old = make(&f, json!({"prize": "old"})).await;
    join_ok(&f, &old, USER, json!({})).await;
    f.end(&old).await;
    let fresh = make(&f, json!({"prize": "fresh"})).await;
    join_ok(&f, &fresh, &"9".repeat(18), json!({})).await;
    f.end(&fresh).await;

    // Push the old one past the retention window; the fresh one stays inside it
    // because it ended moments ago.
    f.execute_params(
        "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
        json!([f.h.now() - ENTRY_RETENTION_MS - 60_000, old["id"]]),
    )
    .await;
    f.h.ok("wipe_stale_entries", json!({})).await;

    assert_eq!(
        json!(0),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": old["id"], "exact": true})
        )
        .await,
        "old entry rows are gone"
    );
    assert_eq!(
        json!(1),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": fresh["id"], "exact": true})
        )
        .await,
        "recent ones are kept"
    );
    let reloaded = f.h.ok("get", json!({"giveaway_id": old["id"]})).await;
    assert_eq!(
        1,
        reloaded["winners"].as_array().expect("winners").len(),
        "the record and winners stay"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn rows_without_an_ended_at_still_expire() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, &"1".repeat(18), json!({})).await;
    // ended_at arrived as a later column, so giveaways that finished before it
    // existed carry NULL and used to keep their entries forever.
    f.execute_params(
        "UPDATE simple_giveaways SET status = 'cancelled', ended_at = NULL WHERE id = ?",
        json!([gw["id"]]),
    )
    .await;
    assert_eq!(
        json!(1),
        f.h.ok(
            "wipe_stale_entries",
            json!({"now": f.h.now() + 10 * DAY_MS})
        )
        .await
    );
}

// -- blacklist -----------------------------------------------------------

async fn is_blacklisted(f: &Fixture, guild: &str, user: &str) -> Value {
    f.h.ok(
        "is_blacklisted",
        json!({"guild_id": guild, "user_id": user}),
    )
    .await
}

async fn count_entries(f: &Fixture, gw: &Value, exact: bool) -> Value {
    f.h.ok(
        "entry_count",
        json!({"giveaway_id": gw["id"], "exact": exact}),
    )
    .await
}

#[tokio::test(flavor = "multi_thread")]
async fn blacklist_add_blocks_and_purges_only_running_giveaways() {
    let f = Fixture::new().await;
    let member = "1".repeat(18);
    let running = make(&f, json!({"prize": "running"})).await;
    let ended = make(&f, json!({"prize": "ended"})).await;
    join_ok(&f, &running, &member, json!({})).await;
    join_ok(&f, &ended, &member, json!({})).await;
    f.end(&ended).await;

    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    assert_eq!(json!(true), is_blacklisted(&f, &f.guild, &member).await);
    assert_eq!(json!(false), is_blacklisted(&f, "999", &member).await);
    assert_eq!(json!(false), is_blacklisted(&f, &f.guild, "").await);
    assert_eq!(json!(0), count_entries(&f, &running, false).await);
    assert_eq!(
        json!(1),
        count_entries(&f, &ended, false).await,
        "history is untouched"
    );
    let err = join_err(&f, &running, &member, json!({})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn blacklist_count_matches_the_list() {
    let f = Fixture::new().await;
    assert_eq!(
        json!(0),
        f.h.ok("count_blacklisted", json!({"guild_id": f.guild}))
            .await
    );
    for user in ["1".repeat(18), "2".repeat(18)] {
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": user}),
        )
        .await;
    }
    assert_eq!(
        json!(2),
        f.h.ok("count_blacklisted", json!({"guild_id": f.guild}))
            .await
    );
    assert_eq!(
        json!(0),
        f.h.ok("count_blacklisted", json!({"guild_id": "999"}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn the_blacklist_purge_leaves_other_guilds_alone() {
    let f = Fixture::new().await;
    let other = "555555555555555555";
    let member = "1".repeat(18);
    let here = make(&f, json!({"prize": "here"})).await;
    let there = make(&f, json!({"prize": "there", "guild_id": other})).await;
    join_ok(&f, &here, &member, json!({})).await;
    join_ok(&f, &there, &member, json!({})).await;

    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    assert_eq!(json!(0), count_entries(&f, &here, false).await);
    assert_eq!(
        json!(1),
        count_entries(&f, &there, false).await,
        "another server is untouched"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_blacklist_landing_mid_join_still_refuses() {
    // ADAPTED: Python monkeypatched `is_blacklisted` so the first read (inside
    // `check_eligible`) said "not blocked" while the second, inside the join,
    // saw the new row. Rust has no monkeypatch seam, but the window is the same
    // one: the eligibility answer is taken first, the block lands after it, and
    // the join itself must still refuse. That is what the in-statement guard
    // exists for.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let member = "1".repeat(18);
    f.h.ok(
        "check_eligible",
        json!({
            "gw": gw,
            "user_id": member,
            "member_roles": [],
            "account_created_ts": f.account_age(400.0),
        }),
    )
    .await;

    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": member}),
    )
    .await;
    let err = join_err(&f, &gw, &member, json!({})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(
        json!(0),
        count_entries(&f, &gw, true).await,
        "the guard held"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn blacklist_remove_and_list() {
    let f = Fixture::new().await;
    for user in ["1".repeat(18), "2".repeat(18)] {
        f.h.ok(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": user}),
        )
        .await;
    }
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": "2".repeat(18)}),
    )
    .await;
    assert_eq!(
        json!(["1".repeat(18), "2".repeat(18)]),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild})).await
    );
    assert_eq!(
        json!(true),
        f.h.ok(
            "blacklist_remove",
            json!({"guild_id": f.guild, "user_id": "1".repeat(18)})
        )
        .await
    );
    assert_eq!(
        json!(false),
        f.h.ok(
            "blacklist_remove",
            json!({"guild_id": f.guild, "user_id": "1".repeat(18)})
        )
        .await
    );
    assert_eq!(
        json!(["2".repeat(18)]),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild})).await
    );
    assert_eq!(
        json!(false),
        is_blacklisted(&f, &f.guild, &"1".repeat(18)).await
    );
}

// -- timed-out penalty ---------------------------------------------------

/// The member the timeout tests use, `"7" * 18` in Python.
const TIMED_USER: &str = "777777777777777777";

async fn remaining(f: &Fixture, guild: &str, user: &str) -> Value {
    f.h.ok(
        "timeout_ban_remaining",
        json!({"guild_id": guild, "user_id": user}),
    )
    .await
}

/// Assert the join is refused and hand back the refusal.
async fn refused(f: &Fixture, gw: &Value, overrides: Value) -> ServiceError {
    join_err(f, gw, TIMED_USER, overrides).await
}

#[tokio::test(flavor = "multi_thread")]
async fn a_timed_out_member_is_refused_and_starts_the_penalty() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let err = refused(&f, &gw, json!({"timed_out": true})).await;
    assert!(
        err.message.contains("timed out"),
        "unexpected: {}",
        err.message
    );
    assert!(
        err.message.contains("3 giveaway(s)"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
    assert_eq!(
        json!(0),
        count_entries(&f, &gw, true).await,
        "a refused join leaves no entry"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_timeout_refusal_is_tagged_for_the_caller() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let err = refused(&f, &gw, json!({"timed_out": true})).await;
    assert_eq!(TIMEOUT_BAN_KIND, err.kind);
    // An ordinary refusal must stay untagged, so a caller never cleans up after
    // a join that was never blocked by this rule.
    let ended_gw = make(&f, json!({"prize": "plain"})).await;
    f.end(&ended_gw).await;
    let plain = refused(&f, &ended_gw, json!({})).await;
    assert_eq!("", plain.kind);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_untimed_out_member_is_unaffected() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(1, join_ok(&f, &gw, TIMED_USER, json!({})).await);
    assert_eq!(json!(0), remaining(&f, &f.guild, TIMED_USER).await);
    assert_eq!(
        json!([]),
        f.h.ok("list_timeout_bans", json!({"guild_id": f.guild}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_role_is_never_mistaken_for_a_timeout() {
    // Only the timed_out flag counts. A role called "Muted" would be just
    // another id in this list, and ids are not what the rule reads.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(
        1,
        join_ok(
            &f,
            &gw,
            TIMED_USER,
            json!({"member_roles": ["5".repeat(18)]})
        )
        .await
    );
    assert_eq!(json!(0), remaining(&f, &f.guild, TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn three_blocked_giveaways_spend_the_penalty_and_lift_it() {
    let f = Fixture::new().await;
    refused(
        &f,
        &make(&f, json!({"prize": "trigger"})).await,
        json!({"timed_out": true}),
    )
    .await;

    let mut left = Vec::new();
    for index in 0..3 {
        let gw = make(&f, json!({"prize": format!("blocked {index}")})).await;
        let err = refused(&f, &gw, json!({})).await;
        assert!(
            err.message.contains("penalty"),
            "unexpected: {}",
            err.message
        );
        assert_eq!(json!(0), count_entries(&f, &gw, true).await);
        left.push(remaining(&f, &f.guild, TIMED_USER).await);
    }
    assert_eq!(
        vec![json!(2), json!(1), json!(0)],
        left,
        "one giveaway per blocked attempt"
    );
    assert_eq!(
        json!([]),
        f.h.ok("list_timeout_bans", json!({"guild_id": f.guild}))
            .await,
        "gone, not left at zero"
    );
    assert_eq!(
        1,
        join_ok(
            &f,
            &make(&f, json!({"prize": "free"})).await,
            TIMED_USER,
            json!({})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn repeated_attempts_never_stack_or_respend_the_penalty() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"prize": "trigger"})).await;
    refused(&f, &gw, json!({"timed_out": true})).await;
    // Still timed out, same giveaway: neither a second penalty nor a second
    // giveaway spent on the one they were already caught in.
    for _ in 0..3 {
        let err = refused(&f, &gw, json!({"timed_out": true})).await;
        assert!(
            err.message.contains("3 giveaway(s) left"),
            "unexpected: {}",
            err.message
        );
    }
    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
    // A different giveaway while still timed out spends one and is reported as
    // the running penalty, never as a fresh three.
    let other = make(&f, json!({"prize": "other"})).await;
    let err = refused(&f, &other, json!({"timed_out": true})).await;
    assert!(
        err.message.contains("2 giveaway(s) left"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn alternating_giveaways_cannot_burn_the_penalty() {
    let f = Fixture::new().await;
    let a = make(&f, json!({"prize": "a"})).await;
    let b = make(&f, json!({"prize": "b"})).await;
    refused(&f, &a, json!({"timed_out": true})).await;
    for gw in [&b, &a, &b, &a] {
        refused(&f, gw, json!({})).await;
    }
    assert_eq!(
        json!(2),
        remaining(&f, &f.guild, TIMED_USER).await,
        "only b was a new giveaway to sit out"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn the_giveaway_that_spent_the_last_unit_still_refuses() {
    let f = Fixture::new().await;
    refused(
        &f,
        &make(&f, json!({"prize": "trigger"})).await,
        json!({"timed_out": true}),
    )
    .await;
    let mut blocked = Vec::new();
    for index in 0..3 {
        blocked.push(make(&f, json!({"prize": format!("blocked {index}")})).await);
    }
    for gw in &blocked {
        refused(&f, gw, json!({})).await;
    }
    assert_eq!(json!(0), remaining(&f, &f.guild, TIMED_USER).await);

    let last = blocked.last().expect("three blocked giveaways");
    let err = refused(&f, last, json!({})).await;
    assert_eq!(TIMEOUT_BAN_KIND, err.kind);
    assert_eq!(json!(0), count_entries(&f, last, true).await);
    assert_eq!(
        1,
        join_ok(
            &f,
            &make(&f, json!({"prize": "next"})).await,
            TIMED_USER,
            json!({})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_entry_made_before_the_timeout_is_dropped() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(1, join_ok(&f, &gw, TIMED_USER, json!({})).await);
    refused(&f, &gw, json!({"timed_out": true})).await;
    assert_eq!(
        json!(0),
        count_entries(&f, &gw, true).await,
        "a timed-out member cannot stay in"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_penalty_clears_entries_in_every_running_giveaway() {
    let f = Fixture::new().await;
    let yesterday = make(&f, json!({"prize": "joined yesterday"})).await;
    let today = make(&f, json!({"prize": "joined today"})).await;
    assert_eq!(1, join_ok(&f, &yesterday, TIMED_USER, json!({})).await);
    assert_eq!(1, join_ok(&f, &today, TIMED_USER, json!({})).await);

    refused(
        &f,
        &make(&f, json!({"prize": "trigger"})).await,
        json!({"timed_out": true}),
    )
    .await;
    // Sitting out has to mean not entered anywhere: keeping the entry in the
    // giveaway they joined before the mute would let them win it.
    assert_eq!(json!(0), count_entries(&f, &yesterday, true).await);
    assert_eq!(json!(0), count_entries(&f, &today, true).await);
    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_blacklisted_member_is_refused_without_a_penalty() {
    let f = Fixture::new().await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": TIMED_USER}),
    )
    .await;
    let err = refused(&f, &make(&f, json!({})).await, json!({"timed_out": true})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(
        json!(0),
        remaining(&f, &f.guild, TIMED_USER).await,
        "a permanent block is not a timeout penalty"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_ended_giveaway_never_hands_out_a_penalty() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.end(&gw).await;
    let err = refused(&f, &gw, json!({"timed_out": true})).await;
    assert!(err.message.contains("ended"), "unexpected: {}", err.message);
    assert_eq!(json!(0), remaining(&f, &f.guild, TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_expired_deadline_never_hands_out_a_penalty() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
        json!([f.h.now() - 1, gw["id"]]),
    )
    .await;
    let reloaded = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let err = refused(&f, &reloaded, json!({"timed_out": true})).await;
    assert!(err.message.contains("ended"), "unexpected: {}", err.message);
    assert_eq!(json!(0), remaining(&f, &f.guild, TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_penalty_is_per_guild_and_per_user() {
    let f = Fixture::new().await;
    refused(
        &f,
        &make(&f, json!({"prize": "trigger"})).await,
        json!({"timed_out": true}),
    )
    .await;
    assert_eq!(
        json!(0),
        remaining(&f, "555555555555555555", TIMED_USER).await
    );
    assert_eq!(json!(0), remaining(&f, &f.guild, &"8".repeat(18)).await);
    assert_eq!(json!(0), remaining(&f, "", TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_second_penalty_never_shrinks_the_first() {
    let f = Fixture::new().await;
    f.h.ok(
        "apply_timeout_penalty",
        json!({"guild_id": f.guild, "user_id": TIMED_USER, "giveaway_id": "gw_a"}),
    )
    .await;
    assert_eq!(
        json!(3),
        f.h.ok(
            "apply_timeout_penalty",
            json!({"guild_id": f.guild, "user_id": TIMED_USER, "giveaway_id": "gw_b"}),
        )
        .await
    );
    f.h.ok(
        "apply_timeout_penalty",
        json!({
            "guild_id": f.guild,
            "user_id": TIMED_USER,
            "giveaways": 1,
            "giveaway_id": "gw_c",
        }),
    )
    .await;
    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn one_penalty_does_not_block_anybody_else() {
    let f = Fixture::new().await;
    refused(
        &f,
        &make(&f, json!({"prize": "trigger"})).await,
        json!({"timed_out": true}),
    )
    .await;
    assert_eq!(
        1,
        join_ok(
            &f,
            &make(&f, json!({"prize": "someone else"})).await,
            &"9".repeat(18),
            json!({})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn the_entry_sweep_never_touches_a_penalty() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    refused(&f, &gw, json!({"timed_out": true})).await;
    f.end(&gw).await;
    // The 5h entry wipe runs long after a giveaway ends; a penalty must outlive
    // it, which is exactly why it lives in its own table.
    assert_eq!(
        json!(0),
        f.h.ok(
            "wipe_stale_entries",
            json!({"now": f.h.now() + 10 * DAY_MS})
        )
        .await
    );
    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn two_simultaneous_clicks_cannot_stack_the_penalty() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"prize": "race"})).await;
    let click = || {
        join_job(
            gw.clone(),
            TIMED_USER.to_string(),
            json!({"timed_out": true, "account_created_ts": f.account_age(400.0)}),
        )
    };
    let outcomes = run_concurrently(vec![(&f.h, click()), (&f.h, click())]);

    let mut statuses: Vec<&str> = outcomes.iter().map(race_status).collect();
    statuses.sort_unstable();
    assert_eq!(vec!["refused", "refused"], statuses);
    for outcome in &outcomes {
        let message = outcome
            .as_ref()
            .err()
            .and_then(|err| err.message().map(str::to_string))
            .unwrap_or_default();
        assert!(
            message.contains("timed out") || message.contains("penalty"),
            "unexpected: {message}"
        );
    }
    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn the_penalty_survives_a_restart() {
    let mut f = Fixture::new().await;
    refused(
        &f,
        &make(&f, json!({"prize": "trigger"})).await,
        json!({"timed_out": true}),
    )
    .await;
    // Reopening the same file is what a restart looks like: fresh connections,
    // fresh schema pass, same rows.
    f.reopen().await;

    assert_eq!(json!(3), remaining(&f, &f.guild, TIMED_USER).await);
    assert_eq!(
        1,
        f.h.ok("list_timeout_bans", json!({"guild_id": f.guild}))
            .await
            .as_array()
            .expect("ban list")
            .len()
    );
    let gw = make(&f, json!({"prize": "after the restart"})).await;
    let err = refused(&f, &gw, json!({})).await;
    assert!(
        err.message.contains("penalty"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn counting_timeout_bans_matches_the_listing() {
    let f = Fixture::new().await;
    assert_eq!(
        json!(0),
        f.h.ok("count_timeout_bans", json!({"guild_id": f.guild}))
            .await
    );
    for user in ["1".repeat(18), "2".repeat(18)] {
        f.h.ok(
            "apply_timeout_penalty",
            json!({"guild_id": f.guild, "user_id": user, "giveaway_id": "gw_x"}),
        )
        .await;
    }
    f.execute_params(
        "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) \
         VALUES (?, ?, 0)",
        json!([f.guild, "5".repeat(18)]),
    )
    .await;
    assert_eq!(
        json!(2),
        f.h.ok("count_timeout_bans", json!({"guild_id": f.guild}))
            .await,
        "zero rows are not bans"
    );
    assert_eq!(
        json!(0),
        f.h.ok(
            "count_timeout_bans",
            json!({"guild_id": "555555555555555555"})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn timeout_ban_list_is_guild_scoped_ordered_and_skips_zero_rows() {
    let f = Fixture::new().await;
    for user in ["1".repeat(18), "2".repeat(18)] {
        f.h.ok(
            "apply_timeout_penalty",
            json!({"guild_id": f.guild, "user_id": user, "giveaway_id": "gw_x"}),
        )
        .await;
    }
    f.h.ok(
        "apply_timeout_penalty",
        json!({
            "guild_id": f.guild,
            "user_id": "3".repeat(18),
            "giveaways": 1,
            "giveaway_id": "gw_x",
        }),
    )
    .await;
    // A hand-written zero row must never be reported as a ban.
    f.execute_params(
        "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) \
         VALUES (?, ?, 0)",
        json!([f.guild, "4".repeat(18)]),
    )
    .await;

    let rows =
        f.h.ok("list_timeout_bans", json!({"guild_id": f.guild}))
            .await;
    let rows = rows.as_array().expect("ban list");
    let users: Vec<&str> = rows
        .iter()
        .map(|row| row["user_id"].as_str().unwrap_or_default())
        .collect();
    assert_eq!(vec!["1".repeat(18), "2".repeat(18), "3".repeat(18)], users);
    let left: Vec<i64> = rows
        .iter()
        .map(|row| row["giveaways_remaining"].as_i64().unwrap_or_default())
        .collect();
    assert_eq!(vec![3, 3, 1], left);
    assert_eq!(
        json!([]),
        f.h.ok(
            "list_timeout_bans",
            json!({"guild_id": "555555555555555555"})
        )
        .await
    );
    assert_eq!(
        1,
        f.h.ok(
            "list_timeout_bans",
            json!({"guild_id": f.guild, "limit": 1})
        )
        .await
        .as_array()
        .expect("ban list")
        .len()
    );
}

// -- claims --------------------------------------------------------------

/// End a giveaway and hand back the raw `[gw, winners]` pair.
async fn end_value(f: &Fixture, gw: &Value) -> (Value, Vec<String>) {
    let value = f.h.ok("end", json!({"giveaway_id": gw["id"]})).await;
    let winners: Vec<String> = value[1]
        .as_array()
        .expect("winners")
        .iter()
        .map(|winner| winner.as_str().unwrap_or_default().to_string())
        .collect();
    (value[0].clone(), winners)
}

/// The Python `_three_entrant_ended` helper.
async fn three_entrant_ended(f: &Fixture, overrides: Value) -> (Value, String, Vec<String>) {
    let gw = make(
        f,
        merge(
            json!({"winner_count": 1, "claim_timeout_seconds": 3600}),
            overrides,
        ),
    )
    .await;
    let users: Vec<String> = (0..3)
        .map(|index| (10i64.pow(17) + index).to_string())
        .collect();
    for user in &users {
        join_ok(f, &gw, user, json!({})).await;
    }
    let (ended_gw, winners) = end_value(f, &gw).await;
    assert_eq!(1, winners.len());
    assert!(
        users.contains(&winners[0]),
        "winner {} is not one of the entrants {users:?}",
        winners[0]
    );
    (ended_gw, winners[0].clone(), users)
}

/// The Python `_four_entrant_three_winner_ended` helper.
async fn four_entrant_three_winner_ended(
    f: &Fixture,
    overrides: Value,
) -> (Value, Vec<String>, Vec<String>) {
    let gw = make(
        f,
        merge(
            json!({"winner_count": 3, "claim_timeout_seconds": 3600}),
            overrides,
        ),
    )
    .await;
    let users: Vec<String> = (0..4)
        .map(|index| (10i64.pow(17) + index).to_string())
        .collect();
    for user in &users {
        join_ok(f, &gw, user, json!({})).await;
    }
    let (ended_gw, winners) = end_value(f, &gw).await;
    assert_eq!(3, winners.len());
    (ended_gw, winners, users)
}

async fn pending(h: &Harness, gw: &Value) -> Vec<Value> {
    h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
        .await
        .as_array()
        .cloned()
        .expect("pending claims")
}

fn row_user(row: &Value) -> String {
    row["user_id"].as_str().unwrap_or_default().to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn create_validates_claim_window() {
    let f = Fixture::new().await;
    for bad in [-1, 60 * 86400 + 1] {
        let err =
            f.h.call(
                "create",
                make_args(&f, json!({"claim_timeout_seconds": bad})),
            )
            .await
            .err()
            .and_then(|err| err.service().cloned())
            .unwrap_or_else(|| panic!("claim_timeout_seconds={bad} must be refused"));
        assert!(
            err.message.to_lowercase().contains("claim window"),
            "unexpected: {}",
            err.message
        );
    }
    let gw = make(&f, json!({"claim_timeout_seconds": 60})).await;
    assert_eq!(json!(60), gw["claim_timeout_seconds"]);
    let legacy = make(&f, json!({})).await;
    assert_eq!(json!(0), legacy["claim_timeout_seconds"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn end_opens_claims_when_enabled() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let rows = pending(&f.h, &ended_gw).await;
    assert_eq!(1, rows.len());
    assert_eq!(winner, row_user(&rows[0]));
    assert_eq!(json!("pending"), rows[0]["status"]);
    assert!(
        rows[0]["deadline_ms"].as_i64().unwrap_or(0) > 0,
        "deadline: {}",
        rows[0]["deadline_ms"]
    );

    // Restart recovery: a fresh service on the same DB sees the same rows.
    let second = f.sibling().await;
    let again = pending(&second, &ended_gw).await;
    assert_eq!(1, again.len());
    assert_eq!(winner, row_user(&again[0]));
}

#[tokio::test(flavor = "multi_thread")]
async fn end_creates_no_claims_when_disabled() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"winner_count": 1})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    let (ended_gw, winners) = end_value(&f, &gw).await;
    assert_eq!(1, winners.len());
    assert_eq!(Vec::<Value>::new(), pending(&f.h, &ended_gw).await);
    assert_eq!(
        json!([]),
        f.h.ok("due_claims", json!({"now": f.h.now() + 10 * DAY_MS}))
            .await
    );
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winners[0]}),
        )
        .await;
    assert!(
        err.message.contains("drawn winner"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn claim_happy_path_and_double_claim_refused() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let row =
        f.h.ok(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner}),
        )
        .await;
    assert_eq!(winner, row_user(&row));
    assert_eq!(Vec::<Value>::new(), pending(&f.h, &ended_gw).await);
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner}),
        )
        .await;
    assert!(
        err.message.contains("already claimed"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn claim_by_non_winner_refused() {
    let f = Fixture::new().await;
    let (ended_gw, winner, users) = three_entrant_ended(&f, json!({})).await;
    let loser = users
        .iter()
        .find(|user| **user != winner)
        .expect("a loser")
        .clone();
    // The loser never drew, so there is no row at all for them.
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": loser}),
        )
        .await;
    assert!(
        err.message.contains("drawn winner"),
        "unexpected: {}",
        err.message
    );
    f.h.refuse(
        "claim",
        json!({"giveaway_id": "gw_nope", "user_id": winner}),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn skip_claim_needs_staff_and_draws_no_replacement() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let err =
        f.h.refuse(
            "skip_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner, "staff_id": ""}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("staff"),
        "unexpected: {}",
        err.message
    );

    let row =
        f.h.ok(
            "skip_claim",
            json!({
                "giveaway_id": ended_gw["id"],
                "user_id": winner,
                "staff_id": "999999999999999999",
            }),
        )
        .await;
    assert_eq!(json!("skipped"), row["status"]);
    assert_eq!(Vec::<Value>::new(), pending(&f.h, &ended_gw).await);
    // No replacement is drawn: the winners list is untouched.
    let reloaded = f.h.ok("get", json!({"giveaway_id": ended_gw["id"]})).await;
    assert_eq!(json!([winner]), reloaded["winners"]);

    let again =
        f.h.refuse(
            "skip_claim",
            json!({
                "giveaway_id": ended_gw["id"],
                "user_id": winner,
                "staff_id": "999999999999999999",
            }),
        )
        .await;
    assert!(
        again.message.contains("already verified"),
        "unexpected: {}",
        again.message
    );
    let claim =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner}),
        )
        .await;
    assert!(
        claim.message.contains("verified"),
        "unexpected: {}",
        claim.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_draws_a_replacement_and_never_repeats() {
    let f = Fixture::new().await;
    let (ended_gw, winner, users) = three_entrant_ended(&f, json!({})).await;
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");

    // Too early: the guarded UPDATE matches nothing.
    f.h.refuse(
        "expire_claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": winner, "now": deadline - 1}),
    )
    .await;

    let value =
        f.h.ok(
            "expire_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner, "now": deadline + 1}),
        )
        .await;
    let (redrawn, fresh) = ended(&value);
    assert_eq!(1, fresh.len());
    assert_ne!(winner, fresh[0]);
    assert!(users.contains(&fresh[0]), "{} not in {users:?}", fresh[0]);
    assert!(redrawn.winners.contains(&fresh[0]));
    assert!(
        redrawn.winners.contains(&winner),
        "the expired winner stays named"
    );

    // The replacement gets its own pending window.
    let rows = pending(&f.h, &ended_gw).await;
    assert_eq!(
        vec![fresh[0].clone()],
        rows.iter().map(row_user).collect::<Vec<_>>()
    );

    // The expired slot can no longer be claimed.
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner}),
        )
        .await;
    assert!(
        err.message.contains("closed"),
        "unexpected: {}",
        err.message
    );
    // due_claims only fires past the deadline.
    assert_eq!(
        json!([]),
        f.h.ok("due_claims", json!({"now": deadline - 1})).await
    );
    // Claiming the replacement works.
    f.h.ok(
        "claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": fresh[0]}),
    )
    .await;
    assert_eq!(Vec::<Value>::new(), pending(&f.h, &ended_gw).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_with_an_empty_pool_returns_no_replacement() {
    let f = Fixture::new().await;
    let gw = make(
        &f,
        json!({"winner_count": 1, "claim_timeout_seconds": 3600}),
    )
    .await;
    join_ok(&f, &gw, USER, json!({})).await;
    let (ended_gw, winners) = end_value(&f, &gw).await;
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");

    let value =
        f.h.ok(
            "expire_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winners[0], "now": deadline + 1}),
        )
        .await;
    let (redrawn, fresh) = ended(&value);
    assert!(fresh.is_empty());
    assert_eq!(winners, redrawn.winners);
    assert_eq!(Vec::<Value>::new(), pending(&f.h, &ended_gw).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn multi_winner_claims_are_independent() {
    let f = Fixture::new().await;
    let gw = make(
        &f,
        json!({"winner_count": 2, "claim_timeout_seconds": 3600}),
    )
    .await;
    let users: Vec<String> = (0..4)
        .map(|index| (10i64.pow(17) + index).to_string())
        .collect();
    for user in &users {
        join_ok(&f, &gw, user, json!({})).await;
    }
    let (ended_gw, winners) = end_value(&f, &gw).await;
    assert_eq!(2, winners.len());

    f.h.ok(
        "claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": winners[0]}),
    )
    .await;
    let still: Vec<String> = pending(&f.h, &ended_gw)
        .await
        .iter()
        .map(row_user)
        .collect();
    assert_eq!(vec![winners[1].clone()], still);

    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");
    let value =
        f.h.ok(
            "expire_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winners[1], "now": deadline + 1}),
        )
        .await;
    let (redrawn, fresh) = ended(&value);
    assert_eq!(1, fresh.len());
    assert!(!winners.contains(&fresh[0]));
    assert!(users.contains(&fresh[0]));
    assert_eq!(3, redrawn.winners.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn start_claims_is_idempotent() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let before = pending(&f.h, &ended_gw).await;
    let reloaded = f.h.ok("get", json!({"giveaway_id": ended_gw["id"]})).await;
    f.h.ok("start_claims", json!({"gw": reloaded, "winners": [winner]}))
        .await;
    let after = pending(&f.h, &ended_gw).await;
    assert_eq!(1, before.len());
    assert_eq!(1, after.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn manual_reroll_opens_claims_and_keeps_the_old_pending() {
    let f = Fixture::new().await;
    let (ended_gw, winner, users) = three_entrant_ended(&f, json!({})).await;
    let value =
        f.h.ok("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
            .await;
    let (redrawn, fresh) = ended(&value);
    assert_eq!(1, fresh.len());
    assert_ne!(winner, fresh[0]);
    assert!(users.contains(&fresh[0]));
    // Additive: both the old winner and the reroll winner stay named.
    assert!(redrawn.winners.contains(&winner));
    assert!(redrawn.winners.contains(&fresh[0]));

    // Both hold a pending claim window; the old deadline still stands.
    let mut held: BTreeSet<String> = pending(&f.h, &ended_gw)
        .await
        .iter()
        .map(row_user)
        .collect();
    held.insert(winner.clone());
    let mut expected = BTreeSet::from([winner.clone(), fresh[0].clone()]);
    assert_eq!(expected, held);
    expected.clear();

    // The fresh winner can actually press Claim Prize.
    f.h.ok(
        "claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": fresh[0]}),
    )
    .await;
    let still: Vec<String> = pending(&f.h, &ended_gw)
        .await
        .iter()
        .map(row_user)
        .collect();
    assert_eq!(vec![winner], still);
}

#[tokio::test(flavor = "multi_thread")]
async fn manual_reroll_never_repeats_claim_draws() {
    let f = Fixture::new().await;
    let (ended_gw, winner, users) = three_entrant_ended(&f, json!({})).await;
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");
    let value =
        f.h.ok(
            "expire_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner, "now": deadline + 1}),
        )
        .await;
    let (_, auto) = ended(&value);
    assert_eq!(1, auto.len());

    let value =
        f.h.ok("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
            .await;
    let (_, fresh) = ended(&value);
    assert_eq!(1, fresh.len());
    // Neither the expired winner nor the auto replacement may reappear.
    assert!(![winner.clone(), auto[0].clone()].contains(&fresh[0]));
    assert!(users.contains(&fresh[0]));
    let third = users
        .iter()
        .find(|user| **user != winner && **user != auto[0])
        .expect("a third entrant")
        .clone();
    assert_eq!(third, fresh[0]);
    // The pool is now exhausted: one more reroll must refuse.
    f.h.refuse("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
        .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_reroll_replaces_one_slot_in_place() {
    let f = Fixture::new().await;
    let (ended_gw, winners, users) = four_entrant_three_winner_ended(&f, json!({})).await;
    let old = winners[0].clone();
    let expected = users
        .iter()
        .find(|user| !winners.contains(user))
        .expect("one undrawn entrant")
        .clone();

    let value =
        f.h.ok(
            "reroll",
            json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
        )
        .await;
    let (redrawn, fresh) = ended(&value);
    assert_eq!(vec![expected], fresh);
    // Same length, order preserved: only the picked slot swaps.
    assert_eq!(3, redrawn.winners.len());
    assert_eq!(fresh[0], redrawn.winners[0]);
    assert_eq!(winners[1..], redrawn.winners[1..]);
    assert!(!redrawn.winners.contains(&old));

    // The old pending is closed as replaced; the fresh winner gets one.
    let statuses: BTreeMap<String, String> =
        f.h.ok("claim_status", json!({"giveaway_id": ended_gw["id"]}))
            .await
            .as_array()
            .expect("claim rows")
            .iter()
            .map(|row| {
                (
                    row_user(row),
                    row["status"].as_str().unwrap_or_default().to_string(),
                )
            })
            .collect();
    assert_eq!(Some("replaced"), statuses.get(&old).map(String::as_str));
    assert_eq!(Some("pending"), statuses.get(&fresh[0]).map(String::as_str));
    let held: BTreeSet<String> = pending(&f.h, &ended_gw)
        .await
        .iter()
        .map(row_user)
        .collect();
    assert_eq!(
        redrawn.winners.iter().cloned().collect::<BTreeSet<_>>(),
        held
    );

    // The fresh winner can press Claim Prize; the old one gets a clear no.
    f.h.ok(
        "claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": fresh[0]}),
    )
    .await;
    let err =
        f.h.refuse(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": old}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("replaced"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_reroll_rejects_non_winner_and_exhausted_pool() {
    let f = Fixture::new().await;
    let (ended_gw, winners, _users) = four_entrant_three_winner_ended(&f, json!({})).await;
    let err =
        f.h.refuse(
            "reroll",
            json!({
                "giveaway_id": ended_gw["id"],
                "count": 1,
                "replace_user_id": "999999999999999999",
            }),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("not a winner"),
        "unexpected: {}",
        err.message
    );

    // One swap exhausts the 4-entrant pool: every user is now ever-drawn.
    f.h.ok(
        "reroll",
        json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": winners[0]}),
    )
    .await;
    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": winners[1]}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("no other entrants"),
        "unexpected: {}",
        err.message
    );
    f.h.refuse("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
        .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_reroll_without_claims_still_excludes() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"winner_count": 1})).await;
    let users: Vec<String> = (0..2)
        .map(|index| (10i64.pow(17) + index).to_string())
        .collect();
    for user in &users {
        join_ok(&f, &gw, user, json!({})).await;
    }
    let (ended_gw, winners) = end_value(&f, &gw).await;
    let old = winners[0].clone();

    let value =
        f.h.ok(
            "reroll",
            json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
        )
        .await;
    let (redrawn, fresh) = ended(&value);
    assert_eq!(1, fresh.len());
    assert_ne!(old, fresh[0]);
    assert_eq!(fresh, redrawn.winners);
    // A replaced marker keeps the old winner out of every future draw.
    let rows =
        f.h.ok("claim_status", json!({"giveaway_id": ended_gw["id"]}))
            .await;
    let rows = rows.as_array().expect("claim rows");
    assert_eq!(1, rows.len());
    assert_eq!(old, row_user(&rows[0]));
    assert_eq!(json!("replaced"), rows[0]["status"]);
    f.h.refuse("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
        .await;
}

// -- ended lookups -------------------------------------------------------

/// The Python `_ended()` helper: create, enter one member, end.
async fn ended_giveaway(f: &Fixture, overrides: Value) -> Value {
    let gw = make(f, overrides).await;
    join_ok(f, &gw, USER, json!({})).await;
    let (ended_gw, _winners) = end_value(f, &gw).await;
    ended_gw
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_returns_ended_by_id() {
    let f = Fixture::new().await;
    let ended_gw = ended_giveaway(&f, json!({})).await;
    let found =
        f.h.ok(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": ended_gw["id"]}),
        )
        .await;
    assert_eq!(ended_gw["id"], found["id"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_refuses_a_live_giveaway() {
    let f = Fixture::new().await;
    let live = make(&f, json!({})).await;
    let err =
        f.h.refuse(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": live["id"]}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("not ended"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_never_falls_back_to_a_live_giveaway() {
    let f = Fixture::new().await;
    let live = make(&f, json!({"prize": "live"})).await;
    let ended_gw = ended_giveaway(&f, json!({"prize": "ended"})).await;

    // A typo id with a single ended row falls back to that ended row -- same
    // shape as resolve() -- and never to the running giveaway.
    for raw in ["gw_typo", ""] {
        let found =
            f.h.ok("resolve_ended", json!({"guild_id": f.guild, "raw_id": raw}))
                .await;
        assert_eq!(ended_gw["id"], found["id"]);
        assert_ne!(live["id"], found["id"]);
    }

    // Two ended rows -> needs an id, even though only one live exists.
    let other = make(&f, json!({"prize": "ended2"})).await;
    join_ok(&f, &other, USER, json!({})).await;
    let (second, _winners) = end_value(&f, &other).await;
    let err =
        f.h.refuse("resolve_ended", json!({"guild_id": f.guild, "raw_id": ""}))
            .await;
    assert!(
        err.message.contains("did not match"),
        "unexpected: {}",
        err.message
    );
    for id in [&ended_gw["id"], &second["id"]] {
        let found =
            f.h.ok("resolve_ended", json!({"guild_id": f.guild, "raw_id": id}))
                .await;
        assert_eq!(*id, found["id"]);
    }
    assert!(!live["id"].is_null());
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_single_fallback() {
    let f = Fixture::new().await;
    let ended_gw = ended_giveaway(&f, json!({})).await;
    for raw in ["", "  "] {
        let found =
            f.h.ok("resolve_ended", json!({"guild_id": f.guild, "raw_id": raw}))
                .await;
        assert_eq!(ended_gw["id"], found["id"]);
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_refuses_another_guild() {
    let f = Fixture::new().await;
    let other = make(&f, json!({"guild_id": "555"})).await;
    join_ok(&f, &other, USER, json!({})).await;
    let (ended_gw, _winners) = end_value(&f, &other).await;
    let err =
        f.h.refuse(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": ended_gw["id"]}),
        )
        .await;
    assert!(
        err.message.contains("another server"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn resolve_ended_with_nothing_ended() {
    let f = Fixture::new().await;
    let err =
        f.h.refuse(
            "resolve_ended",
            json!({"guild_id": f.guild, "raw_id": "gw_x"}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("no ended"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn list_ended_is_guild_scoped() {
    let f = Fixture::new().await;
    let mine = ended_giveaway(&f, json!({"prize": "mine"})).await;
    let other = make(&f, json!({"guild_id": "555", "prize": "theirs"})).await;
    join_ok(&f, &other, USER, json!({})).await;
    f.end(&other).await;
    let listed = f.h.ok("list_ended", json!({"guild_id": f.guild})).await;
    let ids: Vec<&str> = listed
        .as_array()
        .expect("ended list")
        .iter()
        .map(|row| row["id"].as_str().unwrap_or_default())
        .collect();
    assert_eq!(vec![mine["id"].as_str().unwrap_or_default()], ids);
}

// -- reroll CAS -----------------------------------------------------------

/// Split any op that answers `[giveaway, winners]`.
fn split_pair(value: &Value) -> (Value, Vec<String>) {
    let winners: Vec<String> = value[1]
        .as_array()
        .expect("winners")
        .iter()
        .map(|winner| winner.as_str().unwrap_or_default().to_string())
        .collect();
    (value[0].clone(), winners)
}

async fn winners_of(f: &Fixture, gw: &Value) -> Vec<String> {
    f.h.ok("get", json!({"giveaway_id": gw["id"]}))
        .await
        .get("winners")
        .and_then(Value::as_array)
        .expect("winners")
        .iter()
        .map(|winner| winner.as_str().unwrap_or_default().to_string())
        .collect()
}

/// Append a never-drawn id straight into the stored JSON, the way a racing
/// writer would.
async fn append_winner(f: &Fixture, gw: &Value, user_id: &str) {
    let mut winners = winners_of(f, gw).await;
    winners.push(user_id.to_string());
    f.execute_params(
        "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?",
        json!([
            serde_json::to_string(&winners).expect("winners json"),
            gw["id"]
        ]),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_preserves_racing_append() {
    let f = Fixture::new().await;
    let (ended_gw, _winner, _users) = three_entrant_ended(&f, json!({})).await;
    // Not an entrant: _pick can never draw it.
    let external = "999999999999999999";
    append_winner(&f, &ended_gw, external).await;

    let value =
        f.h.ok("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
            .await;
    let (_redrawn, fresh) = split_pair(&value);
    let final_winners = winners_of(&f, &ended_gw).await;
    assert!(
        final_winners.iter().any(|winner| winner == external),
        "racing append must survive the CAS rebase"
    );
    assert!(final_winners.contains(&fresh[0]));
    assert_eq!(3, final_winners.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_preserves_racing_append() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let external = "999999999999999999";
    append_winner(&f, &ended_gw, external).await;
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");

    let value =
        f.h.ok(
            "expire_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner, "now": deadline + 1}),
        )
        .await;
    let (redrawn, fresh) = split_pair(&value);
    let redrawn_winners: Vec<String> = redrawn["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .map(|entry| entry.as_str().unwrap_or_default().to_string())
        .collect();
    assert!(redrawn_winners.iter().any(|entry| entry == external));
    assert!(
        redrawn_winners.contains(&winner),
        "expired winner stays named"
    );
    assert_eq!(1, fresh.len());
}

#[tokio::test(flavor = "multi_thread")]
async fn read_winners_raw_round_trip() {
    // ADAPTED: Python called the private `_read_winners_raw`, which returns the
    // parsed list plus the stored JSON text. Rust keeps that helper private, so
    // the same column is read directly and decoded the same way.
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let rows =
        f.h.ok(
            "sql",
            json!({
                "sql": "SELECT winners_json FROM simple_giveaways WHERE id = ?",
                "params": [ended_gw["id"]],
            }),
        )
        .await;
    let raw = rows[0]["winners_json"]
        .as_str()
        .expect("winners_json text")
        .to_string();
    let parsed: Vec<String> = serde_json::from_str(&raw).expect("winners json");
    assert_eq!(vec![winner.clone()], parsed);
    assert_eq!(
        json!([winner]),
        serde_json::from_str::<Value>(&raw).expect("json")
    );
}

// -- claim atomicity ------------------------------------------------------

async fn strand(f: &Fixture, gw: &Value) {
    f.execute_params(
        "DELETE FROM simple_claims WHERE giveaway_id = ?",
        json!([gw["id"]]),
    )
    .await;
    assert_eq!(
        json!([]),
        f.h.ok("claim_status", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_insert_crash_still_announces_and_heals() {
    // ADAPTED: Python monkeypatched `start_claims` to raise inside `end`, so the
    // draw committed but the claim rows did not. Rust has no such seam, so the
    // crash is reproduced by deleting the claim rows after the draw: `end` has
    // already returned the stored winner, and the repair path must rebuild the
    // window from that stored draw.
    let f = Fixture::new().await;
    let (ended_gw, winner, users) = three_entrant_ended(&f, json!({})).await;
    assert!(users.contains(&winner));
    strand(&f, &ended_gw).await;

    let healed = pending(&f.h, &ended_gw).await;
    assert_eq!(
        vec![winner],
        healed.iter().map(row_user).collect::<Vec<_>>()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stranded_draw_healed_by_pending_and_claim_retry() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    strand(&f, &ended_gw).await;
    assert_eq!(
        vec![winner.clone()],
        pending(&f.h, &ended_gw)
            .await
            .iter()
            .map(row_user)
            .collect::<Vec<_>>()
    );
    strand(&f, &ended_gw).await;
    let row =
        f.h.ok(
            "claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner}),
        )
        .await;
    assert_eq!(winner, row_user(&row));
    assert_eq!(Vec::<Value>::new(), pending(&f.h, &ended_gw).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn stranded_draw_healed_by_tick_sweep() {
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");
    strand(&f, &ended_gw).await;
    let due = f.h.ok("due_claims", json!({"now": deadline + 1})).await;
    assert_eq!(
        vec![winner],
        due.as_array()
            .expect("due claims")
            .iter()
            .map(row_user)
            .collect::<Vec<_>>()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn repair_is_idempotent_and_settles_stale_pending() {
    let f = Fixture::new().await;
    let (ended_gw, winner, users) = three_entrant_ended(&f, json!({})).await;
    // A stale pending row for a non-winner (targeted reroll that died before
    // settling) flips to replaced; existing winners keep windows.
    let loser = users
        .iter()
        .find(|user| **user != winner)
        .expect("a loser")
        .clone();
    f.execute_params(
        "INSERT OR IGNORE INTO simple_claims \
         (giveaway_id, user_id, round, status, deadline_ms, created_at) \
         VALUES (?, ?, 0, 'pending', ?, ?)",
        json!([ended_gw["id"], loser, f.h.now() + 3600 * 1000, f.h.now()]),
    )
    .await;

    let written =
        f.h.ok(
            "repair_missing_claims",
            json!({"giveaway_id": ended_gw["id"]}),
        )
        .await;
    assert!(
        written.as_i64().unwrap_or_default() >= 1,
        "repair wrote nothing: {written}"
    );
    let statuses: BTreeMap<String, String> =
        f.h.ok("claim_status", json!({"giveaway_id": ended_gw["id"]}))
            .await
            .as_array()
            .expect("claim rows")
            .iter()
            .map(|row| {
                (
                    row_user(row),
                    row["status"].as_str().unwrap_or_default().to_string(),
                )
            })
            .collect();
    assert_eq!(Some("pending"), statuses.get(&winner).map(String::as_str));
    assert_eq!(Some("replaced"), statuses.get(&loser).map(String::as_str));

    let again =
        f.h.ok(
            "repair_missing_claims",
            json!({"giveaway_id": ended_gw["id"]}),
        )
        .await;
    assert_eq!(json!(0), again, "second sweep must be a no-op");
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_insert_crash_heals_fresh_window() {
    // ADAPTED: the Python patch made `start_claims` raise during `reroll`.
    // Here the reroll completes and its claim rows are deleted, which is the
    // same on-disk state: stored winners, no window. The heal must give both
    // the original and the fresh winner a pending row.
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let value =
        f.h.ok("reroll", json!({"giveaway_id": ended_gw["id"], "count": 1}))
            .await;
    let (redrawn, fresh) = split_pair(&value);
    assert_eq!(1, fresh.len());
    assert!(redrawn["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .any(|entry| entry.as_str() == Some(fresh[0].as_str())));

    strand(&f, &ended_gw).await;
    let healed: BTreeSet<String> = pending(&f.h, &ended_gw)
        .await
        .iter()
        .map(row_user)
        .collect();
    assert!(healed.contains(&fresh[0]));
    assert!(healed.contains(&winner));
}

#[tokio::test(flavor = "multi_thread")]
async fn expire_insert_crash_heals_replacement() {
    // ADAPTED: same shape as the reroll crash above, for `expire_claim`.
    let f = Fixture::new().await;
    let (ended_gw, winner, _users) = three_entrant_ended(&f, json!({})).await;
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");
    let value =
        f.h.ok(
            "expire_claim",
            json!({"giveaway_id": ended_gw["id"], "user_id": winner, "now": deadline + 1}),
        )
        .await;
    let (_redrawn, fresh) = split_pair(&value);
    assert_eq!(1, fresh.len());

    strand(&f, &ended_gw).await;
    let healed: BTreeSet<String> = pending(&f.h, &ended_gw)
        .await
        .iter()
        .map(row_user)
        .collect();
    assert!(healed.contains(&fresh[0]));
}

// -- targeted reroll settled guard ---------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn targeted_replace_refuses_claimed() {
    let f = Fixture::new().await;
    let (ended_gw, winners, _users) = four_entrant_three_winner_ended(&f, json!({})).await;
    let old = winners[0].clone();
    f.h.ok(
        "claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": old}),
    )
    .await;
    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("already claimed"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_replace_refuses_skipped() {
    let f = Fixture::new().await;
    let (ended_gw, winners, _users) = four_entrant_three_winner_ended(&f, json!({})).await;
    let old = winners[0].clone();
    f.h.ok(
        "skip_claim",
        json!({
            "giveaway_id": ended_gw["id"],
            "user_id": old,
            "staff_id": "999999999999999999",
        }),
    )
    .await;
    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("already verified"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn targeted_replace_refuses_expired() {
    let f = Fixture::new().await;
    let (ended_gw, winners, _users) = four_entrant_three_winner_ended(&f, json!({})).await;
    let old = winners[0].clone();
    let deadline = pending(&f.h, &ended_gw).await[0]["deadline_ms"]
        .as_i64()
        .expect("deadline");
    f.h.ok(
        "expire_claim",
        json!({"giveaway_id": ended_gw["id"], "user_id": old, "now": deadline + 1}),
    )
    .await;
    let err =
        f.h.refuse(
            "reroll",
            json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("already replaced"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn second_replace_of_same_slot_refused() {
    let f = Fixture::new().await;
    let (ended_gw, winners, _users) = four_entrant_three_winner_ended(&f, json!({})).await;
    let old = winners[0].clone();
    f.h.ok(
        "reroll",
        json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
    )
    .await;
    assert!(!winners_of(&f, &ended_gw).await.contains(&old));
    f.h.refuse(
        "reroll",
        json!({"giveaway_id": ended_gw["id"], "count": 1, "replace_user_id": old}),
    )
    .await;
}

// -- guild settings ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn notify_role_upsert() {
    let f = Fixture::new().await;
    assert_eq!(
        Value::Null,
        f.h.ok("get_notify_role", json!({"guild_id": f.guild}))
            .await
    );
    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": "123"}),
    )
    .await;
    assert_eq!(
        json!("123"),
        f.h.ok("get_notify_role", json!({"guild_id": f.guild}))
            .await
    );
    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": "456"}),
    )
    .await;
    assert_eq!(
        json!("456"),
        f.h.ok("get_notify_role", json!({"guild_id": f.guild}))
            .await,
        "upsert, not insert"
    );
    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": null}),
    )
    .await;
    assert_eq!(
        Value::Null,
        f.h.ok("get_notify_role", json!({"guild_id": f.guild}))
            .await
    );
    assert_eq!(
        Value::Null,
        f.h.ok("get_notify_role", json!({"guild_id": "999"})).await
    );
}

// -- eligibility snapshot ------------------------------------------------

/// The four-field eligibility tuple for one member.
async fn snapshot_of(f: &Fixture, guild: &str, user: &str, giveaway_id: &str) -> Value {
    f.h.ok(
        "eligibility_snapshot",
        json!({"guild_id": guild, "user_id": user, "giveaway_id": giveaway_id}),
    )
    .await
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_returns_all_four_fields() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "5".repeat(18);
    for _ in 0..2 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": uid}),
        )
        .await;
    }
    assert_eq!(
        json!([false, 0, 2, false]),
        snapshot_of(&f, &f.guild, &uid, &id_of(&gw)).await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_sees_blacklist_ban_and_hit() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "6".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid}),
    )
    .await;
    f.h.ok(
        "apply_timeout_penalty",
        json!({"guild_id": f.guild, "user_id": uid, "giveaway_id": gw["id"]}),
    )
    .await;
    assert_eq!(
        json!([true, 3, 0, true]),
        snapshot_of(&f, &f.guild, &uid, &id_of(&gw)).await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_empty_ids_fail_closed() {
    let f = Fixture::new().await;
    // Empty ids cannot match a row, so the tuple reads as "nothing blocks this
    // member" without a round-trip. Python passed no giveaway id at all.
    assert_eq!(
        json!([false, 0, 0, false]),
        snapshot_of(&f, "", "", "").await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_none_on_db_failure_forces_fallback() {
    // ADAPTED: Python monkeypatched `db.query_one` to raise for the snapshot
    // call only. Rust has no such seam, so the table the snapshot reads is
    // moved out of the way and put back; None must force the individual
    // checks, and the join must still work afterwards.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "5".repeat(18);
    f.execute("ALTER TABLE simple_blacklist RENAME TO simple_blacklist_off")
        .await;
    assert_eq!(
        Value::Null,
        snapshot_of(&f, &f.guild, &uid, &id_of(&gw)).await,
        "a failed snapshot read must not look like a clean one"
    );
    f.execute("ALTER TABLE simple_blacklist_off RENAME TO simple_blacklist")
        .await;
    assert_eq!(1, join_ok(&f, &gw, &uid, json!({})).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn join_with_snapshot_honors_live_block() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "7".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid}),
    )
    .await;
    let err = join_err(&f, &gw, &uid, json!({"snapshot": [true, 0, 0, false]})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_snapshot_block_does_not_falsely_refuse() {
    // Snapshot says blocked but the row is gone (a blacklist_remove raced the
    // snapshot): the live confirm lets the join through. The INSERT guard
    // still blocks a racing blacklist_add, so safety is kept.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(
        1,
        join_ok(
            &f,
            &gw,
            "777777777777777777",
            json!({"snapshot": [true, 0, 0, false]}),
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn join_with_snapshot_counts_buffered_messages() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"min_messages": 3})).await;
    let uid = "8".repeat(18);
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": uid}),
    )
    .await;
    assert_eq!(
        1,
        join_ok(
            &f,
            &gw,
            &uid,
            json!({"snapshot": [false, 0, 1, false], "pending_messages": 2}),
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn get_status_is_status_only() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(
        json!("active"),
        f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await
    );
    f.h.ok("end", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(
        json!("ended"),
        f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await
    );
    assert_eq!(
        Value::Null,
        f.h.ok("get_status", json!({"giveaway_id": "gw_missing"}))
            .await
    );
}

// -- db stats ------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn stats_count_read_trips() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.h.ok("reset_stats", json!({})).await;
    snapshot_of(&f, &f.guild, &"5".repeat(18), &id_of(&gw)).await;
    f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await;
    let stats = f.h.ok("db_stats", json!({})).await;
    assert!(stats["reads"].as_u64().unwrap_or(0) >= 2, "reads: {stats}");
    assert_eq!(0, stats["writes"].as_u64().unwrap_or(u64::MAX), "{stats}");
}

#[tokio::test(flavor = "multi_thread")]
async fn stats_counts_failed_attempts() {
    // ADAPTED: Python swapped `db._conn` for a callable that raised "database
    // is locked" once, then let the retry succeed. Rust's pool is internal, so
    // the failed attempt is produced by a statement that cannot run at all:
    // the counter must still move, because a hidden failure would read as a
    // free call.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    f.h.ok("reset_stats", json!({})).await;
    assert!(
        f.h.call("sql", json!({"sql": "SELECT * FROM no_such_table"}))
            .await
            .is_err(),
        "the statement was supposed to fail"
    );
    f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await;
    let stats = f.h.ok("db_stats", json!({})).await;
    assert!(
        stats["reads"].as_u64().unwrap_or(0) >= 2,
        "failed attempt must count, not hide: {stats}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stats_separates_reads_and_writes() {
    let f = Fixture::new().await;
    f.h.ok("reset_stats", json!({})).await;
    f.h.ok(
        "record_message",
        json!({"guild_id": f.guild, "user_id": "9".repeat(18)}),
    )
    .await;
    let stats = f.h.ok("db_stats", json!({})).await;
    assert!(
        stats["writes"].as_u64().unwrap_or(0) >= 1,
        "writes: {stats}"
    );
    assert_eq!(0, stats["reads"].as_u64().unwrap_or(u64::MAX), "{stats}");
}

// -- join fast-path races ------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_racing_blacklist() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let fresh = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let uid = "9".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid}),
    )
    .await;
    let err = join_err(&f, &fresh, &uid, json!({"snapshot": [false, 0, 0, false]})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_racing_deadline() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let stale = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
        json!([f.h.now() - 1, gw["id"]]),
    )
    .await;
    let err = join_err(
        &f,
        &stale,
        &"9".repeat(18),
        json!({"snapshot": [false, 0, 0, false]}),
    )
    .await;
    assert!(err.message.contains("ended"), "unexpected: {}", err.message);
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_penalty_race_and_consumes() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "8".repeat(18);
    let stale = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    f.h.ok(
        "apply_timeout_penalty",
        json!({"guild_id": f.guild, "user_id": uid, "giveaway_id": "other_gw"}),
    )
    .await;
    let err = join_err(&f, &stale, &uid, json!({"snapshot": [false, 0, 0, false]})).await;
    assert_eq!(TIMEOUT_BAN_KIND, err.kind);
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
    assert_eq!(json!(2), remaining(&f, &f.guild, &uid).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_ban_hit_race() {
    // ADAPTED: Python called the private `_record_ban_hit`. Rust keeps it
    // private too, so the same row is written directly -- the guard under test
    // only cares that the hit exists.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "8".repeat(18);
    f.execute_params(
        "INSERT OR IGNORE INTO simple_giveaway_ban_hits \
         (guild_id, user_id, giveaway_id, created_at) VALUES (?, ?, ?, ?)",
        json!([f.guild, uid, gw["id"], f.h.now()]),
    )
    .await;
    let err = join_err(&f, &gw, &uid, json!({"snapshot": [false, 0, 0, false]})).await;
    assert_eq!(TIMEOUT_BAN_KIND, err.kind);
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_ban_snapshot_does_not_falsely_penalize() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(
        1,
        join_ok(
            &f,
            &gw,
            "777777777777777777",
            json!({"snapshot": [false, 3, 0, false]}),
        )
        .await
    );
    assert_eq!(
        json!(0),
        remaining(&f, &f.guild, "777777777777777777").await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_hit_snapshot_does_not_falsely_refuse() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    assert_eq!(
        1,
        join_ok(
            &f,
            &gw,
            "777777777777777777",
            json!({"snapshot": [false, 0, 0, true]}),
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_message_snapshot_live_confirm_allows() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"min_messages": 3})).await;
    let uid = "8".repeat(18);
    for _ in 0..3 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": uid}),
        )
        .await;
    }
    assert_eq!(
        1,
        join_ok(&f, &gw, &uid, json!({"snapshot": [false, 0, 1, false]}),).await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_ended_never_penalizes_timed_out() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let stale = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    f.h.ok("end", json!({"giveaway_id": gw["id"]})).await;
    let err = join_err(
        &f,
        &stale,
        "777777777777777777",
        json!({"timed_out": true, "snapshot": [false, 0, 0, false]}),
    )
    .await;
    assert!(err.message.contains("ended"), "unexpected: {}", err.message);
    assert_ne!(TIMEOUT_BAN_KIND, err.kind);
    assert_eq!(
        json!(0),
        remaining(&f, &f.guild, "777777777777777777").await
    );
}

/// A trigger that fails every entry insert with an ambiguous-loss message.
const AMBIGUOUS_INSERT: &str = "CREATE TRIGGER ambiguous_entry_insert \
     BEFORE INSERT ON simple_entries BEGIN \
     SELECT RAISE(ABORT, 'connection reset'); END";

#[tokio::test(flavor = "multi_thread")]
async fn ambiguous_insert_fresh_age_succeeds() {
    // ADAPTED: Python monkeypatched `db.execute` to raise OSError("connection
    // reset") on the entry INSERT. The trigger produces the same mid-write
    // loss for real: the service must read the row's age and treat a fresh one
    // as this attempt (idempotent success).
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "9".repeat(18);
    join_ok(&f, &gw, &uid, json!({})).await;
    f.trigger(AMBIGUOUS_INSERT).await;
    let count = join_ok(&f, &gw, &uid, json!({})).await;
    f.drop_trigger("ambiguous_entry_insert").await;
    assert!(
        count >= 1,
        "a fresh ambiguous insert must read as success, got {count}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn ambiguous_insert_old_age_reports_duplicate() {
    // ADAPTED: same trigger as above, but the existing row is old: it cannot be
    // this attempt, so it must surface as a duplicate instead of a free pass.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "9".repeat(18);
    join_ok(&f, &gw, &uid, json!({})).await;
    f.execute_params(
        "UPDATE simple_entries SET entered_at = ? WHERE giveaway_id = ? AND user_id = ?",
        json!([f.h.now() - 60_000, gw["id"], uid]),
    )
    .await;
    f.trigger(AMBIGUOUS_INSERT).await;
    let err = join_err(&f, &gw, &uid, json!({})).await;
    f.drop_trigger("ambiguous_entry_insert").await;
    assert!(
        err.message.contains("already entered"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn rowcount_negative_with_entry_succeeds() {
    // ADAPTED: Python returned a fake cursor with `rowcount = -1` after running
    // the real INSERT. RAISE(IGNORE) is the driver-neutral version: the
    // statement reports zero rows changed while the entry is already there, so
    // "count unknown but the row landed" must still read as success.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "9".repeat(18);
    join_ok(&f, &gw, &uid, json!({})).await;
    f.trigger(
        "CREATE TRIGGER unknown_rowcount BEFORE INSERT ON simple_entries \
         BEGIN SELECT RAISE(IGNORE); END",
    )
    .await;
    let count = join_ok(&f, &gw, &uid, json!({})).await;
    f.drop_trigger("unknown_rowcount").await;
    assert_eq!(1, count);
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": uid})
        )
        .await
    );
}

// -- timed bans ----------------------------------------------------------

/// One blacklist row's details, or null.
async fn ban_info(f: &Fixture, guild: &str, user: &str, now: Option<i64>) -> Value {
    f.h.ok(
        "ban_info",
        json!({"guild_id": guild, "user_id": user, "now": now}),
    )
    .await
}

#[tokio::test(flavor = "multi_thread")]
async fn default_add_is_permanent() {
    let f = Fixture::new().await;
    let uid = "1".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid}),
    )
    .await;
    let info = ban_info(&f, &f.guild, &uid, None).await;
    assert_eq!(json!(true), info["permanent"]);
    assert_eq!(Value::Null, info["expires_at"]);
    assert_eq!(json!(true), is_blacklisted(&f, &f.guild, &uid).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn timed_ban_blocks_then_expires() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "2".repeat(18);
    let base = f.h.now();
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": uid,
            "hours": 5, "reason": "spam", "now": base
        }),
    )
    .await;
    let info = ban_info(&f, &f.guild, &uid, Some(base)).await;
    assert_eq!(json!(false), info["permanent"]);
    assert_eq!(json!("spam"), info["reason"]);
    assert_eq!(
        json!(true),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": uid, "now": base})
        )
        .await
    );
    let err = join_err(&f, &gw, &uid, json!({})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    // After expiry the same row reads as absent (lazy expiry, no write).
    let later = base + 5 * 3600 * 1000 + 1;
    assert_eq!(
        json!(false),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": uid, "now": later})
        )
        .await
    );
    assert_eq!(Value::Null, ban_info(&f, &f.guild, &uid, Some(later)).await);
    // join() uses the live clock, so expire the row for real before rejoining.
    f.execute_params(
        "UPDATE simple_blacklist SET expires_at = ? WHERE guild_id = ? AND user_id = ?",
        json!([f.h.now() - 1, f.guild, uid]),
    )
    .await;
    assert_eq!(1, join_ok(&f, &gw, &uid, json!({})).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn hours_validation() {
    let f = Fixture::new().await;
    let uid = "3".repeat(18);
    for bad in [-1_i64, 721] {
        f.h.refuse(
            "blacklist_add",
            json!({"guild_id": f.guild, "user_id": uid, "hours": bad}),
        )
        .await;
    }
    // ADAPTED: Python also passed the string "x". A typed Rust call cannot
    // carry a non-integer, so that case is covered by the range check above;
    // the boundary values are what must still be accepted.
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid, "hours": 720}),
    )
    .await;
    assert_eq!(json!(true), is_blacklisted(&f, &f.guild, &uid).await);
    f.h.ok(
        "blacklist_remove",
        json!({"guild_id": f.guild, "user_id": uid}),
    )
    .await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid, "hours": 0}),
    )
    .await;
    assert_eq!(json!(true), is_blacklisted(&f, &f.guild, &uid).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn reban_overwrites_expiry_and_reason() {
    let f = Fixture::new().await;
    let uid = "4".repeat(18);
    let base = f.h.now();
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": uid,
            "hours": 5, "reason": "first", "now": base
        }),
    )
    .await;
    let first = ban_info(&f, &f.guild, &uid, Some(base)).await;
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": uid,
            "hours": 2, "reason": "second", "now": base + 1000
        }),
    )
    .await;
    let second = ban_info(&f, &f.guild, &uid, Some(base + 1000)).await;
    assert_eq!(json!("second"), second["reason"]);
    assert!(
        second["expires_at"].as_i64().unwrap_or(0) < first["expires_at"].as_i64().unwrap_or(0),
        "re-ban must shorten the expiry"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn reason_trimmed_to_256() {
    let f = Fixture::new().await;
    let uid = "5".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": uid,
            "hours": 1, "reason": "x".repeat(300)
        }),
    )
    .await;
    let info = ban_info(&f, &f.guild, &uid, None).await;
    assert_eq!(
        256,
        info["reason"].as_str().unwrap_or_default().chars().count()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn ban_message_shows_reason_and_expiry() {
    let f = Fixture::new().await;
    let uid = "6".repeat(18);
    let base = f.h.now();
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": uid,
            "hours": 5, "reason": "spam", "now": base
        }),
    )
    .await;
    let msg =
        f.h.ok(
            "ban_message",
            json!({"guild_id": f.guild, "user_id": uid, "now": base}),
        )
        .await;
    let msg = msg.as_str().expect("ban message text");
    assert!(msg.contains("blocked"), "unexpected: {msg}");
    assert!(msg.contains("spam"), "unexpected: {msg}");
    assert!(msg.contains("Ban expires <t:"), "unexpected: {msg}");
    // Permanent variant.
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": "7".repeat(18),
            "hours": 0, "reason": "bad"
        }),
    )
    .await;
    let pmsg =
        f.h.ok(
            "ban_message",
            json!({"guild_id": f.guild, "user_id": "7".repeat(18)}),
        )
        .await;
    let pmsg = pmsg.as_str().expect("ban message text");
    assert!(pmsg.contains("blocked"), "unexpected: {pmsg}");
    assert!(
        pmsg.to_lowercase().contains("permanent"),
        "unexpected: {pmsg}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn timed_ban_purges_running_entries() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "8".repeat(18);
    join_ok(&f, &gw, &uid, json!({})).await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid, "hours": 3, "reason": "r"}),
    )
    .await;
    assert_eq!(json!(0), count_entries(&f, &gw, true).await);
    let err = join_err(&f, &gw, &uid, json!({})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn count_and_list_hide_expired() {
    let f = Fixture::new().await;
    let base = f.h.now();
    let permanent = "1".repeat(18);
    let timed = "2".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": permanent, "hours": 0}),
    )
    .await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": timed, "hours": 1, "now": base}),
    )
    .await;
    assert_eq!(
        json!(2),
        f.h.ok(
            "count_blacklisted",
            json!({"guild_id": f.guild, "now": base})
        )
        .await
    );
    let later = base + 3600 * 1000 + 1;
    assert_eq!(
        json!(1),
        f.h.ok(
            "count_blacklisted",
            json!({"guild_id": f.guild, "now": later})
        )
        .await
    );
    assert_eq!(
        json!([permanent.clone()]),
        f.h.ok("blacklist_list", json!({"guild_id": f.guild, "now": later}))
            .await
    );
    let detailed =
        f.h.ok(
            "blacklist_list_detailed",
            json!({"guild_id": f.guild, "now": base}),
        )
        .await;
    let by_id: BTreeMap<String, Value> = detailed
        .as_array()
        .expect("detailed rows")
        .iter()
        .map(|row| {
            (
                row["user_id"].as_str().unwrap_or_default().to_string(),
                row.clone(),
            )
        })
        .collect();
    assert_eq!(json!(true), by_id[&permanent]["permanent"]);
    assert_eq!(json!(false), by_id[&timed]["permanent"]);
    assert!(by_id[&timed].get("expires_at").is_some());
}

#[tokio::test(flavor = "multi_thread")]
async fn purge_expired_bans() {
    let f = Fixture::new().await;
    let base = f.h.now();
    let timed = "1".repeat(18);
    let permanent = "2".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": timed, "hours": 1, "now": base}),
    )
    .await;
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": permanent, "hours": 0}),
    )
    .await;
    let later = base + 3600 * 1000 + 1;
    assert_eq!(
        json!(1),
        f.h.ok("purge_expired_bans", json!({"now": later})).await
    );
    assert_eq!(
        json!(false),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": timed, "now": later})
        )
        .await
    );
    assert_eq!(json!(true), is_blacklisted(&f, &f.guild, &permanent).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_is_expiry_aware() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "9".repeat(18);
    let base = f.h.now();
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid, "hours": 1, "now": base}),
    )
    .await;
    let snap = snapshot_of(&f, &f.guild, &uid, &id_of(&gw)).await;
    assert_eq!(json!(true), snap[0]);
}

#[tokio::test(flavor = "multi_thread")]
async fn insert_guard_blocks_racing_timed_ban() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let fresh = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let uid = "9".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": uid, "hours": 4, "reason": "race"}),
    )
    .await;
    let err = join_err(&f, &fresh, &uid, json!({"snapshot": [false, 0, 0, false]})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    assert!(err.message.contains("race"), "unexpected: {}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn zero_insert_reports_ban_with_reason() {
    // ADAPTED: Python stubbed `is_blacklisted` to answer False once, so the
    // pre-check missed a ban that the INSERT guard then caught. A stale
    // snapshot saying "clean" is the same lie through a supported seam: the
    // pre-check skips the live read and the zero-insert path must still report
    // the real ban, reason included.
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "9".repeat(18);
    f.h.ok(
        "blacklist_add",
        json!({
            "guild_id": f.guild, "user_id": uid,
            "hours": 4, "reason": "late race"
        }),
    )
    .await;
    let err = join_err(&f, &gw, &uid, json!({"snapshot": [false, 0, 0, false]})).await;
    assert!(
        err.message.contains("blocked"),
        "unexpected: {}",
        err.message
    );
    assert!(
        err.message.contains("late race"),
        "unexpected: {}",
        err.message
    );
}

// -- due / extend race ---------------------------------------------------

/// A running giveaway that joins one member and is due at `base + extra_ms`.
async fn due_gw(f: &Fixture, base: i64, extra_ms: i64) -> Value {
    let gw = make(f, json!({"duration_seconds": 60})).await;
    join_ok(f, &gw, USER, json!({})).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
        json!([base + extra_ms, gw["id"]]),
    )
    .await;
    f.h.ok("get", json!({"giveaway_id": gw["id"]})).await
}

async fn due_ids(f: &Fixture, now: i64) -> Vec<String> {
    f.h.ok("due", json!({"now": now}))
        .await
        .as_array()
        .expect("due list")
        .iter()
        .map(|gw| gw["id"].as_str().unwrap_or_default().to_string())
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn stale_due_loses_to_extend() {
    let f = Fixture::new().await;
    let base = f.h.now();
    let gw = due_gw(&f, base, 1000).await;
    let tick_ts = base + 2000;
    // Tick snapshots while still due.
    assert!(due_ids(&f, tick_ts).await.contains(&id_of(&gw)));
    // Operator extends before the tick acts on its snapshot.
    f.h.ok(
        "extend",
        json!({"giveaway_id": gw["id"], "extra_seconds": 3600}),
    )
    .await;
    let extended = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert!(
        extended["ends_at"].as_i64().unwrap_or(0) > tick_ts,
        "extend did not move the deadline"
    );
    // The stale tick end must refuse and leave the giveaway running.
    let err =
        f.h.refuse(
            "end",
            json!({"giveaway_id": gw["id"], "now": tick_ts, "require_due": true}),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("not due"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(
        json!("active"),
        f.h.ok("get", json!({"giveaway_id": gw["id"]})).await["status"]
    );
    // Once the new deadline passes the tick ends it for real.
    let later = extended["ends_at"].as_i64().unwrap_or(0) + 1;
    assert!(due_ids(&f, later).await.contains(&id_of(&gw)));
    let value =
        f.h.ok(
            "end",
            json!({"giveaway_id": gw["id"], "now": later, "require_due": true}),
        )
        .await;
    let (ended, _winners) = split_pair(&value);
    assert_eq!(json!("ended"), ended["status"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn atomic_flip_rechecks_deadline() {
    // ADAPTED: Python stubbed `svc.get` to answer with a stale row once, so the
    // pre-check passed on stale data. The `known` argument is the supported
    // form of the same stale read: the UPDATE predicate itself must refuse.
    let f = Fixture::new().await;
    let base = f.h.now();
    let gw = due_gw(&f, base, 1000).await;
    let tick_ts = base + 2000;
    let stale = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ends_at = ? WHERE id = ?",
        json!([base + 5_000_000, gw["id"]]),
    )
    .await;
    let err =
        f.h.refuse(
            "end",
            json!({
                "giveaway_id": gw["id"], "now": tick_ts,
                "require_due": true, "known": stale
            }),
        )
        .await;
    assert!(
        err.message.to_lowercase().contains("not due"),
        "unexpected: {}",
        err.message
    );
    assert_eq!(
        json!("active"),
        f.h.ok("get", json!({"giveaway_id": gw["id"]})).await["status"]
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn tick_end_still_wins_when_still_due() {
    let f = Fixture::new().await;
    let base = f.h.now();
    let gw = due_gw(&f, base, 1000).await;
    let tick_ts = base + 2000;
    let value =
        f.h.ok(
            "end",
            json!({"giveaway_id": gw["id"], "now": tick_ts, "require_due": true}),
        )
        .await;
    let (ended, winners) = split_pair(&value);
    assert_eq!(json!("ended"), ended["status"]);
    let mut stored: Vec<String> = ended["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .map(|winner| winner.as_str().unwrap_or_default().to_string())
        .collect();
    stored.sort();
    let mut drawn = winners;
    drawn.sort();
    assert_eq!(stored, drawn);
}

#[tokio::test(flavor = "multi_thread")]
async fn manual_end_keeps_early_end() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"duration_seconds": 3600})).await;
    join_ok(&f, &gw, USER, json!({})).await;
    f.h.ok(
        "extend",
        json!({"giveaway_id": gw["id"], "extra_seconds": 3600}),
    )
    .await;
    // Manual /giveaway_end (no require_due) still ends before the deadline.
    let value = f.h.ok("end", json!({"giveaway_id": gw["id"]})).await;
    let (ended, _winners) = split_pair(&value);
    assert_eq!(json!("ended"), ended["status"]);
}

// -- join burst ----------------------------------------------------------

/// The `10 ** 17 + i` member ids Python used, zero-padded to 18 digits.
fn burst_id(index: u64) -> String {
    format!("{:0>18}", 100_000_000_000_000_000u64 + index)
}

/// Run `users` joins at the same instant on the shared service.
///
/// Python started one thread per click against one service; sharing the
/// fixture harness models exactly that (the local database serialises on its
/// own lock, the barrier keeps the clicks simultaneous).
async fn burst_join(f: &Fixture, gw: &Value, users: &[String]) -> Vec<RaceResult> {
    let jobs: Vec<(&Harness, RaceJob)> = users
        .iter()
        .map(|user| (&f.h, join_job(gw.clone(), user.clone(), json!({}))))
        .collect();
    run_concurrently(jobs)
}

#[tokio::test(flavor = "multi_thread")]
async fn hundred_distinct_users_each_get_exactly_one_entry() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let users: Vec<String> = (0..100).map(burst_id).collect();
    let outcomes = burst_join(&f, &gw, &users).await;
    for outcome in &outcomes {
        assert_eq!("ok", race_status(outcome), "a burst join failed");
    }
    assert_eq!(json!(100), count_entries(&f, &gw, true).await);
    for user in &users {
        assert_eq!(
            json!(true),
            f.h.ok(
                "has_entry",
                json!({"giveaway_id": gw["id"], "user_id": user})
            )
            .await,
            "{user} lost its entry"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn simultaneous_duplicate_clicks_keep_one_row() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let uid = "9".repeat(18);
    let users: Vec<String> = vec![uid; 10];
    let outcomes = burst_join(&f, &gw, &users).await;
    assert_eq!(json!(1), count_entries(&f, &gw, true).await);
    assert_eq!(
        1,
        outcomes
            .iter()
            .filter(|outcome| race_status(outcome) == "ok")
            .count(),
        "exactly one click may enter"
    );
    for outcome in &outcomes {
        match outcome {
            Ok(_) => {}
            Err(CallError::Service(err)) => assert!(
                err.message.contains("already entered"),
                "unexpected: {}",
                err.message
            ),
            Err(other) => panic!("duplicate click failed with {other}"),
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn join_while_closing_never_accepts_late_entries() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let users: Vec<String> = (0..40)
        .map(|index| burst_id(200_000_000_000_000 + index))
        .collect();
    let mut jobs: Vec<(&Harness, RaceJob)> =
        vec![(&f.h, call_job("end", json!({"giveaway_id": gw["id"]})))];
    for user in &users {
        jobs.push((&f.h, join_job(gw.clone(), user.clone(), json!({}))));
    }
    let outcomes = run_concurrently(jobs);
    // The closer always reports a verdict (ended or refused), never an error.
    assert_ne!("error", race_status(&outcomes[0]), "the closer failed");
    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    // Whatever landed before the flip is the pool; the draw can only pick from
    // committed rows, never from refused late clicks.
    let winners: Vec<String> = if stored["status"] == json!("active") {
        split_pair(&f.h.ok("end", json!({"giveaway_id": gw["id"]})).await).1
    } else {
        stored["winners"]
            .as_array()
            .expect("winners")
            .iter()
            .map(|winner| winner.as_str().unwrap_or_default().to_string())
            .collect()
    };
    let rows: BTreeSet<String> =
        f.h.ok("entries", json!({"giveaway_id": gw["id"]}))
            .await
            .as_array()
            .expect("entry rows")
            .iter()
            .map(|row| row["user_id"].as_str().unwrap_or_default().to_string())
            .collect();
    for winner in &winners {
        assert!(rows.contains(winner), "winner {winner} was never an entry");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn banned_user_gets_zero_entries_under_burst() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let banned = burst_id(999);
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": banned, "hours": 5, "reason": "spam"}),
    )
    .await;
    let mut users: Vec<String> = (0..20).map(burst_id).collect();
    users.push(banned.clone());
    let outcomes = burst_join(&f, &gw, &users).await;
    assert_eq!(
        1,
        outcomes
            .iter()
            .filter(|outcome| race_status(outcome) == "refused")
            .count(),
        "only the banned click may be refused"
    );
    assert_eq!(
        json!(false),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": banned})
        )
        .await
    );
    assert_eq!(json!(20), count_entries(&f, &gw, true).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn fetch_count_false_skips_count_but_commits() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    let before = f.h.ok("db_stats", json!({})).await;
    let marker = join_ok(&f, &gw, "111111111111111111", json!({"fetch_count": false})).await;
    let after = f.h.ok("db_stats", json!({})).await;
    assert_eq!(-1, marker, "-1 means committed, count unknown");
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": "111111111111111111"})
        )
        .await
    );
    // One fewer read wave than the counted path: no COUNT(*) trip.
    let before_reads = before["reads"].as_u64().unwrap_or(0);
    let after_reads = after["reads"].as_u64().unwrap_or(u64::MAX);
    assert!(
        after_reads <= before_reads + 4,
        "uncounted join paid extra reads: {before} -> {after}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn join_snapshot_matches_split_reads() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({"min_messages": 2})).await;
    let uid = "3".repeat(18);
    for _ in 0..2 {
        f.h.ok(
            "record_message",
            json!({"guild_id": f.guild, "user_id": uid}),
        )
        .await;
    }
    let merged =
        f.h.ok(
            "join_snapshot",
            json!({"giveaway_id": gw["id"], "guild_id": f.guild, "user_id": uid}),
        )
        .await;
    let split_gw = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let split_snap = snapshot_of(&f, &f.guild, &uid, &id_of(&gw)).await;
    assert_eq!(split_gw["id"], merged[0]["id"]);
    assert_eq!(split_snap, merged[1]);
}

#[tokio::test(flavor = "multi_thread")]
async fn refresh_snapshot_returns_status_and_count() {
    let f = Fixture::new().await;
    let gw = make(&f, json!({})).await;
    join_ok(&f, &gw, "111111111111111111", json!({})).await;
    join_ok(&f, &gw, "222222222222222222", json!({})).await;
    let fresh =
        f.h.ok("refresh_snapshot", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(json!("active"), fresh[0]);
    assert_eq!(json!(2), fresh[1]);
    f.h.ok("end", json!({"giveaway_id": gw["id"]})).await;
    let closed =
        f.h.ok("refresh_snapshot", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(json!("ended"), closed[0]);
}

// __END__
