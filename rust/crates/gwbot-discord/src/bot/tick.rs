//! Periodic loops: the draw timer, the message-count flush and the heartbeat.
//!
//! `bot.py` hung these off `discord.ext.tasks`, whose decorators waited for
//! `ready`, restarted a crashed loop and logged the exception. Each loop here is
//! a plain `async fn`: wait for the first `ready`, then repeat forever, logging
//! every failure instead of dying. A dead tick would silently stop drawing
//! winners, so no phase may propagate an error out of the loop.
//!
//! None of these loops owns a gateway event, and serenity 0.12.5 only builds a
//! `Context` inside its dispatch (`Context::new` is crate-private and
//! `Context::easy` is compiled out with the `cache` + `gateway` features), so
//! the loop-side sends carry the same `Arc<Cache>` / `Arc<Http>` pair the client
//! publishes and build their payloads with the very same shared builders.

use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use gwbot_core::components;
use gwbot_core::embeds;
use gwbot_core::models::{Giveaway, ServiceError};
use serde_json::Value;
use serenity::all::{
    Cache, ChannelId, Context, CreateActionRow, CreateAllowedMentions, CreateMessage, EditMessage,
    GuildId, Http, Member, MessageId, RoleId, UserId,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Semaphore;
use tracing::{debug, error, info, warn};

use super::{
    cached_kind, guild_id_of, is_not_found, is_text_kind, json_id, snowflake, Bot,
    LIST_REFRESH_SECS, MESSAGE_FLUSH_SECONDS, REFRESH_CONCURRENCY, ROLE_DELETE_DELAY_SECS,
    ROLE_STRIP_CONCURRENCY, STRIP_BATCH, SWEEP_LEASE_SECS,
};
use crate::convert;

/// The two gateway handles a loop-side Discord call needs.
///
/// `mod.rs::announce` / `refresh_embeds` take a `Context`; the loops have none,
/// so they pass this pair (which is `CacheHttp` on its own) instead.
#[derive(Clone)]
struct Gateway {
    cache: Arc<Cache>,
    http: Arc<Http>,
}

impl Gateway {
    /// `(&Arc<Cache>, &Http)` is the `CacheHttp` impl serenity documents for
    /// callers that have no `Context`.
    fn cache_http(&self) -> (&Arc<Cache>, &Http) {
        (&self.cache, self.http.as_ref())
    }
}

impl Bot {
    // -- the draw timer --------------------------------------------------

    pub(crate) async fn tick_loop(&self) {
        self.wait_ready().await;
        loop {
            self.tick_once().await;
            // `tick_seconds` is clamped to 5 at load time; the cast is safe.
            tokio::time::sleep(Duration::from_secs_f64(
                self.state.settings.tick_seconds.max(1) as f64,
            ))
            .await;
        }
    }

    /// One pass: correctness phases first, then the gated cosmetic half.
    async fn tick_once(&self) {
        let handles = self.gateway();

        // -- due draws ---------------------------------------------------
        let due = match self.db_call(self.service().due(None)).await {
            Ok(due) => due,
            Err(err) => {
                // A failed due read used to end the whole pass, cosmetic half
                // included; keep that so a broken database cannot compound.
                error!("due check failed: {err}");
                return;
            }
        };
        for gw in due {
            let (ended, winners) = match self
                .db_call(self.service().end(&gw.id, None, true, Some(&gw)))
                .await
            {
                Ok(result) => result,
                Err(err) => {
                    // Usually another ender won the claim (the tick and
                    // /giveaway_end firing together), but a giveaway that never
                    // ends should leave a trace instead of a quiet pass.
                    match err.downcast_ref::<ServiceError>() {
                        Some(service) => info!("could not end {}: {}", gw.id, service.message),
                        None => error!("end failed for {}: {err}", gw.id),
                    }
                    continue;
                }
            };
            self.gw_drop(&gw.id);
            // `end` queued the keys its flip invalidated (header, guild list,
            // global list, count). Fire-and-forget so a slow Redis DEL never
            // delays the announcement, which is what members actually see.
            self.spawn_invalidation_flush();
            if let Some(handles) = &handles {
                // Announce before the role strip: the winner mention has to
                // still resolve for everyone who entered.
                self.announce_gateway(&ended, &winners, false, handles)
                    .await;
                self.strip_entrants_role_with(handles, &ended).await;
            }
        }

        // -- claim expiry --------------------------------------------------
        // Restart recovery lives here too: deadlines are durable rows, so a
        // restart simply picks up whatever is due on the next tick.
        let due_claims = match self.db_call(self.service().due_claims(None, 25)).await {
            Ok(rows) => rows,
            Err(err) => {
                error!("due claims check failed: {err}");
                Vec::new()
            }
        };
        for row in due_claims {
            let gid = row_text(&row, "giveaway_id");
            let uid = row_text(&row, "user_id");
            if gid.is_empty() || uid.is_empty() {
                continue;
            }
            let (ended, fresh) = match self
                .db_call(self.service().expire_claim(&gid, &uid, None))
                .await
            {
                Ok(result) => result,
                Err(err) => {
                    match err.downcast_ref::<ServiceError>() {
                        Some(service) => {
                            info!("could not expire claim {gid}/{uid}: {}", service.message)
                        }
                        None => error!("claim expiry failed for {gid}/{uid}: {err}"),
                    }
                    continue;
                }
            };
            info!("claim expired for {gid}/{uid}; replacement={fresh:?}");
            // expire_claim rerolled the winner, so the count + list keys it
            // invalidated go in the background for the same reason as `end`.
            self.spawn_invalidation_flush();
            if !fresh.is_empty() {
                if let Some(handles) = &handles {
                    self.announce_gateway(&ended, &fresh, true, handles).await;
                }
            }
        }

        // -- cosmetic half: lists + embed PATCHes --------------------------
        // Gated to LIST_REFRESH_SECS: due/claims above run every tick
        // (correctness), but two list reads plus N PATCHes would otherwise
        // steal lanes from a join burst landing mid-tick. The first tick always
        // refreshes because the autocomplete caches are cold.
        let now = Instant::now();
        let last = self
            .state
            .last_list_refresh
            .lock()
            .ok()
            .and_then(|slot| *slot);
        let stale = last
            .map(|at| at.elapsed().as_secs_f64() >= LIST_REFRESH_SECS)
            .unwrap_or(true);
        let due_sweep = stale || self.autocomplete_empty();

        let mut lease = None;
        let mut may_sweep = true;
        if due_sweep && self.state.settings.uses_redis() {
            let (token, answered) = self
                .state
                .redis
                .acquire_checked("sweep:live", SWEEP_LEASE_SECS)
                .await;
            lease = token;
            // Only a real answer may narrow the sweep. "Somebody else holds
            // it" means another instance is already doing this pass; "no
            // answer" must leave the tick exactly as it was before Redis
            // existed, or a broken cache would quietly freeze the lists.
            may_sweep = lease.is_some() || !answered;
        }

        if due_sweep && may_sweep {
            match self.db_call(self.service().list_all_active(200)).await {
                Ok(live) => {
                    self.cache_autocomplete(&live);
                    match self.db_call(self.service().list_all_ended(200)).await {
                        Ok(ended) => self.cache_ended_autocomplete(&ended),
                        Err(err) => error!("ended list failed: {err}"),
                    }
                    if let Ok(mut slot) = self.state.last_list_refresh.lock() {
                        *slot = Some(now);
                    }
                    if let Some(handles) = &handles {
                        let window = self.refresh_window(live);
                        self.refresh_embeds_gateway(&window, handles).await;
                    }
                }
                Err(err) => error!("live list failed: {err}"),
            }
        }
        // Released on every path, including the failures above: leaking it
        // would make this process read its OWN token back as "somebody else is
        // sweeping" and skip the window for the rest of the TTL.
        if let Some(token) = lease {
            if !self.state.redis.release("sweep:live", &token).await {
                debug!("sweep lease release failed");
            }
        }

        // -- privacy sweep -------------------------------------------------
        // Join data (who entered) older than 5h past the end is wiped.
        // Giveaway records + winner lists stay; message counts are already
        // cleared at end-time.
        let tick = self.state.tick_count.fetch_add(1, Ordering::Relaxed) + 1;
        if tick % 20 == 0 {
            match self.db_call(self.service().wipe_stale_entries(None)).await {
                Ok(0) => {}
                Ok(wiped) => info!("wiped {wiped} stale entry row(s)"),
                Err(err) => error!("entry wipe failed: {err}"),
            }
        }
    }

    /// Block until the gateway reported `ready` once.
    async fn wait_ready(&self) {
        let mut ready = self.state.ready_rx.clone();
        // Returns immediately when the flag is already set.
        if ready.wait_for(|value| *value).await.is_err() {
            // The sender only drops while the process is shutting down.
            std::future::pending::<()>().await;
        }
    }

    /// Fire-and-forget cache invalidation (never blocks an announcement).
    fn spawn_invalidation_flush(&self) {
        let bot = self.clone();
        tokio::spawn(async move { bot.flush_invalidations().await });
    }

    // -- message-count flush ---------------------------------------------

    pub(crate) async fn flush_loop(&self) {
        self.wait_ready().await;
        loop {
            tokio::time::sleep(Duration::from_secs(MESSAGE_FLUSH_SECONDS)).await;
            if let Err(err) = self.flush_message_buffer().await {
                error!("message-count flush failed: {err}");
            }
        }
    }

    // -- health heartbeat ------------------------------------------------

    pub(crate) async fn heartbeat_loop(&self) {
        self.wait_ready().await;
        loop {
            tokio::time::sleep(Duration::from_secs(60)).await;
            match self.probe_health().await {
                Ok((status, ms)) => info!("health check ok ({status}, {ms:.0}ms)"),
                Err(err) => warn!("health check failed: {err}"),
            }
        }
    }

    /// Ping our own `/health` like an external monitor would.
    ///
    /// No HTTP client is pulled in for this: the request is one fixed line, so
    /// a raw TCP connect, a write, and a read of the status line is the whole
    /// protocol needed. `Connection: close` makes our own server hang up after
    /// the body, so the read always terminates.
    async fn probe_health(&self) -> Result<(u16, f64)> {
        let port = self.state.settings.port.max(1);
        let started = Instant::now();
        let mut stream = tokio::time::timeout(
            Duration::from_secs(10),
            tokio::net::TcpStream::connect(("127.0.0.1", port)),
        )
        .await
        .map_err(|_| anyhow!("health check timed out"))??;
        let request =
            format!("GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
        stream.write_all(request.as_bytes()).await?;
        let mut raw = Vec::new();
        match tokio::time::timeout(Duration::from_secs(10), stream.read_to_end(&mut raw)).await {
            Ok(Ok(_)) => {}
            Ok(Err(err)) => return Err(err.into()),
            // A socket left open is not a failure once the response arrived.
            Err(_) if !raw.is_empty() => {}
            Err(_) => return Err(anyhow!("health check timed out")),
        }
        let status = String::from_utf8_lossy(&raw)
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|code| code.parse::<u16>().ok())
            .ok_or_else(|| anyhow!("health check returned no status line"))?;
        Ok((status, started.elapsed().as_secs_f64() * 1000.0))
    }

    // -- entrants role cleanup -------------------------------------------

    /// Queue a one-shot task that deletes the role after `delay_secs`.
    pub(crate) fn schedule_role_delete(
        &self,
        guild_id: &str,
        role_id: &str,
        giveaway_id: &str,
        delay_secs: f64,
    ) {
        if guild_id.is_empty() || role_id.is_empty() {
            return;
        }
        {
            let Ok(mut guard) = self.state.scheduled_role_deletes.lock() else {
                return;
            };
            if !guard.insert(giveaway_id.to_string()) {
                // Already queued by this process; a second task would race the
                // first one's delete and log a spurious failure.
                return;
            }
        }
        let bot = self.clone();
        let guild_id = guild_id.to_string();
        let role_id = role_id.to_string();
        let giveaway_id = giveaway_id.to_string();
        tokio::spawn(async move {
            if delay_secs > 0.0 {
                tokio::time::sleep(Duration::from_secs_f64(delay_secs)).await;
            }
            bot.delete_entrants_role_later(&guild_id, &role_id, &giveaway_id)
                .await;
        });
    }

    pub(crate) async fn delete_entrants_role_later(
        &self,
        guild_id: &str,
        role_id: &str,
        giveaway_id: &str,
    ) {
        self.delete_entrants_role_attempt(guild_id, role_id, giveaway_id)
            .await;
        // Released on every path, the `Forbidden` one included: the database
        // row is kept for the restart sweep, but the latch must not survive, or
        // this giveaway could never be scheduled again while the process lives.
        if let Ok(mut guard) = self.state.scheduled_role_deletes.lock() {
            guard.remove(giveaway_id);
        }
    }

    async fn delete_entrants_role_attempt(&self, guild_id: &str, role_id: &str, giveaway_id: &str) {
        let Some(handles) = self.gateway() else {
            // No gateway handles: the role is unreachable, so leave the row for
            // the restart sweep rather than clearing a role that still exists.
            return;
        };
        let guild_id = match snowflake(guild_id).map(GuildId::new) {
            Some(id) if handles.cache.guild(id).is_some() => id,
            _ => {
                // The bot cannot see the guild any more (removed, or
                // unavailable right now). The role is unreachable either way,
                // so clear the row: leaving it set makes the restart sweep
                // re-queue this giveaway on every boot, forever, for nothing.
                warn!("guild {guild_id} unavailable; dropping entrants role cleanup for {giveaway_id}");
                self.clear_entrants_role(giveaway_id).await;
                return;
            }
        };
        let role_id = snowflake(role_id).map(RoleId::new);
        let exists = role_id
            .map(|id| {
                handles
                    .cache
                    .guild(guild_id)
                    .map(|guild| guild.roles.contains_key(&id))
                    .unwrap_or(false)
            })
            .unwrap_or(false);
        if let (true, Some(role_id)) = (exists, role_id) {
            let reason = format!("Giveaway {giveaway_id} ended");
            match handles
                .http
                .delete_role(guild_id, role_id, Some(&reason))
                .await
            {
                Ok(()) => info!("deleted entrants role for {giveaway_id}"),
                Err(err) if is_forbidden(&err) => {
                    warn!(
                        "no permission to delete entrants role for {giveaway_id} — move the bot role above it / grant Manage Roles"
                    );
                    // Left in the database on purpose: the restart sweep picks
                    // it up again once the permissions are fixed.
                    return;
                }
                Err(err) => warn!("role delete failed for {giveaway_id} ({err})"),
            }
        }
        // Role already gone (or the delete failed for good): record that so the
        // restart sweep stops queueing it.
        self.clear_entrants_role(giveaway_id).await;
    }

    async fn clear_entrants_role(&self, giveaway_id: &str) {
        if let Err(err) = self
            .db_call(self.service().set_entrants_role(giveaway_id, None))
            .await
        {
            error!("could not clear entrants role for {giveaway_id}: {err}");
        }
    }

    /// Take the role from every entrant now, delete it in five minutes.
    ///
    /// `Context`-shaped for callers that own one; the tick reaches the same
    /// body through [`Self::strip_entrants_role_with`], because a loop has no
    /// event context to borrow.
    #[allow(dead_code)]
    pub(crate) async fn strip_entrants_role(&self, ctx: &Context, gw: &Giveaway) {
        let handles = Gateway {
            cache: ctx.cache.clone(),
            http: ctx.http.clone(),
        };
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

    async fn take_entrants_role_with(&self, handles: &Gateway, gw: &Giveaway, user_id: &str) {
        let Some(guild_id) = snowflake(&gw.guild_id).map(GuildId::new) else {
            return;
        };
        let Some(role_id) = self.entrants_role(&handles.cache, gw) else {
            return;
        };
        let Some(member) = self.member_in(handles, guild_id, user_id).await else {
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

    // -- gateway-free copies of the shared helpers ------------------------
    // Same bodies as `mod.rs`, minus the `Context` the loops cannot build.

    fn gateway(&self) -> Option<Gateway> {
        Some(Gateway {
            cache: self.cache()?,
            http: self.http()?,
        })
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

    async fn member_in(
        &self,
        handles: &Gateway,
        guild_id: GuildId,
        user_id: &str,
    ) -> Option<Member> {
        let uid = UserId::new(snowflake(user_id)?);
        if let Some(member) = handles
            .cache
            .guild(guild_id)
            .and_then(|guild| guild.members.get(&uid).cloned())
        {
            return Some(member);
        }
        handles.http.get_member(guild_id, uid).await.ok()
    }

    /// `<@&...>` for this server's notify role, or empty when unset/gone.
    async fn notify_mention_for(&self, cache: &Cache, guild_id: &str) -> String {
        let Some(role_id) = self.service().get_notify_role_cached(guild_id).await else {
            return String::new();
        };
        let (Some(guild), Some(role)) = (snowflake(guild_id), snowflake(&role_id)) else {
            return String::new();
        };
        let role_id = RoleId::new(role);
        let exists = cache
            .guild(GuildId::new(guild))
            .map(|cached| cached.roles.contains_key(&role_id))
            .unwrap_or(false);
        if exists {
            format!("<@&{role_id}>")
        } else {
            String::new()
        }
    }

    /// Winner celebration, `mod.rs::announce` with the cache/http pair.
    async fn announce_gateway(
        &self,
        gw: &Giveaway,
        winners: &[String],
        reroll: bool,
        handles: &Gateway,
    ) {
        let cache = handles.cache.as_ref();
        let cache_http = handles.cache_http();
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
                    cache_http,
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
                            cache_http,
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
        if let Err(err) = channel_id.send_message(cache_http, message).await {
            warn!("announce failed for {}: {err}", gw.id);
        }
    }

    /// `mod.rs::refresh_embeds`, one `Context` short.
    async fn refresh_embeds_gateway(&self, giveaways: &[Giveaway], handles: &Gateway) {
        let gate = Arc::new(Semaphore::new(REFRESH_CONCURRENCY));
        let mut set = tokio::task::JoinSet::new();
        for gw in giveaways {
            let bot = self.clone();
            let handles = handles.clone();
            let gate = gate.clone();
            let gw = gw.clone();
            set.spawn(async move {
                let Ok(_permit) = gate.acquire().await else {
                    return;
                };
                let _ = bot.refresh_embed_gateway(&gw, &handles).await;
            });
        }
        while set.join_next().await.is_some() {}
    }

    /// Re-render one live giveaway message, re-checking status right before it.
    async fn refresh_embed_gateway(&self, gw: &Giveaway, handles: &Gateway) -> Option<String> {
        if !gw.active() || gw.message_id.is_none() {
            return None;
        }
        let status = match self.db_call(self.service().get_status(&gw.id)).await {
            Ok(value) => value,
            Err(err) => {
                error!("refresh status recheck failed for {}: {err}", gw.id);
                return None;
            }
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
        let count = self.service().entry_count_cached(&gw.id).await.unwrap_or(0);
        let embed = embeds::giveaway_embed(gw, count, self.state.settings.embed_color);
        let rows = convert::to_action_rows(&components::giveaway_buttons(
            &gw.id,
            &self.state.settings.dashboard_url,
        ));
        match channel_id
            .edit_message(
                handles.cache_http(),
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
}

/// True when Discord refused the call for lack of permission (403 / 50013).
fn is_forbidden(err: &serenity::Error) -> bool {
    matches!(
        err,
        serenity::Error::Http(serenity::http::HttpError::UnsuccessfulRequest(response))
            if response.status_code.as_u16() == 403 || response.error.code == 50013
    )
}

/// A string field out of a JSON row, `""` when missing (`str(x or "")`).
fn row_text(row: &Value, key: &str) -> String {
    match row.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        _ => String::new(),
    }
}
