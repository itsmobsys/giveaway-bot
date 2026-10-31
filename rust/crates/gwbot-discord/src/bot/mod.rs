//! Gateway wiring: process state, lifecycle, and the helpers every handler shares.
//!
//! `bot.py` kept all of this on one 3,200-line `commands.Bot` subclass. Here the
//! same surface is split by role:
//!
//! - this module owns [`BotState`], the login retry loop, the event handlers and
//!   every helper that more than one command or button needs;
//! - [`commands`] holds the slash commands and autocomplete;
//! - [`buttons`] holds the component clicks;
//! - [`tick`] holds the periodic loops (draw timer, message-count flush,
//!   heartbeat).
//!
//! `Bot` is a cheap cloneable handle over `Arc<BotState>`, so background loops,
//! spawned role cleanups and the serenity client all share one state without
//! cloning the database handle or the Redis client.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use gwbot_core::cache::RedisCache;
use gwbot_core::components;
use gwbot_core::config::Settings;
use gwbot_core::embeds;
use gwbot_core::health::HealthState;
use gwbot_core::models::{Giveaway, PartialFlush};
use gwbot_core::service::{FlushReceipt, GiveawayService};
use gwbot_core::Database;
use serde_json::Value;
use serenity::all::{
    Cache, CacheHttp, ChannelId, ChannelType, Client, CommandInteraction, ComponentInteraction,
    Context, CreateActionRow, CreateAllowedMentions, CreateCommand, CreateInteractionResponse,
    CreateInteractionResponseMessage, CreateMessage, EditMessage, EventHandler, GatewayError,
    GatewayIntents, GuildId, Http, Interaction, Member, Message, MessageId, PartialChannel, Ready,
    RoleId, UserId,
};
use serenity::async_trait;
use tokio::sync::{watch, RwLock, Semaphore};
use tracing::{debug, error, info, warn};

use crate::convert;

mod buttons;
mod commands;
mod commands_admin;
mod tick;

/// Seconds a single database call may run before the caller gives up.
///
/// Turso's warm round trip is ~0.1s; 8s is generations of headroom, but a hung
/// stream must never park a click (or a tick) forever.
const DB_CALL_TIMEOUT_SECS: f64 = 8.0;

/// Seconds a cached giveaway header stays valid mid-burst.
const GW_CACHE_TTL_SECS: f64 = 2.0;

/// Leading-edge refresh window: the first click re-renders, burst mates reuse it.
const REFRESH_DEBOUNCE_SECS: f64 = 2.0;

/// Seconds a duplicate-click marker stays hot. It only has to outlive a double
/// tap (two gateway events, milliseconds apart), so 120s is generous while
/// keeping the whole map tiny; the database remains the real guard.
const ENTERED_TTL_SECS: f64 = 120.0;

/// Hard cap on tracked giveaways, so a long uptime cannot grow the latch map.
const ENTERED_MAX_GIVEAWAYS: usize = 64;

/// How long one process's claim on the tick's live-list sweep lasts.
const SWEEP_LEASE_SECS: f64 = 90.0;

/// How often the cosmetic half of the tick (lists + embed PATCHes) runs.
const LIST_REFRESH_SECS: f64 = 120.0;

/// How many embeds may be re-rendered at the same time.
const REFRESH_CONCURRENCY: usize = 5;

/// Embeds re-rendered every tick: the soonest deadlines always get a slot.
const REFRESH_PER_TICK: usize = 10;

/// Rotating slots for the giveaways past that cap.
const REFRESH_ROTATING: usize = 5;

/// Entrants loaded for the Participants panel (10 per page).
const PARTICIPANT_WINDOW: i64 = 500;

/// How often buffered counts reach the database.
const MESSAGE_FLUSH_SECONDS: u64 = 15;

/// Distinct users that may pile up before an early flush.
const MESSAGE_BUFFER_MAX: usize = 5000;

/// Grace period between a giveaway ending and its entrants role being deleted.
const ROLE_DELETE_DELAY_SECS: f64 = 300.0;

/// How many members may be stripped of the entrants role at the same time.
const ROLE_STRIP_CONCURRENCY: usize = 5;

/// Entrants per gather. One task per entrant would allocate 50k tasks.
const STRIP_BATCH: usize = 200;

/// How long a timed-out flush is given to settle before its batch is requeued.
const FLUSH_RECONCILE_GRACE_SECS: f64 = 10.0;

/// Base seconds to wait before retrying a rate-limited Discord login.
const LOGIN_RETRY_DELAYS: [u64; 4] = [30, 60, 120, 300];

/// Longest wait `retry_wait` will hand back, before jitter.
const LOGIN_RETRY_MAX_WAIT_SECS: u64 = 300;

/// Jitter added to every login retry, so restarts do not stampede together.
const LOGIN_RETRY_JITTER_SECS: u64 = 5;

/// A page of the ephemeral participant/timeout-ban list.
///
/// `discord.py` kept this state on a `View` instance. Serenity has no such
/// object, so the state lives in a map keyed by the message it renders into and
/// is pruned after [`components::VIEW_TIMEOUT_SECS`].
pub(crate) struct PagerState {
    pub page: usize,
    pub pages: usize,
    pub kind: PagerKind,
    pub touched: Instant,
}

/// What a pager is showing, so a page click can re-render without re-reading.
pub(crate) enum PagerKind {
    Participants {
        prize: String,
        rows: Vec<Value>,
        total: i64,
        mine: i64,
        winner_count: i64,
        /// How many rows the panel actually loaded, when it is a window.
        shown: Option<i64>,
    },
    TimeoutBans {
        rows: Vec<Value>,
        total: i64,
        hidden: i64,
    },
}

/// Everything the process shares between handlers and loops.
pub(crate) struct BotState {
    pub settings: Settings,
    pub service: GiveawayService,
    pub redis: RedisCache,
    pub health: Arc<RwLock<HealthState>>,
    /// Set from the built client just before `start()`, so spawned loops have a
    /// gateway handle without waiting for a `Context`.
    pub http: StdMutex<Option<Arc<serenity::all::Http>>>,
    pub cache: StdMutex<Option<Arc<Cache>>>,
    /// guild_id -> [(giveaway_id, prize)], rebuilt on the list refresh.
    pub autocomplete: StdMutex<HashMap<String, Vec<(String, String)>>>,
    /// Same snapshot for ended giveaways (reroll/skipclaim only accept those).
    pub ended_autocomplete: StdMutex<HashMap<String, Vec<(String, String)>>>,
    /// Giveaway ids whose entrants-role delete is already scheduled.
    pub scheduled_role_deletes: StdMutex<HashSet<String>>,
    /// (guild_id, user_id) -> messages seen since the last flush.
    pub message_buffer: StdMutex<HashMap<(String, String), i64>>,
    /// Tick counter; the entry-wipe sweep runs every ~20 ticks.
    pub tick_count: AtomicU64,
    /// Where the rotating embed-refresh window starts on the next tick.
    pub refresh_cursor: AtomicUsize,
    /// giveaway_id -> last refresh instant (leading-edge debounce).
    pub refresh_stamp: StdMutex<HashMap<String, Instant>>,
    /// Role grants/revokes are paced: 50 at once trips the per-route limit.
    pub role_gate: Semaphore,
    /// giveaway_id -> (Giveaway, stored-at), 2s TTL.
    pub gw_cache: StdMutex<HashMap<String, (Giveaway, Instant)>>,
    /// giveaway_id -> {user_id: joined-at} for joins that committed here.
    pub entered: StdMutex<HashMap<String, HashMap<String, Instant>>>,
    /// Last time the live/ended lists were rebuilt.
    pub last_list_refresh: StdMutex<Option<Instant>>,
    /// One-shot guards for the startup sweeps (a reconnect must not redo them).
    pub commands_registered: AtomicBool,
    pub guild_cmd_cleanup_done: AtomicBool,
    /// Open participant/timeout-ban pagers, keyed by their message.
    pub pagers: StdMutex<HashMap<MessageId, PagerState>>,
    /// Flips to `true` on the first `ready`, releasing the periodic loops.
    pub ready_tx: watch::Sender<bool>,
    pub ready_rx: watch::Receiver<bool>,
}

impl BotState {
    fn new(
        settings: Settings,
        service: GiveawayService,
        redis: RedisCache,
        health: Arc<RwLock<HealthState>>,
    ) -> Self {
        let (ready_tx, ready_rx) = watch::channel(false);
        Self {
            settings,
            service,
            redis,
            health,
            http: StdMutex::new(None),
            cache: StdMutex::new(None),
            autocomplete: StdMutex::new(HashMap::new()),
            ended_autocomplete: StdMutex::new(HashMap::new()),
            scheduled_role_deletes: StdMutex::new(HashSet::new()),
            message_buffer: StdMutex::new(HashMap::new()),
            tick_count: AtomicU64::new(0),
            refresh_cursor: AtomicUsize::new(0),
            refresh_stamp: StdMutex::new(HashMap::new()),
            role_gate: Semaphore::new(ROLE_STRIP_CONCURRENCY),
            gw_cache: StdMutex::new(HashMap::new()),
            entered: StdMutex::new(HashMap::new()),
            last_list_refresh: StdMutex::new(None),
            commands_registered: AtomicBool::new(false),
            guild_cmd_cleanup_done: AtomicBool::new(false),
            pagers: StdMutex::new(HashMap::new()),
            ready_tx,
            ready_rx,
        }
    }
}

/// A cheap handle over the shared process state.
#[derive(Clone)]
pub struct Bot {
    state: Arc<BotState>,
}

/// The two gateway handles a Discord call needs, without a `Context`.
///
/// serenity 0.12.5 only builds a `Context` inside its own dispatch, so the tick
/// loop has none. It does have the `Arc<Cache>` / `Arc<Http>` pair the client
/// publishes, and those are all a send or an edit actually reads. Carrying them
/// here means the loops and the event handlers share one implementation of each
/// side effect instead of two copies that can drift apart.
#[derive(Clone)]
pub(crate) struct Gateway {
    pub(crate) cache: Arc<Cache>,
    pub(crate) http: Arc<Http>,
}

impl From<&Context> for Gateway {
    fn from(ctx: &Context) -> Self {
        Self {
            cache: ctx.cache.clone(),
            http: ctx.http.clone(),
        }
    }
}

impl CacheHttp for Gateway {
    fn http(&self) -> &Http {
        self.http.as_ref()
    }

    fn cache(&self) -> Option<&Arc<Cache>> {
        Some(&self.cache)
    }
}

impl Bot {
    fn new(
        settings: Settings,
        service: GiveawayService,
        redis: RedisCache,
        health: Arc<RwLock<HealthState>>,
    ) -> Self {
        Self {
            state: Arc::new(BotState::new(settings, service, redis, health)),
        }
    }

    // -- accessors -------------------------------------------------------

    fn http(&self) -> Option<Arc<serenity::all::Http>> {
        self.state.http.lock().ok().and_then(|g| g.clone())
    }

    fn cache(&self) -> Option<Arc<Cache>> {
        self.state.cache.lock().ok().and_then(|g| g.clone())
    }

    fn service(&self) -> &GiveawayService {
        &self.state.service
    }

    // -- lifecycle -------------------------------------------------------

    async fn set_health(&self, status: &str, discord: &str) {
        let mut guard = self.state.health.write().await;
        guard.status = status.to_string();
        guard.discord = discord.to_string();
    }

    /// Run the gateway until a shutdown signal arrives, retrying rate limits.
    ///
    /// `discord.py` built a fresh `GiveawayBot` per attempt because `start()`
    /// could not be reused. Here a fresh serenity `Client` is built per attempt
    /// while the shared [`BotState`] (database, cache, latches) persists.
    async fn login_loop(&self) -> Result<()> {
        let intents =
            GatewayIntents::GUILDS | GatewayIntents::GUILD_MESSAGES | GatewayIntents::GUILD_MEMBERS;
        let mut attempt: usize = 0;
        loop {
            attempt += 1;
            let mut client = match Client::builder(&self.state.settings.bot_token, intents)
                .event_handler(self.clone())
                .await
            {
                Ok(client) => client,
                Err(err) => {
                    warn!("could not build the Discord client: {err}");
                    self.set_health(&format!("retrying-{attempt}"), "rate-limited")
                        .await;
                    tokio::time::sleep(retry_wait(attempt, None)).await;
                    continue;
                }
            };
            // The spawned loops have no `Context`, so publish the handles the
            // built client already owns before it starts consuming events.
            if let Ok(mut slot) = self.state.http.lock() {
                *slot = Some(client.http.clone());
            }
            if let Ok(mut slot) = self.state.cache.lock() {
                *slot = Some(client.cache.clone());
            }
            let shard_manager = client.shard_manager.clone();
            self.set_health("online", "online").await;
            let mut handle = tokio::spawn(async move { client.start().await });

            tokio::select! {
                joined = &mut handle => {
                    match joined {
                        Ok(Ok(())) => info!("Discord client stopped"),
                        Ok(Err(err)) => {
                            if is_fatal_login(&err) {
                                self.set_health("online", "auth-failed").await;
                                return Err(err.into());
                            }
                            warn!("Discord login failed: {}", login_summary(&err));
                        }
                        Err(join) => warn!("Discord client task ended unexpectedly: {join}"),
                    }
                    self.set_health(&format!("retrying-{attempt}"), "rate-limited")
                        .await;
                    tokio::time::sleep(retry_wait(attempt, None)).await;
                }
                _ = shutdown_signal() => {
                    info!("shutdown requested; closing the gateway");
                    shard_manager.shutdown_all().await;
                    handle.abort();
                    self.set_health("online", "stopped").await;
                    return Ok(());
                }
            }
        }
    }

    // -- database lane ---------------------------------------------------

    /// Run one service call under the database budget.
    ///
    /// `add_message_counts` deliberately does not go through here: it needs the
    /// [`FlushReceipt`] that a cancelled future would otherwise hide.
    async fn db_call<T>(&self, fut: impl Future<Output = Result<T>>) -> Result<T> {
        match tokio::time::timeout(Duration::from_secs_f64(DB_CALL_TIMEOUT_SECS), fut).await {
            Ok(result) => result,
            Err(_) => Err(anyhow!("database call timed out")),
        }
    }

    /// Drain the cache keys a just-committed write invalidated. Never raises.
    async fn flush_invalidations(&self) {
        let drained = self.state.service.flush_invalidations().await;
        debug!("cache invalidations drained: {drained}");
    }

    // -- hot header cache ------------------------------------------------

    fn gw_cached(&self, giveaway_id: &str) -> Option<Giveaway> {
        let guard = self.state.gw_cache.lock().ok()?;
        let (gw, stored) = guard.get(giveaway_id)?;
        if stored.elapsed().as_secs_f64() > GW_CACHE_TTL_SECS {
            return None;
        }
        Some(gw.clone())
    }

    fn gw_store(&self, giveaway_id: &str, gw: &Giveaway) {
        if let Ok(mut guard) = self.state.gw_cache.lock() {
            guard.insert(giveaway_id.to_string(), (gw.clone(), Instant::now()));
        }
    }

    fn gw_drop(&self, giveaway_id: &str) {
        if let Ok(mut guard) = self.state.gw_cache.lock() {
            guard.remove(giveaway_id);
        }
    }

    // -- duplicate-click latch -------------------------------------------

    /// True only if THIS process saw this member's join commit.
    ///
    /// A `false` means "unknown", never "not entered": the caller then runs the
    /// ordinary path and Turso decides.
    fn entered_recently(&self, giveaway_id: &str, user_id: &str) -> bool {
        let Ok(mut guard) = self.state.entered.lock() else {
            return false;
        };
        let Some(members) = guard.get_mut(giveaway_id) else {
            return false;
        };
        match members.get(user_id) {
            Some(ts) if ts.elapsed().as_secs_f64() <= ENTERED_TTL_SECS => true,
            Some(_) => {
                members.remove(user_id);
                false
            }
            None => false,
        }
    }

    fn mark_entered(&self, giveaway_id: &str, user_id: &str) {
        let Ok(mut guard) = self.state.entered.lock() else {
            return;
        };
        if guard.len() >= ENTERED_MAX_GIVEAWAYS && !guard.contains_key(giveaway_id) {
            guard.clear();
        }
        guard
            .entry(giveaway_id.to_string())
            .or_default()
            .insert(user_id.to_string(), Instant::now());
    }

    fn clear_entered(&self, giveaway_id: &str, user_id: &str) {
        if let Ok(mut guard) = self.state.entered.lock() {
            if let Some(members) = guard.get_mut(giveaway_id) {
                members.remove(user_id);
            }
        }
    }

    /// Leading-edge debounce: the first click refreshes, burst mates skip.
    fn should_refresh(&self, giveaway_id: &str) -> bool {
        let Ok(mut guard) = self.state.refresh_stamp.lock() else {
            return true;
        };
        let now = Instant::now();
        if let Some(last) = guard.get(giveaway_id) {
            if last.elapsed().as_secs_f64() < REFRESH_DEBOUNCE_SECS {
                return false;
            }
        }
        guard.insert(giveaway_id.to_string(), now);
        true
    }

    // -- autocomplete snapshots ------------------------------------------

    fn cache_autocomplete(&self, live: &[Giveaway]) {
        let mut cache: HashMap<String, Vec<(String, String)>> = HashMap::new();
        for gw in live {
            cache
                .entry(gw.guild_id.clone())
                .or_default()
                .push((gw.id.clone(), gw.prize.clone()));
        }
        if let Ok(mut guard) = self.state.autocomplete.lock() {
            *guard = cache;
        }
    }

    fn cache_ended_autocomplete(&self, ended: &[Giveaway]) {
        let mut cache: HashMap<String, Vec<(String, String)>> = HashMap::new();
        for gw in ended {
            cache
                .entry(gw.guild_id.clone())
                .or_default()
                .push((gw.id.clone(), gw.prize.clone()));
        }
        if let Ok(mut guard) = self.state.ended_autocomplete.lock() {
            *guard = cache;
        }
    }

    fn autocomplete_empty(&self) -> bool {
        self.state
            .autocomplete
            .lock()
            .map(|g| g.is_empty())
            .unwrap_or(true)
    }

    fn suggestions(&self, guild_id: &str, needle: &str, ended: bool) -> Vec<(String, String)> {
        let source = if ended {
            &self.state.ended_autocomplete
        } else {
            &self.state.autocomplete
        };
        let Ok(guard) = source.lock() else {
            return Vec::new();
        };
        let needle = needle.to_lowercase();
        guard
            .get(guild_id)
            .map(|rows| {
                rows.iter()
                    .filter(|(id, prize)| {
                        needle.is_empty()
                            || prize.to_lowercase().contains(&needle)
                            || id.to_lowercase().contains(&needle)
                    })
                    .take(25)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    }

    // -- helpers ---------------------------------------------------------

    /// Counts seen but not yet flushed to the database.
    fn pending_messages(&self, guild_id: &str, user_id: &str) -> i64 {
        self.state
            .message_buffer
            .lock()
            .ok()
            .and_then(|g| g.get(&(guild_id.to_string(), user_id.to_string())).copied())
            .unwrap_or(0)
    }

    /// Where a command should post a giveaway: the configured channel when it
    /// is a real text channel, else the interaction's own channel.
    fn target_channel(
        &self,
        cache: &Cache,
        guild_id: GuildId,
        interaction_channel: Option<&PartialChannel>,
        channel_id: ChannelId,
    ) -> Option<ChannelId> {
        if let Some(configured) = snowflake(&self.state.settings.giveaway_channel_id) {
            let configured = ChannelId::new(configured);
            if let Some(kind) = cached_kind(cache, guild_id, configured) {
                if is_text_kind(kind) {
                    return Some(configured);
                }
            }
        }
        if let Some(kind) = cached_kind(cache, guild_id, channel_id) {
            return is_text_kind(kind).then_some(channel_id);
        }
        match interaction_channel {
            Some(partial) if is_text_kind(partial.kind) => Some(channel_id),
            _ => None,
        }
    }

    /// The kind of the interaction's channel, falling back to the payload.
    fn channel_kind(
        &self,
        cache: &Cache,
        guild_id: GuildId,
        interaction_channel: Option<&PartialChannel>,
        channel_id: ChannelId,
    ) -> Option<ChannelType> {
        cached_kind(cache, guild_id, channel_id)
            .or_else(|| interaction_channel.map(|partial| partial.kind))
    }

    // -- embed refresh ---------------------------------------------------

    /// Re-render one live giveaway message, a few at a time.
    ///
    /// Every refresh re-checks live status immediately before the Discord edit:
    /// the snapshot may pre-date an `end`/`cancel` that flipped status since,
    /// and editing now would overwrite the winner announcement.
    async fn refresh_embed(
        &self,
        ctx: &Context,
        gw: &Giveaway,
        entry_count: Option<i64>,
        live_status: Option<&str>,
    ) -> Option<String> {
        let handles = Gateway::from(ctx);
        self.refresh_embed_with(&handles, gw, entry_count, live_status)
            .await
    }

    /// The body of [`Self::refresh_embed`], taking the gateway handles directly
    /// so the tick loop can call it too.
    async fn refresh_embed_with(
        &self,
        handles: &Gateway,
        gw: &Giveaway,
        entry_count: Option<i64>,
        live_status: Option<&str>,
    ) -> Option<String> {
        if !gw.active() || gw.message_id.is_none() {
            return None;
        }
        let status = match live_status {
            Some(value) => Some(value.to_string()),
            None => match self.db_call(self.service().get_status(&gw.id)).await {
                Ok(value) => value,
                Err(err) => {
                    error!("refresh status recheck failed for {}: {err}", gw.id);
                    return None;
                }
            },
        };
        if status.as_deref() != Some("active") {
            return status;
        }
        let (Some(channel_id), Some(message_id)) = (
            snowflake(&gw.channel_id).map(ChannelId::new),
            gw.message_id
                .as_deref()
                .and_then(snowflake)
                .map(MessageId::new),
        ) else {
            return None;
        };
        let is_text = cached_kind(&handles.cache, guild_id_of(gw), channel_id)
            .map(is_text_kind)
            .unwrap_or(false);
        if !is_text {
            return None;
        }
        let count = match entry_count {
            Some(count) => count,
            None => self.service().entry_count_cached(&gw.id).await.unwrap_or(0),
        };
        let embed = embeds::giveaway_embed(gw, count, self.state.settings.embed_color);
        let rows = convert::to_action_rows(&components::giveaway_buttons(
            &gw.id,
            &self.state.settings.dashboard_url,
        ));
        match channel_id
            .edit_message(
                handles,
                message_id,
                EditMessage::new()
                    .embed(convert::to_create_embed(&embed))
                    .components(rows),
            )
            .await
        {
            Ok(_) => Some("active".to_string()),
            Err(err) if is_not_found(&err) => {
                debug!("giveaway message for {} no longer exists", gw.id);
                None
            }
            Err(err) => {
                warn!("embed refresh failed for {} ({err})", gw.id);
                None
            }
        }
    }

    /// The soonest deadlines, plus a rotating slice of everything else.
    fn refresh_window(&self, live: Vec<Giveaway>) -> Vec<Giveaway> {
        let mut head: Vec<Giveaway> = live.iter().take(REFRESH_PER_TICK).cloned().collect();
        let rest = &live[REFRESH_PER_TICK.min(live.len())..];
        if rest.is_empty() {
            return head;
        }
        let start = self.state.refresh_cursor.load(Ordering::Relaxed) % rest.len();
        let count = REFRESH_ROTATING.min(rest.len());
        for step in 0..count {
            head.push(rest[(start + step) % rest.len()].clone());
        }
        self.state
            .refresh_cursor
            .store((start + count) % rest.len(), Ordering::Relaxed);
        head
    }

    // -- message counting ------------------------------------------------

    /// Write every buffered count in as few statements as possible.
    async fn flush_message_buffer(&self) -> Result<()> {
        let batch: Vec<((String, String), i64)> = {
            let Ok(mut guard) = self.state.message_buffer.lock() else {
                return Ok(());
            };
            if guard.is_empty() {
                return Ok(());
            }
            // Swap first: anything counted while the write is in flight lands in
            // the fresh map instead of being overwritten by the batch we send.
            std::mem::take(&mut *guard).into_iter().collect()
        };
        let mut rows: Vec<(String, String, i64)> = batch
            .into_iter()
            .map(|((guild, user), count)| (guild, user, count))
            .collect();
        // The receipt reports progress. A timeout cancels the await, not the
        // request, so without it a retry could count the same messages twice.
        let receipt = Arc::new(FlushReceipt::new());
        let outcome = tokio::time::timeout(
            Duration::from_secs_f64(DB_CALL_TIMEOUT_SECS),
            self.service().add_message_counts(&mut rows, Some(&receipt)),
        )
        .await;
        match outcome {
            Ok(Ok(_)) => Ok(()),
            Ok(Err(err)) => {
                // Chunks before the failure are already stored: only the tail
                // goes back, or everybody's count is inflated.
                let applied = err
                    .downcast_ref::<PartialFlush>()
                    .map(|partial| partial.applied)
                    .unwrap_or(0);
                let applied = applied.min(rows.len());
                self.requeue(&rows[applied..]);
                Err(err)
            }
            Err(_) => {
                self.defer_timed_out_flush(rows, receipt);
                Err(anyhow!("message-count flush timed out"))
            }
        }
    }

    fn defer_timed_out_flush(&self, rows: Vec<(String, String, i64)>, receipt: Arc<FlushReceipt>) {
        let bot = self.clone();
        tokio::spawn(async move {
            bot.reconcile_timed_out_flush(rows, receipt).await;
        });
    }

    /// Hand back only what the abandoned write did not manage to store.
    async fn reconcile_timed_out_flush(
        &self,
        rows: Vec<(String, String, i64)>,
        receipt: Arc<FlushReceipt>,
    ) {
        let deadline = Instant::now() + Duration::from_secs_f64(FLUSH_RECONCILE_GRACE_SECS);
        while !receipt.done() && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        if !receipt.done() {
            // Cannot tell what landed. A possible over-count is still better
            // than silently dropping messages.
            self.requeue(&rows);
            return;
        }
        let before = receipt.before();
        // `reconcile_message_counts` swallows its own read errors and hands back
        // the rows it could not confirm, so only the call budget can fail here.
        match tokio::time::timeout(
            Duration::from_secs_f64(DB_CALL_TIMEOUT_SECS),
            self.service().reconcile_message_counts(&rows, &before),
        )
        .await
        {
            Ok(short) => self.requeue(&short),
            Err(_) => {
                error!("timed-out flush could not be reconciled within budget");
                self.requeue(&rows);
            }
        }
    }

    fn requeue(&self, rows: &[(String, String, i64)]) {
        let Ok(mut guard) = self.state.message_buffer.lock() else {
            return;
        };
        for (guild_id, user_id, count) in rows {
            *guard
                .entry((guild_id.clone(), user_id.clone()))
                .or_insert(0) += count;
        }
    }

    // -- entrants role ---------------------------------------------------

    /// A member, from the cache when it is warm, else one REST lookup.
    ///
    /// Takes the handles directly rather than a `Context`, so the tick loop can
    /// reach the same body.
    async fn member_in(
        &self,
        cache: &Cache,
        http: &Http,
        guild_id: GuildId,
        user_id: &str,
    ) -> Option<Member> {
        let uid = snowflake(user_id)?;
        let user_id = UserId::new(uid);
        if let Some(member) = cache
            .guild(guild_id)
            .and_then(|guild| guild.members.get(&user_id).cloned())
        {
            return Some(member);
        }
        http.get_member(guild_id, user_id).await.ok()
    }

    /// The entrants role of a giveaway, as an id (a cache ref is not `Send`).
    fn entrants_role(&self, cache: &Cache, gw: &Giveaway) -> Option<RoleId> {
        let guild_id = GuildId::new(snowflake(&gw.guild_id)?);
        let role_id = RoleId::new(snowflake(gw.entrants_role_id.as_deref()?)?);
        let exists = cache
            .guild(guild_id)
            .map(|guild| guild.roles.contains_key(&role_id))
            .unwrap_or(false);
        exists.then_some(role_id)
    }

    /// The entrants role of a giveaway, as ids only (a cache ref is not `Send`).
    fn role_for(&self, ctx: &Context, gw: &Giveaway) -> Option<(GuildId, RoleId)> {
        let guild_id = GuildId::new(snowflake(&gw.guild_id)?);
        Some((guild_id, self.entrants_role(&ctx.cache, gw)?))
    }

    /// `<@&...>` for this server's notify role, or empty when unset/gone.
    async fn notify_mention_for(&self, cache: &Cache, guild_id: &str) -> String {
        let Some(role_id) = self.service().get_notify_role_cached(guild_id).await else {
            return String::new();
        };
        let (Some(guild), Some(role)) = (snowflake(guild_id), snowflake(&role_id)) else {
            return String::new();
        };
        let guild_id = GuildId::new(guild);
        let role_id = RoleId::new(role);
        let exists = cache
            .guild(guild_id)
            .map(|cached| cached.roles.contains_key(&role_id))
            .unwrap_or(false);
        if exists {
            format!("<@&{role_id}>")
        } else {
            String::new()
        }
    }

    async fn notify_mention(&self, ctx: &Context, guild_id: &str) -> String {
        self.notify_mention_for(&ctx.cache, guild_id).await
    }

    /// Grant the entrants role, then take it straight back if the giveaway
    /// ended during the call (the grant-after-end TOCTOU window).
    async fn grant_entrants_role(
        &self,
        ctx: &Context,
        gw: &Giveaway,
        member: &Member,
        live_status: Option<&str>,
    ) {
        let Some((guild_id, role_id)) = self.role_for(ctx, gw) else {
            return;
        };
        let user_id = member.user.id;
        let grant = {
            let _permit = self.state.role_gate.acquire().await;
            ctx.http
                .add_member_role(
                    guild_id,
                    user_id,
                    role_id,
                    Some(&format!("Joined giveaway {}", gw.id)),
                )
                .await
        };
        if let Err(err) = grant {
            warn!("could not grant entrants role for {}: {err}", gw.id);
            return;
        }
        let live = match live_status {
            Some(value) => Some(value.to_string()),
            None => match self.db_call(self.service().get_status(&gw.id)).await {
                Ok(value) => value,
                Err(err) => {
                    error!("post-grant status recheck failed for {}: {err}", gw.id);
                    None
                }
            },
        };
        if live.as_deref() != Some("active") {
            let _ = ctx
                .http
                .remove_member_role(
                    guild_id,
                    user_id,
                    role_id,
                    Some(&format!("Giveaway {} ended", gw.id)),
                )
                .await;
            info!(
                "revoked stale entrants role for {} (ended during grant)",
                gw.id
            );
        }
    }

    async fn take_entrants_role(&self, ctx: &Context, gw: &Giveaway, user_id: &str) {
        let handles = Gateway::from(ctx);
        self.take_entrants_role_with(&handles, gw, user_id).await;
    }

    /// The body of [`Self::take_entrants_role`], taking the gateway handles
    /// directly so the tick loop can call it too.
    async fn take_entrants_role_with(&self, handles: &Gateway, gw: &Giveaway, user_id: &str) {
        let Some(guild_id) = snowflake(&gw.guild_id).map(GuildId::new) else {
            return;
        };
        let Some(role_id) = self.entrants_role(&handles.cache, gw) else {
            return;
        };
        let Some(member) = self
            .member_in(&handles.cache, &handles.http, guild_id, user_id)
            .await
        else {
            return;
        };
        let _ = handles
            .http
            .remove_member_role(
                guild_id,
                member.user.id,
                role_id,
                Some(&format!("Left giveaway {}", gw.id)),
            )
            .await;
    }

    /// Take the role from every entrant now, delete it in five minutes.
    ///
    /// `Context`-shaped for the command handlers; the tick reaches the same
    /// body through [`Self::strip_entrants_role_with`], because a loop has no
    /// event context to borrow.
    pub(crate) async fn strip_entrants_role(&self, ctx: &Context, gw: &Giveaway) {
        let handles = Gateway::from(ctx);
        self.strip_entrants_role_with(&handles, gw).await;
    }

    async fn strip_entrants_role_with(&self, handles: &Gateway, gw: &Giveaway) {
        let Some(role_id) = self.entrants_role(&handles.cache, gw) else {
            return;
        };
        let entrants = match self.db_call(self.service().entries(&gw.id, None)).await {
            Ok(rows) => rows,
            Err(err) => {
                error!("could not list entrants for role strip ({}): {err}", gw.id);
                Vec::new()
            }
        };
        let ids: Vec<String> = entrants.iter().filter_map(json_id).collect();
        if !ids.is_empty() {
            // Concurrently, but a few at a time. One member at a time made a
            // 1000-entrant giveaway take minutes to clean up; all of them at
            // once would trip the per-route rate limit.
            let gate = Arc::new(Semaphore::new(ROLE_STRIP_CONCURRENCY));
            for window in ids.chunks(STRIP_BATCH) {
                let mut set = tokio::task::JoinSet::new();
                for user_id in window {
                    let bot = self.clone();
                    let handles = handles.clone();
                    let gw = gw.clone();
                    let gate = gate.clone();
                    let user_id = user_id.clone();
                    set.spawn(async move {
                        let Ok(_permit) = gate.acquire().await else {
                            return;
                        };
                        bot.take_entrants_role_with(&handles, &gw, &user_id).await;
                    });
                }
                while set.join_next().await.is_some() {}
            }
        }
        self.schedule_role_delete(
            &gw.guild_id,
            &role_id.to_string(),
            &gw.id,
            ROLE_DELETE_DELAY_SECS,
        );
        info!(
            "stripped entrants role for {}; deleting it in {:.0}s",
            gw.id, ROLE_DELETE_DELAY_SECS
        );
    }

    // -- announcement ----------------------------------------------------

    /// Winner celebration. Runs BEFORE the entrants role is stripped so the role
    /// mention below still reaches everyone who joined.
    async fn announce(&self, ctx: &Context, gw: &Giveaway, winners: &[String], reroll: bool) {
        let handles = Gateway::from(ctx);
        self.announce_with(&handles, gw, winners, reroll).await;
    }

    /// The body of [`Self::announce`], taking the gateway handles directly.
    ///
    /// The tick loop owns no `Context` (serenity 0.12 only builds one inside its
    /// dispatch), so it calls this with the `Arc<Cache>` / `Arc<Http>` pair the
    /// client publishes.
    async fn announce_with(
        &self,
        handles: &Gateway,
        gw: &Giveaway,
        winners: &[String],
        reroll: bool,
    ) {
        let cache = handles.cache.as_ref();
        let Some(channel_id) = snowflake(&gw.channel_id).map(ChannelId::new) else {
            return;
        };
        let is_text = cached_kind(cache, guild_id_of(gw), channel_id)
            .map(is_text_kind)
            .unwrap_or(false);
        if !is_text {
            return;
        }
        let entries = match gw.entrant_count {
            Some(count) => count,
            None => self
                .db_call(self.service().entry_count(&gw.id, false))
                .await
                .unwrap_or(0),
        };

        let mut claim_deadlines: HashMap<String, i64> = HashMap::new();
        if gw.claim_enabled() && !winners.is_empty() {
            match self.db_call(self.service().pending_claims(&gw.id)).await {
                Ok(pending) => {
                    for row in pending {
                        let Some(uid) = json_id(&row) else { continue };
                        if !winners.iter().any(|w| w == &uid) {
                            continue;
                        }
                        if let Some(deadline) = row
                            .get("deadline_ms")
                            .and_then(Value::as_i64)
                            .filter(|d| *d != 0)
                        {
                            claim_deadlines.insert(uid, deadline);
                        }
                    }
                }
                Err(err) => error!("claim lookup failed for {}: {err}", gw.id),
            }
        }
        let claim_deadline_ms = claim_deadlines.values().copied().min();

        let mut embed = embeds::winner_embed(
            gw,
            winners,
            entries,
            self.state.settings.embed_color,
            claim_deadline_ms,
            (!claim_deadlines.is_empty()).then_some(&claim_deadlines),
        );
        if reroll {
            embed.title = "Giveaway Rerolled".to_string();
        }

        let mut parts: Vec<String> = Vec::new();
        if !winners.is_empty() {
            let mentions = winners
                .iter()
                .map(|w| format!("<@{w}>"))
                .collect::<Vec<_>>()
                .join(" ");
            parts.push(format!("🎉 {mentions}"));
        }
        let notify = self.notify_mention_for(cache, &gw.guild_id).await;
        if !notify.is_empty() {
            parts.push(format!("{notify} — results are in!"));
        }
        if let Some(role_id) = self.entrants_role(cache, gw) {
            parts.push(format!("<@&{role_id}> — thanks to everyone who entered!"));
        } else if winners.is_empty() {
            parts.push("No valid entries — no winners this time.".to_string());
        }
        let content = if parts.is_empty() {
            None
        } else {
            Some(parts.join("\n"))
        };

        let components: Vec<CreateActionRow> = if claim_deadlines.is_empty() {
            Vec::new()
        } else {
            convert::to_action_rows(&components::claim_buttons(&gw.id))
        };
        let allowed = CreateAllowedMentions::new()
            .everyone(false)
            .all_users(true)
            .all_roles(true);
        let create_embed = convert::to_create_embed(&embed);

        if let (Some(message_id), false) = (
            gw.message_id
                .as_deref()
                .and_then(snowflake)
                .map(MessageId::new),
            reroll,
        ) {
            let edited = channel_id
                .edit_message(
                    handles,
                    message_id,
                    EditMessage::new()
                        .embed(create_embed.clone())
                        .components(components.clone()),
                )
                .await;
            if edited.is_ok() {
                if let Some(text) = content {
                    let _ = channel_id
                        .send_message(
                            handles,
                            CreateMessage::new()
                                .content(text)
                                .reference_message((channel_id, message_id))
                                .allowed_mentions(allowed),
                        )
                        .await;
                }
                return;
            }
        }

        let mut message = CreateMessage::new()
            .embed(create_embed)
            .components(components)
            .allowed_mentions(allowed);
        if let Some(text) = content {
            message = message.content(text);
        }
        if let Err(err) = channel_id.send_message(handles, message).await {
            warn!("announce failed for {}: {err}", gw.id);
        }
    }

    // -- interaction replies ---------------------------------------------

    /// Ack a component click first, before any database work.
    async fn defer_component(&self, ctx: &Context, ci: &ComponentInteraction) -> bool {
        ci.defer_ephemeral(ctx).await.is_ok()
    }

    /// Ack a slash command first, before any database work.
    async fn defer_command(&self, ctx: &Context, ci: &CommandInteraction) -> bool {
        ci.defer_ephemeral(ctx).await.is_ok()
    }

    /// Followup send that never raises; a silent ack leaves a warning.
    async fn safe_followup_component(&self, ctx: &Context, ci: &ComponentInteraction, text: &str) {
        if let Err(err) = ci
            .create_followup(
                ctx,
                serenity::all::CreateInteractionResponseFollowup::new()
                    .content(text)
                    .ephemeral(true),
            )
            .await
        {
            warn!("reply failed ({err})");
        }
    }

    /// Followup send for a slash command that never raises.
    async fn safe_followup_command(&self, ctx: &Context, ci: &CommandInteraction, text: &str) {
        if let Err(err) = ci
            .create_followup(
                ctx,
                serenity::all::CreateInteractionResponseFollowup::new()
                    .content(text)
                    .ephemeral(true),
            )
            .await
        {
            warn!("reply failed ({err})");
        }
    }

    /// A plain (non-deferred) ephemeral response.
    async fn reply_ephemeral(&self, ctx: &Context, ci: &CommandInteraction, text: &str) {
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

    // -- event handlers --------------------------------------------------

    async fn on_ready(&self, ctx: &Context, ready: &Ready) {
        let guilds = ctx.cache.guilds();
        info!("logged in as {} ({} guilds)", ready.user.name, guilds.len());

        if !self.state.commands_registered.swap(true, Ordering::SeqCst) {
            let specs = commands::command_specs();
            match ctx.http.create_global_commands(&specs).await {
                Ok(_) => info!("commands synced ({})", specs.len()),
                Err(err) => error!("could not register global commands: {err}"),
            }
        }
        if !self
            .state
            .guild_cmd_cleanup_done
            .swap(true, Ordering::SeqCst)
        {
            // aa22c23 mirrored the global tree into every guild for instant
            // visibility, which left two registrations per command. The global
            // tree propagates on its own, so clear the guild copies once.
            let empty: Vec<CreateCommand> = Vec::new();
            for guild_id in &guilds {
                if let Err(err) = ctx.http.create_guild_commands(*guild_id, &empty).await {
                    error!("guild command cleanup failed for {guild_id}: {err}");
                }
            }
            info!(
                "guild command cleanup done ({} guild(s)); global-only from here",
                guilds.len()
            );
        }

        let live = self
            .db_call(self.service().list_all_active(200))
            .await
            .unwrap_or_default();
        info!(
            "tracking {} active giveaway(s) from the database",
            live.len()
        );
        self.cache_autocomplete(&live);

        let ended = self
            .db_call(self.service().list_all_ended(200))
            .await
            .unwrap_or_default();
        self.cache_ended_autocomplete(&ended);

        // Restart recovery: finished giveaways whose entrants role was never
        // deleted. Clean the pileup now instead of leaving roles forever.
        match self.db_call(self.service().ended_with_roles(50)).await {
            Ok(leftovers) if !leftovers.is_empty() => {
                info!("cleaning up {} leftover entrants role(s)", leftovers.len());
                for row in leftovers {
                    let role_id = row.entrants_role_id.clone().unwrap_or_default();
                    self.schedule_role_delete(&row.guild_id, &role_id, &row.id, 60.0);
                }
            }
            Ok(_) => {}
            Err(err) => error!("leftover role sweep failed: {err}"),
        }

        let _ = self.state.ready_tx.send(true);
    }

    /// Count a message towards its author's min-messages requirement.
    async fn on_message(&self, message: &Message) {
        let (Some(guild_id), false) = (message.guild_id, message.author.bot) else {
            return;
        };
        let key = (guild_id.to_string(), message.author.id.to_string());
        let overflow = {
            let Ok(mut guard) = self.state.message_buffer.lock() else {
                return;
            };
            *guard.entry(key).or_insert(0) += 1;
            guard.len() >= MESSAGE_BUFFER_MAX
        };
        if overflow {
            if let Err(err) = self.flush_message_buffer().await {
                error!("message-count flush failed: {err}");
            }
        }
    }

    async fn on_interaction(&self, ctx: &Context, interaction: Interaction) {
        match interaction {
            Interaction::Command(ci) => commands::handle_command(self, ctx, ci).await,
            Interaction::Autocomplete(ci) => commands::handle_autocomplete(self, ctx, ci).await,
            Interaction::Component(ci) => buttons::handle_component(self, ctx, ci).await,
            _ => {}
        }
    }
}

#[async_trait]
impl EventHandler for Bot {
    async fn ready(&self, ctx: Context, ready: Ready) {
        self.on_ready(&ctx, &ready).await;
    }

    async fn message(&self, ctx: Context, new_message: Message) {
        let _ = &ctx;
        self.on_message(&new_message).await;
    }

    async fn interaction_create(&self, ctx: Context, interaction: Interaction) {
        self.on_interaction(&ctx, interaction).await;
    }
}

/// Start the bot and keep it running until the process is asked to stop.
///
/// Takes the database by value: the service owns it for the process lifetime
/// and closes it here, after a final message-count flush, so a shutdown cannot
/// drop buffered counts on the floor.
pub async fn run_bot(
    settings: Settings,
    db: Database,
    redis: RedisCache,
    health: Arc<RwLock<HealthState>>,
) -> Result<()> {
    let service = GiveawayService::new(db, Some(redis.clone()));
    let bot = Bot::new(settings, service, redis, health);

    // Owned by `run_bot`, so a failed login attempt never starts them and they
    // are gone the moment this function returns.
    let tick_task = tokio::spawn({
        let bot = bot.clone();
        async move { bot.tick_loop().await }
    });
    let flush_task = tokio::spawn({
        let bot = bot.clone();
        async move { bot.flush_loop().await }
    });
    let heartbeat_task = tokio::spawn({
        let bot = bot.clone();
        async move { bot.heartbeat_loop().await }
    });

    let result = bot.login_loop().await;

    tick_task.abort();
    flush_task.abort();
    heartbeat_task.abort();

    if let Err(err) = bot.flush_message_buffer().await {
        warn!("final message-count flush failed: {err}");
    }
    bot.service().db().close().await;
    bot.state.redis.aclose().await;
    result
}

// -- free helpers --------------------------------------------------------

/// Parse a Discord snowflake out of a string, rejecting anything else.
pub(crate) fn snowflake(raw: &str) -> Option<u64> {
    let trimmed = raw.trim();
    if trimmed.len() < 15 || trimmed.len() > 25 {
        return None;
    }
    trimmed.parse::<u64>().ok().filter(|value| *value != 0)
}

/// The guild a giveaway belongs to, when its stored id is a real snowflake.
pub(crate) fn guild_id_of(gw: &Giveaway) -> GuildId {
    GuildId::new(snowflake(&gw.guild_id).unwrap_or(0))
}

/// Kind of a guild channel, looked up through the guild that owns it.
///
/// serenity 0.12 deprecates `Cache::channel` — it was only ever
/// `channels[id] -> guild_id` followed by `guild.channels[id]`, so going
/// through the guild directly is the same lookup without the deprecated shim.
pub(crate) fn cached_kind(
    cache: &Cache,
    guild_id: GuildId,
    channel_id: ChannelId,
) -> Option<ChannelType> {
    cache
        .guild(guild_id)
        .and_then(|guild| guild.channels.get(&channel_id).map(|channel| channel.kind))
}

/// `TextChannel` in discord.py terms: a real text channel or an announcement
/// channel. Threads are deliberately excluded.
pub(crate) fn is_text_kind(kind: ChannelType) -> bool {
    matches!(kind, ChannelType::Text | ChannelType::News)
}

/// Threads cannot hold a giveaway.
pub(crate) fn is_thread_kind(kind: ChannelType) -> bool {
    matches!(
        kind,
        ChannelType::PublicThread | ChannelType::PrivateThread | ChannelType::NewsThread
    )
}

/// True when a serenity error is Discord's "Unknown Message" (10008).
pub(crate) fn is_not_found(err: &serenity::Error) -> bool {
    matches!(
        err,
        serenity::Error::Http(serenity::http::HttpError::UnsuccessfulRequest(response))
            if response.error.code == 10008
    )
}

/// Pull a user id out of a JSON row, accepting strings and numbers.
pub(crate) fn json_id(row: &Value) -> Option<String> {
    match row.get("user_id")? {
        Value::String(text) if !text.is_empty() => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        _ => None,
    }
}

/// A login failure that retrying can never fix.
fn is_fatal_login(err: &serenity::Error) -> bool {
    matches!(
        err,
        serenity::Error::Gateway(
            GatewayError::InvalidAuthentication
                | GatewayError::NoAuthentication
                | GatewayError::DisallowedGatewayIntents
                | GatewayError::InvalidGatewayIntents
        )
    )
}

/// Flatten a login error into one log line, keeping the Discord status.
fn login_summary(err: &serenity::Error) -> String {
    let text = match err {
        serenity::Error::Gateway(gateway) => format!("{gateway:?}"),
        other => format!("{other:?}"),
    };
    let mut flat = text.replace('\n', " ");
    if flat.chars().count() > 160 {
        flat = flat.chars().take(160).collect();
    }
    flat
}

/// How long to wait before the next login attempt.
///
/// `discord.py` used `random.uniform(0, 5)` for jitter; an integer 0..=5 is
/// indistinguishable at this scale and keeps the schedule reproducible in logs.
fn retry_wait(attempt: usize, retry_after: Option<u64>) -> Duration {
    let base = match retry_after {
        Some(seconds) if seconds > 0 && seconds <= LOGIN_RETRY_MAX_WAIT_SECS => seconds,
        _ => LOGIN_RETRY_DELAYS[(attempt.saturating_sub(1)).min(LOGIN_RETRY_DELAYS.len() - 1)],
    };
    let jitter = rand::random::<u64>() % (LOGIN_RETRY_JITTER_SECS + 1);
    Duration::from_secs((base + jitter).min(LOGIN_RETRY_MAX_WAIT_SECS + LOGIN_RETRY_JITTER_SECS))
}

/// Resolves when the process is asked to stop (Ctrl-C, or SIGTERM on unix).
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).ok();
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = async {
                match term.as_mut() {
                    Some(signal) => {
                        signal.recv().await;
                    }
                    None => std::future::pending::<()>().await,
                }
            } => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
