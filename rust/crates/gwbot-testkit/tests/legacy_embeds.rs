//! Embed text: countdown formatting, requirements lines, winner announcements.
//!
//! Ported 1:1 from the retired Python `test_embeds.py`. That suite read discord.py
//! `Embed` internals (`description`, `footer.text`, `colour.value`, `image.url`);
//! the Rust [`gwbot_core::embeds::Embed`] struct is the analogue, so the same
//! fields are asserted with the same expected text.

use std::collections::HashMap;

use gwbot_core::embeds::{countdown, giveaway_embed, participants_embed, winner_embed};
use gwbot_core::models::Giveaway;
use serde_json::{json, Value};

const COLOR: u32 = 0x7C5CFF;

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// A deadline `seconds` from now, in epoch milliseconds, like the Python helper.
fn in_seconds(seconds: f64) -> i64 {
    ((now_secs() + seconds) * 1000.0) as i64
}

/// The Python fixture's giveaway, with the same defaults.
fn a_giveaway() -> Giveaway {
    Giveaway {
        id: "gw_0123456789ab".into(),
        guild_id: "111111111111111111".into(),
        channel_id: "222222222222222222".into(),
        message_id: Some("333333333333333333".into()),
        prize: "Steam key".into(),
        winner_count: 1,
        ends_at: in_seconds(3600.0),
        status: "active".into(),
        required_role_id: None,
        required_role_ids: Vec::new(),
        blocked_role_id: None,
        min_account_age_days: 0,
        min_messages: 0,
        image_url: None,
        entrants_role_id: None,
        host_id: None,
        host_name: None,
        winners: Vec::new(),
        ..Giveaway::default()
    }
}

/// `self.assertRegex(text, pattern)` without pulling in a regex dependency.
///
/// `spec` is the pattern with `\d` written as `#` (exactly one digit) and
/// `\d+` written as `*` (one or more digits). Every other character matches
/// literally, so `"1h *m ##s"` is the same assertion as `^1h \d+m \d\ds$`.
fn assert_shape(text: &str, spec: &str) {
    let actual: Vec<char> = text.chars().collect();
    let mut index = 0;
    for expected in spec.chars() {
        match expected {
            '#' => {
                let digit = actual.get(index).copied().unwrap_or(' ');
                assert!(
                    digit.is_ascii_digit(),
                    "expected the shape {spec:?}, got {text:?}"
                );
                index += 1;
            }
            '*' => {
                let start = index;
                while actual.get(index).is_some_and(|c| c.is_ascii_digit()) {
                    index += 1;
                }
                assert!(index > start, "expected the shape {spec:?}, got {text:?}");
            }
            literal => {
                assert_eq!(
                    Some(&literal),
                    actual.get(index),
                    "expected the shape {spec:?}, got {text:?}"
                );
                index += 1;
            }
        }
    }
    assert_eq!(
        index,
        actual.len(),
        "expected the shape {spec:?}, got {text:?}"
    );
}

// -- countdown ------------------------------------------------------------

#[test]
fn test_shape_per_magnitude() {
    assert_shape(&countdown(in_seconds(59.0)), "0m ##s");
    assert_shape(&countdown(in_seconds(90.0)), "1m ##s");
    assert_shape(&countdown(in_seconds(3700.0)), "1h *m ##s");
    assert_shape(&countdown(in_seconds(90000.0)), "1d *h *m");
}

#[test]
fn test_expired_never_goes_negative() {
    assert_shape(&countdown(in_seconds(-5.0)), "0m 0#s");
    assert_shape(&countdown(0), "0m 0#s");
}

// -- live giveaway message ------------------------------------------------

#[test]
fn test_basics() {
    let embed = giveaway_embed(&a_giveaway(), 12, COLOR);
    assert!(embed.description.contains("Steam key"));
    assert!(embed.description.contains("Winners: **1**"));
    assert!(embed.description.contains("Entries: **12**"));
    assert_eq!(Some("ID: gw_0123456789ab"), embed.footer.as_deref());
    assert_eq!(COLOR, embed.colour);
    assert_eq!(None, embed.image_url);
}

#[test]
fn test_requirements_block_is_omitted_when_empty() {
    assert!(!giveaway_embed(&a_giveaway(), 0, COLOR)
        .description
        .contains("Requirements"));
}

#[test]
fn test_every_requirement_is_listed() {
    let mut gw = a_giveaway();
    gw.required_role_ids = vec!["11".into(), "22".into()];
    gw.blocked_role_id = Some("33".into());
    gw.min_account_age_days = 30;
    gw.min_messages = 5;
    let embed = giveaway_embed(&gw, 0, COLOR);
    for needle in [
        "<@&11>",
        "<@&22>",
        "<@&33>",
        "Account 30+ days old",
        "Send 5+ messages",
    ] {
        assert!(
            embed.description.contains(needle),
            "missing {needle:?} in {:?}",
            embed.description
        );
    }
}

#[test]
fn test_host_and_image() {
    let mut gw = a_giveaway();
    gw.host_id = Some("444444444444444444".into());
    gw.host_name = Some("Mod".into());
    gw.image_url = Some("https://x/y.png".into());
    let embed = giveaway_embed(&gw, 1, COLOR);
    assert!(embed.description.contains("<@444444444444444444> (Mod)"));
    assert_eq!(Some("https://x/y.png"), embed.image_url.as_deref());
}

#[test]
fn test_host_without_a_name_still_renders() {
    let mut gw = a_giveaway();
    gw.host_id = Some("444444444444444444".into());
    let embed = giveaway_embed(&gw, 1, COLOR);
    assert!(embed.description.contains("(host)"));
}

// -- participant list -----------------------------------------------------

/// The Python `rows(n)` helper: `n` entrant rows, ids from 10^17 upward.
fn rows(n: usize) -> Vec<Value> {
    (0..n)
        .map(|i| {
            json!({
                "user_id": (100_000_000_000_000_000i64 + i as i64).to_string(),
                "username": "u",
                "entered_at": i,
            })
        })
        .collect()
}

#[test]
fn test_paging_header_and_odds() {
    let embed = participants_embed("Nitro", &rows(10), 1, 3, 25, 1, 2, COLOR, None);
    assert_eq!("👥 Participants — page 2/3", embed.title);
    assert!(embed.description.contains("Total Participants: 25"));
    assert!(embed.description.contains("Your Entries: 1"));
    assert!(embed.description.contains("8%"));
}

#[test]
fn test_zero_odds_without_an_entry() {
    let embed = participants_embed("Nitro", &rows(1), 0, 1, 1, 0, 1, COLOR, None);
    assert!(embed.description.contains("Your Chance of Winning: 0%"));
}

#[test]
fn test_empty_page_does_not_crash() {
    let embed = participants_embed("Nitro", &[], 4, 5, 50, 0, 1, COLOR, None);
    assert!(embed.description.contains("Total Participants: 50"));
}

// -- ended giveaway -------------------------------------------------------

#[test]
fn test_winners_are_mentioned() {
    let mut gw = a_giveaway();
    gw.winners = vec!["1".repeat(18)];
    let winners = vec!["1".repeat(18)];
    let embed = winner_embed(&gw, &winners, 7, COLOR, None, None);
    assert_eq!("Giveaway Ended", embed.title);
    assert!(embed.description.contains("<@111111111111111111>"));
    assert!(embed.description.contains("Entries: **7**"));
}

#[test]
fn test_no_winners_says_so() {
    let embed = winner_embed(&a_giveaway(), &[], 0, COLOR, None, None);
    assert!(embed.description.contains("No valid entries"));
}

#[test]
fn test_host_line_and_image_carry_over() {
    let mut gw = a_giveaway();
    gw.host_id = Some("5".repeat(18));
    gw.host_name = Some("Mod".into());
    gw.image_url = Some("https://x/y.png".into());
    let embed = winner_embed(&gw, &["6".repeat(18)], 2, COLOR, None, None);
    assert!(embed
        .description
        .contains("Hosted by <@555555555555555555> (Mod)"));
    assert_eq!(Some("https://x/y.png"), embed.image_url.as_deref());
}

#[test]
fn test_single_shared_deadline_renders_once() {
    let mut gw = a_giveaway();
    gw.winners = vec!["1".repeat(18), "2".repeat(18)];
    let winners = gw.winners.clone();
    let deadline = 1_700_000_000_000;
    let embed = winner_embed(&gw, &winners, 4, COLOR, Some(deadline), None);
    assert!(embed.description.contains("Claim Prize"));
    assert_eq!(1, embed.description.matches("Claim Prize").count());
}

#[test]
fn test_divergent_deadlines_render_per_winner() {
    let mut gw = a_giveaway();
    gw.winners = vec!["1".repeat(18), "2".repeat(18)];
    let winners = gw.winners.clone();
    let first = 1_700_000_000_000;
    let second = first + 3_600_000;
    let mut deadlines = HashMap::new();
    deadlines.insert("1".repeat(18), first);
    deadlines.insert("2".repeat(18), second);
    let embed = winner_embed(&gw, &winners, 4, COLOR, None, Some(&deadlines));
    assert!(embed
        .description
        .contains(&format!("<@{}>:", "1".repeat(18))));
    assert!(embed
        .description
        .contains(&format!("<@{}>:", "2".repeat(18))));
    assert!(embed.description.contains("Claim Prize"));
}

#[test]
fn test_no_deadline_renders_no_claim_line() {
    let mut gw = a_giveaway();
    gw.winners = vec!["1".repeat(18)];
    let winners = gw.winners.clone();
    let embed = winner_embed(&gw, &winners, 7, COLOR, None, None);
    assert!(!embed.description.contains("Claim Prize"));
}
