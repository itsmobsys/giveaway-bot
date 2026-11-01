//! Self-checks for the harness itself.
//!
//! If these fail, every other suite is meaningless: they prove the fixture
//! opens a real database, the clock only moves when told, and a refusal keeps
//! its kind.

use gwbot_testkit::{create, ended, merge, CallError, Fixture, Harness};
use serde_json::{json, Value};

#[tokio::test(flavor = "multi_thread")]
async fn a_fresh_harness_is_ready_and_answering() {
    let h = Harness::new().await;
    assert_eq!(json!("pong"), h.ok("ping", Value::Null).await);
    let version = h.ok("schema_version", Value::Null).await;
    assert_eq!(2, version["user_version"].as_i64().unwrap());
}

#[tokio::test(flavor = "multi_thread")]
async fn the_clock_only_moves_when_a_test_moves_it() {
    let h = Harness::new().await;
    let start = h.now();
    assert_eq!(start, h.now());
    assert_eq!(start + 1_500, h.advance(1_500));
    assert_eq!(start + 1_500, h.now());
    assert_eq!(42, h.set_time(42));
    assert_eq!(42, h.now());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_refusal_keeps_its_kind() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({ "winner_count": 1 })).await;
    f.enter(&gw, "100000000000000010").await;

    // Joining twice is refused with the duplicate message, not a generic error.
    let err = f.join_error(&gw, "100000000000000010").await;
    assert!(err.message.contains("already"), "{}", err.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_draw_returns_the_winners_and_the_final_count() {
    let f = Fixture::new().await;
    let (gw, winners) = f.draw(3, json!({})).await;
    assert_eq!("ended", gw.status);
    assert_eq!(3, gw.entrant_count.unwrap_or(0));
    assert_eq!(1, winners.len());
    assert!(gw.winners.contains(&winners[0]));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_sibling_shares_the_file_but_not_the_clock() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let sibling = f.sibling().await;
    let seen = sibling.ok("get", json!({ "giveaway_id": gw["id"] })).await;
    assert_eq!(gw["id"], seen["id"]);
    sibling.advance(5_000);
    assert_ne!(sibling.now(), f.h.now());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_restart_keeps_the_rows() {
    let mut f = Fixture::new().await;
    let gw = f.gw(json!({ "prize": "Survives" })).await;
    f.reopen().await;
    let reloaded = f.h.ok("get", json!({ "giveaway_id": gw["id"] })).await;
    assert_eq!("Survives", reloaded["prize"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn helpers_read_the_ended_pair() {
    let f = Fixture::new().await;
    let gw = f.gw(merge(json!({}), json!({ "prize": "Pair" }))).await;
    let value = f.h.ok("end", json!({ "giveaway_id": gw["id"] })).await;
    let (ended_gw, winners) = ended(&value);
    assert_eq!("Pair", ended_gw.prize);
    assert!(winners.is_empty(), "no entrants means no winner");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_created_giveaway_carries_its_defaults() {
    let h = Harness::new().await;
    let gw = create(&h, json!({ "prize": "Defaults" })).await;
    assert_eq!("Defaults", gw["prize"]);
    assert_eq!("active", gw["status"]);
    assert_eq!(1, gw["winner_count"]);
    let joined = h
        .call("join", json!({ "gw": gw, "user_id": "1", "username": "u" }))
        .await;
    assert!(joined.is_ok(), "{joined:?}");
    assert!(!matches!(joined, Err(CallError::Other(_))));
}
