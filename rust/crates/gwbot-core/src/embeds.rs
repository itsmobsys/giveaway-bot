//! Discord embed text, ported from `bot/giveaway_bot/embeds.py`.
//!
//! The output is a framework-neutral [`Embed`] rather than a `serenity` type so
//! the exact wording, countdown and winner formatting can be unit-tested
//! without a Discord client. The adapter converts one into `CreateEmbed`.

use std::collections::HashMap;

use crate::models::Giveaway;

/// Winners named individually in an announcement. Past this the list is
/// summarised, because the mention text alone would overflow the embed.
pub const WINNERS_NAMED: usize = 50;

/// A rendered embed, before it becomes a Discord API payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Embed {
    pub title: String,
    pub description: String,
    pub colour: u32,
    pub image_url: Option<String>,
    pub footer: Option<String>,
}

impl Embed {
    fn new(title: impl Into<String>, description: impl Into<String>, colour: u32) -> Self {
        Self {
            title: title.into(),
            description: description.into(),
            colour,
            image_url: None,
            footer: None,
        }
    }
}

/// Human "ends in" text, re-rendered every tick so it visibly ticks down.
pub fn countdown(ends_at_ms: i64) -> String {
    countdown_at(ends_at_ms, now_secs())
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Split out so tests can pin "now" instead of racing the clock.
fn countdown_at(ends_at_ms: i64, now_secs: f64) -> String {
    // Python: `int(x)` truncates toward zero, which for a past deadline is the
    // negative side; `max(0, ...)` then floors it. `as i64` matches.
    let secs = ((ends_at_ms as f64 / 1000.0) - now_secs) as i64;
    let mut secs = secs.max(0);
    let days = secs / 86_400;
    secs -= days * 86_400;
    let hours = secs / 3_600;
    secs -= hours * 3_600;
    let minutes = secs / 60;
    secs -= minutes * 60;
    if days > 0 {
        return format!("{days}d {hours}h {minutes}m");
    }
    if hours > 0 {
        return format!("{hours}h {minutes}m {secs:02}s");
    }
    format!("{minutes}m {secs:02}s")
}

fn req_lines(gw: &Giveaway) -> String {
    let mut lines: Vec<String> = Vec::new();
    if !gw.required_role_ids.is_empty() {
        let mentions = gw
            .required_role_ids
            .iter()
            .map(|r| format!("<@&{r}>"))
            .collect::<Vec<_>>()
            .join(" ");
        lines.push(format!("• Requires one of: {mentions}"));
    }
    if let Some(blocked) = gw.blocked_role_id.as_deref().filter(|s| !s.is_empty()) {
        lines.push(format!("• <@&{blocked}> cannot enter"));
    }
    if gw.min_account_age_days > 0 {
        lines.push(format!("• Account {}+ days old", gw.min_account_age_days));
    }
    if gw.min_messages > 0 {
        lines.push(format!(
            "• Send {}+ messages in this server",
            gw.min_messages
        ));
    }
    lines.join("\n")
}

/// The live giveaway message.
pub fn giveaway_embed(gw: &Giveaway, entries: i64, colour: u32) -> Embed {
    let ends = gw.ends_at / 1000;
    let mut desc = format!(
        "🏆 **{}**\n\n⏳ Ends in **{}** (<t:{}:R>)\nWinners: **{}**  •  Entries: **{}**\n",
        gw.prize,
        countdown(gw.ends_at),
        ends,
        gw.winner_count,
        entries
    );
    let reqs = req_lines(gw);
    if !reqs.is_empty() {
        desc.push_str(&format!("\n**Requirements**\n{reqs}\n"));
    }
    desc.push_str("\nClick **Join** to enter, **Leave** to withdraw.");
    if let Some(host_id) = gw.host_id.as_deref().filter(|s| !s.is_empty()) {
        let host_label = gw.host_name.as_deref().unwrap_or("host");
        desc.push_str(&format!("\n\n🎤 Hosted by <@{host_id}> ({host_label})"));
    }
    let mut embed = Embed::new("🎉 Giveaway", desc, colour);
    if let Some(image) = gw.image_url.as_deref().filter(|s| !s.is_empty()) {
        embed.image_url = Some(image.to_string());
    }
    embed.footer = Some(format!("ID: {}", gw.id));
    embed
}

/// One page of the ephemeral entrant list.
#[allow(clippy::too_many_arguments)]
pub fn participants_embed(
    prize: &str,
    rows: &[serde_json::Value],
    page: i64,
    pages: i64,
    total: i64,
    mine: i64,
    winner_count: i64,
    colour: u32,
    shown: Option<i64>,
) -> Embed {
    let lines: Vec<String> = rows
        .iter()
        .map(|row| {
            let user_id = row.get("user_id").and_then(json_id).unwrap_or_default();
            format!("<@{user_id}> (1 entry)")
        })
        .collect();
    // Clamped like every other odds readout in the bot: a giveaway created with
    // 25 winners and 3 entrants would otherwise advertise 833%.
    let chance = if mine > 0 && total > 0 {
        (winner_count as f64 / total as f64 * 100.0).min(100.0)
    } else {
        0.0
    };
    let window = match shown {
        Some(shown) if shown < total => format!("\nShowing the first {shown} of {total}."),
        _ => String::new(),
    };
    let desc = format!(
        "These are the members that have participated in the giveaway of {prize}:{window}\n\n{}\n\nTotal Participants: {total}\nTotal Entries: {total}\n\nYour Entries: {mine}\nYour Chance of Winning: {}%",
        lines.join("\n"),
        fmt_g(chance)
    );
    Embed::new(
        format!("👥 Participants — page {}/{}", page + 1, pages),
        desc,
        colour,
    )
}

/// One page of members sitting out a penalty for joining while timed out.
///
/// Rows carry the `in_guild` flag the caller resolved from its own member
/// cache, so a member who left the server is shown by id instead of a mention
/// that would never resolve.
pub fn timeout_bans_embed(
    rows: &[serde_json::Value],
    page: i64,
    pages: i64,
    total: i64,
    colour: u32,
    hidden: i64,
) -> Embed {
    let mut lines: Vec<String> = Vec::new();
    for row in rows {
        let user_id = row.get("user_id").and_then(json_id).unwrap_or_default();
        let in_guild = row
            .get("in_guild")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let who = if in_guild {
            format!("<@{user_id}>")
        } else {
            format!("`{user_id}` (left the server)")
        };
        let remaining = row
            .get("giveaways_remaining")
            .and_then(json_int)
            .unwrap_or(0);
        let mut line = format!("{who} — **{remaining}** giveaway(s) left");
        let since = row.get("created_at").and_then(json_int).unwrap_or(0) / 1000;
        if since != 0 {
            line.push_str(&format!(" · since <t:{since}:R>"));
        }
        lines.push(line);
    }
    let mut desc = format!(
        "{}\n\nTotal: **{total}** member(s) banned from giveaways",
        lines.join("\n")
    );
    if hidden > 0 {
        // The listing is capped, so say so rather than letting the page count
        // pass for the whole picture.
        desc.push_str(&format!("\n{hidden} more not shown."));
    }
    Embed::new(
        format!("⏳ Timed-out giveaway bans — page {}/{}", page + 1, pages),
        desc,
        colour,
    )
}

/// The ended-giveaway announcement, with optional claim deadlines.
pub fn winner_embed(
    gw: &Giveaway,
    winners: &[String],
    entries: i64,
    colour: u32,
    claim_deadline_ms: Option<i64>,
    claim_deadlines: Option<&HashMap<String, i64>>,
) -> Embed {
    let mut desc = if !winners.is_empty() {
        // Discord caps a description at 4096 characters, so an old reroll row
        // holding hundreds of winners would lose the whole announcement: name
        // the first fifty and count the rest.
        let named = &winners[..winners.len().min(WINNERS_NAMED)];
        let mut mentions = named
            .iter()
            .map(|w| format!("<@{w}>"))
            .collect::<Vec<_>>()
            .join(", ");
        if winners.len() > named.len() {
            mentions.push_str(&format!(" …and {} more", winners.len() - named.len()));
        }
        format!(
            "## 🎊 {} 🎊\n\nCongratulations {mentions} — you won!\n\n👥 Entries: **{entries}**  •  🏆 Winners: **{}**",
            gw.prize,
            winners.len()
        )
    } else {
        format!(
            "## 🎊 {} 🎊\n\nNo valid entries — no winners this time.",
            gw.prize
        )
    };

    let mut deadlines: HashMap<String, i64> = claim_deadlines.cloned().unwrap_or_default();
    if let Some(deadline) = claim_deadline_ms.filter(|d| *d != 0) {
        // Back-compat: callers that only know the earliest deadline fill every
        // announced winner with it; callers with per-winner data pass the map.
        if !winners.is_empty() {
            for winner in winners {
                deadlines.entry(winner.clone()).or_insert(deadline);
            }
        }
    }
    if !deadlines.is_empty() && !winners.is_empty() {
        let mut uniq: Vec<i64> = deadlines.values().copied().filter(|v| *v != 0).collect();
        uniq.sort_unstable();
        uniq.dedup();
        if uniq.len() == 1 {
            let ts = uniq[0] / 1000;
            desc.push_str(&format!(
                "\n\n🎁 Winners: claim by <t:{ts}:R> — press **Claim Prize** below."
            ));
        } else {
            // Divergent windows (expiry replacements land later): one line per
            // winner so nobody misses their own deadline. Capped at the named
            // fifty so a huge reroll history cannot blow the 4096-char limit.
            let lines: Vec<String> = winners
                .iter()
                .take(WINNERS_NAMED)
                .filter_map(|w| {
                    let deadline = deadlines.get(w).copied().filter(|d| *d != 0)?;
                    Some(format!("<@{w}>: <t:{}:R>", deadline / 1000))
                })
                .collect();
            if !lines.is_empty() {
                desc.push_str("\n\n🎁 Claim by — press **Claim Prize** below:\n");
                desc.push_str(&lines.join("\n"));
            }
        }
    }

    if let Some(host_id) = gw.host_id.as_deref().filter(|s| !s.is_empty()) {
        let host_label = gw.host_name.as_deref().unwrap_or("host");
        desc.push_str(&format!("\n🎤 Hosted by <@{host_id}> ({host_label})"));
    }
    let mut embed = Embed::new("Giveaway Ended", desc, colour);
    if let Some(image) = gw.image_url.as_deref().filter(|s| !s.is_empty()) {
        embed.image_url = Some(image.to_string());
    }
    embed.footer = Some(format!("ID: {}", gw.id));
    embed
}

/// Python's `{:g}`: six significant digits, trailing zeros stripped.
///
/// Only the odds readout uses it, so the general exponential branch is kept
/// simple; every real value here is between 0 and 100.
pub fn fmt_g(value: f64) -> String {
    if !value.is_finite() {
        return value.to_string();
    }
    if value == 0.0 {
        return "0".to_string();
    }
    let exp = value.abs().log10().floor() as i32;
    if !(-4..6).contains(&exp) {
        let formatted = format!("{value:e}");
        // Rust renders `1.23457e7`; Python renders `1.23457e+07`.
        return match formatted.split_once('e') {
            Some((mantissa, exponent)) => {
                let exponent: i32 = exponent.parse().unwrap_or(0);
                let sign = if exponent < 0 { '-' } else { '+' };
                format!("{mantissa}e{sign}{:02}", exponent.abs())
            }
            None => formatted,
        };
    }
    let decimals = (6 - 1 - exp).max(0) as usize;
    let formatted = format!("{:.*}", decimals, value);
    if formatted.contains('.') {
        formatted
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_string()
    } else {
        formatted
    }
}

fn json_id(value: &serde_json::Value) -> Option<String> {
    match value {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn json_int(value: &serde_json::Value) -> Option<i64> {
    match value {
        serde_json::Value::Number(n) => n.as_i64(),
        serde_json::Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn giveaway() -> Giveaway {
        Giveaway {
            id: "gw_abc".into(),
            guild_id: "g".into(),
            channel_id: "c".into(),
            prize: "Nitro".into(),
            winner_count: 2,
            ends_at: 1_700_000_000_000,
            status: "active".into(),
            ..Giveaway::default()
        }
    }

    #[test]
    fn countdown_switches_units_exactly_like_python() {
        let now = 1_000_000.0;
        // 90061s = 1d 1h 1m 1s
        assert_eq!(countdown_at((now as i64 + 90_061) * 1000, now), "1d 1h 1m");
        // 3661s = 1h 1m 1s
        assert_eq!(countdown_at((now as i64 + 3_661) * 1000, now), "1h 1m 01s");
        // 61s = 1m 1s
        assert_eq!(countdown_at((now as i64 + 61) * 1000, now), "1m 01s");
        // A past deadline floors at zero.
        assert_eq!(countdown_at((now as i64 - 5_000) * 1000, now), "0m 00s");
    }

    #[test]
    fn giveaway_embed_renders_requirements_and_host() {
        let mut gw = giveaway();
        gw.required_role_ids = vec!["111".into(), "222".into()];
        gw.blocked_role_id = Some("333".into());
        gw.min_account_age_days = 7;
        gw.min_messages = 10;
        gw.host_id = Some("999".into());
        gw.host_name = Some("Mo".into());
        let embed = giveaway_embed(&gw, 4, 0x7C5CFF);
        assert_eq!(embed.title, "🎉 Giveaway");
        assert!(embed
            .description
            .contains("• Requires one of: <@&111> <@&222>"));
        assert!(embed.description.contains("• <@&333> cannot enter"));
        assert!(embed.description.contains("• Account 7+ days old"));
        assert!(embed
            .description
            .contains("• Send 10+ messages in this server"));
        assert!(embed.description.contains("🎤 Hosted by <@999> (Mo)"));
        assert!(embed
            .description
            .contains("Winners: **2**  •  Entries: **4**"));
        assert_eq!(embed.footer.as_deref(), Some("ID: gw_abc"));
    }

    #[test]
    fn winner_embed_names_fifty_and_counts_the_rest() {
        let winners: Vec<String> = (0..55).map(|i| format!("u{i}")).collect();
        let embed = winner_embed(&giveaway(), &winners, 60, 0, None, None);
        assert!(embed.description.contains("…and 5 more"));
        assert!(embed.description.contains("<@u49>"));
        assert!(!embed.description.contains("<@u50>"));
        assert!(embed.description.contains("Winners: **55**"));
    }

    #[test]
    fn winner_embed_collapses_a_shared_deadline() {
        let winners = vec!["u1".to_string(), "u2".to_string()];
        let mut deadlines = HashMap::new();
        deadlines.insert("u1".to_string(), 1_700_000_060_000);
        deadlines.insert("u2".to_string(), 1_700_000_060_000);
        let embed = winner_embed(&giveaway(), &winners, 3, 0, None, Some(&deadlines));
        assert!(embed
            .description
            .contains("🎁 Winners: claim by <t:1700000060:R> — press **Claim Prize** below."));
    }

    #[test]
    fn winner_embed_lists_divergent_deadlines_per_winner() {
        let winners = vec!["u1".to_string(), "u2".to_string()];
        let mut deadlines = HashMap::new();
        deadlines.insert("u1".to_string(), 1_700_000_060_000);
        deadlines.insert("u2".to_string(), 1_700_000_120_000);
        let embed = winner_embed(&giveaway(), &winners, 3, 0, None, Some(&deadlines));
        assert!(embed.description.contains("<@u1>: <t:1700000060:R>"));
        assert!(embed.description.contains("<@u2>: <t:1700000120:R>"));
    }

    #[test]
    fn participants_embed_clamps_chance_at_one_hundred() {
        let rows = vec![json!({"user_id": "u1"}), json!({"user_id": "u2"})];
        let embed = participants_embed("Nitro", &rows, 0, 1, 2, 1, 25, 0, None);
        assert_eq!(embed.title, "👥 Participants — page 1/1");
        assert!(embed.description.contains("Your Chance of Winning: 100%"));
        assert!(embed.description.contains("<@u1> (1 entry)"));
    }

    #[test]
    fn participants_embed_reports_a_truncated_window() {
        let rows = vec![json!({"user_id": "u1"})];
        let embed = participants_embed("Nitro", &rows, 0, 1, 500, 0, 1, 0, Some(100));
        assert!(embed.description.contains("Showing the first 100 of 500."));
        assert!(embed.description.contains("Your Chance of Winning: 0%"));
    }

    #[test]
    fn timeout_bans_embed_marks_members_who_left() {
        let rows = vec![
            json!({"user_id": "u1", "giveaways_remaining": 2, "in_guild": true, "created_at": 1_700_000_000_000i64}),
            json!({"user_id": "u2", "giveaways_remaining": 1, "in_guild": false}),
        ];
        let embed = timeout_bans_embed(&rows, 0, 1, 2, 0, 3);
        assert!(embed.description.contains("<@u1> — **2** giveaway(s) left"));
        assert!(embed.description.contains("· since <t:1700000000:R>"));
        assert!(embed
            .description
            .contains("`u2` (left the server) — **1** giveaway(s) left"));
        assert!(embed.description.contains("Total: **2** member(s)"));
        assert!(embed.description.contains("3 more not shown."));
    }

    #[test]
    fn fmt_g_matches_pythons_six_significant_digits() {
        assert_eq!(fmt_g(0.0), "0");
        assert_eq!(fmt_g(100.0), "100");
        assert_eq!(fmt_g(50.0), "50");
        assert_eq!(fmt_g(12.5), "12.5");
        assert_eq!(fmt_g(100.0 / 3.0), "33.3333");
        assert_eq!(fmt_g(1.0 / 3.0), "0.333333");
    }
}
