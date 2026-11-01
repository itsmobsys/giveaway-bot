//! Statistical invariants for the draw, not for a particular draw.
//!
//! Every assertion here is about the *shape* of a random outcome: which members
//! are eligible, how many can win, whether a reroll can hand the same prize out
//! twice. No test may name a winner: the RNG is seeded from entropy, so a test
//! that expects member X to win is a test that fails one run in five.
//!
//! Sizes are deliberately modest (hundreds of draws, not thousands) so the whole
//! module stays a couple of seconds while still making a broken RNG (a constant,
//! a first-element pick, an off-by-one slice) impossible to miss.

use std::collections::{BTreeMap, BTreeSet};

use gwbot_testkit::{ended, ids, Fixture};
use serde_json::{json, Value};

/// Five members are enough for a 1-of-N draw to be visibly biased and cheap
/// enough to run 400 of them.
const POOL: [&str; 5] = [
    "700000000000000000",
    "700000000000000001",
    "700000000000000002",
    "700000000000000003",
    "700000000000000004",
];

/// 400 draws of 1-of-5: uniform expectation is 80 per member, sd ~8. 45% (180)
/// is over twelve standard deviations away, so it only trips on real bias.
const DRAWS: usize = 400;

fn pool_set() -> BTreeSet<String> {
    POOL.iter().map(|id| (*id).to_string()).collect()
}

/// `POOL[..upto]` as a set: the members that entered a partial pool.
fn pool_upto(upto: usize) -> BTreeSet<String> {
    POOL[..upto].iter().map(|id| (*id).to_string()).collect()
}

/// `POOL[from..]` as a set, for the exclusion assertions.
fn pool_from(from: usize) -> BTreeSet<String> {
    POOL[from..].iter().map(|id| (*id).to_string()).collect()
}

fn set_of(values: &[String]) -> BTreeSet<String> {
    values.iter().cloned().collect()
}

fn strings(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_string()).collect()
}

/// One `pick` call, decoded into ids. `exclude` is omitted entirely when absent
/// so the op sees exactly what the Python call sent.
async fn pick(f: &Fixture, gw: &Value, n: i64, exclude: Option<&[String]>) -> Vec<String> {
    let mut args = json!({ "giveaway_id": gw["id"], "n": n });
    if let Some(exclude) = exclude {
        args["exclude"] = json!(exclude);
    }
    ids(&f.h.ok("pick", args).await)
}

/// The stored winner list, read back through `get`.
async fn stored_winners(f: &Fixture, gw: &Value) -> Vec<String> {
    let seen = f.h.ok("get", json!({ "giveaway_id": gw["id"] })).await;
    ids(&seen["winners"])
}

// -- pick distribution ---------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn test_one_from_five_covers_every_entrant_without_dominating() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }

    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for _ in 0..DRAWS {
        let picked = pick(&f, &gw, 1, None).await;
        assert_eq!(1, picked.len(), "one pick returned {picked:?}");
        assert!(
            POOL.contains(&picked[0].as_str()),
            "picked a non-entrant: {picked:?}"
        );
        *counts.entry(picked[0].clone()).or_insert(0) += 1;
    }

    assert_eq!(
        pool_set(),
        counts.keys().cloned().collect::<BTreeSet<_>>(),
        "an entrant was never drawn: {counts:?}"
    );
    // `int(DRAWS * 0.45)` and `int(DRAWS * 0.05)` in the Python test: 180 and 20.
    let max = counts.values().copied().max().unwrap_or(0);
    let min = counts.values().copied().min().unwrap_or(0);
    assert!(
        max < (DRAWS as f64 * 0.45) as usize,
        "one entrant took more than 45% of the draws: {counts:?}"
    );
    assert!(
        min > (DRAWS as f64 * 0.05) as usize,
        "one entrant was almost never drawn: {counts:?}"
    );

    // Chi-square for 4 degrees of freedom. 30 sits around p = 5e-6, so this
    // catches a skewed picker without turning a lucky run into a failure.
    let expected = DRAWS as f64 / POOL.len() as f64;
    let chi_square: f64 = counts
        .values()
        .map(|&count| {
            let delta = count as f64 - expected;
            delta * delta / expected
        })
        .sum();
    assert!(
        chi_square < 30.0,
        "draw distribution is skewed: {counts:?} (chi-square {chi_square})"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn test_two_from_five_never_repeats_within_one_draw() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }

    let mut seen: BTreeSet<String> = BTreeSet::new();
    for _ in 0..200 {
        let picked = pick(&f, &gw, 2, None).await;
        assert_eq!(2, picked.len(), "two picks returned {picked:?}");
        // choose_multiple samples without replacement: a duplicate would mean
        // the same member got two copies of one prize.
        assert_eq!(
            set_of(&picked).len(),
            picked.len(),
            "the same member was picked twice in one draw: {picked:?}"
        );
        assert!(set_of(&picked).is_subset(&pool_set()), "{picked:?}");
        seen.extend(picked);
    }

    assert_eq!(pool_set(), seen, "an entrant was never drawn: {seen:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn test_excluded_members_are_never_picked() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }
    let excluded = strings(&POOL[..3]);

    for _ in 0..50 {
        let picked = pick(&f, &gw, 2, Some(&excluded)).await;
        assert_eq!(
            pool_from(3),
            set_of(&picked),
            "an excluded member was picked: {picked:?}"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn test_pick_is_capped_by_the_pool_size() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in &POOL[..3] {
        f.enter(&gw, member).await;
    }

    let picked = pick(&f, &gw, 10, None).await;

    assert_eq!(
        pool_upto(3),
        set_of(&picked),
        "the draw left the pool: {picked:?}"
    );
    assert_eq!(
        3,
        picked.len(),
        "the pool of three capped the draw: {picked:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn test_non_positive_pick_size_returns_nothing() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }

    for n in [0, -3] {
        let picked = pick(&f, &gw, n, None).await;
        assert!(picked.is_empty(), "n={n} returned {picked:?}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn test_pick_on_an_empty_giveaway_returns_nothing() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    let picked = pick(&f, &gw, 3, None).await;
    assert!(picked.is_empty(), "an empty giveaway returned {picked:?}");
}

// -- end draw ------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn test_eighty_draws_cover_every_entrant() {
    // The same five members enter eighty separate giveaways, so the draws
    // are independent and a picker that always takes the first row would
    // leave four members with zero wins.
    let f = Fixture::new().await;
    let mut wins: BTreeMap<String, usize> = BTreeMap::new();
    for _ in 0..80 {
        let gw = f.gw(json!({})).await;
        for member in POOL {
            f.enter(&gw, member).await;
        }
        let (_, winners) = f.end(&gw).await;
        assert_eq!(1, winners.len(), "one winner expected, got {winners:?}");
        assert!(POOL.contains(&winners[0].as_str()), "{winners:?}");
        *wins.entry(winners[0].clone()).or_insert(0) += 1;
    }

    assert_eq!(
        pool_set(),
        wins.keys().cloned().collect::<BTreeSet<_>>(),
        "an entrant never won: {wins:?}"
    );
    // 80 draws: a fair picker gives each member ~16 wins. 40 (50%) is far
    // outside the tail; the exact 45% bound belongs to the 400-draw test.
    assert!(
        wins.values().copied().max().unwrap_or(0) < 40,
        "one entrant dominated: {wins:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn test_winner_count_never_exceeds_the_pool_or_repeats() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({ "winner_count": 3 })).await;
    for member in &POOL[..2] {
        f.enter(&gw, member).await;
    }

    let (_, winners) = f.end(&gw).await;

    // Only two members entered, so three slots collapse to two winners.
    assert_eq!(pool_upto(2), set_of(&winners));
    assert_eq!(2, winners.len(), "three slots collapsed to {winners:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn test_draw_never_names_the_same_member_twice() {
    let f = Fixture::new().await;
    for _ in 0..20 {
        let gw = f.gw(json!({ "winner_count": 4 })).await;
        for member in POOL {
            f.enter(&gw, member).await;
        }
        let (_, winners) = f.end(&gw).await;
        assert_eq!(4, winners.len(), "four winners expected, got {winners:?}");
        assert_eq!(
            set_of(&winners).len(),
            winners.len(),
            "the same member won twice: {winners:?}"
        );
        assert!(set_of(&winners).is_subset(&pool_set()), "{winners:?}");
    }
}

// -- reroll fairness -----------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn test_reroll_only_promotes_members_who_have_never_won() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }
    let (_, first) = f.end(&gw).await;

    let mut ever_drawn = set_of(&first);
    let mut winners: Vec<String> = first.clone();
    for _ in 0..4 {
        let fresh = ended(
            &f.h.ok("reroll", json!({ "giveaway_id": gw["id"], "count": 1 }))
                .await,
        )
        .1;
        assert_eq!(
            1,
            fresh.len(),
            "one rerolled winner expected, got {fresh:?}"
        );
        assert!(POOL.contains(&fresh[0].as_str()), "{fresh:?}");
        assert!(
            !ever_drawn.contains(&fresh[0]),
            "reroll handed the prize out twice: {fresh:?} after {ever_drawn:?}"
        );
        ever_drawn.insert(fresh[0].clone());
        winners.extend(fresh);
    }

    // A plain reroll is additive: it appends to the stored list.
    let stored = stored_winners(&f, &gw).await;
    assert_eq!(
        winners, stored,
        "a plain reroll must append to the stored list"
    );
    assert_eq!(
        set_of(&stored).len(),
        stored.len(),
        "the stored list repeats a winner: {stored:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn test_reroll_returns_nothing_once_everyone_has_won() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }
    f.end(&gw).await;
    // Four rerolls exhaust a five-member pool whose single winner is stored.
    for _ in 0..4 {
        f.h.ok("reroll", json!({ "giveaway_id": gw["id"], "count": 1 }))
            .await;
    }

    let caught =
        f.h.refuse("reroll", json!({ "giveaway_id": gw["id"], "count": 1 }))
            .await;
    assert_eq!("No other entrants left to reroll", caught.message);
}

#[tokio::test(flavor = "multi_thread")]
async fn test_targeted_reroll_swaps_one_slot_and_keeps_the_rest() {
    let f = Fixture::new().await;
    let gw = f.gw(json!({ "winner_count": 3 })).await;
    for member in POOL {
        f.enter(&gw, member).await;
    }
    let (_, winners) = f.end(&gw).await;
    assert_eq!(3, winners.len(), "three winners expected, got {winners:?}");

    let target = winners[1].clone();
    let replacement = ended(
        &f.h.ok(
            "reroll",
            json!({
                "giveaway_id": gw["id"],
                "count": 1,
                "replace_user_id": target,
            }),
        )
        .await,
    )
    .1;

    assert_eq!(
        1,
        replacement.len(),
        "one replacement expected, got {replacement:?}"
    );
    assert!(POOL.contains(&replacement[0].as_str()), "{replacement:?}");
    assert!(
        !winners.contains(&replacement[0]),
        "the replacement was already a winner: {replacement:?} in {winners:?}"
    );
    let updated = stored_winners(&f, &gw).await;
    assert_eq!(
        3,
        updated.len(),
        "the swap changed the list length: {updated:?}"
    );
    // Only the named slot changes; order is preserved so the announcement
    // edit does not shuffle the other winners.
    assert_eq!(
        vec![
            winners[0].clone(),
            replacement[0].clone(),
            winners[2].clone()
        ],
        updated
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn test_draw_pool_is_the_join_order_not_a_reused_snapshot() {
    // A late joiner must be drawable: a picker that caches the first
    // snapshot would never see members 4 and 5.
    let f = Fixture::new().await;
    let gw = f.gw(json!({})).await;
    for (index, member) in POOL.iter().enumerate() {
        f.enter(&gw, member).await;
        let count =
            f.h.ok(
                "entry_count",
                json!({ "giveaway_id": gw["id"], "exact": true }),
            )
            .await;
        assert_eq!(
            Some(index as i64 + 1),
            count.as_i64(),
            "the counter after {} joins was {count}",
            index + 1
        );
    }

    let mut seen: BTreeSet<String> = BTreeSet::new();
    for _ in 0..100 {
        seen.extend(pick(&f, &gw, 1, None).await);
    }
    assert_eq!(pool_set(), seen, "a late joiner was never drawn: {seen:?}");
}
