//! Failure recovery: half-applied writes, duplicate clicks, races and penalties.
//!
//! Every test starts from a fresh database and drives the real service. The
//! point is narrower than "the happy path works":
//!
//! * a flush that only half landed must be recoverable without double counting;
//! * a duplicate click (join, leave, end) must be refused, not applied twice;
//! * a race must leave exactly one durable outcome;
//! * the sweeps that run unattended must never eat the blacklist or a penalty.
//!
//! Nothing here re-implements a rule: assertions are about what the engine
//! stored, returned or refused. Time only moves through the manual clock, and
//! the cross-process races use [`run_concurrently`], because one process cannot
//! lose its own write.

use gwbot_testkit::{call_job, ended, race_status, run_concurrently, CallError, Fixture, Harness};
use serde_json::{json, Value};

const USER: &str = "100000000000000010";
const OTHER: &str = "100000000000000011";

/// Mirrors `MESSAGE_FLUSH_CHUNK` in gwbot-core; a chunk boundary is 300 rows.
const FLUSH_CHUNK: usize = 300;

/// Mirrors `TIMEOUT_PENALTY`: giveaways a timed-out member sits out.
const PENALTY: i64 = 3;

/// Mirrors `ENTRY_RETENTION_MS`: entry rows expire five hours after the end.
const RETENTION_MS: i64 = 5 * 60 * 60 * 1000;

/// Deterministic, unique user ids that never collide across tests.
fn ids(prefix: &str, count: usize) -> Vec<String> {
    (0..count)
        .map(|index| format!("{prefix}{index:013}"))
        .collect()
}

/// `[[guild, user, delta], ...]`, the shape `add_message_counts` takes.
fn rows(guild: &str, users: &[String], delta: i64) -> Value {
    Value::Array(
        users
            .iter()
            .map(|user| json!([guild, user, delta]))
            .collect(),
    )
}

async fn scalar_int(h: &Harness, statement: &str, params: Value) -> i64 {
    h.scalar_params(statement, params)
        .await
        .and_then(|value| value.as_i64())
        .expect("expected an integer scalar")
}

fn int_column(value: &Value, index: usize) -> Vec<i64> {
    value
        .as_array()
        .expect("expected a row list")
        .iter()
        .map(|row| row[index].as_i64().unwrap_or_default())
        .collect()
}

// -- partial flush -------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn chunk_boundaries_report_applied_and_before() {
    let f = Fixture::new().await;
    // 299/300/301 straddle the chunk size: the receipt counts rows applied
    // (not statements issued) and distinct keys snapshotted, both equal here.
    for size in [FLUSH_CHUNK - 1, FLUSH_CHUNK, FLUSH_CHUNK + 1] {
        let guild = format!("{}-{size}", f.guild);
        let users: Vec<String> = (0..size).map(|index| format!("31{index:014}")).collect();

        let receipt =
            f.h.ok(
                "add_message_counts",
                json!({ "rows": rows(&guild, &users, 1) }),
            )
            .await;

        assert_eq!(json!({"applied": size, "before": size}), receipt);
        assert_eq!(
            size as i64,
            scalar_int(
                &f.h,
                "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ?",
                json!([guild]),
            )
            .await
        );
        assert_eq!(
            size as i64,
            scalar_int(
                &f.h,
                "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?",
                json!([guild]),
            )
            .await
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn before_counts_distinct_keys_not_rows() {
    let f = Fixture::new().await;
    // Two rows for one key and one for another: three rows, two keys. The
    // receipt says two, and the deltas add up (2 + 3 = 5, never 3).
    let receipt =
        f.h.ok(
            "add_message_counts",
            json!({
                "rows": [
                    [f.guild, USER, 2],
                    [f.guild, USER, 3],
                    [f.other_guild, USER, 4],
                ],
            }),
        )
        .await;
    assert_eq!(json!({"applied": 3, "before": 2}), receipt);
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

    // A later flush only adds its own delta: counts are monotonic and are never
    // inflated by re-reading the same key twice.
    assert_eq!(
        json!({"applied": 1, "before": 1}),
        f.h.ok("add_message_counts", json!({"rows": [[f.guild, USER, 5]]}))
            .await
    );
    assert_eq!(
        json!(10),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_partial_flush_replays_only_the_missing_tail() {
    let f = Fixture::new().await;
    let users = ids("41", 400);
    let batch = rows(&f.guild, &users, 1);

    // Simulate a flush whose first chunk committed and whose second chunk never
    // ran: exactly what a PartialFlush leaves behind.
    let head =
        f.h.ok(
            "add_message_counts",
            json!({ "rows": Value::Array(batch.as_array().unwrap()[..FLUSH_CHUNK].to_vec()) }),
        )
        .await;
    assert_eq!(json!({"applied": FLUSH_CHUNK, "before": FLUSH_CHUNK}), head);

    // The caller reconciles the *whole* batch against the pre-flush snapshot
    // (every key was 0). Only the rows still missing come back.
    let before = rows(&f.guild, &users, 0);
    let shortfall =
        f.h.ok(
            "reconcile_message_counts",
            json!({ "rows": batch, "before": before }),
        )
        .await;
    assert_eq!(
        rows(&f.guild, &users[FLUSH_CHUNK..], 1),
        shortfall,
        "only the chunk that never ran is replayed"
    );

    let replay =
        f.h.ok("add_message_counts", json!({ "rows": shortfall }))
            .await;
    assert_eq!(
        json!({"applied": users.len() - FLUSH_CHUNK, "before": users.len() - FLUSH_CHUNK}),
        replay
    );

    // Every message counted exactly once: no loss, no double count.
    assert_eq!(
        users.len() as i64,
        scalar_int(
            &f.h,
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
    );
    assert_eq!(
        users.len() as i64,
        scalar_int(
            &f.h,
            "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
    );
    assert_eq!(
        0,
        scalar_int(
            &f.h,
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ? AND count > 1",
            json!([f.guild]),
        )
        .await
    );

    // Reconciling the finished batch again finds nothing left to send.
    assert_eq!(
        json!([]),
        f.h.ok(
            "reconcile_message_counts",
            json!({ "rows": batch, "before": rows(&f.guild, &users, 0) }),
        )
        .await
    );
}

// -- message count reconciliation ----------------------------------------

/// The pre-flush snapshot a worker captured: `[[guild, user, count], ...]`.
async fn snapshot(f: &Fixture, users: &[String]) -> Value {
    let stored =
        f.h.sql_params(
            "SELECT user_id, count FROM simple_message_counts WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await;
    let mut by_user = std::collections::BTreeMap::new();
    for row in stored.as_array().expect("row list") {
        by_user.insert(
            row["user_id"].as_str().unwrap_or_default().to_string(),
            row["count"].as_i64().unwrap_or_default(),
        );
    }
    Value::Array(
        users
            .iter()
            .map(|user| json!([f.guild, user, by_user.get(user).copied().unwrap_or(0)]))
            .collect(),
    )
}

#[tokio::test(flavor = "multi_thread")]
async fn a_finished_worker_leaves_no_tail_to_requeue() {
    let f = Fixture::new().await;
    let users = ids("42", 5);
    let seeded: Vec<Value> = users
        .iter()
        .enumerate()
        .map(|(index, user)| json!([f.guild, user, index as i64 + 1]))
        .collect();
    f.h.ok("add_message_counts", json!({ "rows": seeded }))
        .await;
    let before = snapshot(&f, &users).await;
    assert_eq!(vec![1, 2, 3, 4, 5], int_column(&before, 2));

    // The abandoned worker went on to commit the whole batch (+2 each).
    let batch = rows(&f.guild, &users, 2);
    f.h.ok("add_message_counts", json!({ "rows": batch })).await;

    // Reconciling against the snapshot it captured must find nothing: the
    // caller hands back an empty tail instead of re-sending the batch.
    assert_eq!(
        json!([]),
        f.h.ok(
            "reconcile_message_counts",
            json!({ "rows": batch, "before": before }),
        )
        .await
    );
    let mut counts = Vec::new();
    for user in &users {
        counts.push(
            f.h.ok(
                "message_count",
                json!({"guild_id": f.guild, "user_id": user}),
            )
            .await
            .as_i64()
            .unwrap(),
        );
    }
    assert_eq!(vec![3, 4, 5, 6, 7], counts);
}

#[tokio::test(flavor = "multi_thread")]
async fn only_the_shortfall_of_each_key_is_requeued() {
    let f = Fixture::new().await;
    let users = ids("43", 3);
    let full = users[0].clone();
    let partial = users[1].clone();
    let untouched = users[2].clone();
    f.h.ok(
        "add_message_counts",
        json!({ "rows": rows(&f.guild, &users, 1) }),
    )
    .await;
    let before = snapshot(&f, &users).await;

    // Each key is due +4. One landed completely, one landed half way, one never
    // landed at all -- the three shapes a lost response can take.
    let batch = rows(&f.guild, &users, 4);
    f.h.ok(
        "add_message_counts",
        json!({ "rows": [[f.guild, full, 4]] }),
    )
    .await;
    f.execute_params(
        "UPDATE simple_message_counts SET count = count + 2 \
         WHERE guild_id = ? AND user_id = ?",
        json!([f.guild, partial]),
    )
    .await;

    let shortfall =
        f.h.ok(
            "reconcile_message_counts",
            json!({ "rows": batch, "before": before }),
        )
        .await;

    // full: 1 + 4 - 5 = 0; partial: 1 + 4 - 3 = 2; untouched: 1 + 4 - 1 = 4.
    assert_eq!(
        json!([[f.guild, partial, 2], [f.guild, untouched, 4]]),
        shortfall
    );
    f.h.ok("add_message_counts", json!({ "rows": shortfall }))
        .await;

    // Every key ends at before + delta, exactly once.
    let mut counts = Vec::new();
    for user in &users {
        counts.push(
            f.h.ok(
                "message_count",
                json!({"guild_id": f.guild, "user_id": user}),
            )
            .await
            .as_i64()
            .unwrap(),
        );
    }
    assert_eq!(vec![5, 5, 5], counts);
    assert_eq!(
        15,
        scalar_int(
            &f.h,
            "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
    );
}

// -- duplicate clicks ----------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn double_tap_joins_once() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;

    assert_eq!(1, f.enter(&gw, USER).await);
    let entered_at =
        f.h.scalar_params(
            "SELECT entered_at FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
            json!([gw["id"], USER]),
        )
        .await
        .expect("entered_at");

    // The second tap is refused by the duplicate-entry guard, not silently
    // merged into a second row or a second counter bump.
    assert_eq!(
        "You are already entered.",
        f.join_error(&gw, USER).await.message
    );

    assert_eq!(1, f.entry_rows(gw["id"].as_str().unwrap()).await);
    assert_eq!(
        json!(1),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        json!(1),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("entrant_count")
    );
    assert_eq!(
        json!(entered_at),
        f.h.scalar_params(
            "SELECT entered_at FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
            json!([gw["id"], USER]),
        )
        .await
        .expect("entered_at")
    );
    assert_eq!(
        1,
        f.h.ok("entries", json!({"giveaway_id": gw["id"]}))
            .await
            .as_array()
            .unwrap()
            .len()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_different_member_is_unaffected_by_the_refusal() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.join_error(&gw, USER).await;

    // A refusal for one member must not poison the next one's entry.
    assert_eq!(2, f.enter(&gw, OTHER).await);
    assert_eq!(2, f.entry_rows(gw["id"].as_str().unwrap()).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn leaving_twice_removes_once() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;

    assert_eq!(
        json!(true),
        f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
            .await
    );
    // "not entered" is the same answer the button gives a member who never
    // joined: a second leave is a no-op, not an error and not a second bump.
    assert_eq!(
        json!(false),
        f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
            .await
    );
    assert_eq!(
        json!([false, 0, ""]),
        f.h.ok(
            "leave_with_count",
            json!({"giveaway_id": gw["id"], "user_id": USER}),
        )
        .await
    );

    assert_eq!(0, f.entry_rows(gw["id"].as_str().unwrap()).await);
    assert_eq!(
        json!(0),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        json!(false),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": USER})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_repeated_leave_cannot_underflow_the_counter() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;

    assert_eq!(
        json!(true),
        f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
            .await
    );
    for _ in 0..3 {
        assert_eq!(
            json!(false),
            f.h.ok("leave", json!({"giveaway_id": gw["id"], "user_id": USER}))
                .await
        );
    }

    // GREATEST(0, ...) in the DELETE path: the counter is not a signed ledger,
    // so a hammered Leave button cannot drive it below zero.
    assert_eq!(
        json!(1),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        json!(1),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("entrant_count")
    );
    assert_eq!(
        json!(true),
        f.h.ok(
            "has_entry",
            json!({"giveaway_id": gw["id"], "user_id": OTHER})
        )
        .await
    );
}

// -- end / reroll races --------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn two_ends_transition_once() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    let entrants = ids("51", 3);
    for member in &entrants {
        f.enter(&gw, member).await;
    }
    let now = f.h.advance(61_000);
    let second = f.sibling().await;

    let outcomes = run_concurrently(vec![
        (
            &f.h,
            call_job(
                "end",
                json!({"giveaway_id": gw["id"], "now": now, "require_due": true}),
            ),
        ),
        (
            &second,
            call_job(
                "end",
                json!({"giveaway_id": gw["id"], "now": now, "require_due": true}),
            ),
        ),
    ]);

    // One guarded UPDATE: exactly one caller stores a draw, the other is told
    // the giveaway is already over.
    let mut statuses: Vec<&str> = outcomes.iter().map(race_status).collect();
    statuses.sort();
    assert_eq!(vec!["ok", "refused"], statuses);

    let (ok_value, refused) = match (&outcomes[0], &outcomes[1]) {
        (Ok(value), Err(CallError::Service(err))) => (value, err),
        (Err(CallError::Service(err)), Ok(value)) => (value, err),
        other => panic!("expected one draw and one refusal, got {other:?}"),
    };
    let (ended_gw, winners) = ended(ok_value);
    assert_eq!("This giveaway has already ended.", refused.message);
    assert_eq!("ended", ended_gw.status);
    assert_eq!(1, winners.len());
    assert!(entrants.contains(&winners[0]));

    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("ended"), stored["status"]);
    assert_eq!(json!(winners), stored["winners"]);
    assert_eq!(json!(3), stored["entrant_count"]);
    let raw =
        f.h.scalar_params(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("winners_json");
    assert_eq!(
        json!(winners),
        serde_json::from_str::<Value>(raw.as_str().expect("text")).expect("json")
    );
    assert_eq!(
        now,
        scalar_int(
            &f.h,
            "SELECT ended_at FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn end_after_end_keeps_the_first_draw() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    let (ended_gw, winners) = f.end(&gw).await;
    let raw_before =
        f.h.scalar_params(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("winners_json");
    let ended_at =
        f.h.scalar_params(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("ended_at");

    for _ in 0..2 {
        let err = f.end_error(&gw).await;
        assert_eq!("This giveaway has already ended.", err.message);
    }

    // The loser of the race must not rewrite the winner set it lost to.
    assert_eq!(json!(winners), json!(ended_gw.winners));
    assert_eq!(
        raw_before,
        f.h.scalar_params(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("winners_json")
    );
    assert_eq!(
        ended_at,
        f.h.scalar_params(
            "SELECT ended_at FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("ended_at")
    );
    assert_eq!(
        json!(1),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn reroll_racing_end_never_duplicates_a_winner() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 1})).await;
    let entrants = ids("52", 4);
    for member in &entrants {
        f.enter(&gw, member).await;
    }
    let second = f.sibling().await;

    let outcomes = run_concurrently(vec![
        (&f.h, call_job("end", json!({"giveaway_id": gw["id"]}))),
        (
            &second,
            call_job("reroll", json!({"giveaway_id": gw["id"], "count": 1})),
        ),
    ]);

    // end always wins its own guarded flip; the reroll either read the row
    // while it was still active (refused) or rebased onto the stored draw.
    assert_eq!("ok", race_status(&outcomes[0]));
    let (_, end_winners) = ended(outcomes[0].as_ref().expect("end succeeded"));
    let fresh = match &outcomes[1] {
        Ok(value) => ended(value).1,
        Err(CallError::Service(err)) => {
            assert!(
                matches!(
                    err.message.as_str(),
                    "End the giveaway before rerolling."
                        | "Reroll collided with another draw. Try again."
                ),
                "unexpected reroll refusal: {}",
                err.message
            );
            Vec::new()
        }
        Err(other) => panic!("reroll failed unexpectedly: {other}"),
    };
    assert!(matches!(race_status(&outcomes[1]), "ok" | "refused"));

    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    let stored_winners: Vec<String> = stored["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .map(|item| item.as_str().unwrap_or_default().to_string())
        .collect();
    let unique: std::collections::HashSet<&String> = stored_winners.iter().collect();
    assert_eq!(
        unique.len(),
        stored_winners.len(),
        "a winner was named twice"
    );
    assert!(end_winners
        .iter()
        .all(|winner| stored_winners.contains(winner)));
    assert!(fresh.iter().all(|winner| stored_winners.contains(winner)));
    assert!(stored_winners.len() <= 2);
    assert_eq!(
        json!("ended"),
        f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await
    );
    assert_eq!(
        json!(4),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
}

// -- extend vs end -------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn a_stale_due_snapshot_loses_to_extend() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    f.enter(&gw, USER).await;
    let now = f.h.advance(61_000);
    // The row the auto-draw tick read before the extend landed.
    let due = f.h.ok("due", json!({"now": now})).await;
    let stale = due[0].clone();
    assert_eq!(gw["id"], stale["id"]);

    let extended =
        f.h.ok(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": 600}),
        )
        .await;

    let err = f
        .end_error_with(
            &stale,
            json!({"now": now, "require_due": true, "known": stale}),
        )
        .await;
    assert_eq!("This giveaway is not due to end yet.", err.message);

    // The extend is not overwritten: still running, no winners, new deadline.
    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("active"), stored["status"]);
    assert_eq!(json!([]), stored["winners"]);
    assert_eq!(extended["ends_at"], stored["ends_at"]);
    assert_eq!(
        json!(gw["ends_at"].as_i64().expect("ends_at") + 600_000),
        stored["ends_at"]
    );
    assert_eq!(
        json!("[]"),
        f.h.scalar_params(
            "SELECT winners_json FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .expect("winners_json")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn the_guarded_flip_rechecks_the_deadline() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    let now = f.h.advance(61_000);
    // A copy whose ends_at is already in the past: every pre-check passes, so
    // only the deadline inside the UPDATE can refuse this call.
    let mut stale = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    stale["ends_at"] = json!(now - 1);
    f.h.ok(
        "extend",
        json!({"giveaway_id": gw["id"], "extra_seconds": 60}),
    )
    .await;

    let err = f
        .end_error_with(
            &stale,
            json!({"now": now, "require_due": true, "known": stale}),
        )
        .await;
    assert_eq!("This giveaway is not due to end yet.", err.message);
    assert_eq!(
        json!("active"),
        f.h.ok("get_status", json!({"giveaway_id": gw["id"]})).await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn extend_after_end_refuses_and_keeps_the_draw() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    let (_, winners) = f.end(&gw).await;

    let err =
        f.h.refuse(
            "extend",
            json!({"giveaway_id": gw["id"], "extra_seconds": 60}),
        )
        .await;
    assert_eq!("Only a running giveaway can be extended.", err.message);
    assert_eq!(
        json!(winners),
        f.h.ok("get", json!({"giveaway_id": gw["id"]})).await["winners"]
    );
}

// -- claim expiry --------------------------------------------------------

/// A winner list as the ids it names, for comparing claim rows.
fn claim_users(rows: &Value) -> Vec<String> {
    rows.as_array()
        .expect("expected a claim row list")
        .iter()
        .map(|row| row["user_id"].as_str().unwrap_or_default().to_string())
        .collect()
}

/// One column of the claim rows, keyed by user id.
fn claim_map(rows: &Value, field: &str) -> std::collections::HashMap<String, Value> {
    rows.as_array()
        .expect("expected a claim row list")
        .iter()
        .map(|row| {
            (
                row["user_id"].as_str().unwrap_or_default().to_string(),
                row[field].clone(),
            )
        })
        .collect()
}

/// End a draw whose claim window is open, so `end` seeds the claim rows.
async fn draw_with_claims(f: &Fixture, entrants: usize) -> (Value, Vec<String>, Vec<String>) {
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    let users = ids("61", entrants);
    for member in &users {
        f.enter(&gw, member).await;
    }
    let (_, winners) = f.end(&gw).await;
    assert_eq!(1, winners.len());
    (gw, winners, users)
}

#[tokio::test(flavor = "multi_thread")]
async fn expiry_replaces_the_winner_and_settles_the_row() {
    let f = Fixture::new().await;
    let (gw, winners, users) = draw_with_claims(&f, 4).await;
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(vec![winners[0].clone()], claim_users(&pending));
    let deadline = pending[0]["deadline_ms"].as_i64().expect("deadline_ms");

    // Too early: the guarded UPDATE matches nothing and draws no one.
    let err =
        f.h.refuse(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline - 1}),
        )
        .await;
    assert_eq!("That claim is no longer pending.", err.message);

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
    assert_eq!(2, ended_gw.winners.len());
    let unique: std::collections::HashSet<&String> = ended_gw.winners.iter().collect();
    assert_eq!(unique.len(), ended_gw.winners.len());
    assert!(ended_gw.winners.contains(&winners[0]));
    assert!(ended_gw.winners.contains(&replacements[0]));

    let statuses =
        f.h.ok("claim_status", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(
        2,
        statuses.as_array().expect("claim rows").len(),
        "one claim row per drawn member, no duplicates"
    );
    let by_user = claim_map(&statuses, "status");
    let deadlines = claim_map(&statuses, "deadline_ms");
    assert_eq!(json!("expired"), by_user[winners[0].as_str()].clone());
    assert_eq!(json!("pending"), by_user[replacements[0].as_str()].clone());
    assert_eq!(
        vec![replacements[0].clone()],
        claim_users(
            &f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
                .await
        )
    );
    // The replacement's window starts at the expiry, not at the draw.
    assert_eq!(
        json!(deadline + 1 + 300_000),
        deadlines[replacements[0].as_str()].clone()
    );

    // The expired slot is closed; the replacement can still claim.
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

    // Expiring the same slot twice is refused rather than drawing twice.
    let err =
        f.h.refuse(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline + 10_000}),
        )
        .await;
    assert_eq!("That claim is no longer pending.", err.message);
    assert_eq!(
        2,
        f.h.ok("claim_status", json!({"giveaway_id": gw["id"]}))
            .await
            .as_array()
            .expect("claim rows")
            .len()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_expired_claim_survives_a_restart() {
    let mut f = Fixture::new().await;
    let (gw, winners, _) = draw_with_claims(&f, 4).await;
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    let deadline = pending[0]["deadline_ms"].as_i64().expect("deadline_ms");
    let ended_at = scalar_int(
        &f.h,
        "SELECT ended_at FROM simple_giveaways WHERE id = ?",
        json!([gw["id"]]),
    )
    .await;
    assert_eq!(ended_at + 300_000, deadline);

    let (_, replacements) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline + 1}),
        )
        .await,
    );
    assert_eq!(1, replacements.len());

    // A restart must not resurrect the expired slot nor lose the replacement:
    // both rows are durable, and the pending one is still pending.
    f.reopen().await;

    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(vec![replacements[0].clone()], claim_users(&pending));
    assert_eq!(json!(deadline + 1 + 300_000), pending[0]["deadline_ms"]);
    let statuses =
        f.h.ok("claim_status", json!({"giveaway_id": gw["id"]}))
            .await;
    let by_user = claim_map(&statuses, "status");
    assert_eq!(json!("expired"), by_user[winners[0].as_str()].clone());
    assert_eq!(json!("pending"), by_user[replacements[0].as_str()].clone());
    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await["winners"].clone();
    let stored = stored.as_array().expect("winners");
    assert_eq!(2, stored.len());
    let unique: std::collections::HashSet<&str> = stored
        .iter()
        .map(|item| item.as_str().unwrap_or_default())
        .collect();
    assert_eq!(unique.len(), stored.len());

    // The tick finds it due and can expire it into a fresh replacement.
    let due =
        f.h.ok("due_claims", json!({"now": deadline + 1 + 300_000}))
            .await;
    assert_eq!(vec![replacements[0].clone()], claim_users(&due));
}

#[tokio::test(flavor = "multi_thread")]
async fn expiry_with_an_exhausted_pool_draws_nothing() {
    let f = Fixture::new().await;
    let (gw, winners, _) = draw_with_claims(&f, 1).await;
    let pending =
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await;
    let deadline = pending[0]["deadline_ms"].as_i64().expect("deadline_ms");

    let (ended_gw, replacements) = ended(
        &f.h.ok(
            "expire_claim",
            json!({"giveaway_id": gw["id"], "user_id": winners[0], "now": deadline + 1}),
        )
        .await,
    );

    assert_eq!(Vec::<String>::new(), replacements);
    assert_eq!(winners, ended_gw.winners);
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
    let statuses =
        f.h.ok("claim_status", json!({"giveaway_id": gw["id"]}))
            .await;
    assert_eq!(1, statuses.as_array().expect("claim rows").len());
    assert_eq!(json!("expired"), statuses[0]["status"]);
}

// -- timeout penalty -----------------------------------------------------

/// Giveaways this member still has to sit out.
async fn remaining(f: &Fixture, user_id: &str) -> i64 {
    f.h.ok(
        "timeout_ban_remaining",
        json!({"guild_id": f.guild, "user_id": user_id}),
    )
    .await
    .as_i64()
    .expect("timeout_ban_remaining")
}

/// One hit row per giveaway the member was blocked from.
async fn hits(f: &Fixture, user_id: &str) -> i64 {
    scalar_int(
        &f.h,
        "SELECT COUNT(*) FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ?",
        json!([f.guild, user_id]),
    )
    .await
}

#[tokio::test(flavor = "multi_thread")]
async fn the_penalty_decrements_to_zero_and_stops() {
    let f = Fixture::new().await;
    let trigger = f.gw(json!({})).await;
    let elsewhere = f.gw(json!({})).await;
    f.enter(&trigger, USER).await;
    f.enter(&elsewhere, USER).await;

    let error = f.timed_join_error(&trigger, USER).await;
    assert_eq!("timeout_ban", error.kind);
    assert_eq!(PENALTY, remaining(&f, USER).await);
    // The timeout drops every entry the member already held.
    assert_eq!(0, f.entry_rows(trigger["id"].as_str().expect("id")).await);
    assert_eq!(0, f.entry_rows(elsewhere["id"].as_str().expect("id")).await);
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": trigger["id"]}))
            .await
    );
    assert_eq!(
        1,
        hits(&f, USER).await,
        "the trigger giveaway is counted, not spent"
    );

    let blocked = vec![
        f.gw(json!({})).await,
        f.gw(json!({})).await,
        f.gw(json!({})).await,
    ];
    let mut left = Vec::new();
    for gw in &blocked {
        // No timed_out flag here: the penalty row alone keeps refusing.
        let err = f.join_error(gw, USER).await;
        assert!(
            err.message.contains("penalty"),
            "unexpected refusal: {}",
            err.message
        );
        assert_eq!(0, f.entry_rows(gw["id"].as_str().expect("id")).await);
        left.push(remaining(&f, USER).await);
    }

    assert_eq!(
        vec![2, 1, 0],
        left,
        "exactly one giveaway per blocked attempt"
    );
    assert_eq!(
        PENALTY + 1,
        hits(&f, USER).await,
        "one hit row per giveaway sat out"
    );
    // At zero the row is deleted rather than left behind as a listing entry.
    assert_eq!(
        json!([]),
        f.h.ok("list_timeout_bans", json!({"guild_id": f.guild}))
            .await
    );
    assert_eq!(
        0,
        scalar_int(
            &f.h,
            "SELECT COUNT(*) FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?",
            json!([f.guild, USER]),
        )
        .await
    );

    // The giveaway that spent the last unit still refuses: rejoining it would
    // hand the penalty straight back.
    let error = f.join_error(blocked.last().expect("blocked"), USER).await;
    assert_eq!("timeout_ban", error.kind);
    assert!(error.message.contains("sat out this giveaway"));
    assert_eq!(0, remaining(&f, USER).await);

    // A giveaway never seen before is enterable again.
    assert_eq!(1, f.enter(&f.gw(json!({})).await, USER).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_lifted_penalty_can_start_again() {
    let f = Fixture::new().await;
    let trigger = f.gw(json!({})).await;
    f.timed_join_error(&trigger, USER).await;
    for _ in 0..PENALTY {
        f.join_error(&f.gw(json!({})).await, USER).await;
    }
    assert_eq!(0, remaining(&f, USER).await);
    assert_eq!(PENALTY + 1, hits(&f, USER).await);

    // Being caught timed out again starts a fresh penalty; the remembered
    // giveaways are not repaid and nothing stacks.
    let again = f.gw(json!({})).await;
    assert_eq!("timeout_ban", f.timed_join_error(&again, USER).await.kind);
    assert_eq!(PENALTY, remaining(&f, USER).await);
    assert_eq!(PENALTY + 2, hits(&f, USER).await);

    // Same giveaway, still timed out: the hit row is already there, so the
    // penalty is neither restacked nor spent twice.
    for _ in 0..3 {
        let err = f.timed_join_error(&again, USER).await;
        assert!(
            err.message.contains("3 giveaway(s) left"),
            "unexpected refusal: {}",
            err.message
        );
    }
    assert_eq!(PENALTY, remaining(&f, USER).await);
    assert_eq!(PENALTY + 2, hits(&f, USER).await);
}

// -- stale entry wipe ----------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn the_wipe_never_touches_the_blacklist_or_a_penalty() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.enter(&gw, OTHER).await;
    f.end(&gw).await;
    f.execute_params(
        "UPDATE simple_giveaways SET ended_at = ? WHERE id = ?",
        json!([f.h.now() - RETENTION_MS - 1000, gw["id"]]),
    )
    .await;
    // Both rows are seeded through the service so the test asserts the real
    // write path, not a hand-written INSERT the service would never produce.
    f.h.ok(
        "blacklist_add",
        json!({"guild_id": f.guild, "user_id": USER}),
    )
    .await;
    f.h.ok(
        "apply_timeout_penalty",
        json!({
            "guild_id": f.guild,
            "user_id": OTHER,
            "giveaways": 2,
            "giveaway_id": gw["id"],
        }),
    )
    .await;

    assert_eq!(json!(2), f.h.ok("wipe_stale_entries", Value::Null).await);

    assert_eq!(0, f.entry_rows(gw["id"].as_str().expect("id")).await);
    assert_eq!(
        json!(0),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        0,
        scalar_int(
            &f.h,
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
    );
    // The retention sweep is entries-only, by design: a blacklist row and a
    // penalty are not stale data, they are live moderation state.
    assert_eq!(
        json!(true),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        1,
        scalar_int(
            &f.h,
            "SELECT COUNT(*) FROM simple_blacklist WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
    );
    assert_eq!(
        json!(2),
        f.h.ok(
            "timeout_ban_remaining",
            json!({"guild_id": f.guild, "user_id": OTHER})
        )
        .await
    );
    assert_eq!(
        1,
        scalar_int(
            &f.h,
            "SELECT COUNT(*) FROM simple_giveaway_bans WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
    );
    assert_eq!(
        1,
        scalar_int(
            &f.h,
            "SELECT COUNT(*) FROM simple_giveaway_ban_hits WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
    );

    // A second sweep is a no-op and still leaves both tables alone.
    assert_eq!(json!(0), f.h.ok("wipe_stale_entries", Value::Null).await);
    assert_eq!(
        json!(true),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": USER})
        )
        .await
    );
    assert_eq!(
        json!(2),
        f.h.ok(
            "timeout_ban_remaining",
            json!({"guild_id": f.guild, "user_id": OTHER})
        )
        .await
    );
}

// -- cache invalidation --------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn flush_is_a_safe_noop_without_redis() {
    let f = Fixture::new().await;
    assert_eq!(
        Value::Null,
        f.h.ok("cache_get", json!({"key": "gw:missing"})).await
    );
    assert_eq!(
        Value::Null,
        f.h.ok("cache_get_int", json!({"key": "count:missing"}))
            .await
    );
    // Neither of these may raise: every write path calls them unconditionally.
    f.h.ok(
        "cache_put",
        json!({"key": "gw:x", "value": {"id": "x"}, "ttl": 8.0}),
    )
    .await;
    f.h.ok("cache_drop", json!({"suffix": "gw:x"})).await;

    f.h.ok(
        "mark_stale",
        json!({"suffixes": ["gw:x", "guild:g:active"]}),
    )
    .await;
    assert_eq!(json!([]), f.h.ok("drain_invalidations", Value::Null).await);
    assert_eq!(json!(0), f.h.ok("flush_invalidations", Value::Null).await);
    assert_eq!(json!(0), f.h.ok("flush_invalidations", Value::Null).await);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_real_write_path_flushes_safely() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    f.enter(&gw, USER).await;
    f.h.ok(
        "set_notify_role",
        json!({"guild_id": f.guild, "role_id": "r1"}),
    )
    .await;
    f.end(&gw).await;

    // create/join/end/set_notify_role all queue invalidations internally; with
    // no cache the queue stays empty and the flush still answers zero.
    assert_eq!(json!(0), f.h.ok("flush_invalidations", Value::Null).await);
    assert_eq!(
        Value::Null,
        f.h.ok(
            "cache_get",
            json!({"key": format!("count:{}", gw["id"].as_str().expect("id"))}),
        )
        .await
    );
}

// __END__
