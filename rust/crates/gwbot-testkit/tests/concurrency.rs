//! Cross-process races: two bot processes, one SQLite file.
//!
//! The Python suite simulated a race with threads inside one event loop. The
//! Rust engine is what actually runs in production, where the auto-draw tick and
//! a slash command are *separate processes*, so these tests run each job on its
//! own thread and runtime over the same database file (WAL + a 10s busy timeout,
//! see `db.rs`), released together by a barrier.
//!
//! Each test asserts an invariant that holds no matter who wins the race. Never
//! "process A wins": the scheduler decides that, and a test that depends on it
//! is a flake generator.

use std::collections::HashSet;

use gwbot_testkit::{
    call_job, ended, join_batch_job, join_job, leave_batch_job, race_status, run_concurrently,
    CallError, Fixture, Harness, RaceJob, RaceResult,
};
use serde_json::{json, Value};

/// The statuses of a race, sorted so a test never depends on job order.
fn sorted_statuses(outcomes: &[RaceResult]) -> Vec<&'static str> {
    let mut statuses: Vec<&'static str> = outcomes.iter().map(race_status).collect();
    statuses.sort();
    statuses
}

/// The value a race job produced, panicking if it failed.
fn success(outcome: &RaceResult) -> &Value {
    outcome
        .as_ref()
        .unwrap_or_else(|err| panic!("expected a success, got {err}"))
}

/// The (message, kind) pair of a refused race job.
fn refusal_message(outcome: &RaceResult) -> (&str, &str) {
    match outcome {
        Err(CallError::Service(err)) => (err.message.as_str(), err.kind.as_str()),
        other => panic!("expected a refusal, got {other:?}"),
    }
}

/// The refusal among a two-job race, whichever side lost.
fn refusal(outcomes: &[RaceResult]) -> &RaceResult {
    outcomes
        .iter()
        .find(|outcome| outcome.is_err())
        .expect("expected one job to be refused")
}

/// End a draw whose claim window is open, so `end` seeds the claim rows.
async fn ended_with_claims(f: &Fixture, entrants: usize) -> (Value, Vec<String>) {
    let gw = f.gw(json!({"claim_timeout_seconds": 300})).await;
    let users: Vec<String> = (0..entrants)
        .map(|index| format!("97000000000000{index:04}"))
        .collect();
    for member in &users {
        f.enter(&gw, member).await;
    }
    let (_, winners) = f.end(&gw).await;
    assert_eq!(1, winners.len());
    (gw, winners)
}

/// The stored winner ids of a giveaway.
async fn stored_winners(f: &Fixture, gw: &Value) -> Vec<String> {
    f.h.ok("get", json!({"giveaway_id": gw["id"]})).await["winners"]
        .as_array()
        .expect("winners")
        .iter()
        .map(|item| item.as_str().unwrap_or_default().to_string())
        .collect()
}

// -- end races -----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn two_end_calls_cannot_both_draw() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"duration_seconds": 60})).await;
    f.enter(&gw, "900000000000000001").await;
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

    // The flip is a single guarded UPDATE, so exactly one caller stores a draw
    // and the other is told the giveaway is already over.
    assert_eq!(vec!["ok", "refused"], sorted_statuses(&outcomes));
    let ok = outcomes
        .iter()
        .find(|outcome| outcome.is_ok())
        .expect("one draw");
    let (ended_gw, winners) = ended(success(ok));
    assert_eq!("ended", ended_gw.status);
    assert_eq!(1, winners.len());
    assert_eq!(
        "This giveaway has already ended.",
        refusal_message(refusal(&outcomes)).0
    );

    let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(json!("ended"), stored["status"]);
    assert_eq!(json!(winners), stored["winners"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn parallel_ends_of_different_giveaways_all_succeed() {
    let f = Fixture::new().await;
    let mut giveaways = Vec::new();
    for _ in 0..4 {
        giveaways.push(f.gw(json!({})).await);
    }
    for (index, gw) in giveaways.iter().enumerate() {
        f.enter(gw, &format!("91000000000000{index:04}")).await;
    }
    let mut workers = Vec::new();
    for _ in 0..4 {
        workers.push(f.sibling().await);
    }

    let jobs: Vec<(&Harness, RaceJob)> = workers
        .iter()
        .zip(&giveaways)
        .map(|(worker, gw)| (worker, call_job("end", json!({"giveaway_id": gw["id"]}))))
        .collect();
    let outcomes = run_concurrently(jobs);

    assert_eq!(vec!["ok", "ok", "ok", "ok"], sorted_statuses(&outcomes));
    for gw in &giveaways {
        let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
        assert_eq!(json!("ended"), stored["status"]);
        assert_eq!(1, stored["winners"].as_array().expect("winners").len());
    }
}

// -- join races ----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn parallel_joins_never_duplicate_or_lose_entries() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let users: Vec<String> = (0..20)
        .map(|index| format!("92000000000000{index:04}"))
        .collect();
    let mut workers = Vec::new();
    for _ in 0..4 {
        workers.push(f.sibling().await);
    }
    let batches: Vec<Vec<String>> = users.chunks(5).map(|chunk| chunk.to_vec()).collect();

    let jobs: Vec<(&Harness, RaceJob)> = workers
        .iter()
        .zip(batches)
        .map(|(worker, batch)| (worker, join_batch_job(gw.clone(), batch)))
        .collect();
    let outcomes = run_concurrently(jobs);

    assert_eq!(vec!["ok", "ok", "ok", "ok"], sorted_statuses(&outcomes));
    let mut counts = Vec::new();
    for outcome in &outcomes {
        counts.extend(
            success(outcome)
                .as_array()
                .expect("counts")
                .iter()
                .map(|count| count.as_i64().expect("count")),
        );
    }
    assert!(counts.iter().all(|count| *count > 0));

    let rows =
        f.h.ok("entries", json!({"giveaway_id": gw["id"], "limit": 50}))
            .await;
    let rows = rows.as_array().expect("rows");
    assert_eq!(20, rows.len());
    let stored: HashSet<&str> = rows
        .iter()
        .map(|row| row["user_id"].as_str().expect("user_id"))
        .collect();
    let expected: HashSet<&str> = users.iter().map(String::as_str).collect();
    assert_eq!(expected, stored);
    assert_eq!(
        json!(20),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn same_user_joining_from_two_processes_enters_once() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let second = f.sibling().await;
    let member = "930000000000000001".to_string();

    let outcomes = run_concurrently(vec![
        (&f.h, join_job(gw.clone(), member.clone(), json!({}))),
        (&second, join_job(gw.clone(), member.clone(), json!({}))),
    ]);

    // The unique entry index turns the loser's INSERT into a refusal; it must
    // never read as a second successful join.
    assert_eq!(vec!["ok", "refused"], sorted_statuses(&outcomes));
    assert_eq!(
        "You are already entered.",
        refusal_message(refusal(&outcomes)).0
    );
    assert_eq!(1, f.entry_rows(gw["id"].as_str().expect("id")).await);
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
async fn blacklist_racing_a_join_leaves_no_entry_behind() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let member = "940000000000000001".to_string();
    let second = f.sibling().await;

    let outcomes = run_concurrently(vec![
        (&f.h, join_job(gw.clone(), member.clone(), json!({}))),
        (
            &second,
            call_job(
                "blacklist_add",
                json!({"guild_id": f.guild, "user_id": member, "hours": 0}),
            ),
        ),
    ]);

    // Either the ban lands first (join refused) or the join lands first and
    // blacklist_add purges it. A surviving entry would let a banned member win
    // a prize they were banned before.
    assert!(!outcomes
        .iter()
        .any(|outcome| race_status(outcome) == "error"));
    assert_eq!(0, f.entry_rows(gw["id"].as_str().expect("id")).await);
    assert_eq!(
        json!(0),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    assert_eq!(
        json!(true),
        f.h.ok(
            "is_blacklisted",
            json!({"guild_id": f.guild, "user_id": member})
        )
        .await
    );
}

// -- penalty and counter races -------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn two_simultaneous_clicks_cannot_stack_the_penalty() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let member = "950000000000000001".to_string();
    let second = f.sibling().await;

    let click = || join_job(gw.clone(), member.clone(), json!({"timed_out": true}));
    let outcomes = run_concurrently(vec![(&f.h, click()), (&second, click())]);

    assert_eq!(vec!["refused", "refused"], sorted_statuses(&outcomes));
    for outcome in &outcomes {
        let (message, kind) = refusal_message(outcome);
        assert_eq!("timeout_ban", kind);
        assert!(
            message.contains("timed out") || message.contains("penalty"),
            "unexpected refusal: {message}"
        );
    }

    // The click that starts the penalty records this giveaway as its trigger,
    // so the other click's consume finds the hit already there and spends
    // nothing. Both interleavings therefore leave exactly three.
    assert_eq!(
        json!(3),
        f.h.ok(
            "timeout_ban_remaining",
            json!({"guild_id": f.guild, "user_id": member})
        )
        .await
    );
    assert_eq!(
        json!(1),
        f.h.ok("count_timeout_bans", json!({"guild_id": f.guild}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn parallel_joins_and_leaves_keep_the_counter_honest() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let users: Vec<String> = (0..10)
        .map(|index| format!("96000000000000{index:04}"))
        .collect();
    let second = f.sibling().await;

    let joined = run_concurrently(vec![
        (&f.h, join_batch_job(gw.clone(), users[..5].to_vec())),
        (&second, join_batch_job(gw.clone(), users[5..].to_vec())),
    ]);
    assert_eq!(vec!["ok", "ok"], sorted_statuses(&joined));

    let left = run_concurrently(vec![
        (&f.h, leave_batch_job(json!(gw["id"]), users[..5].to_vec())),
        (
            &second,
            leave_batch_job(json!(gw["id"]), users[5..].to_vec()),
        ),
    ]);
    assert_eq!(vec!["ok", "ok"], sorted_statuses(&left));
    for outcome in &left {
        let flags = success(outcome).as_array().expect("flags");
        assert!(flags.iter().all(|flag| flag.as_bool().unwrap_or(false)));
    }

    assert_eq!(0, f.entry_rows(gw["id"].as_str().expect("id")).await);
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
        second
            .ok(
                "entry_count",
                json!({"giveaway_id": gw["id"], "exact": true})
            )
            .await
    );
}

// -- claim and reroll races ----------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn two_claims_settle_exactly_once() {
    let f = Fixture::new().await;
    let (gw, winners) = ended_with_claims(&f, 4).await;
    let winner = winners[0].clone();
    let second = f.sibling().await;

    let claim = || call_job("claim", json!({"giveaway_id": gw["id"], "user_id": winner}));
    let outcomes = run_concurrently(vec![(&f.h, claim()), (&second, claim())]);

    assert_eq!(vec!["ok", "refused"], sorted_statuses(&outcomes));
    assert_eq!(
        "This prize was already claimed.",
        refusal_message(refusal(&outcomes)).0
    );

    let statuses =
        f.h.ok("claim_status", json!({"giveaway_id": gw["id"]}))
            .await;
    let statuses: Vec<&str> = statuses
        .as_array()
        .expect("claim rows")
        .iter()
        .map(|row| row["status"].as_str().expect("status"))
        .collect();
    assert_eq!(vec!["claimed"], statuses);
    assert_eq!(
        json!([]),
        f.h.ok("pending_claims", json!({"giveaway_id": gw["id"]}))
            .await
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn parallel_rerolls_never_duplicate_or_drop_a_winner() {
    let f = Fixture::new().await;
    let (gw, first) = ended_with_claims(&f, 5).await;
    let second = f.sibling().await;

    let reroll = || call_job("reroll", json!({"giveaway_id": gw["id"], "count": 1}));
    let outcomes = run_concurrently(vec![(&f.h, reroll()), (&second, reroll())]);

    // Both may win the CAS race (append one each) or one may exhaust its
    // retries and refuse. Either way the stored list is a superset of the
    // original draw with no duplicate.
    assert!(
        outcomes.iter().any(|outcome| race_status(outcome) == "ok"),
        "at least one reroll must succeed"
    );
    let stored = stored_winners(&f, &gw).await;
    let unique: HashSet<&String> = stored.iter().collect();
    assert_eq!(unique.len(), stored.len(), "a winner was named twice");
    assert!(first.iter().all(|winner| stored.contains(winner)));
    assert!(stored.len() >= 2);
    assert!(stored.len() <= 3);
}

#[tokio::test(flavor = "multi_thread")]
async fn parallel_targeted_rerolls_replace_the_slot_once() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": 2})).await;
    let users: Vec<String> = (0..5)
        .map(|index| format!("98000000000000{index:04}"))
        .collect();
    for member in &users {
        f.enter(&gw, member).await;
    }
    let (_, winners) = f.end(&gw).await;
    assert_eq!(2, winners.len());
    let target = winners[0].clone();
    let second = f.sibling().await;

    let reroll = || {
        call_job(
            "reroll",
            json!({
                "giveaway_id": gw["id"],
                "count": 1,
                "replace_user_id": target,
            }),
        )
    };
    let outcomes = run_concurrently(vec![(&f.h, reroll()), (&second, reroll())]);

    // The second writer rebases onto the first swap, finds the target gone
    // and refuses: the slot is replaced once, never twice.
    assert_eq!(vec!["ok", "refused"], sorted_statuses(&outcomes));
    assert_eq!(
        "That member is not a winner of this giveaway.",
        refusal_message(refusal(&outcomes)).0
    );
    let stored = stored_winners(&f, &gw).await;
    assert_eq!(2, stored.len());
    let unique: HashSet<&String> = stored.iter().collect();
    assert_eq!(unique.len(), stored.len());
    assert!(!stored.contains(&target));
    assert!(stored.contains(&winners[1]));
}
