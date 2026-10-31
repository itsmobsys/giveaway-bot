//! Admin slash commands: blacklist, timeout bans, host bans, and the latency
//! probe. Split out of `commands` purely for review size.
//!
//! Every handler keeps `bot.py`'s shape exactly: the cheap guards (guild +
//! **Manage Server**) answer before the 3-second interaction budget is at risk,
//! the command is deferred, the database work happens, and only then is the
//! member answered. `/gw_request_time` is the one exception: its "ack" leg IS
//! the measurement, so the defer is timed rather than issued up front.

use std::future::Future;
use std::time::Instant;

use anyhow::Result;
use gwbot_core::cache::RedisCache;
use gwbot_core::components::{self, PAGE_SIZE, VIEW_TIMEOUT_SECS};
use gwbot_core::embeds;
use gwbot_core::models::ServiceError;
use serde_json::Value;
use serenity::all::{
    CommandDataOptionValue, CommandInteraction, CommandOptionType, Context, CreateCommand,
    CreateCommandOption, CreateInteractionResponseFollowup, GuildId, MessageId, UserId,
};
use tracing::{error, info, warn};

use super::{json_id, snowflake, Bot, PagerKind, PagerState};
use crate::convert;

/// The command specs this module owns, merged into the global table.
pub(crate) fn admin_specs() -> Vec<CreateCommand> {
    vec![
        CreateCommand::new("giveaway_blacklist_add")
            .description("Block a user from all giveaways")
            .add_option(
                CreateCommandOption::new(CommandOptionType::User, "user", "The member to block")
                    .required(true),
            ),
        CreateCommand::new("giveaway_blacklist_remove")
            .description("Unblock a user from giveaways")
            .add_option(
                CreateCommandOption::new(CommandOptionType::User, "user", "The member to unblock")
                    .required(true),
            ),
        CreateCommand::new("giveaway_blacklist_list").description("Show blocked users"),
        CreateCommand::new("giveaway_ban")
            .description("Ban a user for N hours (0 = permanent)")
            .add_option(
                CreateCommandOption::new(CommandOptionType::User, "user", "The member to ban")
                    .required(true),
            )
            .add_option(CreateCommandOption::new(
                CommandOptionType::Integer,
                "hours",
                "0 = permanent, 1-720 hours",
            ))
            .add_option(CreateCommandOption::new(
                CommandOptionType::String,
                "reason",
                "Reason shown to the member and staff",
            )),
        CreateCommand::new("giveaway_unban")
            .description("Unban a user from giveaways")
            .add_option(
                CreateCommandOption::new(CommandOptionType::User, "user", "The member to unban")
                    .required(true),
            ),
        CreateCommand::new("giveaway_ban_list")
            .description("Show banned users with expiry and reason"),
        CreateCommand::new("giveaway_timeout_bans")
            .description("List members banned from giveaways by the timed-out penalty"),
        CreateCommand::new("gw_request_time")
            .description("Time a real giveaway request (Redis + Discord legs)"),
    ]
}

/// Handle one of this module's commands. Returns false when `name` is not ours.
pub(crate) async fn handle_admin(
    bot: &Bot,
    ctx: &Context,
    ci: &CommandInteraction,
    name: &str,
) -> bool {
    let outcome = match name {
        "giveaway_blacklist_add" => blacklist_add(bot, ctx, ci).await,
        "giveaway_blacklist_remove" => blacklist_remove(bot, ctx, ci).await,
        "giveaway_blacklist_list" => blacklist_list(bot, ctx, ci).await,
        "giveaway_ban" => ban(bot, ctx, ci).await,
        "giveaway_unban" => unban(bot, ctx, ci).await,
        "giveaway_ban_list" => ban_list(bot, ctx, ci).await,
        "giveaway_timeout_bans" => timeout_bans(bot, ctx, ci).await,
        "gw_request_time" => request_time(bot, ctx, ci).await,
        _ => return false,
    };
    if let Err(err) = outcome {
        // `bot.py`'s `on_app_command_error`: the traceback names the command,
        // and the member gets a retry hint instead of silence.
        error!("command /{name} failed: {err}");
        bot.safe_followup_command(ctx, ci, &format!("⚠️ /{name} failed. Please try again."))
            .await;
    }
    true
}

// -- guards and options --------------------------------------------------

/// `_can_manage`: the payload's own permission bits, no cache round trip.
fn can_manage(ci: &CommandInteraction) -> bool {
    ci.member
        .as_deref()
        .and_then(|member| member.permissions)
        .map(|permissions| permissions.manage_guild() || permissions.administrator())
        .unwrap_or(false)
}

/// The shared `interaction.guild is None or not _can_manage(...)` guard.
async fn manage_guard(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Option<GuildId> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return None;
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return None;
    }
    Some(guild_id)
}

fn option_value<'a>(ci: &'a CommandInteraction, name: &str) -> Option<&'a CommandDataOptionValue> {
    ci.data
        .options
        .iter()
        .find(|option| option.name == name)
        .map(|option| &option.value)
}

fn string_option(ci: &CommandInteraction, name: &str) -> Option<String> {
    match option_value(ci, name)? {
        CommandDataOptionValue::String(value) => Some(value.clone()),
        _ => None,
    }
}

fn int_option(ci: &CommandInteraction, name: &str) -> Option<i64> {
    match option_value(ci, name)? {
        CommandDataOptionValue::Integer(value) => Some(*value),
        _ => None,
    }
}

fn user_option(ci: &CommandInteraction, name: &str) -> Option<UserId> {
    match option_value(ci, name)? {
        CommandDataOptionValue::User(id) => Some(*id),
        _ => None,
    }
}

// -- blacklist -----------------------------------------------------------

/// Block + yank their entries from running giveaways + strip roles.
async fn blacklist_add(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let gid = guild_id.to_string();
    // Required by the spec; a missing value can only mean a malformed payload.
    let Some(user_id) = user_option(ci, "user") else {
        return Ok(());
    };
    let uid = user_id.to_string();
    let updated = match bot
        .db_call(bot.service().blacklist_add(&gid, &uid, 0, "", "", None))
        .await
    {
        Ok(_) => bot.db_call(bot.service().list_active(&gid)).await,
        Err(err) => Err(err),
    };
    let active = match updated {
        Ok(active) => active,
        Err(err) => {
            error!("blacklist add failed for {uid}: {err}");
            bot.safe_followup_command(ctx, ci, "⚠️ Could not update the blacklist. Try again.")
                .await;
            return Ok(());
        }
    };
    for gw in &active {
        // Their entries are gone, so this process's entry markers must go too,
        // or the next honest click is refused as "already entered".
        bot.clear_entered(&gw.id, &uid);
        bot.take_entrants_role(ctx, gw, &uid).await;
    }
    info!("blacklisted {uid} in guild {gid}");
    bot.safe_followup_command(
        ctx,
        ci,
        &format!(
            "🚫 <@{uid}> is blocked from giveaways — entries removed from {} running giveaway(s).",
            active.len()
        ),
    )
    .await;
    Ok(())
}

async fn blacklist_remove(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    // Ack first: the write below is a database round-trip, and a slow one used
    // to blow the 3-second window even though the unblock had happened.
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let Some(user_id) = user_option(ci, "user") else {
        return Ok(());
    };
    let uid = user_id.to_string();
    let removed = match bot
        .db_call(bot.service().blacklist_remove(&guild_id.to_string(), &uid))
        .await
    {
        Ok(removed) => removed,
        Err(err) => {
            error!("blacklist remove failed for {uid}: {err}");
            bot.safe_followup_command(ctx, ci, "⚠️ Could not update the blacklist. Try again.")
                .await;
            return Ok(());
        }
    };
    let text = if removed {
        format!("✅ <@{uid}> can join giveaways again.")
    } else {
        format!("<@{uid}> was not on the blacklist.")
    };
    bot.safe_followup_command(ctx, ci, &text).await;
    Ok(())
}

async fn blacklist_list(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let gid = guild_id.to_string();
    let loaded = match bot
        .db_call(bot.service().blacklist_list(&gid, 100, None))
        .await
    {
        Ok(ids) => bot
            .db_call(bot.service().count_blacklisted(&gid, None))
            .await
            .map(|total| (ids, total)),
        Err(err) => Err(err),
    };
    let (ids, total) = match loaded {
        Ok(pair) => pair,
        Err(err) => {
            error!("blacklist list failed: {err}");
            bot.safe_followup_command(ctx, ci, "⚠️ Could not load the blacklist. Try again.")
                .await;
            return Ok(());
        }
    };
    if ids.is_empty() {
        bot.safe_followup_command(ctx, ci, "Blacklist is empty — nobody is blocked.")
            .await;
        return Ok(());
    }
    // A mention is 21 characters and Discord rejects a message over 2000, so
    // the body stops at 80 — the same chunk size /giveaway_ping uses — while
    // the header keeps the real count instead of the shown one.
    let shown = &ids[..ids.len().min(80)];
    let lines = shown
        .iter()
        .map(|uid| format!("<@{uid}>"))
        .collect::<Vec<_>>()
        .join("\n");
    let extra = if total > shown.len() as i64 {
        format!("\n…plus {} more.", total - shown.len() as i64)
    } else {
        String::new()
    };
    bot.safe_followup_command(
        ctx,
        ci,
        &format!("🚫 **Blocked ({total}):**\n{lines}{extra}"),
    )
    .await;
    Ok(())
}

// -- bans ----------------------------------------------------------------

/// Timed ban + yank entries + strip roles. hours=0 means permanent.
async fn ban(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let gid = guild_id.to_string();
    let Some(user_id) = user_option(ci, "user") else {
        return Ok(());
    };
    let uid = user_id.to_string();
    let hours = int_option(ci, "hours").unwrap_or(0);
    let reason = string_option(ci, "reason").unwrap_or_default();
    let banned_by = ci.user.id.to_string();
    let updated = match bot
        .db_call(
            bot.service()
                .blacklist_add(&gid, &uid, hours, &reason, &banned_by, None),
        )
        .await
    {
        Ok(info) => bot
            .db_call(bot.service().list_active(&gid))
            .await
            .map(|active| (info, active)),
        Err(err) => Err(err),
    };
    let (info, active) = match updated {
        Ok(pair) => pair,
        Err(err) => {
            if let Some(refusal) = err.downcast_ref::<ServiceError>() {
                let text = format!("⚠️ {}", refusal.message);
                bot.safe_followup_command(ctx, ci, &text).await;
            } else {
                error!("ban failed for {uid}: {err}");
                bot.safe_followup_command(ctx, ci, "⚠️ Could not update the ban. Try again.")
                    .await;
            }
            return Ok(());
        }
    };
    for gw in &active {
        // Their entries are gone, so this process's entry markers must go too,
        // or the next honest click is refused as "already entered".
        bot.clear_entered(&gw.id, &uid);
        bot.take_entrants_role(ctx, gw, &uid).await;
    }
    let clean_reason = reason.trim().to_string();
    info!(
        "banned {uid} in guild {gid} for {hours}h reason={:?}",
        clean_reason.chars().take(64).collect::<String>()
    );
    let reason_bit = if clean_reason.is_empty() {
        String::new()
    } else {
        format!(
            " Reason: {}",
            clean_reason.chars().take(256).collect::<String>()
        )
    };
    let permanent = info
        .get("permanent")
        .and_then(Value::as_bool)
        .unwrap_or(hours == 0);
    let expires_at = info.get("expires_at").and_then(Value::as_i64);
    let when = if permanent {
        "permanently".to_string()
    } else if let Some(exp) = expires_at {
        format!("until <t:{}:R>", exp / 1000)
    } else {
        format!("for {hours}h")
    };
    bot.safe_followup_command(
        ctx,
        ci,
        &format!(
            "🚫 <@{uid}> is banned {when} — entries removed from {} running giveaway(s).{reason_bit}",
            active.len()
        ),
    )
    .await;
    Ok(())
}

async fn unban(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let Some(user_id) = user_option(ci, "user") else {
        return Ok(());
    };
    let uid = user_id.to_string();
    let removed = match bot
        .db_call(bot.service().blacklist_remove(&guild_id.to_string(), &uid))
        .await
    {
        Ok(removed) => removed,
        Err(err) => {
            error!("unban failed for {uid}: {err}");
            bot.safe_followup_command(ctx, ci, "⚠️ Could not update the ban. Try again.")
                .await;
            return Ok(());
        }
    };
    let text = if removed {
        format!("✅ <@{uid}> can join giveaways again.")
    } else {
        format!("<@{uid}> was not banned.")
    };
    bot.safe_followup_command(ctx, ci, &text).await;
    Ok(())
}

async fn ban_list(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let gid = guild_id.to_string();
    // A purge failure must not hide the list: expired rows are already treated
    // as absent by every read, so this is only housekeeping.
    if let Err(err) = bot.db_call(bot.service().purge_expired_bans(None)).await {
        error!("ban purge failed: {err}");
    }
    let loaded = match bot
        .db_call(bot.service().blacklist_list_detailed(&gid, 100, None))
        .await
    {
        Ok(rows) => bot
            .db_call(bot.service().count_blacklisted(&gid, None))
            .await
            .map(|total| (rows, total)),
        Err(err) => Err(err),
    };
    let (rows, total) = match loaded {
        Ok(pair) => pair,
        Err(err) => {
            error!("ban list failed: {err}");
            bot.safe_followup_command(ctx, ci, "⚠️ Could not load the ban list. Try again.")
                .await;
            return Ok(());
        }
    };
    if rows.is_empty() {
        bot.safe_followup_command(ctx, ci, "Ban list is empty — nobody is banned.")
            .await;
        return Ok(());
    }
    let shown = &rows[..rows.len().min(20)];
    let mut lines: Vec<String> = Vec::with_capacity(shown.len());
    for row in shown {
        let uid = json_id(row).unwrap_or_default();
        let reason = row
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_string();
        let when = match row.get("expires_at").and_then(Value::as_i64) {
            None => "permanent".to_string(),
            Some(exp) => format!("expires <t:{}:R>", exp / 1000),
        };
        let tail = if reason.is_empty() {
            String::new()
        } else {
            format!(" — {}", reason.chars().take(100).collect::<String>())
        };
        lines.push(format!("<@{uid}> ({when}){tail}"));
    }
    let body = lines.join("\n");
    let extra = if total > shown.len() as i64 {
        format!("\n…plus {} more.", total - shown.len() as i64)
    } else {
        String::new()
    };
    bot.safe_followup_command(ctx, ci, &format!("🚫 **Banned ({total}):**\n{body}{extra}"))
        .await;
    Ok(())
}

// -- timeout bans --------------------------------------------------------

/// Who is sitting out the penalty for joining while timed out.
async fn timeout_bans(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = manage_guard(bot, ctx, ci).await else {
        return Ok(());
    };
    // Ack first: two database round-trips before the answer used to be able to
    // exceed the 3-second window.
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let gid = guild_id.to_string();
    let loaded = match bot
        .db_call(bot.service().list_timeout_bans(&gid, 100))
        .await
    {
        Ok(rows) => bot
            .db_call(bot.service().count_timeout_bans(&gid))
            .await
            .map(|total| (rows, total)),
        Err(err) => Err(err),
    };
    let (mut rows, total) = match loaded {
        Ok(pair) => pair,
        Err(err) => {
            error!("timeout ban list failed: {err}");
            bot.safe_followup_command(ctx, ci, "⚠️ Could not load the timeout bans. Try again.")
                .await;
            return Ok(());
        }
    };
    if rows.is_empty() {
        bot.safe_followup_command(ctx, ci, "No users are currently banned from giveaways.")
            .await;
        return Ok(());
    }
    // Only from the member cache: a fetch per row would be one HTTP request per
    // banned member for what is a snapshot anyway. With nothing cached (members
    // intent off) every id would look like it had left, so plain mentions are
    // used instead of an invented label.
    //
    // The flags are resolved in their own scope: `CacheRef` borrows the cache
    // read lock and is not `Send`, so it must be dropped before the followup
    // below is awaited.
    let in_guild_flags: Vec<bool> = {
        let guild = ctx.cache.guild(guild_id);
        let cached = guild
            .as_ref()
            .map(|guild| !guild.members.is_empty())
            .unwrap_or(false);
        rows.iter()
            .map(|row| {
                if !cached {
                    return true;
                }
                let Some(uid) = json_id(row) else {
                    return false;
                };
                let Some(id) = snowflake(&uid) else {
                    return false;
                };
                guild
                    .as_ref()
                    .map(|guild| guild.members.contains_key(&UserId::new(id)))
                    .unwrap_or(false)
            })
            .collect()
    };
    for (row, in_guild) in rows.iter_mut().zip(in_guild_flags) {
        if let Value::Object(map) = row {
            map.insert("in_guild".to_string(), Value::Bool(in_guild));
        }
    }
    let pages = rows.len().div_ceil(PAGE_SIZE).max(1);
    // The listing is capped, and the embed has to state the real total instead
    // of the size of the page set.
    let hidden = (total - rows.len() as i64).max(0);
    let colour = bot.state.settings.embed_color;
    let embed =
        embeds::timeout_bans_embed(page_rows(&rows, 0), 0, pages as i64, total, colour, hidden);
    let specs = components::participants_pager(0, pages);
    let message = CreateInteractionResponseFollowup::new()
        .embed(convert::to_create_embed(&embed))
        .components(convert::to_action_rows(&specs))
        .ephemeral(true);
    // Key the pager by the message Discord hands back: that id is what a page
    // click arrives attached to.
    match ci.create_followup(ctx, message).await {
        Ok(sent) => store_pager(
            bot,
            sent.id,
            PagerState {
                page: 0,
                pages,
                kind: PagerKind::TimeoutBans {
                    rows,
                    total,
                    hidden,
                },
                touched: Instant::now(),
            },
        ),
        // A dead token leaves the deferred ack and nothing else, as in Python.
        Err(err) => warn!("timeout-ban panel failed: {err}"),
    }
    Ok(())
}

/// One page of rows, clamped to what the listing actually loaded.
fn page_rows(rows: &[Value], page: usize) -> &[Value] {
    let start = page * PAGE_SIZE;
    if start >= rows.len() {
        return &[];
    }
    &rows[start..(start + PAGE_SIZE).min(rows.len())]
}

/// Record a pager, pruning the ones whose buttons are already dead.
///
/// `buttons.rs` owns the same pair of helpers for the participant panel; both
/// write the one map keyed by the ephemeral message the panel rendered into.
fn store_pager(bot: &Bot, message_id: MessageId, state: PagerState) {
    let Ok(mut guard) = bot.state.pagers.lock() else {
        return;
    };
    guard.retain(|_, pager| pager.touched.elapsed().as_secs() <= VIEW_TIMEOUT_SECS);
    guard.insert(message_id, state);
}

// -- request-time probe --------------------------------------------------

/// Live cost of the cache path of one request — not a gateway ping.
///
/// Every leg is measured, never simulated: one PING through the governed shared
/// client, then the real cached readers on their real keys. Each reader runs
/// twice, so the report shows what an empty cache costs (the miss) beside the
/// warm read a repeat click pays.
///
/// No database leg is timed or printed. The one database call that remains —
/// the active-giveaway lookup — exists only to supply a real key for the cached
/// readers, so it is neither timed nor reported.
async fn request_time(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "Use this in a server.").await;
        return Ok(());
    };
    let started = Instant::now();
    // Reference only: one yield back through the runtime. A healthy loop answers
    // in ~0.01ms; milliseconds here mean the loop was stalled (blocked callback
    // / CPU steal), and that same stall sits inside the ack number below. Never
    // in the slowest-leg race.
    let lag_started = Instant::now();
    tokio::task::yield_now().await;
    let loop_lag_ms = Some(lag_started.elapsed().as_secs_f64() * 1000.0);

    let ack_started = Instant::now();
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let ack_ms = Some(ack_started.elapsed().as_secs_f64() * 1000.0);

    let gid = guild_id.to_string();
    // serenity 0.12 does not publish the shard heartbeat latency to handlers
    // (`ShardMessenger` has no accessor), so this leg is always unmeasured.
    let gateway_ms: Option<f64> = None;

    let redis: &RedisCache = &bot.state.redis;
    let redis_on = redis.configured();

    let mut redis_ms: Option<f64> = None;
    // The PING leg's note: a cache that is off never measured one, so the
    // fall-through explanation is the default rather than a dead assignment.
    let mut redis_note =
        "not configured (no REDIS_URL) — every read here falls through to the database".to_string();
    let mut cache_ops: Option<i64> = None;
    let mut redis_hits: Option<i64> = None;
    let mut redis_misses: Option<i64> = None;
    let mut redis_skipped: Option<i64> = None;
    let mut redis_failed: Option<i64> = None;
    let mut count_ms: Option<f64> = None;
    let mut count_note = String::new();
    let mut role_ms: Option<f64> = None;
    let mut role_note = String::new();
    let mut list_ms: Option<f64> = None;
    let mut list_note = String::new();
    // The same pair of cached reads as a total, first (cold) then second (warm)
    // run, so the repeat-click cost can be compared honestly.
    let mut cold_total_ms: Option<f64> = None;
    let mut warm_total_ms: Option<f64> = None;
    // Only true when the second run of the cached pair was answered from the
    // cache in full; until then the report must not claim a cache hit.
    let mut warm_pair = false;
    let mut cache_gets: Option<i64> = None;
    let mut cache_stores: Option<i64> = None;
    let mut redis_probe_ms: Option<f64> = None;
    // Same shape as the PING note: default to "not configured" so the branch
    // below only has to overwrite it when the write path was actually measured.
    let mut redis_probe_note = "not configured".to_string();

    // The one database call still made here: the same pre-ack list the Join
    // button reads. Deliberately not timed and not reported, and a failure is
    // not fatal: with nothing live the probe falls back to a throwaway preview
    // row for the same purpose.
    let live = match bot.db_call(bot.service().list_active(&gid)).await {
        Ok(live) => live,
        Err(err) => {
            error!("request-time active-giveaway lookup failed: {err}");
            Vec::new()
        }
    };
    // A preview key must exist, or the cache legs can never report a HIT for
    // anyone — the Join button only appears on a running giveaway, so a probe
    // that refuses without one can only ever print "failed" and tell the
    // invoker nothing about Redis. This read is real: the preview row is
    // written and read through the same code the create path uses.
    let mut preview_id = String::new();
    if live.is_empty() {
        let channel_id = ci.channel_id.to_string();
        let created_by = ci.user.id.to_string();
        let created = bot
            .db_call(bot.service().create(
                &gid,
                &channel_id,
                "Cache preview (deleted immediately)",
                1,
                300,
                &created_by,
                None,
                None,
                None,
                0,
                0,
                None,
                None,
                None,
                0,
            ))
            .await;
        match created {
            Ok(preview) => preview_id = preview.id,
            Err(err) => error!("request-time preview row could not be created: {err}"),
        }
    }
    let gw_id = live.first().map(|gw| gw.id.clone()).unwrap_or_default();

    if redis_on {
        let before = redis.stats().await;

        // Leg 1a: the active-giveaway list a click reads. No micro-TTL here — it
        // is the key create/end/discard invalidate — so it is the cheapest
        // honest read to show beside the count. Read BEFORE the PING so the
        // HIT/MISS is the state a click would have found, not one this probe
        // just warmed.
        let leg_started = Instant::now();
        match bot.db_call(bot.service().list_active_cached(&gid)).await {
            Ok(list_cached) => {
                list_ms = Some(leg_started.elapsed().as_secs_f64() * 1000.0);
                let before_hits = counter(&before, "hits").unwrap_or(0);
                let now_hits = counter(&redis.stats().await, "hits").unwrap_or(0);
                list_note = if now_hits > before_hits {
                    format!(
                        "HIT - {} active giveaway(s), from the cache",
                        list_cached.len()
                    )
                } else {
                    format!(
                        "MISS (read the database) - {} active giveaway(s)",
                        list_cached.len()
                    )
                };
            }
            Err(err) => {
                error!("request-time cached list probe failed: {err}");
                list_note = "cached list probe failed".to_string();
            }
        }

        // `ping_ms` reports its own failure as None and never raises, so the
        // note is unconditional on this path — exactly like the Python source.
        redis_ms = redis.ping_ms().await;
        redis_note = "PING through the shared client (auth + pool reuse included)".to_string();

        // The cached readers live on the service; it only has them when a cache
        // is wired in. Reading through the service is what a real click does.
        if bot.state.service.redis().is_none() {
            count_note = "no cache wired into the service".to_string();
            role_note = count_note.clone();
        } else if gw_id.is_empty() && preview_id.is_empty() {
            count_note = "no live giveaway, and the preview row could not be created".to_string();
        } else {
            // Twice on purpose: run 1 is what a cold cache costs (it falls
            // through to the database), run 2 is the warm cost a click pays.
            let probe_id = if gw_id.is_empty() {
                preview_id.clone()
            } else {
                gw_id.clone()
            };
            let mut count_runs: Vec<Leg> = Vec::with_capacity(2);
            let mut role_runs: Vec<Leg> = Vec::with_capacity(2);
            let mut n_cached: Option<i64> = None;
            for _ in 0..2 {
                let (ms, value, served) = timed_cached(redis, || async {
                    bot.service().entry_count_cached(&probe_id).await.ok()
                })
                .await;
                if value.is_some() {
                    n_cached = value;
                }
                count_runs.push(Leg {
                    ms,
                    label: leg_label(ms, served),
                    served,
                });
                let (ms, _role, served) = timed_cached(redis, || async {
                    bot.service().get_notify_role_cached(&gid).await
                })
                .await;
                role_runs.push(Leg {
                    ms,
                    label: leg_label(ms, served),
                    served,
                });
            }
            // The headline number is the warm run (what a repeat click pays);
            // both runs stay visible in the note beside it.
            let warm_count_ms = count_runs[1].ms;
            let warm_role_ms = role_runs[1].ms;
            count_ms = warm_count_ms;
            role_ms = warm_role_ms;
            // "Served from the cache" is a claim, not a label: it may only be
            // printed when BOTH runs of the pair were answered without a
            // fall-through. If either read the database, the warm number is the
            // cost of a read that missed.
            warm_pair = count_runs[1].served == Some(true) && role_runs[1].served == Some(true);
            let cold_vals: Vec<f64> = [count_runs[0].ms, role_runs[0].ms]
                .into_iter()
                .flatten()
                .collect();
            if cold_vals.len() == 2 {
                cold_total_ms = Some(cold_vals.iter().sum());
            }
            let warm_vals: Vec<f64> = [warm_count_ms, warm_role_ms]
                .into_iter()
                .flatten()
                .collect();
            if warm_vals.len() == 2 {
                warm_total_ms = Some(warm_vals.iter().sum());
            }
            // A failed count read has no value, and Python printed the same
            // "None" there rather than inventing a zero.
            let entrants = n_cached
                .map(|n| n.to_string())
                .unwrap_or_else(|| "None".to_string());
            count_note = format!("{} - {entrants} entrant(s)", runs_note(&count_runs));
            role_note = runs_note(&role_runs);
            if gw_id.is_empty() {
                // Say plainly that the per-giveaway key was measured on a
                // throwaway row, so nobody reads it as a live giveaway.
                let note = "(no live giveaway — measured on a preview row this probe deleted)";
                count_note = format!("{count_note} {note}");
                role_note = format!("{role_note} {note}");
            }
        }

        let after = redis.stats().await;
        let delta = |name: &str| match (counter(&before, name), counter(&after, name)) {
            (Some(before), Some(after)) => Some(after - before),
            _ => None,
        };
        redis_hits = delta("hits");
        redis_misses = delta("misses");
        redis_skipped = delta("skipped");
        redis_failed = delta("failed");
        cache_ops = delta("attempted");
        cache_stores = delta("ok");
        cache_gets = match (redis_hits, redis_misses) {
            (Some(hits), Some(misses)) => Some(hits + misses),
            _ => None,
        };
        // Python also cleared both notes when `stats()` returned nothing after
        // having returned something; `stats()` is infallible here, so that
        // branch has no equivalent.
    }

    if !preview_id.is_empty() {
        // The preview exists only to give the cache legs a real key to read. It
        // must not outlive the probe that made it.
        if let Err(err) = bot.db_call(bot.service().discard(&preview_id)).await {
            error!("request-time preview cleanup failed: {err}");
        }
    }

    // Leg 2: one real SET + DEL on a probe-only Redis key, so the write path is
    // measured and not just the reads. The key is namespaced and deleted again
    // on every path.
    const PROBE_KEY: &str = "probe:gw_request_time";
    if redis_on {
        let probe_started = Instant::now();
        let wrote = redis.set(PROBE_KEY, "1", 10.0).await && redis.delete(&[PROBE_KEY]).await >= 0;
        redis_probe_ms = Some(probe_started.elapsed().as_secs_f64() * 1000.0);
        redis_probe_note = if wrote {
            "SET then DEL one probe key (2 round trips)".to_string()
        } else {
            "SET/DEL refused (Redis degraded or over budget)".to_string()
        };
    }

    // Leg 6: a real Discord REST send, timed. It is then edited into the report
    // so the invoker sees one ephemeral reply, not two.
    let reply_started = Instant::now();
    let sent = ci
        .create_followup(
            ctx,
            CreateInteractionResponseFollowup::new()
                .content("Measuring Discord reply leg…")
                .ephemeral(true),
        )
        .await;
    let (probe_msg, rest_ms) = match sent {
        Ok(message) => (
            Some(message),
            Some(reply_started.elapsed().as_secs_f64() * 1000.0),
        ),
        Err(err) => {
            warn!("request-time reply probe failed: {err}");
            (None, None)
        }
    };
    let total_ms = started.elapsed().as_secs_f64() * 1000.0;

    // One decimal on purpose: 0.0ms is only reachable when the cache answered
    // without a network round trip, so the precision is the evidence that the
    // reader was served from Redis and not from the database. A leg that never
    // ran says so instead of showing 0.
    let leg_text = |value: Option<f64>| match value {
        Some(ms) => format!("{ms:.1}ms"),
        None => "not measured".to_string(),
    };
    let ops_text = || -> String {
        let Some(gets) = cache_gets else {
            return "n/a".to_string();
        };
        let mut bits = vec![format!("{gets} get(s)")];
        if let Some(stores) = cache_stores {
            bits.push(format!("{stores} set(s)"));
        }
        if let Some(ops) = cache_ops {
            bits.push(format!("{ops} op(s)"));
        }
        if let (Some(hits), Some(misses)) = (redis_hits, redis_misses) {
            bits.push(format!("{hits} hit / {misses} miss"));
        }
        if let Some(skipped) = redis_skipped.filter(|value| *value != 0) {
            bits.push(format!("{skipped} skipped over budget"));
        }
        if let Some(failed) = redis_failed.filter(|value| *value != 0) {
            bits.push(format!("{failed} failed"));
        }
        bits.join(", ")
    };

    let short = [
        ("Discord ack", ack_ms),
        ("Discord reply", rest_ms),
        ("Redis ping", redis_ms),
        ("Redis cache count", count_ms),
        ("Redis cache list", list_ms),
        ("Redis cache notify-role", role_ms),
        ("Redis write", redis_probe_ms),
    ];
    // Python's `max` keeps the first leg on a tie, so the fold only replaces a
    // strictly slower one.
    let slowest = short
        .iter()
        .filter(|(_, value)| value.is_some())
        .fold(None::<(&str, f64)>, |best, (name, value)| {
            let value = value.unwrap_or_default();
            match best {
                Some((_, best_value)) if best_value >= value => best,
                _ => Some((name, value)),
            }
        })
        .map(|(name, _)| name)
        .unwrap_or("n/a");

    let gateway_txt = match gateway_ms {
        Some(ms) => format!("{ms:.0}ms"),
        None => "n/a (offline)".to_string(),
    };
    let lag_txt = match loop_lag_ms {
        Some(ms) => format!("{ms:.2}ms"),
        None => "n/a".to_string(),
    };
    let warm_txt = match warm_total_ms {
        None => "n/a".to_string(),
        Some(warm) => match cold_total_ms {
            Some(cold) if cold > warm => format!("{cold:.0}ms cold -> {warm:.0}ms warm"),
            _ => format!("{warm:.0}ms"),
        },
    };

    let lines = [
        "Request-time breakdown — live legs, timed just now:".to_string(),
        format!(
            "• Cached reads this request (repeat click): **{}**{}",
            leg_text(warm_total_ms),
            if warm_pair {
                " — same keys served from the cache, no database read at all"
            } else {
                " — NOT a cache hit: the second read still fell through to the database, so this is not what a warm click pays"
            }
        ),
        format!(
            "• First read on an empty cache: **{}** (Redis miss) — that path falls through to the database, which is not timed in this report",
            leg_text(cold_total_ms)
        ),
        "• — Discord, the one leg the cache cannot cover —".to_string(),
        format!("• Discord ack (defer): **{}**", leg_text(ack_ms)),
        format!("• Discord reply (REST send): **{}**", leg_text(rest_ms)),
        "• — Redis: every leg below is one real cache call, timed —".to_string(),
        format!(
            "• Redis cache list: **{}** — {}",
            leg_text(list_ms),
            or_na(&list_note)
        ),
        format!(
            "• Redis cache count: **{}** — {}",
            leg_text(count_ms),
            or_na(&count_note)
        ),
        format!(
            "• Redis cache notify-role: **{}** — {}",
            leg_text(role_ms),
            or_na(&role_note)
        ),
        format!(
            "• Redis ping: **{}** — {}",
            leg_text(redis_ms),
            or_na(&redis_note)
        ),
        format!(
            "• Redis write: **{}** — {}",
            leg_text(redis_probe_ms),
            or_na(&redis_probe_note)
        ),
        format!("• Redis ops this command: {}", ops_text()),
        "• — reference only, neither is a cache read —".to_string(),
        format!("• Gateway heartbeat: **{gateway_txt}** (Discord websocket)"),
        format!("• Event-loop lag: **{lag_txt}** (sleep(0) sample)"),
        format!("• Warm cache legs (2nd run, what a repeat click pays): **{warm_txt}**"),
        format!("• Slowest leg: **{slowest}**"),
        format!(
            "• Total measured: **{total_ms:.0}ms** (excludes this message's edit and the one untimed database lookup this probe makes for its key)"
        ),
        String::new(),
        "_(No database leg is measured or printed by this command. A cache miss and a Redis outage both fall through to the database — that cost is real and is deliberately not shown here; read it from the database side, not from this report.)_".to_string(),
    ];
    let report = lines.join("\n");

    match probe_msg {
        // One message, not a spam of replies: the timed probe is edited in
        // place into the final report.
        Some(message) => {
            let edited = ci
                .edit_followup(
                    ctx,
                    message.id,
                    CreateInteractionResponseFollowup::new().content(&report),
                )
                .await;
            if let Err(err) = edited {
                warn!("request-time report edit failed: {err}");
            }
        }
        None => bot.safe_followup_command(ctx, ci, &report).await,
    }
    Ok(())
}

/// One run of a cached reader: (milliseconds, value, served-from-cache).
///
/// `served` is read off the cache's own hit counter, never guessed, so the
/// report can say HIT or MISS with the cost that came with it. A warm read and
/// a fall-through to the database are very different numbers and must never be
/// blurred into one.
struct Leg {
    ms: Option<f64>,
    label: String,
    served: Option<bool>,
}

async fn timed_cached<T, F, Fut>(
    redis: &RedisCache,
    read: F,
) -> (Option<f64>, Option<T>, Option<bool>)
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = Option<T>>,
{
    let before = counter(&redis.stats().await, "hits");
    let started = Instant::now();
    let value = read().await;
    let elapsed = started.elapsed().as_secs_f64() * 1000.0;
    let Some(value) = value else {
        return (None, None, None);
    };
    let served = match (before, counter(&redis.stats().await, "hits")) {
        (Some(before), Some(after)) => Some(after > before),
        _ => None,
    };
    (Some(elapsed), Some(value), served)
}

/// One counter out of `RedisCache::stats()`'s `counters` object.
fn counter(stats: &Value, name: &str) -> Option<i64> {
    stats.get("counters")?.get(name)?.as_i64()
}

fn leg_label(ms: Option<f64>, served: Option<bool>) -> String {
    if ms.is_none() {
        return "failed".to_string();
    }
    match served {
        None => "hit/miss unknown".to_string(),
        Some(true) => "HIT".to_string(),
        Some(false) => "MISS (fell through to the database)".to_string(),
    }
}

/// Both runs of one reader, in one phrase when they agree.
fn runs_note(runs: &[Leg]) -> String {
    if runs.len() == 2 && runs[0].label == runs[1].label {
        // Both runs took the same path: one phrase says it better than
        // "1st X; 2nd X", which reads like a stutter.
        let label = &runs[1].label;
        return match runs[1].ms {
            None => format!("both runs {label}"),
            Some(second) => format!(
                "both runs {label}, 1st {:.0}ms -> 2nd {second:.0}ms",
                runs[0].ms.unwrap_or_default()
            ),
        };
    }
    let mut parts = Vec::with_capacity(runs.len());
    for (index, leg) in runs.iter().enumerate() {
        let shown = if leg.label == "failed" {
            String::new()
        } else {
            match leg.ms {
                Some(ms) => format!(" {ms:.0}ms"),
                None => " n/a".to_string(),
            }
        };
        parts.push(format!(
            "{}{shown} {}",
            if index == 0 { "1st" } else { "2nd" },
            leg.label
        ));
    }
    parts.join("; ")
}

/// Python's `note or "n/a"`.
fn or_na(note: &str) -> &str {
    if note.is_empty() {
        "n/a"
    } else {
        note
    }
}
