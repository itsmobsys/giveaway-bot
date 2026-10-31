//! Component clicks: join, leave, participants, claim, pager.
//!
//! Every click is dispatched off the custom id alone (`gw_join:gw_abc`), so a
//! button posted before a restart still works and nothing is registered per
//! giveaway. A click is acknowledged before any database work
//! (`defer_ephemeral`) and then answered with an ephemeral followup, which is
//! exactly `bot.py`'s `_safe_defer` + `_safe_followup` pair.
//!
//! The pager is the only state: `discord.py` kept it on a `View` instance and
//! serenity has no such object, so [`PagerState`] lives in
//! `BotState::pagers`, keyed by the ephemeral message it renders into and
//! pruned after [`VIEW_TIMEOUT_SECS`].

use std::time::Instant;

use gwbot_core::components::{self, Click, PAGE_SIZE, VIEW_TIMEOUT_SECS};
use gwbot_core::embeds;
use gwbot_core::models::{Giveaway, ServiceError, TIMEOUT_BAN_KIND};
use serde_json::Value;
use serenity::all::{
    ComponentInteraction, Context, CreateInteractionResponse, CreateInteractionResponseFollowup,
    CreateInteractionResponseMessage, MessageId, Timestamp,
};
use tracing::{error, info, warn};

use super::{Bot, PagerKind, PagerState, PARTICIPANT_WINDOW};
use crate::convert;

/// The four eligibility subqueries `join_snapshot` returns alongside the row.
type Snapshot = Option<(bool, i64, i64, bool)>;

/// What a page click says once its pager is gone.
///
/// `discord.py` dropped these clicks silently (the `View` had timed out, so no
/// callback ran). An interaction must be answered here, and silence reads as a
/// broken bot, so the click is told to open the panel again.
const PAGER_EXPIRED: &str = "This list expired — click Participants again.";

pub(crate) async fn handle_component(bot: &Bot, ctx: &Context, ci: ComponentInteraction) {
    let Some(click) = components::parse_custom_id(&ci.data.custom_id) else {
        return;
    };
    match click {
        Click::Join(id) => handle_join(bot, ctx, &ci, &id).await,
        Click::Leave(id) => handle_leave(bot, ctx, &ci, &id).await,
        Click::Participants(id) => handle_participants(bot, ctx, &ci, &id).await,
        Click::Claim(id) => handle_claim(bot, ctx, &ci, &id).await,
        Click::PagePrev => handle_page(bot, ctx, &ci, false).await,
        Click::PageNext => handle_page(bot, ctx, &ci, true).await,
        // Decoration only: a disabled component is never dispatched anyway.
        Click::PageCounter => {}
    }
}

// -- join ----------------------------------------------------------------

async fn handle_join(bot: &Bot, ctx: &Context, ci: &ComponentInteraction, giveaway_id: &str) {
    let (Some(guild_id), Some(member)) = (ci.guild_id, ci.member.clone()) else {
        // Not deferred on purpose: Python answered this one with a plain
        // ephemeral response, before any acknowledgement.
        reply_ephemeral(ctx, ci, "Use this button inside the server.").await;
        return;
    };
    if !bot.defer_component(ctx, ci).await {
        return;
    }
    let guild_id_str = guild_id.to_string();
    let uid = member.user.id.to_string();
    let name = member.display_name().to_string();
    let roles: Vec<String> = member
        .roles
        .iter()
        .map(|role| role.get().to_string())
        .collect();
    let created_ts = Some(member.user.created_at().unix_timestamp() as f64);
    // Discord's native timeout, read here but acted on inside the service's
    // eligibility gate, so it lands in the same place as every other
    // "you may not enter" rule instead of becoming a second join path. For a
    // component click the member comes from the interaction payload, so this is
    // the state Discord reported when they clicked.
    let timed_out = member
        .communication_disabled_until
        .map(|until| until > Timestamp::now())
        .unwrap_or(false);
    // A plain in-process set of (giveaway, user) ids whose join COMMITTED here.
    // It only ever short-circuits a refusal: a missing marker runs the normal
    // path and the database still decides.
    if bot.entered_recently(giveaway_id, &uid) {
        bot.safe_followup_component(ctx, ci, "⚠️ You are already entered.")
            .await;
        return;
    }
    let (gw, snapshot) = match join_header(bot, giveaway_id, &guild_id_str, &uid).await {
        Ok(pair) => pair,
        Err(err) => {
            if err.downcast_ref::<ServiceError>().is_some() {
                // Log the id plus what IS live: that tells a stale button (id
                // absent from the live list) apart from a phantom empty read
                // (id present but the lookup missed it).
                let live = bot
                    .db_call(bot.service().list_all_active(50))
                    .await
                    .unwrap_or_default();
                let live_ids: Vec<&str> = live.iter().map(|row| row.id.as_str()).collect();
                let live = if live_ids.is_empty() {
                    "none".to_string()
                } else {
                    live_ids.join(",")
                };
                warn!(
                    "join for unknown giveaway {giveaway_id} (guild {guild_id_str}, live: {live})"
                );
                let text = format!("Giveaway not found (`{giveaway_id}`).");
                bot.safe_followup_component(ctx, ci, &text).await;
            } else {
                error!("join lookup failed for {giveaway_id}: {err}");
                bot.safe_followup_component(ctx, ci, "⚠️ Could not load the giveaway. Try again.")
                    .await;
            }
            return;
        }
    };
    // One lane for the write and the status/count read: the count the member is
    // told comes out of this snapshot, never out of the cache.
    let joined = bot
        .db_call(bot.service().join_and_refresh(
            &gw,
            &uid,
            &name,
            &roles,
            created_ts,
            // Messages this member sent since the last flush still count.
            bot.pending_messages(&guild_id_str, &uid),
            timed_out,
            snapshot,
        ))
        .await;
    let (status, count) = match joined {
        Ok(pair) => pair,
        Err(err) => {
            let Some(refusal) = err.downcast_ref::<ServiceError>() else {
                error!("join failed for {giveaway_id}: {err}");
                bot.safe_followup_component(ctx, ci, "⚠️ Could not enter you. Try again.")
                    .await;
                return;
            };
            let message = refusal.message.clone();
            let kind = refusal.kind.clone();
            bot.safe_followup_component(ctx, ci, &format!("⚠️ {message}"))
                .await;
            if kind == TIMEOUT_BAN_KIND {
                // The gate dropped the entry they already had, so the entrants
                // role it earned has to go too — in every active giveaway whose
                // entries the service dropped, not just this one. The same
                // sweep clears this process's entry markers: those joins no
                // longer exist, so a cached "already entered" would otherwise
                // refuse their next honest click.
                match bot.db_call(bot.service().list_active(&gw.guild_id)).await {
                    Ok(active) => {
                        for live in active {
                            bot.clear_entered(&live.id, &uid);
                            bot.take_entrants_role(ctx, &live, &uid).await;
                        }
                    }
                    Err(err) => error!("timeout role lookup failed for {uid}: {err}"),
                }
            }
            return;
        }
    };
    // The commit landed. The cache keys its own write made wrong are dropped in
    // the background: a cache hop must never sit between the commit and the
    // member's answer, and the keys heal on their TTL anyway.
    spawn_flush(bot);
    // Only a committed join sets this marker, so a refused click can never
    // poison the next one.
    bot.mark_entered(giveaway_id, &uid);
    let Some(status) = status else {
        bot.safe_followup_component(ctx, ci, "🎟️ You're in!").await;
        return;
    };
    // Ack first: the entry is committed, so the count is honest even if the
    // Discord API work below fails.
    let ack = format!("🎟️ You're in! Entry #{count}.");
    bot.safe_followup_component(ctx, ci, &ack).await;
    if status == "active" {
        // Each post-ack step is isolated so a role-grant failure cannot cancel
        // the embed refresh (or vice versa), and neither may undo the entry.
        // The refresh is debounced: every click grants the role, but only the
        // first in the window re-renders the embed.
        let grant = bot.grant_entrants_role(ctx, &gw, &member, Some(status.as_str()));
        if bot.should_refresh(giveaway_id) {
            let refresh = bot.refresh_embed(ctx, &gw, Some(count), Some(status.as_str()));
            let _ = tokio::join!(grant, refresh);
        } else {
            grant.await;
        }
    } else {
        // The draw won the race: this member joined in the same instant the
        // giveaway ended and the strip pass may already have walked past them.
        // Granting the role now would leave them holding the ping-everyone role
        // of a giveaway that is over. The entry stays but missed this draw, so
        // say so — "You're in!" above must not be the last word.
        bot.take_entrants_role(ctx, &gw, &uid).await;
        bot.safe_followup_component(
            ctx,
            ci,
            "This giveaway just ended — your entry arrived too late for the draw.",
        )
        .await;
    }
}

/// Header + eligibility snapshot for a join click, from the hot cache if warm.
///
/// The cached row is not re-read, but the eligibility snapshot still is: it is
/// the only thing that lets the service's timeout-ban gate skip its own live row
/// read. Dropping it was measured in Python and saved nothing (one read moved
/// from one place to the other), so it stays.
async fn join_header(
    bot: &Bot,
    giveaway_id: &str,
    guild_id: &str,
    uid: &str,
) -> anyhow::Result<(Giveaway, Snapshot)> {
    if let Some(gw) = bot
        .gw_cached(giveaway_id)
        .filter(|gw| gw.guild_id == guild_id)
    {
        let snapshot = bot
            .db_call(async {
                Ok(bot
                    .service()
                    .eligibility_snapshot(guild_id, uid, Some(giveaway_id))
                    .await)
            })
            .await
            .ok()
            .flatten();
        return Ok((gw, snapshot));
    }
    // Fast path: one round trip returns the giveaway row + the four eligibility
    // subqueries (was: two parallel queries on two threads).
    let (gw, snapshot) = bot
        .db_call(bot.service().join_snapshot(giveaway_id, guild_id, uid))
        .await?;
    bot.gw_store(giveaway_id, &gw);
    Ok((gw, snapshot))
}

// -- participants --------------------------------------------------------

async fn handle_participants(
    bot: &Bot,
    ctx: &Context,
    ci: &ComponentInteraction,
    giveaway_id: &str,
) {
    if !bot.defer_component(ctx, ci).await {
        return;
    }
    let viewer = ci.user.id.to_string();
    // A header read this burst already paid for is reused: the panel needs the
    // prize and the winner count, both immutable for a giveaway's lifetime
    // except the count, which the window read supplies anyway.
    let gw = match bot.gw_cached(giveaway_id) {
        Some(gw) => gw,
        None => match bot.db_call(bot.service().get(giveaway_id)).await {
            Ok(gw) => {
                bot.gw_store(giveaway_id, &gw);
                gw
            }
            Err(err) => {
                match err.downcast_ref::<ServiceError>() {
                    Some(refusal) => {
                        let text = format!("⚠️ {}", refusal.message);
                        bot.safe_followup_component(ctx, ci, &text).await;
                    }
                    None => {
                        error!("participants lookup failed for {giveaway_id}: {err}");
                        bot.safe_followup_component(
                            ctx,
                            ci,
                            "⚠️ Could not load the list. Try again.",
                        )
                        .await;
                    }
                }
                return;
            }
        },
    };
    // One bounded window instead of the whole table, and in the SAME read: the
    // true total and the "am I in it" answer. A failed read returns None, which
    // is reported rather than printed as zeros.
    let found = match bot
        .db_call(async {
            Ok(bot
                .service()
                .entries_snapshot(giveaway_id, Some(PARTICIPANT_WINDOW), &viewer)
                .await)
        })
        .await
    {
        Ok(found) => found,
        Err(err) => {
            error!("participants lookup failed for {giveaway_id}: {err}");
            bot.safe_followup_component(ctx, ci, "⚠️ Could not load the list. Try again.")
                .await;
            return;
        }
    };
    let (total, rows, mine) = found.unwrap_or((0, Vec::new(), 0));
    if rows.is_empty() {
        let text = format!("🏆 **{}** — no entrants yet.", gw.prize);
        bot.safe_followup_component(ctx, ci, &text).await;
        return;
    }
    let pages = rows.len().div_ceil(PAGE_SIZE);
    // The panel is a bounded window, not the whole table: say so when the
    // giveaway is bigger than the window, or the header total looks like a lie
    // next to a pager that stops early.
    let shown = (total > rows.len() as i64).then_some(rows.len() as i64);
    let colour = bot.state.settings.embed_color;
    let embed = embeds::participants_embed(
        &gw.prize,
        page_rows(&rows, 0),
        0,
        pages as i64,
        total,
        mine,
        gw.winner_count,
        colour,
        shown,
    );
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
                kind: PagerKind::Participants {
                    prize: gw.prize.clone(),
                    rows,
                    total,
                    mine,
                    winner_count: gw.winner_count,
                    shown,
                },
                touched: Instant::now(),
            },
        ),
        // A dead token leaves the deferred ack and nothing else, as in Python.
        Err(err) => warn!("participants panel failed for {giveaway_id}: {err}"),
    }
}

// -- leave ---------------------------------------------------------------

async fn handle_leave(bot: &Bot, ctx: &Context, ci: &ComponentInteraction, giveaway_id: &str) {
    if !bot.defer_component(ctx, ci).await {
        return;
    }
    let uid = ci.user.id.to_string();
    // The delete and the counter bump are one lane, and the count and status
    // come back with them, so the embed refresh needs no read of its own.
    let (removed, count, live_status) = match bot
        .db_call(bot.service().leave_with_count(giveaway_id, &uid))
        .await
    {
        Ok(result) => result,
        Err(err) => {
            error!("leave failed for {giveaway_id}: {err}");
            bot.safe_followup_component(ctx, ci, "⚠️ Could not update your entry. Try again.")
                .await;
            return;
        }
    };
    // A leave undoes the entry the latch remembers, so the marker goes first:
    // otherwise this member's next join would be refused with "already entered".
    if removed {
        bot.clear_entered(giveaway_id, &uid);
    }
    let answer = if removed {
        "You left the giveaway."
    } else {
        "You were not entered."
    };
    // Reply first, drop the queued keys afterwards: a slow Redis DEL must never
    // sit between the member's action and their answer.
    bot.safe_followup_component(ctx, ci, answer).await;
    spawn_flush(bot);
    if !removed {
        return;
    }
    let guild_id_str = ci.guild_id.map(|id| id.to_string()).unwrap_or_default();
    // The header is immutable except for the entry counter, which the refresh
    // re-reads. A click that just came through the join path has it cached, so
    // re-reading the row here would be a round trip for data already in hand.
    let fresh = match bot
        .gw_cached(giveaway_id)
        .filter(|gw| gw.guild_id == guild_id_str)
    {
        Some(gw) => gw,
        None => match bot.db_call(bot.service().get(giveaway_id)).await {
            Ok(gw) => {
                bot.gw_store(giveaway_id, &gw);
                gw
            }
            Err(err) => {
                if err.downcast_ref::<ServiceError>().is_none() {
                    error!("post-leave lookup failed for {giveaway_id}: {err}");
                }
                return;
            }
        },
    };
    bot.take_entrants_role(ctx, &fresh, &uid).await;
    let live = Some(live_status.as_str()).filter(|status| !status.is_empty());
    bot.refresh_embed(ctx, &fresh, Some(count), live).await;
}

// -- claim ---------------------------------------------------------------

async fn handle_claim(bot: &Bot, ctx: &Context, ci: &ComponentInteraction, giveaway_id: &str) {
    if !bot.defer_component(ctx, ci).await {
        return;
    }
    let uid = ci.user.id.to_string();
    match bot.db_call(bot.service().claim(giveaway_id, &uid)).await {
        Ok(_) => {
            info!("claim recorded for {giveaway_id}/{uid}");
            bot.safe_followup_component(ctx, ci, "🎁 Prize claimed! A host will contact you.")
                .await;
        }
        Err(err) => match err.downcast_ref::<ServiceError>() {
            Some(refusal) => {
                let text = format!("⚠️ {}", refusal.message);
                bot.safe_followup_component(ctx, ci, &text).await;
            }
            None => {
                error!("claim failed for {giveaway_id}: {err}");
                bot.safe_followup_component(ctx, ci, "⚠️ Could not record your claim. Try again.")
                    .await;
            }
        },
    }
}

// -- pager ---------------------------------------------------------------

async fn handle_page(bot: &Bot, ctx: &Context, ci: &ComponentInteraction, forward: bool) {
    let message_id = ci.message.id;
    // Taking the state out of the map keeps the lock off every await below; it
    // goes back only once Discord accepted the edit, because a failed edit must
    // not advance the page the buttons describe (Python restored the old page
    // for the same reason).
    let pager = bot.state.pagers.lock().ok().and_then(|mut guard| {
        guard.retain(|_, pager| is_fresh(pager));
        guard.remove(&message_id)
    });
    let Some(mut pager) = pager else {
        reply_ephemeral(ctx, ci, PAGER_EXPIRED).await;
        return;
    };
    let target = if forward {
        pager.page.saturating_add(1)
    } else {
        pager.page.saturating_sub(1)
    };
    let page = target.min(pager.pages.saturating_sub(1));
    let colour = bot.state.settings.embed_color;
    let (embed, specs) = match &pager.kind {
        PagerKind::Participants {
            prize,
            rows,
            total,
            mine,
            winner_count,
            shown,
        } => (
            embeds::participants_embed(
                prize,
                page_rows(rows, page),
                page as i64,
                pager.pages as i64,
                *total,
                *mine,
                *winner_count,
                colour,
                *shown,
            ),
            components::participants_pager(page, pager.pages),
        ),
        PagerKind::TimeoutBans {
            rows,
            total,
            hidden,
        } => (
            embeds::timeout_bans_embed(
                page_rows(rows, page),
                page as i64,
                pager.pages as i64,
                *total,
                colour,
                *hidden,
            ),
            components::participants_pager(page, pager.pages),
        ),
    };
    // Type 7: update the message the button is attached to. The pager message is
    // an ephemeral followup, and an ephemeral message is only editable through
    // the interaction that owns it — a channel-level PATCH would not find it.
    let response = CreateInteractionResponse::UpdateMessage(
        CreateInteractionResponseMessage::new()
            .embed(convert::to_create_embed(&embed))
            .components(convert::to_action_rows(&specs)),
    );
    match ci.create_response(ctx, response).await {
        Ok(()) => {
            pager.page = page;
            pager.touched = Instant::now();
        }
        Err(err) => warn!("pager edit failed ({err})"),
    }
    store_pager(bot, message_id, pager);
}

/// One page of rows, clamped to what the window actually loaded.
fn page_rows(rows: &[Value], page: usize) -> &[Value] {
    let start = page * PAGE_SIZE;
    if start >= rows.len() {
        return &[];
    }
    &rows[start..(start + PAGE_SIZE).min(rows.len())]
}

/// Whether a pager's buttons are still inside the interaction token's life.
fn is_fresh(pager: &PagerState) -> bool {
    pager.touched.elapsed().as_secs() <= VIEW_TIMEOUT_SECS
}

/// Record a pager, pruning the ones whose buttons are already dead.
fn store_pager(bot: &Bot, message_id: MessageId, state: PagerState) {
    let Ok(mut guard) = bot.state.pagers.lock() else {
        return;
    };
    guard.retain(|_, pager| is_fresh(pager));
    guard.insert(message_id, state);
}

// -- small helpers -------------------------------------------------------

/// A plain (non-deferred) ephemeral answer.
async fn reply_ephemeral(ctx: &Context, ci: &ComponentInteraction, text: &str) {
    let message = CreateInteractionResponseMessage::new()
        .content(text)
        .ephemeral(true);
    if let Err(err) = ci
        .create_response(ctx, CreateInteractionResponse::Message(message))
        .await
    {
        warn!("reply failed ({err})");
    }
}

/// Drain the cache keys a just-committed write invalidated, off the reply path.
///
/// `bot.py` spawned this for the same reason: on a blackholed Redis one awaited
/// DEL costs the full cache timeout, and the member's answer must never wait on
/// a cache hop. The keys heal on their own TTL either way.
fn spawn_flush(bot: &Bot) {
    let bot = bot.clone();
    tokio::spawn(async move { bot.flush_invalidations().await });
}
