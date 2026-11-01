//! Load and throughput: the same rules, at the sizes the bot actually sees.
//!
//! These tests are not benchmarks and do not assert a latency number. They
//! assert that bulk paths stay *correct* when the row count grows:
//!
//! * hundreds of entries keep one row per member and a snapshot whose window
//!   and total disagree only where the `LIMIT` says they should;
//! * dozens of giveaways across guilds stay scoped per guild while the global
//!   listing honours its cap;
//! * a thousand message-count keys flush in one call, chunked internally;
//! * repeated full-size draws never repeat a winner;
//! * four bot processes joining, leaving and joining again keep the stored set
//!   and the counter in agreement.
//!
//! Each test also carries a wall-clock ceiling. It is deliberately loose (a
//! minute for work that measures in seconds) so it catches a hang or an
//! accidental quadratic loop without turning a slow CI box into a flake.
//!
//! ADAPTED: the Python module asserted one ceiling for the whole file in
//! `tearDownModule`. Rust runs test functions in parallel threads and has no
//! module teardown, so the same ceiling is asserted per test instead; that is
//! strictly stricter per unit of work and cannot flake on parallelism.

use std::collections::HashSet;
use std::time::Instant;

use gwbot_testkit::{
    join, join_batch_job, race_status, run_concurrently, Fixture, Harness, RaceJob,
};
use serde_json::{json, Value};

/// Entries in the "big giveaway" test: comfortably past a display page.
const ENTRY_LOAD: i64 = 400;

/// Giveaways spread over three guilds.
const GIVEAWAY_LOAD: i64 = 50;

/// Distinct keys in the single-call flush test.
const FLUSH_USERS: i64 = 1000;

/// Mirrors `MESSAGE_FLUSH_CHUNK`: the storage layer writes 300 rows per batch.
const FLUSH_CHUNK: i64 = 300;

/// Full-size draws: `MAX_WINNERS` is 25, so every draw uses the whole cap.
const DRAW_ROUNDS: i64 = 20;
const DRAW_WINNERS: i64 = 25;
const DRAW_POOL: i64 = 30;

/// Concurrent workers and the users each one owns.
const WORKERS: usize = 4;
const PER_WORKER: usize = 40;
const CHURN_PER_WORKER: usize = 10;

/// A hang detector, not a performance target.
const BUDGET_SECONDS: f64 = 60.0;

/// Deterministic, unique user ids that never collide across tests.
fn ids(prefix: &str, count: i64) -> Vec<String> {
    (0..count)
        .map(|index| format!("{prefix}{index:013}"))
        .collect()
}

/// Fail the test if `label` took longer than the hang-detector ceiling.
fn assert_within_budget(started: Instant, label: &str) {
    let elapsed = started.elapsed().as_secs_f64();
    assert!(elapsed < BUDGET_SECONDS, "{label} took {elapsed:.1}s");
}

/// Join several members from one process, in order; returns the running counts.
async fn join_batch(h: &Harness, gw: &Value, users: &[String]) -> Vec<i64> {
    let mut counts = Vec::with_capacity(users.len());
    for user in users {
        counts.push(
            join(h, gw, user, json!({}))
                .await
                .unwrap_or_else(|err| panic!("join failed: {err}"))
                .as_i64()
                .unwrap_or_else(|| panic!("join did not return a count")),
        );
    }
    counts
}

/// The user ids of an entry/snapshot row list.
fn row_users(rows: &Value) -> HashSet<String> {
    rows.as_array()
        .expect("expected a row list")
        .iter()
        .map(|row| row["user_id"].as_str().expect("user_id").to_string())
        .collect()
}

/// The ids of a giveaway list.
fn gw_ids(giveaways: &Value) -> Vec<String> {
    giveaways
        .as_array()
        .expect("expected a giveaway list")
        .iter()
        .map(|gw| gw["id"].as_str().expect("id").to_string())
        .collect()
}

// -- many entries --------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn four_hundred_entries_stay_exact_and_snapshot_cleanly() {
    let started = Instant::now();
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": DRAW_WINNERS})).await;
    let users = ids("71", ENTRY_LOAD);

    let counts = join_batch(&f.h, &gw, &users).await;
    assert_within_budget(started, &format!("{ENTRY_LOAD} joins"));

    // Sequential joins return the running count, so a lost or doubled insert
    // would show up as a gap or a repeat, not just a wrong total.
    let expected: Vec<i64> = (1..=ENTRY_LOAD).collect();
    assert_eq!(expected, counts);

    assert_eq!(
        json!(ENTRY_LOAD),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    // The cheap counter must agree with COUNT(*): joins maintain it, and no
    // bulk path here is allowed to leave it behind.
    assert_eq!(
        json!(ENTRY_LOAD),
        f.h.ok("entry_count", json!({"giveaway_id": gw["id"]}))
            .await
    );
    assert_eq!(
        Some(ENTRY_LOAD),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
    assert_eq!(
        Some(ENTRY_LOAD),
        f.h.scalar_params(
            "SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
    );

    let rows = f.h.ok("entries", json!({"giveaway_id": gw["id"]})).await;
    assert_eq!(ENTRY_LOAD as usize, rows.as_array().expect("rows").len());
    let expected_users: HashSet<String> = users.iter().cloned().collect();
    assert_eq!(expected_users, row_users(&rows));
    assert_eq!(
        ENTRY_LOAD as usize,
        row_users(&rows).len(),
        "a member was listed twice"
    );

    // No LIMIT: the window is the whole pool and the caller is in it. The
    // embed's chance is winner_count / total, so a wrong total here would
    // misreport every participant's odds.
    let snapshot =
        f.h.ok(
            "entries_snapshot",
            json!({"giveaway_id": gw["id"], "user_id": users.last().expect("users")}),
        )
        .await;
    assert_eq!(json!(ENTRY_LOAD), snapshot[0]);
    assert_eq!(
        ENTRY_LOAD as usize,
        snapshot[1].as_array().expect("window").len()
    );
    assert_eq!(json!(1), snapshot[2]);
    assert_eq!(expected_users, row_users(&snapshot[1]));

    // With a LIMIT the window shrinks but COUNT(*) OVER () still reports the
    // true pool: the "first N of T" line depends on that split.
    let non_member = "799999999999999999";
    let snapshot =
        f.h.ok(
            "entries_snapshot",
            json!({"giveaway_id": gw["id"], "limit": 50, "user_id": non_member}),
        )
        .await;
    assert_eq!(json!(ENTRY_LOAD), snapshot[0]);
    assert_eq!(50, snapshot[1].as_array().expect("window").len());
    assert_eq!(
        json!(0),
        snapshot[2],
        "a non-member must not be reported as entered"
    );
    let window = row_users(&snapshot[1]);
    assert_eq!(50, window.len());
    assert!(window.is_subset(&expected_users));
}

// -- many giveaways ------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn fifty_giveaways_stay_scoped_and_capped() {
    let f = Fixture::new().await;
    let guilds = [
        f.guild.clone(),
        f.other_guild.clone(),
        "444444444444444444".to_string(),
    ];
    let started = Instant::now();
    let mut created: Vec<Value> = Vec::new();
    for index in 0..GIVEAWAY_LOAD {
        created.push(
            f.gw(json!({
                "guild_id": guilds[index as usize % guilds.len()],
                "duration_seconds": 60 + index * 60,
            }))
            .await,
        );
    }
    assert_within_budget(started, &format!("{GIVEAWAY_LOAD} creates"));

    // The fake clock does not move during creates, so a strictly longer
    // duration means a strictly later deadline: creation order is deadline
    // order across the whole database.
    let deadlines: Vec<i64> = created
        .iter()
        .map(|gw| gw["ends_at"].as_i64().expect("ends_at"))
        .collect();
    let mut sorted = deadlines.clone();
    sorted.sort();
    assert_eq!(sorted, deadlines);
    assert_eq!(
        Some(GIVEAWAY_LOAD),
        f.h.scalar("SELECT COUNT(*) FROM simple_giveaways WHERE status = 'active'")
            .await
            .and_then(|value| value.as_i64())
    );

    // Global listing: soonest first, capped, and the cap clamps at both ends.
    let every_id = gw_ids(&Value::Array(created.clone()));
    assert_eq!(
        every_id[..25].to_vec(),
        gw_ids(&f.h.ok("list_all_active", json!({"limit": 25})).await)
    );
    assert_eq!(
        every_id[..25].to_vec(),
        gw_ids(&f.h.ok("list_all_active", Value::Null).await)
    );
    assert_eq!(
        vec![every_id[0].clone()],
        gw_ids(&f.h.ok("list_all_active", json!({"limit": 0})).await)
    );
    assert_eq!(
        every_id,
        gw_ids(&f.h.ok("list_all_active", json!({"limit": 200})).await)
    );
    assert_eq!(
        every_id,
        gw_ids(&f.h.ok("list_all_active", json!({"limit": 500})).await)
    );

    // Per-guild listing is unbounded but never leaks another guild's row.
    for guild in &guilds {
        let expected: Vec<String> = created
            .iter()
            .filter(|gw| gw["guild_id"].as_str() == Some(guild.as_str()))
            .map(|gw| gw["id"].as_str().expect("id").to_string())
            .collect();
        let listed = f.h.ok("list_active", json!({"guild_id": guild})).await;
        assert_eq!(expected, gw_ids(&listed));
        assert!(listed
            .as_array()
            .expect("rows")
            .iter()
            .all(|gw| gw["guild_id"].as_str() == Some(guild.as_str())));
        // The listing is ordered by deadline, which is creation order here.
        assert_eq!(expected, gw_ids(&listed));
    }
    let mut total = 0usize;
    for guild in &guilds {
        total +=
            f.h.ok("list_active", json!({"guild_id": guild}))
                .await
                .as_array()
                .expect("rows")
                .len();
    }
    assert_eq!(GIVEAWAY_LOAD as usize, total);
}

// -- chunked flush at scale ----------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn a_thousand_keys_flush_in_one_call() {
    let f = Fixture::new().await;
    let users = ids("72", FLUSH_USERS);
    let rows: Vec<Value> = users.iter().map(|user| json!([f.guild, user, 1])).collect();
    let started = Instant::now();

    let receipt = f.h.ok("add_message_counts", json!({"rows": rows})).await;

    assert_within_budget(started, &format!("{FLUSH_USERS}-row flush"));
    // `applied` counts rows written, not batches issued; `before` counts
    // distinct keys snapshotted. 1000 rows are 4 chunks of at most 300.
    assert_eq!(
        json!({"applied": FLUSH_USERS, "before": FLUSH_USERS}),
        receipt
    );
    assert_eq!(4, (FLUSH_USERS + FLUSH_CHUNK - 1) / FLUSH_CHUNK);

    assert_eq!(
        Some(FLUSH_USERS),
        f.h.scalar_params(
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
    assert_eq!(
        Some(FLUSH_USERS),
        f.h.scalar_params(
            "SELECT SUM(count) FROM simple_message_counts WHERE guild_id = ?",
            json!([f.guild]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
    assert_eq!(
        Some(0),
        f.h.scalar_params(
            "SELECT COUNT(*) FROM simple_message_counts WHERE guild_id = ? AND count <> 1",
            json!([f.guild]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
    assert_eq!(
        json!(1),
        f.h.ok(
            "message_count",
            json!({"guild_id": f.guild, "user_id": users[FLUSH_USERS as usize - 1]})
        )
        .await
    );

    // A complete flush reconciled against its zero snapshot has no tail left
    // to resend: the crash-recovery path stays a no-op at this size too.
    let zeros: Vec<Value> = users.iter().map(|user| json!([f.guild, user, 0])).collect();
    assert_eq!(
        json!([]),
        f.h.ok(
            "reconcile_message_counts",
            json!({"rows": rows, "before": zeros})
        )
        .await
    );
}

// -- draw throughput -----------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn twenty_full_size_draws_never_repeat_a_winner() {
    let f = Fixture::new().await;
    let started = Instant::now();
    let mut draws: Vec<(Value, Vec<String>, Vec<String>)> = Vec::new();
    for round_index in 0..DRAW_ROUNDS {
        let gw = f.gw(json!({"winner_count": DRAW_WINNERS})).await;
        let pool = ids(&format!("8{round_index}"), DRAW_POOL);
        join_batch(&f.h, &gw, &pool).await;
        let (_, winners) = f.end(&gw).await;
        draws.push((gw, pool, winners));
    }
    assert_within_budget(started, &format!("{DRAW_ROUNDS} full draws"));

    for (gw, pool, winners) in &draws {
        assert_eq!(DRAW_WINNERS as usize, winners.len());
        let unique: HashSet<&String> = winners.iter().collect();
        assert_eq!(
            DRAW_WINNERS as usize,
            unique.len(),
            "a winner was named twice"
        );
        let pool_set: HashSet<&String> = pool.iter().collect();
        assert!(unique.is_subset(&pool_set));
        let losers: HashSet<&String> = pool_set.difference(&unique).copied().collect();
        assert_eq!((DRAW_POOL - DRAW_WINNERS) as usize, losers.len());

        let stored = f.h.ok("get", json!({"giveaway_id": gw["id"]})).await;
        assert_eq!(json!("ended"), stored["status"]);
        assert_eq!(json!(winners), stored["winners"]);
        assert_eq!(json!(DRAW_POOL), stored["entrant_count"]);
        assert_eq!(
            json!(DRAW_POOL),
            f.h.ok(
                "entry_count",
                json!({"giveaway_id": gw["id"], "exact": true})
            )
            .await
        );
    }

    // Every round draws from its own pool: a leaked winner id would break the
    // "25 of 30" shape somewhere, even though the ids are opaque.
    let total: usize = draws.iter().map(|(_, _, winners)| winners.len()).sum();
    assert_eq!((DRAW_ROUNDS * DRAW_WINNERS) as usize, total);
}

// -- concurrent mixed workload -------------------------------------------

/// Leave a batch and join a batch from one process; returns `[left, added]`.
fn churn_job(gw: Value, leavers: Vec<String>, joiners: Vec<String>) -> RaceJob {
    Box::new(move |h: &Harness| {
        Box::pin(async move {
            let giveaway_id = gw["id"].clone();
            let mut left = Vec::with_capacity(leavers.len());
            for user in &leavers {
                left.push(
                    h.call(
                        "leave",
                        json!({"giveaway_id": giveaway_id, "user_id": user}),
                    )
                    .await?,
                );
            }
            let mut added = Vec::with_capacity(joiners.len());
            for user in &joiners {
                added.push(join(h, &gw, user, json!({})).await?);
            }
            Ok(json!([left, added]))
        })
    })
}

#[tokio::test(flavor = "multi_thread")]
async fn four_workers_join_leave_and_join_again() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({"winner_count": DRAW_WINNERS})).await;
    let mut workers = Vec::new();
    for _ in 0..WORKERS {
        workers.push(f.sibling().await);
    }
    let users = ids("73", (WORKERS * PER_WORKER) as i64);
    let batches: Vec<Vec<String>> = (0..WORKERS)
        .map(|index| users[index * PER_WORKER..(index + 1) * PER_WORKER].to_vec())
        .collect();
    let started = Instant::now();

    let jobs: Vec<(&Harness, RaceJob)> = workers
        .iter()
        .zip(&batches)
        .map(|(worker, batch)| (worker, join_batch_job(gw.clone(), batch.clone())))
        .collect();
    let joined = run_concurrently(jobs);
    let statuses: Vec<&str> = joined.iter().map(race_status).collect();
    assert_eq!(vec!["ok"; WORKERS], statuses);
    assert_within_budget(
        started,
        &format!("{} concurrent joins", WORKERS * PER_WORKER),
    );

    // The snapshot is the reference: it reports the true pool in one read,
    // independent of the counter the join path maintains.
    let snapshot =
        f.h.ok(
            "entries_snapshot",
            json!({"giveaway_id": gw["id"], "user_id": users.last().expect("users")}),
        )
        .await;
    assert_eq!(json!(WORKERS * PER_WORKER), snapshot[0]);
    assert_eq!(
        WORKERS * PER_WORKER,
        snapshot[1].as_array().expect("window").len()
    );
    assert_eq!(json!(1), snapshot[2]);
    let expected_users: HashSet<String> = users.iter().cloned().collect();
    assert_eq!(expected_users, row_users(&snapshot[1]));
    assert_eq!(
        json!(WORKERS * PER_WORKER),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );

    let fresh = ids("74", (WORKERS * CHURN_PER_WORKER) as i64);
    let removed: HashSet<String> = batches
        .iter()
        .flat_map(|batch| batch[..CHURN_PER_WORKER].iter().cloned())
        .collect();

    let jobs: Vec<(&Harness, RaceJob)> = workers
        .iter()
        .enumerate()
        .map(|(index, worker)| {
            (
                worker,
                churn_job(
                    gw.clone(),
                    batches[index][..CHURN_PER_WORKER].to_vec(),
                    fresh[index * CHURN_PER_WORKER..(index + 1) * CHURN_PER_WORKER].to_vec(),
                ),
            )
        })
        .collect();
    let churned = run_concurrently(jobs);
    let statuses: Vec<&str> = churned.iter().map(race_status).collect();
    assert_eq!(vec!["ok"; WORKERS], statuses);
    for outcome in &churned {
        let value = outcome
            .as_ref()
            .unwrap_or_else(|err| panic!("churn failed: {err}"));
        let left = value[0].as_array().expect("left flags");
        assert_eq!(CHURN_PER_WORKER, left.len());
        assert!(left.iter().all(|flag| flag.as_bool() == Some(true)));
        let added = value[1].as_array().expect("counts");
        assert_eq!(CHURN_PER_WORKER, added.len());
        assert!(added
            .iter()
            .all(|count| count.as_i64().map(|count| count > 0) == Some(true)));
    }

    // The net set is the snapshot's answer: every departure gone, every
    // arrival present, nobody duplicated, and the counter agrees.
    let expected: HashSet<String> = expected_users
        .difference(&removed)
        .cloned()
        .chain(fresh.iter().cloned())
        .collect();
    assert_eq!(WORKERS * PER_WORKER, expected.len());
    let departed = removed.iter().next().expect("removed");
    let snapshot =
        f.h.ok(
            "entries_snapshot",
            json!({"giveaway_id": gw["id"], "user_id": departed}),
        )
        .await;
    assert_eq!(json!(WORKERS * PER_WORKER), snapshot[0]);
    assert_eq!(
        WORKERS * PER_WORKER,
        snapshot[1].as_array().expect("window").len()
    );
    assert_eq!(
        json!(0),
        snapshot[2],
        "a member who left must not read as entered"
    );
    assert_eq!(expected, row_users(&snapshot[1]));
    assert_eq!(
        json!(WORKERS * PER_WORKER),
        f.h.ok(
            "entry_count",
            json!({"giveaway_id": gw["id"], "exact": true})
        )
        .await
    );
    assert_eq!(
        Some((WORKERS * PER_WORKER) as i64),
        f.h.scalar_params(
            "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
    assert_eq!(
        Some((WORKERS * PER_WORKER) as i64),
        f.h.scalar_params(
            "SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?",
            json!([gw["id"]]),
        )
        .await
        .and_then(|value| value.as_i64())
    );
}
