//! Giveaway domain rules. Ported 1:1 from `bot/giveaway_bot/service.py`.
//!
//! Every method is `async` because the only backend is Turso over HTTP; the
//! Python original was sync only because its driver was thread-local. The
//! rules themselves are unchanged, including statement order, guarded writes
//! and the exact refusal text.

use std::collections::{BTreeMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex as StdMutex;

use anyhow::Result;
use rand::seq::SliceRandom;
use serde_json::{json, Value};

use crate::cache::RedisCache;
use crate::db::{is_ambiguous_loss, Database, Row, SqlValue};
use crate::models::{
    changed, now_ms, Giveaway, PartialFlush, ServiceError, ENTRY_RETENTION_MS, MAX_WINNERS,
    MESSAGE_FLUSH_CHUNK, TIMEOUT_BAN_KIND, TIMEOUT_PENALTY,
};

//: TTL of the short-lived read caches. Everything here is derivable from
//: Turso, so a stale hit only ever costs a few seconds of cosmetic staleness
//: (a list one giveaway behind, a count one entry behind) -- never a wrong
//: decision, because every mutating statement re-checks the database itself.
pub const LIST_CACHE_TTL: f64 = 8.0;
//: Guild settings change only through the bot's own command, which clears the
//: key, so this TTL is just a safety net for a missed invalidation.
pub const SETTINGS_CACHE_TTL: f64 = 10.0;
//: Giveaway-id-shaped keys are self-invalidating: a new giveaway gets a new
//: id, so its id can never address a *different* giveaway's data.
pub const GW_CACHE_TTL: f64 = 6.0;
pub const COUNT_CACHE_TTL: f64 = 4.0;
//: Longest key suffix this bot will cache. Ids are short (gw_xxxxxxxx) and an
//: unbounded caller-supplied string would let one odd request write an
//: arbitrarily long key into a 30 MB plan.
pub const KEY_SUFFIX_MAX: usize = 96;
//: Suffixes carried by ONE invalidation DEL. A burst (100 joins) plus the
//: sweep's list keys can queue far more than this; the rest goes back on the
//: queue for the next flush instead of being dropped.
pub const FLUSH_BATCH_MAX: usize = 64;
//: Hard cap on the pending queue itself, so a deployment where nothing flushes
//: for a long stretch cannot grow it without bound. Over cap the OLDEST
//: suffixes go: their keys expire on their own TTL (4-10s) anyway.
pub const PENDING_INVALIDATIONS_MAX: usize = 1024;

//: Claim row statuses. `pending` rows hold a deadline; the rest are final.
pub const CLAIM_PENDING: &str = "pending";
pub const CLAIM_CLAIMED: &str = "claimed";
pub const CLAIM_EXPIRED: &str = "expired";
pub const CLAIM_SKIPPED: &str = "skipped";
//: A pending claim closed by a targeted /reroll (replaced, not expired).
pub const CLAIM_REPLACED: &str = "replaced";

//: Max timed-ban duration in hours. 0 = permanent (the original
//: blacklist_add() had no expiry, so the default stays permanent).
pub const BAN_MAX_HOURS: i64 = 720;

/// Readable after the call that filled it, including after a cancelled or
/// timed-out flush: the Python receipt was a plain dict the caller could read
/// once `wait_for` gave up on the worker that was still writing.
#[derive(Debug, Default)]
pub struct FlushReceipt {
    before: StdMutex<BTreeMap<(String, String), i64>>,
    applied: AtomicUsize,
    done: AtomicBool,
}

impl FlushReceipt {
    pub fn new() -> Self {
        Self::default()
    }

    /// Stored counts read just before each chunk ran.
    pub fn before(&self) -> BTreeMap<(String, String), i64> {
        self.before
            .lock()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    /// Rows committed so far.
    pub fn applied(&self) -> usize {
        self.applied.load(Ordering::SeqCst)
    }

    /// True once the call has stopped writing, on success or failure alike.
    pub fn done(&self) -> bool {
        self.done.load(Ordering::SeqCst)
    }
}

/// Giveaway rules over one database, plus the optional read cache.
pub struct GiveawayService {
    db: Database,
    redis: Option<RedisCache>,
    /// Key suffixes whose cached value a just-committed write invalidated.
    /// The write path cannot await a Redis delete, so it records them here and
    /// the caller drains the list right after the same DB call, so the next
    /// read is live rather than waiting out a TTL.
    pending_invalidations: StdMutex<Vec<String>>,
}

impl GiveawayService {
    pub fn new(db: Database, redis: Option<RedisCache>) -> Self {
        Self {
            db,
            redis,
            pending_invalidations: StdMutex::new(Vec::new()),
        }
    }

    pub fn db(&self) -> &Database {
        &self.db
    }

    pub fn redis(&self) -> Option<&RedisCache> {
        self.redis.as_ref()
    }

    // -- optional redis read cache --------------------------------------

    /// Read one cached value. Never raises; a degraded Redis means None.
    pub async fn cache_get(&self, key: &str) -> Option<Value> {
        let cache = self.redis.as_ref()?;
        cache.get_json(key).await
    }

    pub async fn cache_get_int(&self, key: &str) -> Option<i64> {
        let raw = self.cache_get(key).await?;
        json_int(&raw)
    }

    /// Best-effort write-through. A failure only costs a later read.
    pub async fn cache_put(&self, key: &str, value: &Value, ttl: f64) {
        let Some(cache) = self.redis.as_ref() else {
            return;
        };
        if key.chars().count() > KEY_SUFFIX_MAX {
            return;
        }
        cache.set_json(key, value, ttl).await;
    }

    /// Record keys a committed write just made wrong (never raises).
    pub fn mark_stale(&self, suffixes: &[&str]) {
        if self.redis.is_none() {
            return;
        }
        let Ok(mut pending) = self.pending_invalidations.lock() else {
            return;
        };
        pending.extend(suffixes.iter().map(|s| (*s).to_string()));
        if pending.len() > PENDING_INVALIDATIONS_MAX {
            // Bounded queue: the oldest suffixes are the ones whose keys have
            // had the longest to expire on their own TTL.
            let overflow = pending.len() - PENDING_INVALIDATIONS_MAX;
            pending.drain(..overflow);
            tracing::debug!("invalidation queue over cap, dropped {overflow} oldest");
        }
    }

    /// Take the pending key suffixes (clearing them) for an async flush.
    pub fn drain_invalidations(&self) -> Vec<String> {
        match self.pending_invalidations.lock() {
            Ok(mut pending) => std::mem::take(&mut *pending),
            Err(_) => Vec::new(),
        }
    }

    /// Delete every key a committed write invalidated. Never raises.
    ///
    /// One DEL carries at most FLUSH_BATCH_MAX suffixes; the rest goes back on
    /// the queue.
    pub async fn flush_invalidations(&self) -> i64 {
        let Some(cache) = self.redis.as_ref() else {
            return 0;
        };
        let pending = self.drain_invalidations();
        if pending.is_empty() {
            return 0;
        }
        // Deduplicate: a burst of joins for one giveaway queues the same count
        // key N times, and one DEL is enough for all of them.
        let mut seen = HashSet::new();
        let unique: Vec<String> = pending
            .into_iter()
            .filter(|key| seen.insert(key.clone()))
            .collect();
        if unique.is_empty() {
            return 0;
        }
        let split = FLUSH_BATCH_MAX.min(unique.len());
        let batch = &unique[..split];
        let rest = &unique[split..];
        if !rest.is_empty() {
            // More suffixes than one DEL should carry. The overflow goes BACK
            // on the queue instead of being dropped. The rebuild happens
            // inside the lock: a writer that marks a key stale while this runs
            // must not have its suffix overwritten.
            if let Ok(mut queue) = self.pending_invalidations.lock() {
                let mut keep: Vec<String> = Vec::new();
                let mut keep_seen: HashSet<String> = HashSet::new();
                for key in rest {
                    if keep_seen.insert(key.clone()) {
                        keep.push(key.clone());
                    }
                }
                for key in queue.iter() {
                    if !keep_seen.contains(key) {
                        keep_seen.insert(key.clone());
                        keep.push(key.clone());
                    }
                }
                keep.truncate(PENDING_INVALIDATIONS_MAX);
                *queue = keep;
            }
        }
        let refs: Vec<&str> = batch.iter().map(String::as_str).collect();
        cache.delete(&refs).await
    }

    /// Delete one key. Called after a write so the next read is live.
    pub async fn cache_drop(&self, suffix: &str) {
        let Some(cache) = self.redis.as_ref() else {
            return;
        };
        cache.delete(&[suffix]).await;
    }

    /// Flat, self-describing snapshot: never a dataclass the cache owns.
    ///
    /// A hand-written shape (rather than a derived one) means the cached blob
    /// cannot silently break every reader when a field is added.
    pub fn dump(gw: &Giveaway) -> Value {
        json!({
            "id": &gw.id,
            "guild_id": &gw.guild_id,
            "channel_id": &gw.channel_id,
            "message_id": gw.message_id.as_deref(),
            "prize": &gw.prize,
            "winner_count": gw.winner_count,
            "ends_at": gw.ends_at,
            "status": &gw.status,
            "required_role_id": gw.required_role_id.as_deref(),
            "required_role_ids": &gw.required_role_ids,
            "blocked_role_id": gw.blocked_role_id.as_deref(),
            "min_account_age_days": gw.min_account_age_days,
            "min_messages": gw.min_messages,
            "image_url": gw.image_url.as_deref(),
            "entrants_role_id": gw.entrants_role_id.as_deref(),
            "host_id": gw.host_id.as_deref(),
            "host_name": gw.host_name.as_deref(),
            "winners": &gw.winners,
            "claim_timeout_seconds": gw.claim_timeout_seconds,
            "entrant_count": gw.entrant_count,
        })
    }

    /// Rebuild a Giveaway from a cached blob, or None if it is unusable.
    pub fn load_gw(blob: &Value) -> Option<Giveaway> {
        let obj = blob.as_object()?;
        let id_value = obj.get("id")?;
        if !truthy(id_value) {
            return None;
        }
        Some(Giveaway {
            id: py_str(id_value),
            guild_id: json_text_or_empty(obj.get("guild_id")),
            channel_id: json_text_or_empty(obj.get("channel_id")),
            message_id: json_opt_text(obj.get("message_id")),
            prize: json_text_or_empty(obj.get("prize")),
            winner_count: json_int_or(obj.get("winner_count"), 0),
            ends_at: json_int_or(obj.get("ends_at"), 0),
            status: json_text_or_empty(obj.get("status")),
            required_role_id: json_opt_text(obj.get("required_role_id")),
            required_role_ids: json_str_list(obj.get("required_role_ids")),
            blocked_role_id: json_opt_text(obj.get("blocked_role_id")),
            min_account_age_days: json_int_or(obj.get("min_account_age_days"), 0),
            min_messages: json_int_or(obj.get("min_messages"), 0),
            image_url: json_opt_text(obj.get("image_url")),
            entrants_role_id: json_opt_text(obj.get("entrants_role_id")),
            host_id: json_opt_text(obj.get("host_id")),
            host_name: json_opt_text(obj.get("host_name")),
            winners: json_str_list(obj.get("winners")),
            claim_timeout_seconds: json_int_or(obj.get("claim_timeout_seconds"), 0),
            entrant_count: obj
                .get("entrant_count")
                .filter(|value| !value.is_null())
                .map(|value| json_int_or(Some(value), 0)),
        })
    }
}

// -- small value helpers -------------------------------------------------

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().map(|n| n != 0.0).unwrap_or(false),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `str(value)` as Python renders it, for the values a cached blob can hold.
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(flag) => {
            if *flag {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(number) => number.to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// `str(blob.get(name) or "")`.
fn json_text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(inner) if truthy(inner) => py_str(inner),
        _ => String::new(),
    }
}

/// `str(blob[name]) if blob.get(name) else None`.
fn json_opt_text(value: Option<&Value>) -> Option<String> {
    match value {
        Some(inner) if truthy(inner) => Some(py_str(inner)),
        _ => None,
    }
}

/// `int(value or 0)`, with `default` for the falsy and unparseable cases.
fn json_int_or(value: Option<&Value>, default: i64) -> i64 {
    let Some(inner) = value else {
        return default;
    };
    if !truthy(inner) {
        return default;
    }
    json_int(inner).unwrap_or(default)
}

fn json_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|n| n as i64)),
        Value::String(text) => text
            .trim()
            .parse::<i64>()
            .ok()
            .or_else(|| text.trim().parse::<f64>().ok().map(|n| n as i64)),
        Value::Bool(flag) => Some(i64::from(*flag)),
        _ => None,
    }
}

/// `[str(item) for item in (value or [])]` for the array shape the cache writes.
fn json_str_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items.iter().map(py_str).collect(),
        _ => Vec::new(),
    }
}

/// `json.dumps(list_of_strings)`: the cache stores Python's spaced separators.
fn json_array(items: &[String]) -> String {
    let encoded: Vec<String> = items
        .iter()
        .map(|item| serde_json::to_string(item).unwrap_or_else(|_| "\"\"".to_string()))
        .collect();
    format!("[{}]", encoded.join(", "))
}

fn sql_to_json(value: &SqlValue) -> Value {
    match value {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(number) => json!(number),
        SqlValue::Real(number) => json!(number),
        SqlValue::Text(text) => json!(text),
        SqlValue::Blob(bytes) => json!(String::from_utf8_lossy(bytes).to_string()),
    }
}

/// One result row as the dict-shaped value the Python service returns.
fn row_to_json(row: &Row) -> Value {
    let mut map = serde_json::Map::new();
    for (name, value) in row.columns().iter().zip(row.values()) {
        map.insert(name.clone(), sql_to_json(value));
    }
    Value::Object(map)
}

fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

fn random_id() -> String {
    let raw: u64 = rand::random();
    format!("gw_{:012x}", raw & 0x0000_ffff_ffff_ffff)
}

fn dedup_preserving(items: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut seen = HashSet::new();
    items
        .into_iter()
        .filter(|item| seen.insert(item.clone()))
        .collect()
}

impl GiveawayService {
    // -- create ---------------------------------------------------------

    #[allow(clippy::too_many_arguments)]
    pub async fn create(
        &self,
        guild_id: &str,
        channel_id: &str,
        prize: &str,
        winner_count: i64,
        duration_seconds: i64,
        created_by: &str,
        required_role_id: Option<&str>,
        required_role_ids: Option<&[String]>,
        blocked_role_id: Option<&str>,
        min_account_age_days: i64,
        min_messages: i64,
        image_url: Option<&str>,
        host_id: Option<&str>,
        host_name: Option<&str>,
        claim_timeout_seconds: i64,
    ) -> Result<Giveaway> {
        let prize = prize.trim();
        if prize.is_empty() || prize.chars().count() > 256 {
            return Err(ServiceError::new("Prize must be 1-256 characters.").into());
        }
        if !(0..=60 * 86400).contains(&claim_timeout_seconds) {
            return Err(ServiceError::new(
                "Claim window must be 0 (off) or up to 60 days in seconds.",
            )
            .into());
        }
        if !(1..=MAX_WINNERS).contains(&winner_count) {
            return Err(ServiceError::new(format!("Winner count must be 1-{MAX_WINNERS}.")).into());
        }
        if !(30..=60 * 86400).contains(&duration_seconds) {
            return Err(ServiceError::new("Duration must be 30 seconds to 60 days.").into());
        }
        if !(0..=100000).contains(&min_messages) {
            return Err(ServiceError::new("Minimum messages must be 0-100000.").into());
        }
        if !(0..=3650).contains(&min_account_age_days) {
            return Err(ServiceError::new("Minimum account age must be 0-3650 days.").into());
        }
        let image_url = image_url.map(str::trim).filter(|url| !url.is_empty());
        if let Some(url) = image_url {
            if url.chars().count() > 512
                || !(url.starts_with("http://") || url.starts_with("https://"))
            {
                return Err(ServiceError::new("Image must be an http(s) URL.").into());
            }
        }
        let mut role_ids: Vec<String> = required_role_ids
            .unwrap_or(&[])
            .iter()
            .map(|role| role.trim().to_string())
            .filter(|role| !role.is_empty())
            .collect();
        if let Some(single) = required_role_id {
            let stripped = single.trim();
            if !single.is_empty() && !role_ids.iter().any(|role| role == stripped) {
                role_ids.push(stripped.to_string());
            }
        }
        role_ids.truncate(5);
        let gid = random_id();
        let created = now_ms();
        let ends_at = created + duration_seconds * 1000;
        let blocked = blocked_role_id.filter(|role| !role.is_empty());
        let min_age = min_account_age_days.max(0);
        let min_msgs = min_messages.max(0);
        let host = host_id.filter(|id| !id.is_empty()).unwrap_or(created_by);
        let clean_host_name: String = host_name.unwrap_or("").chars().take(64).collect();
        let host_name_value = if clean_host_name.is_empty() {
            None
        } else {
            Some(clean_host_name)
        };
        let role_json = json_array(&role_ids);
        let params: Vec<SqlValue> = vec![
            gid.as_str().into(),
            guild_id.into(),
            channel_id.into(),
            prize.into(),
            winner_count.into(),
            ends_at.into(),
            role_ids.first().map(String::as_str).into(),
            role_json.into(),
            blocked.into(),
            min_age.into(),
            min_msgs.into(),
            image_url.into(),
            created_by.into(),
            created.into(),
            host.into(),
            host_name_value.as_deref().into(),
            claim_timeout_seconds.into(),
        ];
        self.db
            .execute(
                "INSERT INTO simple_giveaways (id, guild_id, channel_id, prize, winner_count, ends_at, status, required_role_id, required_role_ids, blocked_role_id, min_account_age_days, min_messages, image_url, created_by, created_at, host_id, host_name, claim_timeout_seconds) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                &params,
            )
            .await?;
        // A brand-new running giveaway changes both the guild list and the
        // global list; without this an autocomplete would not offer it until
        // the 8s TTL lapsed.
        self.mark_stale(&[&format!("guild:{guild_id}:active"), "all:active"]);
        Ok(Giveaway {
            id: gid,
            guild_id: guild_id.to_string(),
            channel_id: channel_id.to_string(),
            message_id: None,
            prize: prize.to_string(),
            winner_count,
            ends_at,
            status: "active".to_string(),
            required_role_id: role_ids.first().cloned(),
            required_role_ids: role_ids,
            blocked_role_id: blocked.map(str::to_string),
            min_account_age_days: min_age,
            min_messages: min_msgs,
            image_url: image_url.map(str::to_string),
            entrants_role_id: None,
            host_id: Some(host.to_string()),
            host_name: host_name_value,
            winners: Vec::new(),
            claim_timeout_seconds,
            entrant_count: Some(0),
        })
    }

    // -- guild settings (one-time notify-role setup) ---------------------

    async fn get_notify_role_uncached(&self, guild_id: &str) -> Result<Option<String>> {
        let row = self
            .db
            .query_one(
                "SELECT notify_role_id FROM simple_guild_settings WHERE guild_id = ?",
                &[guild_id.into()],
            )
            .await?;
        Ok(row
            .and_then(|row| row.text("notify_role_id"))
            .filter(|role| !role.is_empty()))
    }

    pub async fn get_notify_role(&self, guild_id: &str) -> Result<Option<String>> {
        self.get_notify_role_uncached(guild_id).await
    }

    /// Guild notify-role, served from the cache when warm.
    ///
    /// A guild that has never set one has no row, so the read is cached as the
    /// explicit marker 'none' rather than as an absent key: otherwise a repeat
    /// visitor would pay a Turso round trip on every join forever.
    pub async fn get_notify_role_cached(&self, guild_id: &str) -> Option<String> {
        let key = format!("settings:{guild_id}:notify_role");
        let cached = self.cache_get(&key).await;
        if let Some(Value::String(text)) = &cached {
            if text == "none" {
                return None;
            }
            if !text.is_empty() {
                return Some(text.clone());
            }
        }
        let role = match self.get_notify_role_uncached(guild_id).await {
            Ok(role) => role,
            Err(_) => return None,
        };
        let stored = json!(role.clone().unwrap_or_else(|| "none".to_string()));
        self.cache_put(&key, &stored, SETTINGS_CACHE_TTL).await;
        role
    }

    pub async fn set_notify_role(&self, guild_id: &str, role_id: Option<&str>) -> Result<()> {
        self.db
            .execute(
                "INSERT INTO simple_guild_settings (guild_id, notify_role_id) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET notify_role_id = excluded.notify_role_id",
                &[guild_id.into(), role_id.into()],
            )
            .await?;
        Ok(())
    }

    /// Drop the cached notify-role after a write.
    pub async fn invalidate_notify_role(&self, guild_id: &str) {
        self.cache_drop(&format!("settings:{guild_id}:notify_role"))
            .await;
    }

    // -- read -----------------------------------------------------------

    pub async fn get(&self, giveaway_id: &str) -> Result<Giveaway> {
        let row = self
            .db
            .query_one(
                "SELECT * FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        match row {
            Some(row) => Ok(Giveaway::from_row(&row)),
            None => Err(ServiceError::new("Giveaway not found.").into()),
        }
    }

    pub async fn get_by_message(&self, message_id: &str) -> Result<Option<Giveaway>> {
        let row = self
            .db
            .query_one(
                "SELECT * FROM simple_giveaways WHERE message_id = ?",
                &[message_id.into()],
            )
            .await?;
        Ok(row.as_ref().map(Giveaway::from_row))
    }

    pub async fn list_active(&self, guild_id: &str) -> Result<Vec<Giveaway>> {
        let rows = self
            .db
            .query(
                "SELECT * FROM simple_giveaways WHERE guild_id = ? AND status = 'active' ORDER BY ends_at ASC",
                &[guild_id.into()],
            )
            .await?;
        Ok(rows.iter().map(Giveaway::from_row).collect())
    }

    /// Every active giveaway, soonest deadline first.
    ///
    /// Feeds both the embed refresher and the autocomplete cache, so the cap is
    /// per tick rather than per guild.
    pub async fn list_all_active(&self, limit: i64) -> Result<Vec<Giveaway>> {
        let capped = limit.clamp(1, 200);
        let rows = self
            .db
            .query(
                "SELECT * FROM simple_giveaways WHERE status = 'active' ORDER BY ends_at ASC LIMIT ?",
                &[capped.into()],
            )
            .await?;
        Ok(rows.iter().map(Giveaway::from_row).collect())
    }

    /// This guild's active giveaways, from the cache when warm.
    ///
    /// Used by the paths where 8s of staleness is cosmetic (an autocomplete
    /// list, a role sweep).
    pub async fn list_active_cached(&self, guild_id: &str) -> Result<Vec<Giveaway>> {
        let key = format!("guild:{guild_id}:active");
        if let Some(Value::Array(items)) = self.cache_get(&key).await {
            return Ok(items.iter().filter_map(GiveawayService::load_gw).collect());
        }
        let live = self.list_active(guild_id).await?;
        let dumps: Vec<Value> = live.iter().map(GiveawayService::dump).collect();
        self.cache_put(&key, &Value::Array(dumps), LIST_CACHE_TTL)
            .await;
        Ok(live)
    }

    /// Find a giveaway by id, falling back to the live one.
    ///
    /// Operators keep mistyping ids, so when the id does not match and exactly
    /// one giveaway is running in this server, that one is used instead.
    pub async fn resolve(&self, guild_id: &str, raw_id: &str) -> Result<Giveaway> {
        let raw_id = raw_id.trim();
        if !raw_id.is_empty() {
            if let Ok(gw) = self.get(raw_id).await {
                // An id can be typed by hand (or copied from another server's
                // message), and every command here is guild-scoped: refuse
                // rather than end someone else's giveaway.
                if gw.guild_id != guild_id {
                    return Err(
                        ServiceError::new("That giveaway belongs to another server.").into(),
                    );
                }
                return Ok(gw);
            }
        }
        let active = self.list_active(guild_id).await?;
        if active.len() == 1 {
            return Ok(active.into_iter().next().expect("one active giveaway"));
        }
        if active.is_empty() {
            return Err(ServiceError::new("No active giveaway in this server.").into());
        }
        Err(ServiceError::new(
            "That ID did not match. Pick one from the suggestions as you type, or copy the ID from /giveaway_list.",
        )
        .into())
    }

    /// Ended giveaways in one server, most recently ended first.
    pub async fn list_ended(&self, guild_id: &str, limit: i64) -> Result<Vec<Giveaway>> {
        let capped = limit.clamp(1, 100);
        let rows = self
            .db
            .query(
                "SELECT * FROM simple_giveaways WHERE guild_id = ? AND status = 'ended' ORDER BY ended_at DESC LIMIT ?",
                &[guild_id.into(), capped.into()],
            )
            .await?;
        Ok(rows.iter().map(Giveaway::from_row).collect())
    }

    /// Every ended giveaway, most recently ended first.
    pub async fn list_all_ended(&self, limit: i64) -> Result<Vec<Giveaway>> {
        let capped = limit.clamp(1, 200);
        let rows = self
            .db
            .query(
                "SELECT * FROM simple_giveaways WHERE status = 'ended' ORDER BY ended_at DESC LIMIT ?",
                &[capped.into()],
            )
            .await?;
        Ok(rows.iter().map(Giveaway::from_row).collect())
    }

    /// Find an ended giveaway by id, falling back to the ended one.
    ///
    /// Reroll/skipclaim only work on ended giveaways, so unlike resolve() this
    /// never falls back to a live one.
    pub async fn resolve_ended(&self, guild_id: &str, raw_id: &str) -> Result<Giveaway> {
        let raw_id = raw_id.trim();
        if !raw_id.is_empty() {
            if let Ok(gw) = self.get(raw_id).await {
                if gw.guild_id != guild_id {
                    return Err(
                        ServiceError::new("That giveaway belongs to another server.").into(),
                    );
                }
                if gw.active() || gw.status != "ended" {
                    return Err(ServiceError::new("That giveaway has not ended yet.").into());
                }
                return Ok(gw);
            }
        }
        let ended = self.list_ended(guild_id, 25).await?;
        if ended.len() == 1 {
            return Ok(ended.into_iter().next().expect("one ended giveaway"));
        }
        if ended.is_empty() {
            return Err(ServiceError::new("No ended giveaway in this server.").into());
        }
        Err(ServiceError::new(
            "That ID did not match. Pick one from the suggestions as you type, or copy the ID from /giveaway_list.",
        )
        .into())
    }

    /// Live entry count, counter-first with COUNT(*) backfill.
    ///
    /// exact=True forces COUNT(*): the counter is a fast path, not the source
    /// of truth after bulk deletes.
    pub async fn entry_count(&self, giveaway_id: &str, exact: bool) -> Result<i64> {
        if exact {
            let true_row = self
                .db
                .query_one(
                    "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?",
                    &[giveaway_id.into()],
                )
                .await?;
            return Ok(true_row.and_then(|row| row.int("n")).unwrap_or(0));
        }
        let row = self
            .db
            .query_one(
                "SELECT entrant_count FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        let cached = row.and_then(|row| row.int("entrant_count")).unwrap_or(0);
        if cached > 0 {
            return Ok(cached);
        }
        let true_row = self
            .db
            .query_one(
                "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        let true_n = true_row.and_then(|row| row.int("n")).unwrap_or(0);
        if true_n > 0 {
            // Heal a legacy NULL/0 counter; a failure only costs the next read.
            let _ = self
                .db
                .execute(
                    "UPDATE simple_giveaways SET entrant_count = ? WHERE id = ? AND (entrant_count IS NULL OR entrant_count < ?)",
                    &[true_n.into(), giveaway_id.into(), true_n.into()],
                )
                .await;
        }
        Ok(true_n)
    }

    /// Entry count for display paths, cached briefly.
    pub async fn entry_count_cached(&self, giveaway_id: &str) -> Result<i64> {
        let key = format!("count:{giveaway_id}");
        if let Some(cached) = self.cache_get_int(&key).await {
            return Ok(cached);
        }
        let value = self.entry_count(giveaway_id, false).await?;
        // A count of zero is a real answer and gets cached like any other.
        self.cache_put(&key, &json!(value), COUNT_CACHE_TTL).await;
        Ok(value)
    }

    /// Reset counter to COUNT(*) after a bulk drop (ban/timeout).
    ///
    /// Rare path (bans only), so a full COUNT(*) here is fine; failures only
    /// log (the read path heals it).
    pub async fn sync_counter(&self, giveaway_id: &str) {
        let Ok(Some(row)) = self
            .db
            .query_one(
                "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?",
                &[giveaway_id.into()],
            )
            .await
        else {
            return;
        };
        let Some(count) = row.int("n") else {
            return;
        };
        if self
            .db
            .execute(
                "UPDATE simple_giveaways SET entrant_count = ? WHERE id = ?",
                &[count.into(), giveaway_id.into()],
            )
            .await
            .is_err()
        {
            return;
        }
        // The cached display count for this giveaway is now wrong: the rows
        // were dropped here, not through leave(), so nothing else marks it.
        self.mark_stale(&[&format!("count:{giveaway_id}")]);
    }
}

impl GiveawayService {
    // -- entries ---------------------------------------------------------

    /// Window + true count + membership flag in ONE read of the entries table.
    ///
    /// The participants panel used to read the same table three times: the
    /// count, the window, then "am I in it". One statement answers all three --
    /// the rows the window needs, COUNT(*) OVER () for the true total (SQLite
    /// window functions, 3.25+), and whether the caller's own row is among the
    /// rows the window returned. Returns None when the read fails, so the
    /// caller can keep its own "could not load the list" text rather than
    /// printing a zero as if it were a fact.
    pub async fn entries_snapshot(
        &self,
        giveaway_id: &str,
        limit: Option<i64>,
        user_id: &str,
    ) -> Option<(i64, Vec<Value>, i64)> {
        let (statement, params): (&str, Vec<SqlValue>) = match limit {
            None => (
                "SELECT user_id, username, entered_at, COUNT(*) OVER () AS total, (user_id = ?) AS me FROM simple_entries WHERE giveaway_id = ? ORDER BY entered_at ASC",
                vec![user_id.into(), giveaway_id.into()],
            ),
            Some(limit) => (
                "SELECT user_id, username, entered_at, COUNT(*) OVER () AS total, (user_id = ?) AS me FROM simple_entries WHERE giveaway_id = ? ORDER BY entered_at ASC LIMIT ?",
                vec![user_id.into(), giveaway_id.into(), limit.max(1).into()],
            ),
        };
        let rows = match self.db.query(statement, &params).await {
            Ok(rows) => rows,
            Err(_) => return None,
        };
        if rows.is_empty() {
            return Some((0, Vec::new(), 0));
        }
        let total = rows[0].int("total").unwrap_or(0);
        // COUNT(*) OVER () counts every matching row even though LIMIT caps the
        // rows we keep, so the header total stays the true one on a giveaway
        // bigger than the window (the "Showing the first N of T" line depends
        // on that). The flag is the caller's own row, compared as a string.
        let mut mine = 0i64;
        let mut window = Vec::with_capacity(rows.len());
        for row in &rows {
            if row.text("user_id").as_deref() == Some(user_id) {
                mine = 1;
            }
            window.push(json!({
                "user_id": row.get("user_id").map(sql_to_json).unwrap_or(Value::Null),
                "username": row.get("username").map(sql_to_json).unwrap_or(Value::Null),
                "entered_at": row.get("entered_at").map(sql_to_json).unwrap_or(Value::Null),
            }));
        }
        Some((total, window, mine))
    }

    /// Entrants in entry order. Pass limit for a display path.
    ///
    /// The draw needs the whole pool, but a listing shows the first page only:
    /// a giveaway with tens of thousands of entries should not ship all of them
    /// over the wire (and allocate them) to print fifty mentions.
    pub async fn entries(&self, giveaway_id: &str, limit: Option<i64>) -> Result<Vec<Value>> {
        let statement = "SELECT user_id, username, entered_at FROM simple_entries WHERE giveaway_id = ? ORDER BY entered_at ASC";
        let rows = match limit {
            None => self.db.query(statement, &[giveaway_id.into()]).await?,
            Some(limit) => {
                self.db
                    .query(
                        &format!("{statement} LIMIT ?"),
                        &[giveaway_id.into(), limit.max(1).into()],
                    )
                    .await?
            }
        };
        Ok(rows.iter().map(row_to_json).collect())
    }

    /// Whether this member is entered: one lookup, no rows loaded.
    ///
    /// The participants panel shows a bounded window, so "am I in it" cannot be
    /// answered by scanning the rows it happens to hold.
    pub async fn has_entry(&self, giveaway_id: &str, user_id: &str) -> Result<bool> {
        if user_id.is_empty() {
            return Ok(false);
        }
        Ok(self
            .db
            .query_one(
                "SELECT 1 AS one FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
                &[giveaway_id.into(), user_id.into()],
            )
            .await?
            .is_some())
    }

    // -- join / leave ---------------------------------------------------

    #[allow(clippy::too_many_arguments)]
    pub async fn check_eligible(
        &self,
        gw: &Giveaway,
        member_roles: &[String],
        account_created_ts: Option<f64>,
        user_id: &str,
        pending_messages: i64,
        timed_out: bool,
        snapshot: Option<(bool, i64, i64, bool)>,
    ) -> Result<()> {
        let mut snap_bl: Option<bool> = None;
        let mut snap_ban: Option<i64> = None;
        let mut snap_msgs: Option<i64> = None;
        let mut snap_hit: Option<bool> = None;
        if let Some((bl, ban, msgs, hit)) = snapshot {
            snap_bl = Some(bl);
            snap_ban = Some(ban);
            snap_msgs = Some(msgs);
            snap_hit = Some(hit);
        }
        if !gw.active() {
            return Err(ServiceError::new("This giveaway has ended.").into());
        }
        if !user_id.is_empty() && snap_bl == Some(true) {
            // Snapshot says blocked: confirm live before refusing, so a
            // blacklist_remove landing between the snapshot and this thread
            // cannot falsely refuse. Snapshot-False proceeds: the INSERT
            // guard below still blocks a racing blacklist_add.
            if self.is_blacklisted(&gw.guild_id, user_id, None).await? {
                return Err(ServiceError::new(
                    self.ban_message(&gw.guild_id, user_id, None).await?,
                )
                .into());
            }
        } else if !user_id.is_empty()
            && snap_bl.is_none()
            && self.is_blacklisted(&gw.guild_id, user_id, None).await?
        {
            return Err(
                ServiceError::new(self.ban_message(&gw.guild_id, user_id, None).await?).into(),
            );
        }
        if gw.ends_at <= now_ms() {
            return Err(ServiceError::new("This giveaway has ended.").into());
        }
        // Discord's native timeout (/mute), and anyone still sitting out the
        // penalty that started from it. Deliberately placed after the "is this
        // giveaway still joinable" checks, so a stale button on an ended one
        // cannot hand out a penalty. This is the only check here that writes:
        // it also drops an entry the blocked member already had.
        self.check_timeout_ban(gw, user_id, timed_out, snap_ban, snap_hit)
            .await?;
        let roles: HashSet<&str> = member_roles.iter().map(String::as_str).collect();
        if !gw.required_role_ids.is_empty()
            && !gw
                .required_role_ids
                .iter()
                .any(|role| roles.contains(role.as_str()))
        {
            return Err(ServiceError::new("You need one of the required roles to enter.").into());
        }
        if let Some(blocked) = gw
            .blocked_role_id
            .as_deref()
            .filter(|role| !role.is_empty())
        {
            if roles.contains(blocked) {
                return Err(ServiceError::new("Your role is not allowed to enter.").into());
            }
        }
        if gw.min_account_age_days > 0 {
            let Some(created) = account_created_ts else {
                // The age could not be read, so the requirement cannot be
                // checked. Refusing is the safe side of that trade: waving it
                // through would silently disable the alt-account filter.
                return Err(ServiceError::new(
                    "Could not check your account age — try again in a moment.",
                )
                .into());
            };
            let age_days = (now_secs() - created) / 86400.0;
            if age_days < gw.min_account_age_days as f64 {
                return Err(ServiceError::new(format!(
                    "Account must be {}+ days old.",
                    gw.min_account_age_days
                ))
                .into());
            }
        }
        if gw.min_messages > 0 {
            // pending_messages adds the rows still sitting in the bot's in-memory
            // buffer, so nobody is told they sent fewer messages than they did
            // since the last flush.
            let pending = pending_messages.max(0);
            let mut sent = match snap_msgs {
                Some(msgs) => msgs + pending,
                None => self.message_count(&gw.guild_id, user_id).await? + pending,
            };
            if sent < gw.min_messages {
                // Snapshot staleness: the tuple was read before this thread
                // started, so a flush landing in between reads low and would
                // falsely refuse. Re-read live once before refusing; the happy
                // path (snapshot already sufficient) costs nothing extra.
                if snap_msgs.is_some() {
                    sent = self.message_count(&gw.guild_id, user_id).await? + pending;
                }
                if sent < gw.min_messages {
                    return Err(ServiceError::new(format!(
                        "You need {}+ messages in this server ({} counted).",
                        gw.min_messages, sent
                    ))
                    .into());
                }
            }
        }
        Ok(())
    }

    pub async fn message_count(&self, guild_id: &str, user_id: &str) -> Result<i64> {
        let row = self
            .db
            .query_one(
                "SELECT count FROM simple_message_counts WHERE guild_id = ? AND user_id = ?",
                &[guild_id.into(), user_id.into()],
            )
            .await?;
        Ok(row.and_then(|row| row.int("count")).unwrap_or(0))
    }

    /// Lightweight post-join liveness check: status only, no full row.
    ///
    /// handle_join already holds the Giveaway it joined; re-reading the whole
    /// row just to decide grant-vs-take + refresh would cost a second full
    /// SELECT. Status is all the race needs (ended/cancelled vs active).
    pub async fn get_status(&self, giveaway_id: &str) -> Result<Option<String>> {
        let row = self
            .db
            .query_one(
                "SELECT status FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        Ok(row.text("status").filter(|status| !status.is_empty()))
    }

    /// Status + entry count in one round-trip for the post-ack refresh.
    ///
    /// handle_join used to pay get_status() then entry_count() sequentially
    /// (2 waves, ~200ms warm) just to decide grant-vs-take and render the
    /// embed. One SELECT with scalar subqueries returns both; a dead read
    /// returns (None, 0) so the caller skips background work fail-closed
    /// instead of mis-rendering. P2-2: count comes from the live counter
    /// column (1 indexed row), never COUNT(*) -- the scan cost no longer
    /// grows with entries.
    pub async fn refresh_snapshot(&self, giveaway_id: &str) -> (Option<String>, i64) {
        let Ok(Some(row)) = self
            .db
            .query_one(
                "SELECT status, COALESCE(entrant_count, 0) AS n FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await
        else {
            return (None, 0);
        };
        let status = row.text("status").filter(|status| !status.is_empty());
        let count = row.int("n").unwrap_or(0);
        (status, count)
    }

    /// Giveaway row + eligibility tuple in one round-trip (fast-path).
    ///
    /// One SELECT returns the giveaway columns plus the 4 snapshot subqueries,
    /// so the pre-ack phase costs 1 wave on 1 thread. Snapshot parse failures
    /// return None (fail-closed to the individual checks); a missing giveaway
    /// raises ServiceError.
    pub async fn join_snapshot(
        &self,
        giveaway_id: &str,
        guild_id: &str,
        user_id: &str,
    ) -> Result<(Giveaway, Option<(bool, i64, i64, bool)>)> {
        let snap_now = now_ms();
        let row = self
            .db
            .query_one(
                "SELECT g.*, (SELECT 1 FROM simple_blacklist WHERE guild_id = ? AND user_id = ? AND (expires_at IS NULL OR expires_at > ?)) AS bl, (SELECT giveaways_remaining FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?) AS ban, (SELECT count FROM simple_message_counts WHERE guild_id = ? AND user_id = ?) AS msgs, (SELECT 1 FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ? AND giveaway_id = ?) AS hit FROM simple_giveaways AS g WHERE g.id = ?",
                &[
                    guild_id.into(),
                    user_id.into(),
                    snap_now.into(),
                    guild_id.into(),
                    user_id.into(),
                    guild_id.into(),
                    user_id.into(),
                    guild_id.into(),
                    user_id.into(),
                    giveaway_id.into(),
                    giveaway_id.into(),
                ],
            )
            .await;
        let row = match row {
            Ok(row) => row,
            Err(_) => {
                // Read failed ambiguously: fall back to the two-query path so a
                // dead stream can never look like "giveaway not found".
                let gw = self.get(giveaway_id).await?;
                let snap = self
                    .eligibility_snapshot(guild_id, user_id, Some(giveaway_id))
                    .await;
                return Ok((gw, snap));
            }
        };
        let Some(row) = row else {
            return Err(ServiceError::new("Giveaway not found.").into());
        };
        let gw = Giveaway::from_row(&row);
        let bl = row.get("bl").map(|value| !value.is_null()).unwrap_or(false);
        let ban = row.int("ban").unwrap_or(0).max(0);
        let msgs = row.int("msgs").unwrap_or(0);
        let hit = row
            .get("hit")
            .map(|value| !value.is_null())
            .unwrap_or(false);
        Ok((gw, Some((bl, ban, msgs, hit))))
    }

    /// Blacklist + timeout-ban + message count + ban-hit in one round-trip.
    ///
    /// Returns (blacklisted, ban_remaining, msg_count, was_blocked), or None
    /// when the read itself failed: None forces the caller back onto the
    /// individual checks (fail-closed), so a dead stream can never look like
    /// "not blacklisted, no penalty".
    pub async fn eligibility_snapshot(
        &self,
        guild_id: &str,
        user_id: &str,
        giveaway_id: Option<&str>,
    ) -> Option<(bool, i64, i64, bool)> {
        if guild_id.is_empty() || user_id.is_empty() {
            return Some((false, 0, 0, false));
        }
        let snap_now = now_ms();
        let row = self
            .db
            .query_one(
                "SELECT  (SELECT 1 FROM simple_blacklist WHERE guild_id = ? AND user_id = ? AND (expires_at IS NULL OR expires_at > ?)) AS bl,  (SELECT giveaways_remaining FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?) AS ban,  (SELECT count FROM simple_message_counts WHERE guild_id = ? AND user_id = ?) AS msgs,  (SELECT 1 FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ? AND giveaway_id = ?) AS hit",
                &[
                    guild_id.into(),
                    user_id.into(),
                    snap_now.into(),
                    guild_id.into(),
                    user_id.into(),
                    guild_id.into(),
                    user_id.into(),
                    guild_id.into(),
                    user_id.into(),
                    giveaway_id.unwrap_or("").into(),
                ],
            )
            .await
            .ok()?;
        let row = row?;
        let bl = row.get("bl").map(|value| !value.is_null()).unwrap_or(false);
        let ban = row.int("ban").unwrap_or(0).max(0);
        let msgs = row.int("msgs").unwrap_or(0);
        let hit = row
            .get("hit")
            .map(|value| !value.is_null())
            .unwrap_or(false);
        Some((bl, ban, msgs, hit))
    }

    pub async fn record_message(&self, guild_id: &str, user_id: &str) -> Result<()> {
        self.db
            .execute(
                "INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES (?, ?, 1) ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
                &[guild_id.into(), user_id.into()],
            )
            .await?;
        Ok(())
    }
}

/// Sets the receipt's `done` flag when the flush stops, including when the
/// caller abandons the future: Python's `finally` ran on cancellation too, and
/// the bot reads this flag to tell "stopped writing" from "still running".
struct ReceiptDone<'a>(Option<&'a FlushReceipt>);

impl Drop for ReceiptDone<'_> {
    fn drop(&mut self) {
        if let Some(receipt) = self.0 {
            receipt.done.store(true, Ordering::SeqCst);
        }
    }
}

impl GiveawayService {
    // -- message counts (batched) ----------------------------------------

    /// Apply many (guild_id, user_id, delta) counts. Returns rows applied.
    ///
    /// One statement per flush instead of one per message is the entire point:
    /// N messages in the server cost ceil(N / MESSAGE_FLUSH_CHUNK) round-trips
    /// per flush interval rather than N immediate ones.
    ///
    /// Each chunk commits on its own, so a failure part way leaves the earlier
    /// chunks stored. The failure is raised as PartialFlush, which says how many
    /// rows landed: the caller hands back only the tail, because restoring the
    /// whole batch would write the committed head a second time.
    ///
    /// `receipt`, when the caller passes one, is filled in place while the work
    /// runs: `before` (the snapshot taken for each chunk), `applied` (rows
    /// committed so far) and `done` (set once this call has stopped writing,
    /// on success or failure alike). A timed-out flush keeps writing, so the
    /// receipt is how the caller learns what the abandoned worker actually
    /// committed instead of re-sending the batch.
    pub async fn add_message_counts(
        &self,
        rows: &mut Vec<(String, String, i64)>,
        receipt: Option<&FlushReceipt>,
    ) -> Result<usize> {
        if let Some(receipt) = receipt {
            if let Ok(mut before) = receipt.before.lock() {
                before.clear();
            }
            receipt.applied.store(0, Ordering::SeqCst);
        }
        let _done = ReceiptDone(receipt);
        self.apply_message_chunks(rows, receipt).await
    }

    /// Write the batch chunk by chunk; see add_message_counts.
    async fn apply_message_chunks(
        &self,
        rows: &mut Vec<(String, String, i64)>,
        receipt: Option<&FlushReceipt>,
    ) -> Result<usize> {
        let mut applied = 0usize;
        let mut start = 0usize;
        while start < rows.len() {
            let end = (start + MESSAGE_FLUSH_CHUNK).min(rows.len());
            let chunk: Vec<(String, String, i64)> = rows[start..end].to_vec();
            // Snapshot first: if this chunk later fails ambiguously (a mid-write
            // "connection reset" db.execute will not replay), the snapshot
            // lets the retry requeue only what is still missing instead of the
            // whole chunk. One batched read per chunk, never one per key.
            let before = self.read_counts(&chunk).await;
            if let Some(receipt) = receipt {
                if let Ok(mut seen) = receipt.before.lock() {
                    seen.extend(before.clone());
                }
            }
            let values = vec!["(?, ?, ?)"; chunk.len()].join(", ");
            let mut params: Vec<SqlValue> = Vec::with_capacity(chunk.len() * 3);
            for (guild_id, user_id, count) in &chunk {
                params.push(guild_id.as_str().into());
                params.push(user_id.as_str().into());
                params.push((*count).into());
            }
            // Interpolated text is the placeholder list only: every value still
            // travels as a bound parameter, so there is nothing to inject.
            let statement = format!(
                "INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES {values} ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count"
            );
            if let Err(exc) = self.db.execute(&statement, &params).await {
                if is_ambiguous_loss(&exc.to_string().to_lowercase()) {
                    // The write may have landed before the response was lost.
                    // applied counts whole chunks and the caller requeues
                    // rows[applied:], so the reconciled shortfall replaces the
                    // chunk in place (padded with zero-deltas to keep the list
                    // length stable) and the retry adds each key once.
                    let short = self.reconcile_chunk(&chunk, &before).await;
                    let filler: Vec<(String, String, i64)> = chunk[short.len()..]
                        .iter()
                        .map(|(guild_id, user_id, _)| (guild_id.clone(), user_id.clone(), 0))
                        .collect();
                    let mut replacement = short;
                    replacement.extend(filler);
                    rows.splice(start..end, replacement);
                }
                return Err(anyhow::Error::from(PartialFlush {
                    applied,
                    error: exc,
                }));
            }
            applied += chunk.len();
            if let Some(receipt) = receipt {
                receipt.applied.store(applied, Ordering::SeqCst);
            }
            start += chunk.len();
        }
        Ok(applied)
    }

    /// Requeue only what a timed-out flush has not already written.
    ///
    /// `rows` is a batch whose worker may still have been running when the
    /// caller gave up waiting, and `before` is that batch's pre-write snapshot
    /// (handed over in the receipt). Requeueing the whole batch in that case
    /// double-counted every message the abandoned worker went on to commit, so
    /// the shortfall is measured against the stored table instead.
    pub async fn reconcile_message_counts(
        &self,
        rows: &[(String, String, i64)],
        before: &BTreeMap<(String, String), i64>,
    ) -> Vec<(String, String, i64)> {
        self.reconcile_chunk(rows, before).await
    }

    /// Stored count per key before a flush chunk runs.
    ///
    /// One statement per guild in the chunk: one round-trip per key made a
    /// 300-user flush pay 300 sequential reads before writing anything and
    /// routinely blew the call budget. The interpolated text is only the
    /// placeholder list, so nothing is injectable.
    async fn read_counts(
        &self,
        chunk: &[(String, String, i64)],
    ) -> BTreeMap<(String, String), i64> {
        let mut out: BTreeMap<(String, String), i64> = BTreeMap::new();
        let mut by_guild: Vec<(String, Vec<String>)> = Vec::new();
        for (guild_id, user_id, _) in chunk {
            match by_guild.iter_mut().find(|(guild, _)| guild == guild_id) {
                Some((_, user_ids)) => user_ids.push(user_id.clone()),
                None => by_guild.push((guild_id.clone(), vec![user_id.clone()])),
            }
        }
        for (guild_id, user_ids) in &by_guild {
            let marks = vec!["?"; user_ids.len()].join(", ");
            let mut params: Vec<SqlValue> = Vec::with_capacity(user_ids.len() + 1);
            params.push(guild_id.as_str().into());
            for user_id in user_ids {
                params.push(user_id.as_str().into());
            }
            let statement = format!(
                "SELECT user_id, count AS n FROM simple_message_counts WHERE guild_id = ? AND user_id IN ({marks})"
            );
            let rows = match self.db.query(&statement, &params).await {
                Ok(rows) => rows,
                Err(_) => {
                    tracing::warn!(
                        keys = user_ids.len(),
                        guild = %guild_id,
                        "flush snapshot read failed"
                    );
                    continue;
                }
            };
            for row in &rows {
                let user_id = row.text("user_id").unwrap_or_else(|| "None".to_string());
                let count = row.int("n").unwrap_or(0);
                out.insert((guild_id.clone(), user_id), count);
            }
        }
        // A key with no row simply has count 0; recording that lets a later
        // reconcile tell "read it, it is empty" from "could not read it".
        for (guild_id, user_id, _) in chunk {
            out.entry((guild_id.clone(), user_id.clone())).or_insert(0);
        }
        out
    }

    /// Shortfall per key after an ambiguous chunk failure.
    ///
    /// `before` holds each key's count read just before the chunk ran. Whatever
    /// is already reflected in the table is not requeued, so retrying the flush
    /// cannot double-add the failed chunk. Keys that cannot be re-read are
    /// requeued whole: over-counting beats silently losing messages.
    async fn reconcile_chunk(
        &self,
        chunk: &[(String, String, i64)],
        before: &BTreeMap<(String, String), i64>,
    ) -> Vec<(String, String, i64)> {
        // One batched read for the whole chunk, not one round-trip per key.
        let stored_now = self.read_counts(chunk).await;
        let mut shortfall: Vec<(String, String, i64)> = Vec::new();
        for (guild_id, user_id, delta) in chunk {
            let key = (guild_id.clone(), user_id.clone());
            let Some(base) = before.get(&key) else {
                // No snapshot for this key (the pre-read failed): the stored
                // value cannot be compared, so requeue the whole delta rather
                // than risk silently losing messages.
                shortfall.push((guild_id.clone(), user_id.clone(), *delta));
                continue;
            };
            let Some(stored) = stored_now.get(&key) else {
                shortfall.push((guild_id.clone(), user_id.clone(), *delta));
                continue;
            };
            let missing = base + delta - stored;
            if missing > 0 {
                shortfall.push((guild_id.clone(), user_id.clone(), missing));
            }
        }
        shortfall
    }

    // -- join ------------------------------------------------------------

    #[allow(clippy::too_many_arguments)]
    pub async fn join(
        &self,
        gw: &Giveaway,
        user_id: &str,
        username: &str,
        member_roles: &[String],
        account_created_ts: Option<f64>,
        pending_messages: i64,
        timed_out: bool,
        snapshot: Option<(bool, i64, i64, bool)>,
        fetch_count: bool,
    ) -> Result<i64> {
        self.check_eligible(
            gw,
            member_roles,
            account_created_ts,
            user_id,
            pending_messages,
            timed_out,
            snapshot,
        )
        .await?;
        // The giveaway, its deadline, the blacklist, the timeout-ban row and
        // the ban-hit row are re-checked inside the statement itself.
        // check_eligible above reads rows fetched moments earlier (the snapshot
        // is older still: read in parallel with get, before this thread even
        // started), so an end() / blacklist_add / penalty landing in between
        // would otherwise let the entry through. The statement makes each of
        // those impossible without a second round-trip. Roles / account age /
        // message counts need no guard here: roles and age come from the live
        // Discord member object (not the database), and message counts only
        // move fail-closed (a stale low count refuses; a wipe is skipped while
        // an active min-messages giveaway exists -- see end()).
        // A currently-timed-out member (timed_out=true) never reaches this
        // statement: check_eligible above always refuses them first. The bans
        // guard below therefore covers the penalty-row race only.
        let now = now_ms();
        let username: String = username.chars().take(64).collect();
        let inserted = match self
            .db
            .execute(
                "INSERT INTO simple_entries (giveaway_id, user_id, username, entered_at) SELECT ?, ?, ?, ? WHERE EXISTS (   SELECT 1 FROM simple_giveaways   WHERE id = ? AND status = 'active' AND ends_at > ? ) AND NOT EXISTS (   SELECT 1 FROM simple_blacklist WHERE guild_id = ? AND user_id = ?   AND (expires_at IS NULL OR expires_at > ?) ) AND NOT EXISTS (   SELECT 1 FROM simple_giveaway_bans   WHERE guild_id = ? AND user_id = ? AND giveaways_remaining > 0 ) AND NOT EXISTS (   SELECT 1 FROM simple_giveaway_ban_hits   WHERE guild_id = ? AND user_id = ? AND giveaway_id = ? )",
                &[
                    gw.id.as_str().into(),
                    user_id.into(),
                    username.as_str().into(),
                    now.into(),
                    gw.id.as_str().into(),
                    now.into(),
                    gw.guild_id.as_str().into(),
                    user_id.into(),
                    now.into(),
                    gw.guild_id.as_str().into(),
                    user_id.into(),
                    gw.guild_id.as_str().into(),
                    user_id.into(),
                    gw.id.as_str().into(),
                ],
            )
            .await
        {
            Ok(changed_rows) => changed_rows,
            Err(exc) => {
                let message = exc.to_string().to_uppercase();
                // Only the duplicate-entry constraint means "already entered".
                // Any other constraint failure (NOT NULL, CHECK, ...) is a real
                // bug and must surface instead of being reported as a harmless
                // duplicate.
                if message.contains("UNIQUE") || message.contains("PRIMARY") {
                    return Err(ServiceError::new("You are already entered.").into());
                }
                // Ambiguous mid-write loss: the server may have applied the
                // INSERT before the response was lost. Disambiguate by entry
                // age without weakening duplicate protection: a row written
                // within seconds is this attempt (idempotent success); an older
                // row is a duplicate that raced the network failure ("already
                // entered"); absent -> surface so the caller can retry.
                if is_ambiguous_loss(&exc.to_string().to_lowercase()) {
                    if let Ok(Some(row)) = self
                        .db
                        .query_one(
                            "SELECT entered_at FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
                            &[gw.id.as_str().into(), user_id.into()],
                        )
                        .await
                    {
                        if let Some(entered_at) = row.int("entered_at") {
                            let age = now_ms() - entered_at;
                            if age < 15000 {
                                return if fetch_count {
                                    self.entry_count(&gw.id, false).await
                                } else {
                                    Ok(-1)
                                };
                            }
                            return Err(ServiceError::new("You are already entered.").into());
                        }
                    }
                }
                return Err(exc);
            }
        };
        // Some drivers report -1 when the affected count is unknown: treat
        // any non-positive value as "guard refused" so a blocked join can
        // never read as success. Guards are diagnosed first (live, ended ->
        // blacklist -> timeout); only when every guard passes does a present
        // entry mean "the INSERT landed but the count is unknown".
        if inserted == 0 {
            // The statement guard can refuse for several reasons; diagnose with
            // live reads (no snapshot) so a stale snapshot cannot mislabel the
            // refusal. Ended/deadline first (no side effects, and a timed-out
            // member must never earn a penalty from a stale button on an ended
            // giveaway), then blacklist, then the timeout-ban gate live (it
            // consumes the penalty and drops entries, exactly as the non-race
            // path would have).
            let fresh = match self.get(&gw.id).await {
                Ok(fresh) => fresh,
                Err(err) => {
                    if err.downcast_ref::<ServiceError>().is_some() {
                        return Err(ServiceError::new("This giveaway has ended.").into());
                    }
                    return Err(err);
                }
            };
            if !fresh.active() || fresh.ends_at <= now_ms() {
                return Err(ServiceError::new("This giveaway has ended.").into());
            }
            if self.is_blacklisted(&gw.guild_id, user_id, None).await? {
                return Err(ServiceError::new(
                    self.ban_message(&gw.guild_id, user_id, None).await?,
                )
                .into());
            }
            self.check_timeout_ban(&fresh, user_id, timed_out, None, None)
                .await?;
            // Every guard passes yet nothing inserted: either the block raced
            // away between the statement and this re-check (retry honestly) or
            // the driver hid a successful INSERT (confirm by entry).
            if let Ok(true) = self.has_entry(&gw.id, user_id).await {
                return if fetch_count {
                    self.entry_count(&gw.id, false).await
                } else {
                    Ok(-1)
                };
            }
            return Err(ServiceError::new("Could not enter you. Try again.").into());
        }
        // Counter bump on success (P2-2): same lane, 1 extra write, only when
        // the INSERT actually landed. COALESCE heals legacy NULL counters;
        // the status guard keeps a join that raced the draw from bumping a
        // frozen end/cancel count. Concurrent bumps never overwrite: +1 is
        // atomic in the statement itself, not read-modify-write.
        let _ = self
            .db
            .execute(
                "UPDATE simple_giveaways SET entrant_count = COALESCE(entrant_count, 0) + 1 WHERE id = ? AND status = 'active'",
                &[gw.id.as_str().into()],
            )
            .await;
        // The cached display count is now one behind. Both cached numbers are
        // cosmetic (nothing decides eligibility from them) but a fresh joiner
        // must not be shown a count that predates their own entry, so it is
        // dropped rather than left to expire.
        self.mark_stale(&[&format!("count:{}", gw.id)]);
        // fetch_count=false skips the COUNT so the ack never waits on it:
        // the post-ack refresh_snapshot() supplies the count for the embed.
        // -1 means "committed, count unknown"; callers must not print it.
        if !fetch_count {
            return Ok(-1);
        }
        self.entry_count(&gw.id, false).await
    }

    /// P0-2: commit + status/count in ONE db lane acquisition.
    ///
    /// This bundles the write (INSERT + counter bump) and the read (status +
    /// counter) in one lane, so a 50-burst holds 50 lanes instead of 150
    /// sequential acquires. Same statements, same order, same atomic guards --
    /// only the lane/thread overhead is removed. Refresh never raises (returns
    /// (None, 0) on dead read), so any exception here means the join failed.
    #[allow(clippy::too_many_arguments)]
    pub async fn join_and_refresh(
        &self,
        gw: &Giveaway,
        user_id: &str,
        username: &str,
        member_roles: &[String],
        account_created_ts: Option<f64>,
        pending_messages: i64,
        timed_out: bool,
        snapshot: Option<(bool, i64, i64, bool)>,
    ) -> Result<(Option<String>, i64)> {
        self.join(
            gw,
            user_id,
            username,
            member_roles,
            account_created_ts,
            pending_messages,
            timed_out,
            snapshot,
            false,
        )
        .await?;
        // This read has to happen AFTER the write, not be folded into it: a draw
        // or a cancel that lands between the INSERT and the reply must be seen,
        // so the handler can strip the role instead of restoring a live embed.
        // The counter bump cannot answer that -- its status is the pre-race one.
        Ok(self.refresh_snapshot(&gw.id).await)
    }

    /// Drop this member's entry. Only while the giveaway is running.
    ///
    /// Leaving an ended giveaway would rewrite who took part after the draw (and
    /// a cancelled one keeps its entries on purpose), so it reads as "not
    /// entered" there: the caller already reports false that way.
    pub async fn leave(&self, giveaway_id: &str, user_id: &str) -> Result<bool> {
        Ok(self.leave_with_count(giveaway_id, user_id).await?.0)
    }

    /// leave(), plus the entry count and status afterwards, in one round trip.
    ///
    /// The click path used to issue the DELETE, a counter UPDATE, then two more
    /// reads (the row and its status) purely to refresh the panel it was about
    /// to draw. The counter bump already produces the exact number, and the
    /// row's status is read by the same statement, so both are returned with
    /// the cursor instead of being selected back.
    pub async fn leave_with_count(
        &self,
        giveaway_id: &str,
        user_id: &str,
    ) -> Result<(bool, i64, String)> {
        let removed = self
            .db
            .execute(
                "DELETE FROM simple_entries WHERE giveaway_id = ? AND user_id = ? AND EXISTS ( SELECT 1 FROM simple_giveaways WHERE id = ? AND status = 'active')",
                &[giveaway_id.into(), user_id.into(), giveaway_id.into()],
            )
            .await?
            > 0;
        if !removed {
            return Ok((false, 0, String::new()));
        }
        // Keep the live counter exact: a leave must undo the join bump.
        // GREATEST-equivalent via CASE (portable sqlite/libsql) so a racing
        // double-leave can never drive it negative. The count is then read back
        // from the same row, which is also where the status comes from: one
        // more hop for one integer, but no dependence on driver RETURNING
        // support.
        let _ = self
            .db
            .execute(
                "UPDATE simple_giveaways SET entrant_count = CASE WHEN COALESCE(entrant_count, 0) > 0 THEN entrant_count - 1 ELSE 0 END WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await;
        let (count, status) = match self
            .db
            .query_one(
                "SELECT COALESCE(entrant_count, 0) AS n, status FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await
        {
            Ok(Some(row)) => (
                row.int("n").unwrap_or(0),
                row.text("status").unwrap_or_default(),
            ),
            _ => (0, String::new()),
        };
        self.mark_stale(&[&format!("count:{giveaway_id}")]);
        Ok((true, count.max(0), status))
    }
}

/// `sorted(drawn)`: a stable exclusion order for the draw helpers.
fn sorted_ids(ids: &HashSet<String>) -> Vec<String> {
    let mut sorted: Vec<String> = ids.iter().cloned().collect();
    sorted.sort();
    sorted
}

impl GiveawayService {
    // -- end / reroll / cancel ------------------------------------------

    /// Sample `n` entrants without replacement, excluding the given ids.
    ///
    /// The exclusion set is built once per draw: rebuilding it for every row
    /// made picking from a large giveaway quadratic for no reason.
    pub async fn pick(
        &self,
        giveaway_id: &str,
        n: i64,
        exclude: Option<&[String]>,
    ) -> Result<Vec<String>> {
        let skipped: HashSet<&str> = exclude.unwrap_or(&[]).iter().map(String::as_str).collect();
        let pool: Vec<String> = self
            .entries(giveaway_id, None)
            .await?
            .iter()
            .filter_map(|row| row.get("user_id"))
            .map(py_str)
            .filter(|user_id| !skipped.contains(user_id.as_str()))
            .collect();
        if pool.is_empty() || n < 1 {
            return Ok(Vec::new());
        }
        let take = (n as usize).min(pool.len());
        let mut rng = rand::thread_rng();
        Ok(pool.choose_multiple(&mut rng, take).cloned().collect())
    }

    /// Current winners plus the stored raw JSON for a CAS update.
    ///
    /// json.dumps has one canonical form here, but comparing the raw text
    /// instead of re-encoding avoids any mismatch tripping the
    /// compare-and-swap in reroll/replace/expire.
    async fn read_winners_raw(&self, giveaway_id: &str) -> Result<(Vec<String>, String)> {
        let row = self
            .db
            .query_one(
                "SELECT winners_json FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        let raw = row
            .and_then(|row| row.text("winners_json"))
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| "[]".to_string());
        let winners = match serde_json::from_str::<Value>(&raw) {
            Ok(Value::Array(items)) => items.iter().map(py_str).collect(),
            _ => Vec::new(),
        };
        Ok((winners, raw))
    }

    /// End a running giveaway and draw its winners.
    ///
    /// The draw happens first, then one UPDATE writes status, ended_at, the
    /// winners and the entrant count together, with "status = 'active'" in the
    /// WHERE clause as the claim: the auto-draw tick and /giveaway_end can fire
    /// in the same second, and only one of them may store (and announce) its
    /// draw. Whoever loses the claim is told the giveaway is already over.
    pub async fn end(
        &self,
        giveaway_id: &str,
        now: Option<i64>,
        require_due: bool,
        known: Option<&Giveaway>,
    ) -> Result<(Giveaway, Vec<String>)> {
        // A caller that already holds the row (the tick's due() snapshot, or
        // /giveaway_end's resolve) passes it in instead of being served a second
        // full SELECT. A stale copy cannot change the outcome: the guarded flip
        // below re-checks status and (for the tick) the deadline in the
        // statement itself, and the losing branch re-reads live to explain why.
        let gw = match known {
            Some(gw) => gw.clone(),
            None => self.get(giveaway_id).await?,
        };
        if !gw.active() {
            return Err(ServiceError::new("This giveaway has already ended.").into());
        }
        // One timestamp for the due check, the flip and the claim windows: the
        // repair sweep rebuilds deadlines as ended_at + timeout, so both must
        // share a base, and the tick test needs a deterministic clock.
        let ts = now.unwrap_or_else(now_ms);
        if require_due && gw.ends_at > ts {
            return Err(ServiceError::new("This giveaway is not due to end yet.").into());
        }
        let pool: Vec<String> = self
            .entries(&gw.id, None)
            .await?
            .iter()
            .filter_map(|row| row.get("user_id"))
            .map(py_str)
            .collect();
        let winners: Vec<String> = if pool.is_empty() {
            Vec::new()
        } else {
            let take = (gw.winner_count.max(0) as usize).min(pool.len());
            let mut rng = rand::thread_rng();
            pool.choose_multiple(&mut rng, take).cloned().collect()
        };
        let winners_json = json_array(&winners);
        let flipped = if require_due {
            self.db
                .execute(
                    "UPDATE simple_giveaways SET status = 'ended', ended_at = ?, winners_json = ?, entrant_count = ? WHERE id = ? AND status = 'active' AND ends_at <= ?",
                    &[
                        ts.into(),
                        winners_json.into(),
                        (pool.len() as i64).into(),
                        gw.id.as_str().into(),
                        ts.into(),
                    ],
                )
                .await?
        } else {
            self.db
                .execute(
                    "UPDATE simple_giveaways SET status = 'ended', ended_at = ?, winners_json = ?, entrant_count = ? WHERE id = ? AND status = 'active'",
                    &[
                        ts.into(),
                        winners_json.into(),
                        (pool.len() as i64).into(),
                        gw.id.as_str().into(),
                    ],
                )
                .await?
        };
        if !changed(flipped) {
            // The draw lost the claim. Only this branch needs to know *why*,
            // because it is about to tell the user, so the re-read lives here
            // instead of on the winning path where the row is already known.
            let live = match self.get(&gw.id).await {
                Ok(live) => live,
                Err(err) => {
                    if err.downcast_ref::<ServiceError>().is_some() {
                        return Err(ServiceError::new("This giveaway has already ended.").into());
                    }
                    return Err(err);
                }
            };
            if live.active() && require_due && live.ends_at > ts {
                return Err(ServiceError::new("This giveaway is not due to end yet.").into());
            }
            return Err(ServiceError::new("This giveaway has already ended.").into());
        }
        // Fresh grind for the next giveaway *in this server*: message counts go
        // back to zero here, so the next min-messages requirement measures
        // activity after this giveaway rather than lifetime activity. Scoped by
        // guild -- wiping the whole table would silently zero every other
        // server's counts -- and skipped while another running giveaway here
        // still has a min-messages requirement, which would otherwise be reset
        // under the members already grinding for it.
        // Post-flip failures must not strand the announcement: the draw is
        // stored, so wipe/claim errors are logged and healed (repair sweep
        // below + every claim read) while the caller still announces. Only the
        // guarded flip above may raise.
        if let Err(err) = self
            .db
            .execute(
                "DELETE FROM simple_message_counts WHERE guild_id = ? AND NOT EXISTS ( SELECT 1 FROM simple_giveaways WHERE guild_id = ? AND status = 'active' AND min_messages > 0)",
                &[gw.guild_id.as_str().into(), gw.guild_id.as_str().into()],
            )
            .await
        {
            tracing::warn!(error = %err, giveaway = %gw.id, "end cleanup wipe failed");
        }
        // The row is rebuilt from the guarded flip above rather than selected
        // back: status and winners are exactly what that statement wrote, and
        // nothing between here and the return can change them (start_claims
        // only touches simple_claims). The pool that was just drawn IS the final
        // entry count, and the flip above wrote it to the row.
        let mut ended = gw.clone();
        ended.status = "ended".to_string();
        ended.winners = winners.clone();
        ended.entrant_count = Some(pool.len() as i64);
        // Claim windows open here so the deadline survives restarts: rows are
        // durable, and the tick picks them up even if the process dies first.
        // Shared ts above keeps flip and inserts on one deadline.
        if let Err(err) = self.start_claims(&ended, &winners, Some(ts)).await {
            tracing::warn!(error = %err, giveaway = %gw.id, "end claim insert failed; repairing");
            let _ = self.repair_missing_claims(&gw.id, Some(ts)).await;
        }
        // The row just flipped to 'ended': the cached header, the cached list
        // and the cached count are all wrong the moment this commits.
        self.mark_stale(&[
            &format!("gw:{}", gw.id),
            &format!("guild:{}:active", gw.guild_id),
            "all:active",
            &format!("count:{}", gw.id),
        ]);
        Ok((ended, winners))
    }

    pub async fn reroll(
        &self,
        giveaway_id: &str,
        count: i64,
        replace_user_id: Option<&str>,
    ) -> Result<(Giveaway, Vec<String>)> {
        let gw = self.get(giveaway_id).await?;
        if gw.active() {
            return Err(ServiceError::new("End the giveaway before rerolling.").into());
        }
        if gw.status != "ended" {
            // A cancelled giveaway keeps its entries, so without this a reroll
            // would hand out prizes for a giveaway the server called off.
            return Err(ServiceError::new("A cancelled giveaway cannot be rerolled.").into());
        }
        if let Some(replace_user_id) = replace_user_id {
            return self.reroll_replace(&gw, replace_user_id).await;
        }
        // The whole list has to stay inside MAX_WINNERS. create() bounds the
        // first draw, but a reroll only ever adds, so repeated rerolls used to
        // grow the announcement past what Discord will accept -- and the post
        // then failed.
        let prev = gw.winners.clone();
        let room = MAX_WINNERS - prev.len() as i64;
        if room < 1 {
            return Err(ServiceError::new(format!(
                "This giveaway already names {} winners, the most one announcement can hold.",
                prev.len()
            ))
            .into());
        }
        let wanted = 1.max((if count != 0 { count } else { gw.winner_count }).min(room));
        // Claim-aware exclusion: winners_json holds every manual draw,
        // simple_claims holds every auto draw (expired/claimed/skipped). Union
        // both so a manual /reroll can never re-pick someone the claim timer
        // already drew, and a later expiry can never re-pick this reroll.
        let mut drawn: HashSet<String> = prev.iter().cloned().collect();
        for row in self
            .db
            .query(
                "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                &[gw.id.as_str().into()],
            )
            .await?
        {
            drawn.insert(row.text("user_id").unwrap_or_else(|| "None".to_string()));
        }
        let mut fresh = self.pick(&gw.id, wanted, Some(&sorted_ids(&drawn))).await?;
        if fresh.is_empty() {
            // Redrawing from everyone would announce earlier winners as new
            // ones (or nobody at all once the entries were wiped): say so.
            return Err(ServiceError::new("No other entrants left to reroll").into());
        }
        // CAS append: two rerolls/expiries racing both read the same
        // winners_json and the loser would overwrite the winner's draw.
        // Re-read + compare-and-swap so the loser rebases and retries.
        for _ in 0..5 {
            let (winners_now, raw_now) = self.read_winners_raw(&gw.id).await?;
            let room_now = MAX_WINNERS - winners_now.len() as i64;
            if room_now < 1 {
                return Err(ServiceError::new(format!(
                    "This giveaway already names {} winners, the most one announcement can hold.",
                    winners_now.len()
                ))
                .into());
            }
            // Fresh was picked against a possibly stale drawn set; drop any
            // that landed while we were picking so we never duplicate.
            let mut use_now: Vec<String> = fresh
                .iter()
                .filter(|winner| !winners_now.contains(winner))
                .cloned()
                .collect();
            if use_now.is_empty() {
                // Everything we drew is already named (a racing draw picked
                // the same entrant): re-pick the shortfall, still excluding
                // everyone ever drawn.
                let mut drawn_now: HashSet<String> = winners_now.iter().cloned().collect();
                for row in self
                    .db
                    .query(
                        "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                        &[gw.id.as_str().into()],
                    )
                    .await?
                {
                    drawn_now.insert(row.text("user_id").unwrap_or_else(|| "None".to_string()));
                }
                let need = 1.max((if count != 0 { count } else { gw.winner_count }).min(room_now));
                let repick = self
                    .pick(&gw.id, need, Some(&sorted_ids(&drawn_now)))
                    .await?;
                use_now = repick
                    .into_iter()
                    .filter(|winner| !winners_now.contains(winner))
                    .collect();
                if use_now.is_empty() {
                    return Err(ServiceError::new("No other entrants left to reroll").into());
                }
                fresh = use_now.clone();
            }
            let mut combined = winners_now.clone();
            combined.extend(use_now.iter().cloned());
            let written = self
                .db
                .execute(
                    "UPDATE simple_giveaways SET winners_json = ? WHERE id = ? AND winners_json = ?",
                    &[
                        json_array(&combined).into(),
                        gw.id.as_str().into(),
                        raw_now.as_str().into(),
                    ],
                )
                .await?;
            if changed(written) {
                // The CAS above stored the draw; a crash before the inserts
                // must not strand the fresh winners. start_claims is idempotent
                // (INSERT OR IGNORE), failures are logged and healed by the
                // repair sweep, and the stored draw is still returned for the
                // announce. The old winners' rows are left alone: a manual
                // reroll is additive, and each deadline still expires/skips
                // independently.
                let claims = async {
                    let gw_now = self.get(&gw.id).await?;
                    self.start_claims(&gw_now, &use_now, None).await
                }
                .await;
                if let Err(err) = claims {
                    tracing::warn!(error = %err, giveaway = %gw.id, "reroll claim insert failed; repairing");
                    let _ = self.repair_missing_claims(&gw.id, None).await;
                }
                self.mark_stale(&[&format!("gw:{}", gw.id)]);
                return Ok((self.get(&gw.id).await?, use_now));
            }
        }
        Err(ServiceError::new("Reroll collided with another draw. Try again.").into())
    }

    /// Swap one named winner for a fresh entrant. Returns (giveaway, [new]).
    ///
    /// Targeted /reroll: the admin picks which winner loses their slot (e.g.
    /// C never claimed while A and B did). The old winner is swapped out in
    /// place so the winners list never grows, and their pending claim (if any)
    /// is closed as `replaced` so the tick can never expire it into a duplicate
    /// slot. The replacement gets its own claim window. Never re-picks anyone
    /// ever drawn (winners_json + every simple_claims row, any status).
    async fn reroll_replace(
        &self,
        gw: &Giveaway,
        replace_user_id: &str,
    ) -> Result<(Giveaway, Vec<String>)> {
        let prev = gw.winners.clone();
        if !prev.iter().any(|winner| winner == replace_user_id) {
            return Err(ServiceError::new("That member is not a winner of this giveaway.").into());
        }
        // Settled claims cannot be replaced: a claimed/skipped prize is done,
        // and an expired/replaced slot already drew its replacement. Only a
        // pending (unclaimed) winner or a claims-disabled row may be swapped.
        let clash = self
            .db
            .query_one(
                "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ? ORDER BY round DESC LIMIT 1",
                &[gw.id.as_str().into(), replace_user_id.into()],
            )
            .await?;
        if let Some(clash) = clash {
            let settled = clash.text("status").unwrap_or_default();
            if settled == CLAIM_CLAIMED {
                return Err(ServiceError::new("That prize was already claimed.").into());
            }
            if settled == CLAIM_SKIPPED {
                return Err(ServiceError::new("That claim was already verified by staff.").into());
            }
            if settled == CLAIM_EXPIRED || settled == CLAIM_REPLACED {
                return Err(ServiceError::new("That winner was already replaced.").into());
            }
            if settled != CLAIM_PENDING {
                return Err(ServiceError::new("That claim window has already closed.").into());
            }
        }
        let mut drawn: HashSet<String> = prev.iter().cloned().collect();
        for row in self
            .db
            .query(
                "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                &[gw.id.as_str().into()],
            )
            .await?
        {
            drawn.insert(row.text("user_id").unwrap_or_else(|| "None".to_string()));
        }
        let fresh = self.pick(&gw.id, 1, Some(&sorted_ids(&drawn))).await?;
        let Some(mut new_user) = fresh.into_iter().next() else {
            return Err(ServiceError::new("No other entrants left to reroll").into());
        };
        // CAS swap with retry: a racing reroll/expire may rewrite winners_json
        // between our read and write; the loser rebases instead of overwriting.
        for _ in 0..5 {
            let (winners_now, raw_now) = self.read_winners_raw(&gw.id).await?;
            if !winners_now.iter().any(|winner| winner == replace_user_id) {
                return Err(
                    ServiceError::new("That member is not a winner of this giveaway.").into(),
                );
            }
            if winners_now.contains(&new_user) {
                // Racing draw picked the same entrant: re-pick, still excluding
                // everyone ever drawn.
                let mut drawn_now: HashSet<String> = winners_now.iter().cloned().collect();
                for row in self
                    .db
                    .query(
                        "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                        &[gw.id.as_str().into()],
                    )
                    .await?
                {
                    drawn_now.insert(row.text("user_id").unwrap_or_else(|| "None".to_string()));
                }
                let repick = self.pick(&gw.id, 1, Some(&sorted_ids(&drawn_now))).await?;
                let Some(repick) = repick.into_iter().next() else {
                    return Err(ServiceError::new("No other entrants left to reroll").into());
                };
                new_user = repick;
            }
            // Swap in place: first matching slot only, order preserved.
            let swapped_list: Vec<String> = winners_now
                .iter()
                .map(|winner| {
                    if winner == replace_user_id {
                        new_user.clone()
                    } else {
                        winner.clone()
                    }
                })
                .collect();
            // Defensive: if the winners list somehow held the old id twice,
            // only the first slot swaps and the rest stay (never duplicate).
            let mut seen_new = false;
            let mut fixed: Vec<String> = Vec::with_capacity(swapped_list.len());
            for winner in swapped_list {
                if winner == new_user {
                    if seen_new {
                        fixed.push(replace_user_id.to_string());
                        continue;
                    }
                    seen_new = true;
                }
                fixed.push(winner);
            }
            let written = self
                .db
                .execute(
                    "UPDATE simple_giveaways SET winners_json = ? WHERE id = ? AND winners_json = ?",
                    &[
                        json_array(&fixed).into(),
                        gw.id.as_str().into(),
                        raw_now.as_str().into(),
                    ],
                )
                .await?;
            if !changed(written) {
                continue;
            }
            // Close the old slot's pending claim so a later tick cannot expire
            // it into a second replacement for the same slot. Any status counts
            // as "ever drawn", so the exclusion holds even after the swap.
            // Settle/start failures after the CAS are logged and healed (the
            // repair sweep settles the old slot and opens the fresh window)
            // while the stored swap is still returned for the announce.
            let settle = async {
                self.db
                    .execute(
                        "UPDATE simple_claims SET status = ? WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                        &[
                            CLAIM_REPLACED.into(),
                            gw.id.as_str().into(),
                            replace_user_id.into(),
                        ],
                    )
                    .await?;
                let row = self
                    .db
                    .query_one(
                        "SELECT user_id FROM simple_claims WHERE giveaway_id = ? AND user_id = ? LIMIT 1",
                        &[gw.id.as_str().into(), replace_user_id.into()],
                    )
                    .await?;
                if row.is_none() {
                    // No claim row at all (e.g. claims disabled): leave a marker
                    // so this user stays excluded from every future draw.
                    self.db
                        .execute(
                            "INSERT OR IGNORE INTO simple_claims (giveaway_id, user_id, round, status, deadline_ms, created_at) VALUES (?, ?, 0, ?, 0, ?)",
                            &[
                                gw.id.as_str().into(),
                                replace_user_id.into(),
                                CLAIM_REPLACED.into(),
                                now_ms().into(),
                            ],
                        )
                        .await?;
                }
                // Fresh winner's own claim window. No-op when claims are disabled.
                let gw_now = self.get(&gw.id).await?;
                self.start_claims(&gw_now, std::slice::from_ref(&new_user), None)
                    .await
            }
            .await;
            if let Err(err) = settle {
                tracing::warn!(error = %err, giveaway = %gw.id, "reroll-replace settle failed; repairing");
                let _ = self.repair_missing_claims(&gw.id, None).await;
            }
            return Ok((self.get(&gw.id).await?, vec![new_user]));
        }
        Err(ServiceError::new("Reroll collided with another draw. Try again.").into())
    }
}

impl GiveawayService {
    // -- winner claims ---------------------------------------------------

    /// Open a claim window for freshly drawn winners. Idempotent.
    ///
    /// Called once per draw (end, and each claim-timeout replacement). Rows are
    /// INSERT OR IGNORE so a retried announce never doubles a deadline. No-op
    /// when the giveaway has no claim window configured.
    pub async fn start_claims(
        &self,
        gw: &Giveaway,
        winners: &[String],
        now: Option<i64>,
    ) -> Result<()> {
        if !gw.claim_enabled() || winners.is_empty() {
            return Ok(());
        }
        let ts = now.unwrap_or_else(now_ms);
        let deadline = ts + gw.claim_timeout_seconds * 1000;
        for user_id in winners {
            self.db
                .execute(
                    "INSERT OR IGNORE INTO simple_claims (giveaway_id, user_id, round, status, deadline_ms, created_at) VALUES (?, ?, 0, 'pending', ?, ?)",
                    &[
                        gw.id.as_str().into(),
                        user_id.as_str().into(),
                        deadline.into(),
                        ts.into(),
                    ],
                )
                .await?;
        }
        Ok(())
    }

    /// Recreate claim rows lost between a draw and its inserts. Idempotent.
    ///
    /// end()/reroll()/expire_claim() store winners first and open the claim
    /// windows right after as separate statements (one timestamp shared by the
    /// flip and the inserts, no migration, no cross-driver transaction), so a
    /// crash in between strands winners with no rows. Every insert here is
    /// INSERT OR IGNORE: winners holding any claim row (any status) are left
    /// alone and only winners with no row at all gain a fresh pending window.
    /// Pending rows for users no longer named (a targeted reroll that died
    /// before settling the old slot) flip to replaced so the tick can never
    /// expire them into a duplicate slot. Returns rows written.
    pub async fn repair_missing_claims(&self, giveaway_id: &str, now: Option<i64>) -> Result<i64> {
        let gw = match self.get(giveaway_id).await {
            Ok(gw) => gw,
            Err(err) => {
                if err.downcast_ref::<ServiceError>().is_some() {
                    return Ok(0);
                }
                return Err(err);
            }
        };
        if !gw.claim_enabled() || gw.winners.is_empty() {
            return Ok(0);
        }
        let rows = match self
            .db
            .query(
                "SELECT user_id, status, deadline_ms FROM simple_claims WHERE giveaway_id = ?",
                &[gw.id.as_str().into()],
            )
            .await
        {
            Ok(rows) => rows,
            Err(_) => return Ok(0),
        };
        let mut have: HashSet<String> = HashSet::new();
        let mut latest: i64 = 0;
        for row in &rows {
            let uid = row.text("user_id").unwrap_or_else(|| "None".to_string());
            have.insert(uid);
            // A deadline that cannot be read is skipped, exactly like the
            // Python row-level warning: the user still counts as "has a row".
            latest = latest.max(row.int_or("deadline_ms", 0));
        }
        let ts = now.unwrap_or_else(now_ms);
        let deadline = if latest > 0 {
            // Never hand a repaired winner an already-past deadline: a later
            // draw (reroll/expire) must start its own window at repair time,
            // while a partially inserted initial batch simply gains a few ms.
            latest.max(ts + gw.claim_timeout_seconds * 1000)
        } else {
            let base = match self
                .db
                .query_one(
                    "SELECT ended_at FROM simple_giveaways WHERE id = ?",
                    &[gw.id.as_str().into()],
                )
                .await
            {
                Ok(Some(row)) => row
                    .int("ended_at")
                    .filter(|value| *value != 0)
                    .unwrap_or(ts),
                _ => ts,
            };
            base + gw.claim_timeout_seconds * 1000
        };
        let winners = dedup_preserving(gw.winners.iter().map(|winner| winner.to_string()));
        let mut written: i64 = 0;
        for uid in &winners {
            if have.contains(uid) {
                continue;
            }
            let insert = self
                .db
                .execute(
                    "INSERT OR IGNORE INTO simple_claims (giveaway_id, user_id, round, status, deadline_ms, created_at) VALUES (?, ?, 0, 'pending', ?, ?)",
                    &[
                        gw.id.as_str().into(),
                        uid.as_str().into(),
                        deadline.into(),
                        ts.into(),
                    ],
                )
                .await;
            match insert {
                Ok(_) => written += 1,
                Err(err) => {
                    tracing::warn!(
                        error = %err,
                        giveaway = %gw.id,
                        user = %uid,
                        "claim repair insert failed"
                    );
                    continue;
                }
            }
        }
        if !winners.is_empty() {
            let marks = vec!["?"; winners.len()].join(", ");
            let sql = format!(
                "UPDATE simple_claims SET status = ? WHERE giveaway_id = ? AND status = 'pending' AND user_id NOT IN ({marks})"
            );
            let mut params: Vec<SqlValue> = Vec::with_capacity(winners.len() + 2);
            params.push(CLAIM_REPLACED.into());
            params.push(gw.id.as_str().into());
            params.extend(winners.iter().map(|uid| uid.as_str().into()));
            match self.db.execute(&sql, &params).await {
                Ok(count) => written += count as i64,
                Err(err) => {
                    tracing::warn!(error = %err, giveaway = %gw.id, "claim repair settle failed");
                }
            }
        }
        Ok(written)
    }

    /// Heal every stranded claim window. due_claims() runs this per tick.
    ///
    /// A giveaway stranded with zero pending rows never appears in due_claims,
    /// so per-giveaway repair alone would never visit it: this bounded sweep
    /// (oldest ended first) recreates those windows, and the tick then picks
    /// them up like any restart recovery. Idempotent; failures heal on a later
    /// tick. Returns rows written.
    pub async fn repair_all_missing_claims(&self, now: Option<i64>, limit: i64) -> Result<i64> {
        let ts = now.unwrap_or_else(now_ms);
        let rows = match self
            .db
            .query(
                // Plain ended_at, not COALESCE(ended_at, 0): the COALESCE
                // wrapped the column in an expression, so no index could serve
                // the order and every tick sorted the whole ended set. Rows
                // from before the column existed have claim_timeout_seconds =
                // 0 and are excluded by the line above anyway.
                "SELECT id FROM simple_giveaways WHERE status = 'ended' AND claim_timeout_seconds > 0 AND winners_json != '[]' ORDER BY ended_at ASC LIMIT ?",
                &[limit.clamp(1, 100).into()],
            )
            .await
        {
            Ok(rows) => rows,
            Err(_) => return Ok(0),
        };
        let mut total: i64 = 0;
        for row in &rows {
            let gid = row.text("id").unwrap_or_else(|| "None".to_string());
            match self.repair_missing_claims(&gid, Some(ts)).await {
                Ok(count) => total += count,
                Err(err) => {
                    tracing::warn!(error = %err, "claim repair sweep failed for a giveaway");
                }
            }
        }
        Ok(total)
    }

    /// Pending claim rows for a giveaway, oldest deadline first.
    ///
    /// Heals stranded windows first, so a crash between a draw and its inserts
    /// never reads as "no claims".
    pub async fn pending_claims(&self, giveaway_id: &str) -> Result<Vec<Value>> {
        let _ = self.repair_missing_claims(giveaway_id, None).await;
        let rows = self
            .db
            .query(
                "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at, skipped_by, skipped_at, created_at FROM simple_claims WHERE giveaway_id = ? AND status = 'pending' ORDER BY deadline_ms ASC",
                &[giveaway_id.into()],
            )
            .await?;
        Ok(rows.iter().map(row_to_json).collect())
    }

    /// Every claim row for a giveaway (for the dashboard/admin view).
    pub async fn claim_status(&self, giveaway_id: &str) -> Result<Vec<Value>> {
        let rows = self
            .db
            .query(
                "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at, skipped_by, skipped_at, created_at FROM simple_claims WHERE giveaway_id = ? ORDER BY created_at ASC",
                &[giveaway_id.into()],
            )
            .await?;
        Ok(rows.iter().map(row_to_json).collect())
    }

    /// Pending claims past their deadline. Tick feeds on this.
    ///
    /// Heals stranded windows first (bounded sweep, oldest ended first), so a
    /// crash between a draw and its inserts never hides as "no due claims".
    pub async fn due_claims(&self, now: Option<i64>, limit: i64) -> Result<Vec<Value>> {
        let ts = now.unwrap_or_else(now_ms);
        let _ = self.repair_all_missing_claims(Some(ts), 25).await;
        let rows = self
            .db
            .query(
                "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at, skipped_by, skipped_at, created_at FROM simple_claims WHERE status = 'pending' AND deadline_ms <= ? ORDER BY deadline_ms ASC LIMIT ?",
                &[ts.into(), limit.clamp(1, 100).into()],
            )
            .await?;
        Ok(rows.iter().map(row_to_json).collect())
    }

    /// Claim a prize as its drawn winner. Returns the claim row.
    ///
    /// Guarded UPDATE: only a pending row for this exact (giveaway, winner)
    /// flips, so double-clicks and two processes racing both collapse into one
    /// success and one "already claimed" refusal.
    pub async fn claim(&self, giveaway_id: &str, user_id: &str) -> Result<Value> {
        let mut flipped = self
            .db
            .execute(
                "UPDATE simple_claims SET status = 'claimed', claimed_at = ? WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                &[now_ms().into(), giveaway_id.into(), user_id.into()],
            )
            .await?;
        if !changed(flipped) {
            // A crash between a draw and its inserts strands winners with no
            // row: heal once, then retry so the rightful winner still claims.
            if self.repair_missing_claims(giveaway_id, None).await.is_ok() {
                let retry = self
                    .db
                    .execute(
                        "UPDATE simple_claims SET status = 'claimed', claimed_at = ? WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                        &[now_ms().into(), giveaway_id.into(), user_id.into()],
                    )
                    .await;
                if let Ok(retry) = retry {
                    if changed(retry) {
                        flipped = retry;
                    }
                }
            }
        }
        if !changed(flipped) {
            let row = self
                .db
                .query_one(
                    "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ? ORDER BY round DESC LIMIT 1",
                    &[giveaway_id.into(), user_id.into()],
                )
                .await?;
            let Some(row) = row else {
                return Err(
                    ServiceError::new("Only the drawn winner can claim this prize.").into(),
                );
            };
            let status = row.text("status").unwrap_or_default();
            if status == CLAIM_CLAIMED {
                return Err(ServiceError::new("This prize was already claimed.").into());
            }
            if status == CLAIM_SKIPPED {
                return Err(ServiceError::new("This prize was already verified by staff.").into());
            }
            if status == CLAIM_REPLACED {
                return Err(ServiceError::new("This winner was replaced by a reroll.").into());
            }
            return Err(ServiceError::new("This claim window has closed.").into());
        }
        let row = self
            .db
            .query_one(
                "SELECT * FROM simple_claims WHERE giveaway_id = ? AND user_id = ? AND status = 'claimed' ORDER BY round DESC LIMIT 1",
                &[giveaway_id.into(), user_id.into()],
            )
            .await?;
        Ok(row.as_ref().map(row_to_json).unwrap_or_else(|| json!({})))
    }

    /// Mark a pending claim manually verified (ticket path). No replacement.
    ///
    /// Guarded like claim(): only a pending row flips, so a second /skipclaim
    /// (or a claim racing it) gets a clear refusal instead of a double write.
    pub async fn skip_claim(
        &self,
        giveaway_id: &str,
        user_id: &str,
        staff_id: &str,
    ) -> Result<Value> {
        if staff_id.is_empty() {
            return Err(ServiceError::new("A staff member must verify the claim.").into());
        }
        let mut flipped = self
            .db
            .execute(
                "UPDATE simple_claims SET status = 'skipped', skipped_by = ?, skipped_at = ? WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                &[
                    staff_id.into(),
                    now_ms().into(),
                    giveaway_id.into(),
                    user_id.into(),
                ],
            )
            .await?;
        if !changed(flipped) {
            // Same stranded-window heal as claim(): a missing row may just be a
            // draw whose inserts never landed.
            if self.repair_missing_claims(giveaway_id, None).await.is_ok() {
                let retry = self
                    .db
                    .execute(
                        "UPDATE simple_claims SET status = 'skipped', skipped_by = ?, skipped_at = ? WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
                        &[
                            staff_id.into(),
                            now_ms().into(),
                            giveaway_id.into(),
                            user_id.into(),
                        ],
                    )
                    .await;
                if let Ok(retry) = retry {
                    if changed(retry) {
                        flipped = retry;
                    }
                }
            }
        }
        if !changed(flipped) {
            let row = self
                .db
                .query_one(
                    "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ? ORDER BY round DESC LIMIT 1",
                    &[giveaway_id.into(), user_id.into()],
                )
                .await?;
            let Some(row) = row else {
                return Err(ServiceError::new(
                    "No pending claim for that member in this giveaway.",
                )
                .into());
            };
            let status = row.text("status").unwrap_or_default();
            if status == CLAIM_CLAIMED {
                return Err(ServiceError::new("That prize was already claimed.").into());
            }
            if status == CLAIM_SKIPPED {
                return Err(ServiceError::new("That claim was already verified by staff.").into());
            }
            if status == CLAIM_REPLACED {
                return Err(ServiceError::new("That winner was replaced by a reroll.").into());
            }
            return Err(ServiceError::new("That claim window has already closed.").into());
        }
        let row = self
            .db
            .query_one(
                "SELECT * FROM simple_claims WHERE giveaway_id = ? AND user_id = ? AND status = 'skipped' ORDER BY round DESC LIMIT 1",
                &[giveaway_id.into(), user_id.into()],
            )
            .await?;
        Ok(row.as_ref().map(row_to_json).unwrap_or_else(|| json!({})))
    }

    /// Expire one pending claim and draw its replacement. Atomic-ish.
    ///
    /// The guarded UPDATE is the concurrency gate: whoever flips pending ->
    /// expired owns the replacement draw, and a racing tick/claim/skip sees a
    /// non-pending row and stops. The replacement reuses pick() with every
    /// ever-drawn user excluded, so nobody is ever selected twice. Returns the
    /// giveaway and the (possibly empty) replacement list.
    pub async fn expire_claim(
        &self,
        giveaway_id: &str,
        user_id: &str,
        now: Option<i64>,
    ) -> Result<(Giveaway, Vec<String>)> {
        let gw = self.get(giveaway_id).await?;
        if gw.active() {
            return Err(ServiceError::new("This giveaway is still running.").into());
        }
        let ts = now.unwrap_or_else(now_ms);
        let flipped = self
            .db
            .execute(
                "UPDATE simple_claims SET status = 'expired' WHERE giveaway_id = ? AND user_id = ? AND status = 'pending' AND deadline_ms <= ?",
                &[giveaway_id.into(), user_id.into(), ts.into()],
            )
            .await?;
        if !changed(flipped) {
            return Err(ServiceError::new("That claim is no longer pending.").into());
        }
        let mut drawn: HashSet<String> = HashSet::new();
        for row in self
            .db
            .query(
                "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                &[giveaway_id.into()],
            )
            .await?
        {
            drawn.insert(row.text("user_id").unwrap_or_else(|| "None".to_string()));
        }
        let mut fresh = self.pick(&gw.id, 1, Some(&sorted_ids(&drawn))).await?;
        if fresh.is_empty() {
            return Ok((self.get(&gw.id).await?, Vec::new()));
        }
        // CAS append with retry: a racing additive reroll may rewrite
        // winners_json between our pick and write; the loser rebases instead of
        // overwriting the other draw.
        for _ in 0..5 {
            let (winners_now, raw_now) = self.read_winners_raw(&gw.id).await?;
            let use_now: Vec<String> = fresh
                .iter()
                .filter(|winner| !winners_now.contains(winner))
                .cloned()
                .collect();
            if use_now.is_empty() {
                // Racing draw took the same entrant: pick another, still
                // excluding everyone ever drawn.
                let mut redrawn: HashSet<String> = winners_now.iter().cloned().collect();
                for row in self
                    .db
                    .query(
                        "SELECT user_id FROM simple_claims WHERE giveaway_id = ?",
                        &[giveaway_id.into()],
                    )
                    .await?
                {
                    redrawn.insert(row.text("user_id").unwrap_or_else(|| "None".to_string()));
                }
                let repick = self.pick(&gw.id, 1, Some(&sorted_ids(&redrawn))).await?;
                if repick.is_empty() {
                    return Ok((self.get(&gw.id).await?, Vec::new()));
                }
                fresh = repick;
                continue;
            }
            let mut combined = winners_now.clone();
            combined.extend(use_now.iter().cloned());
            let written = self
                .db
                .execute(
                    "UPDATE simple_giveaways SET winners_json = ? WHERE id = ? AND winners_json = ?",
                    &[
                        json_array(&combined).into(),
                        gw.id.as_str().into(),
                        raw_now.as_str().into(),
                    ],
                )
                .await?;
            if !changed(written) {
                continue;
            }
            // Stored above; a crash before the inserts must not strand the
            // replacement. Idempotent inserts plus the repair sweep heal it,
            // and the stored draw is still returned for the announce.
            let claims = async {
                let gw_now = self.get(&gw.id).await?;
                self.start_claims(&gw_now, &use_now, Some(ts)).await
            }
            .await;
            if let Err(err) = claims {
                tracing::warn!(error = %err, giveaway = %gw.id, "expire claim insert failed; repairing");
                let _ = self.repair_missing_claims(&gw.id, Some(ts)).await;
            }
            return Ok((self.get(&gw.id).await?, use_now));
        }
        Err(ServiceError::new("Reroll collided with another draw. Try again.").into())
    }
}

impl GiveawayService {
    // -- lifecycle -------------------------------------------------------

    /// Drop a giveaway that never reached Discord.
    ///
    /// Used when the announcement message could not be posted: the row would
    /// otherwise stay 'active' forever, auto-end on schedule, and try to
    /// announce winners into a channel that already refused the bot.
    pub async fn discard(&self, giveaway_id: &str) -> Result<()> {
        self.db
            .execute(
                "DELETE FROM simple_claims WHERE giveaway_id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        self.db
            .execute(
                "DELETE FROM simple_entries WHERE giveaway_id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        self.db
            .execute(
                "DELETE FROM simple_giveaways WHERE id = ?",
                &[giveaway_id.into()],
            )
            .await?;
        // The row is gone: a cached active list still holding it would serve a
        // giveaway that no longer exists. The guild-scoped list key is not
        // marked because this path has no guild id to build it from; it ages
        // out on its own short TTL.
        self.mark_stale(&["all:active", &format!("count:{giveaway_id}")]);
        Ok(())
    }

    pub async fn cancel(&self, giveaway_id: &str) -> Result<Giveaway> {
        let gw = self.get(giveaway_id).await?;
        if !gw.active() {
            return Err(ServiceError::new("Only an active giveaway can be cancelled.").into());
        }
        // Guarded like end(): an end() or a second cancel landing between the
        // read above and this write must not be overwritten.
        let flipped = self
            .db
            .execute(
                "UPDATE simple_giveaways SET status = 'cancelled', ended_at = ?, entrant_count = (SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?) WHERE id = ? AND status = 'active'",
                &[
                    now_ms().into(),
                    gw.id.as_str().into(),
                    gw.id.as_str().into(),
                ],
            )
            .await?;
        if !changed(flipped) {
            return Err(ServiceError::new("Only an active giveaway can be cancelled.").into());
        }
        self.mark_stale(&[
            &format!("gw:{}", gw.id),
            &format!("guild:{}:active", gw.guild_id),
            "all:active",
            &format!("count:{}", gw.id),
        ]);
        self.get(&gw.id).await
    }

    pub async fn set_message(&self, giveaway_id: &str, message_id: &str) -> Result<()> {
        self.db
            .execute(
                "UPDATE simple_giveaways SET message_id = ? WHERE id = ?",
                &[message_id.into(), giveaway_id.into()],
            )
            .await?;
        Ok(())
    }

    /// Push the deadline back. Only on a running giveaway.
    pub async fn extend(&self, giveaway_id: &str, extra_seconds: i64) -> Result<Giveaway> {
        let gw = self.get(giveaway_id).await?;
        if !gw.active() {
            return Err(ServiceError::new("Only a running giveaway can be extended.").into());
        }
        if !(60..=60 * 86400).contains(&extra_seconds) {
            return Err(ServiceError::new("Extend by 1 minute to 60 days at a time.").into());
        }
        let written = self
            .db
            .execute(
                "UPDATE simple_giveaways SET ends_at = ends_at + ? WHERE id = ? AND status = 'active'",
                &[(extra_seconds * 1000).into(), gw.id.as_str().into()],
            )
            .await?;
        if !changed(written) {
            return Err(ServiceError::new("Only a running giveaway can be extended.").into());
        }
        self.mark_stale(&[
            &format!("gw:{}", gw.id),
            &format!("guild:{}:active", gw.guild_id),
            "all:active",
        ]);
        self.get(&gw.id).await
    }

    pub async fn set_entrants_role(&self, giveaway_id: &str, role_id: Option<&str>) -> Result<()> {
        self.db
            .execute(
                "UPDATE simple_giveaways SET entrants_role_id = ? WHERE id = ?",
                &[role_id.into(), giveaway_id.into()],
            )
            .await?;
        Ok(())
    }

    /// Finished giveaways whose entrants role was never deleted.
    ///
    /// Restart recovery for the delayed role cleanup: anything listed here is a
    /// leftover whose 5-minute delete never ran.
    pub async fn ended_with_roles(&self, limit: i64) -> Result<Vec<Giveaway>> {
        let rows = self
            .db
            .query(
                "SELECT * FROM simple_giveaways WHERE status != 'active' AND entrants_role_id IS NOT NULL AND entrants_role_id != '' ORDER BY ended_at DESC LIMIT ?",
                &[limit.into()],
            )
            .await?;
        Ok(rows.iter().map(Giveaway::from_row).collect())
    }

    /// Delete entry rows for giveaways finished over 5h ago. Returns count.
    ///
    /// Also zeroes the live counter on wiped giveaways: otherwise entry_count()
    /// would keep returning the stale pre-wipe bump.
    pub async fn wipe_stale_entries(&self, now: Option<i64>) -> Result<i64> {
        let cutoff = now.unwrap_or_else(now_ms) - ENTRY_RETENTION_MS;
        let _ = self
            .db
            .execute(
                "UPDATE simple_giveaways SET entrant_count = 0 WHERE status != 'active' AND COALESCE(ended_at, created_at) <= ? AND NOT EXISTS (SELECT 1 FROM simple_claims WHERE simple_claims.giveaway_id = simple_giveaways.id AND simple_claims.status = 'pending')",
                &[cutoff.into()],
            )
            .await;
        let deleted = self
            .db
            .execute(
                // ended_at was added to a table that already held data, so
                // giveaways that finished before that migration carry NULL
                // forever; created_at is the fallback that still lets their
                // entry rows expire. A pending claim still needs its entry pool
                // for the replacement draw, so its giveaway is exempt until the
                // claim settles.
                "DELETE FROM simple_entries WHERE giveaway_id IN (SELECT id FROM simple_giveaways WHERE status != 'active' AND COALESCE(ended_at, created_at) <= ? AND NOT EXISTS (SELECT 1 FROM simple_claims WHERE simple_claims.giveaway_id = simple_giveaways.id AND simple_claims.status = 'pending'))",
                &[cutoff.into()],
            )
            .await?;
        Ok(deleted as i64)
    }
}

impl GiveawayService {
    // -- blacklist (timed + permanent bans) ------------------------------

    /// Active ban row, or None when not banned / expired.
    ///
    /// Permanent = expires_at NULL. Timed = expires_at > now. Expired rows are
    /// treated as absent (lazy expiry, no extra write on the hot path).
    pub async fn ban_info(
        &self,
        guild_id: &str,
        user_id: &str,
        now: Option<i64>,
    ) -> Result<Option<Value>> {
        if guild_id.is_empty() || user_id.is_empty() {
            return Ok(None);
        }
        let ts = now.unwrap_or_else(now_ms);
        let row = self
            .db
            .query_one(
                "SELECT expires_at, reason, banned_by, created_at FROM simple_blacklist WHERE guild_id = ? AND user_id = ? AND (expires_at IS NULL OR expires_at > ?)",
                &[guild_id.into(), user_id.into(), ts.into()],
            )
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let expires_at = row.int("expires_at");
        let reason = row.text("reason").unwrap_or_default();
        let banned_by = row.text("banned_by").unwrap_or_default();
        let permanent = expires_at.is_none();
        let remaining = match expires_at {
            Some(exp) => (exp - ts).max(0),
            None => 0,
        };
        Ok(Some(json!({
            "expires_at": expires_at,
            "reason": reason,
            "banned_by": banned_by,
            "permanent": permanent,
            "remaining_ms": remaining,
        })))
    }

    /// Refusal text for a banned member. Always contains "blocked".
    ///
    /// Shown both to the refused member (join path) and reused by staff
    /// confirmations. Reason appears in both places when set.
    pub async fn ban_message(
        &self,
        guild_id: &str,
        user_id: &str,
        now: Option<i64>,
    ) -> Result<String> {
        let base = "\u{1F6AB} You are blocked from giveaways in this server.";
        let Some(info) = self.ban_info(guild_id, user_id, now).await? else {
            return Ok(base.to_string());
        };
        let mut parts = vec![base.to_string()];
        let reason = info
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_string();
        if !reason.is_empty() {
            let clipped: String = reason.chars().take(256).collect();
            parts.push(format!("Reason: {clipped}"));
        }
        if !info
            .get("permanent")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            let exp = info.get("expires_at").and_then(json_int).unwrap_or(0);
            if exp != 0 {
                parts.push(format!("Ban expires <t:{}:R>.", exp.div_euclid(1000)));
            }
        } else {
            parts.push("This ban is permanent.".to_string());
        }
        Ok(parts.join(" "))
    }

    pub async fn is_blacklisted(
        &self,
        guild_id: &str,
        user_id: &str,
        now: Option<i64>,
    ) -> Result<bool> {
        if guild_id.is_empty() || user_id.is_empty() {
            return Ok(false);
        }
        Ok(self.ban_info(guild_id, user_id, now).await?.is_some())
    }

    /// Block a user for N hours (0 = permanent), purge running entries.
    ///
    /// hours 0..720 enforced; reason trimmed to 256 chars. Re-ban overwrites
    /// the previous expiry/reason (upsert). Returns the active ban_info.
    pub async fn blacklist_add(
        &self,
        guild_id: &str,
        user_id: &str,
        hours: i64,
        reason: &str,
        banned_by: &str,
        now: Option<i64>,
    ) -> Result<Value> {
        if !(0..=BAN_MAX_HOURS).contains(&hours) {
            return Err(ServiceError::new(format!(
                "Ban duration must be 0-{BAN_MAX_HOURS} hours (0 = permanent)."
            ))
            .into());
        }
        let clean_reason: String = reason.trim().chars().take(256).collect();
        let clean_by: String = banned_by.trim().chars().take(64).collect();
        let ts = now.unwrap_or_else(now_ms);
        let expires_at: Option<i64> = if hours == 0 {
            None
        } else {
            Some(ts + hours * 3600 * 1000)
        };
        self.db
            .execute(
                "INSERT INTO simple_blacklist (guild_id, user_id, expires_at, reason, banned_by, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET expires_at = excluded.expires_at, reason = excluded.reason, banned_by = excluded.banned_by, created_at = excluded.created_at",
                &[
                    guild_id.into(),
                    user_id.into(),
                    expires_at.into(),
                    clean_reason.as_str().into(),
                    clean_by.as_str().into(),
                    ts.into(),
                ],
            )
            .await?;
        self.db
            .execute(
                "DELETE FROM simple_entries WHERE user_id = ? AND giveaway_id IN (SELECT id FROM simple_giveaways WHERE guild_id = ? AND status = 'active')",
                &[user_id.into(), guild_id.into()],
            )
            .await?;
        for row in self.list_active(guild_id).await? {
            self.sync_counter(&row.id).await;
        }
        match self.ban_info(guild_id, user_id, Some(ts)).await? {
            Some(info) => Ok(info),
            None => Ok(json!({"permanent": hours == 0, "reason": clean_reason})),
        }
    }

    pub async fn blacklist_remove(&self, guild_id: &str, user_id: &str) -> Result<bool> {
        let removed = self
            .db
            .execute(
                "DELETE FROM simple_blacklist WHERE guild_id = ? AND user_id = ?",
                &[guild_id.into(), user_id.into()],
            )
            .await?;
        Ok(removed > 0)
    }

    /// Delete timed bans already past expiry. Returns rows removed.
    pub async fn purge_expired_bans(&self, now: Option<i64>) -> Result<i64> {
        let ts = now.unwrap_or_else(now_ms);
        let removed = self
            .db
            .execute(
                "DELETE FROM simple_blacklist WHERE expires_at IS NOT NULL AND expires_at <= ?",
                &[ts.into()],
            )
            .await?;
        Ok(removed as i64)
    }

    /// How many members of this guild are blocked from giveaways.
    ///
    /// Counts active bans only (permanent + unexpired timed). The listing is
    /// capped, so this is what keeps a caller from reporting the size of the
    /// page set as if it were the whole blacklist.
    pub async fn count_blacklisted(&self, guild_id: &str, now: Option<i64>) -> Result<i64> {
        let ts = now.unwrap_or_else(now_ms);
        let row = self
            .db
            .query_one(
                "SELECT COUNT(*) AS n FROM simple_blacklist WHERE guild_id = ? AND (expires_at IS NULL OR expires_at > ?)",
                &[guild_id.into(), ts.into()],
            )
            .await?;
        Ok(row.and_then(|row| row.int("n")).unwrap_or(0))
    }

    pub async fn blacklist_list(
        &self,
        guild_id: &str,
        limit: i64,
        now: Option<i64>,
    ) -> Result<Vec<String>> {
        let ts = now.unwrap_or_else(now_ms);
        let rows = self
            .db
            .query(
                "SELECT user_id FROM simple_blacklist WHERE guild_id = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY user_id ASC LIMIT ?",
                &[guild_id.into(), ts.into(), limit.into()],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|row| row.text("user_id").unwrap_or_else(|| "None".to_string()))
            .collect())
    }

    /// Active bans with expiry + reason for the staff list.
    pub async fn blacklist_list_detailed(
        &self,
        guild_id: &str,
        limit: i64,
        now: Option<i64>,
    ) -> Result<Vec<Value>> {
        let ts = now.unwrap_or_else(now_ms);
        let rows = self
            .db
            .query(
                "SELECT user_id, expires_at, reason, banned_by, created_at FROM simple_blacklist WHERE guild_id = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY user_id ASC LIMIT ?",
                &[guild_id.into(), ts.into(), limit.clamp(1, 500).into()],
            )
            .await?;
        let mut out = Vec::with_capacity(rows.len());
        for row in &rows {
            let exp = row.int("expires_at");
            out.push(json!({
                "user_id": row.text("user_id").unwrap_or_else(|| "None".to_string()),
                "expires_at": exp,
                "permanent": exp.is_none(),
                "reason": row.text("reason").unwrap_or_default(),
                "banned_by": row.text("banned_by").unwrap_or_default(),
            }));
        }
        Ok(out)
    }
}

impl GiveawayService {
    // -- timed-out penalty (Discord's native /mute) ----------------------

    /// Giveaways this member still has to sit out. 0 means they may enter.
    pub async fn timeout_ban_remaining(&self, guild_id: &str, user_id: &str) -> Result<i64> {
        if guild_id.is_empty() || user_id.is_empty() {
            return Ok(0);
        }
        let row = self
            .db
            .query_one(
                "SELECT giveaways_remaining FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ?",
                &[guild_id.into(), user_id.into()],
            )
            .await?;
        Ok(row
            .and_then(|row| row.int("giveaways_remaining"))
            .map(|left| left.max(0))
            .unwrap_or(0))
    }

    /// Start the penalty for a member caught timed out. Returns what is left.
    ///
    /// MAX() rather than a plain overwrite is the "never stack" rule made
    /// durable: somebody already sitting out three giveaways stays at three no
    /// matter how often they are caught timed out in the meantime. The giveaway
    /// they were caught in is remembered as the one already counted, so
    /// hammering the same Join button cannot burn the penalty down.
    pub async fn apply_timeout_penalty(
        &self,
        guild_id: &str,
        user_id: &str,
        giveaways: i64,
        giveaway_id: Option<&str>,
    ) -> Result<i64> {
        let count = giveaways.max(1);
        let ts = now_ms();
        // Hits from giveaways that are over can never refuse anyone again, so
        // each new penalty starts by dropping this member's stale ones.
        self.db
            .execute(
                "DELETE FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ? AND giveaway_id NOT IN (SELECT id FROM simple_giveaways WHERE status = 'active')",
                &[guild_id.into(), user_id.into()],
            )
            .await?;
        if let Some(trigger) = giveaway_id.filter(|id| !id.is_empty()) {
            // The trigger giveaway is recorded but never spends a unit, which
            // is what keeps hammering its Join button from burning the penalty.
            self.record_ban_hit(guild_id, user_id, trigger).await?;
        }
        self.db
            .execute(
                "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining, last_blocked_giveaway_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET giveaways_remaining = MAX(giveaways_remaining, excluded.giveaways_remaining), last_blocked_giveaway_id = excluded.last_blocked_giveaway_id, updated_at = excluded.updated_at",
                &[
                    guild_id.into(),
                    user_id.into(),
                    count.into(),
                    giveaway_id.into(),
                    ts.into(),
                    ts.into(),
                ],
            )
            .await?;
        self.timeout_ban_remaining(guild_id, user_id).await
    }

    /// Remember a giveaway this member was blocked from. True when new.
    ///
    /// The primary key makes it idempotent, and the row count says whether this
    /// call was the one that inserted it: that is the "count once" test, safe
    /// against two clicks racing each other.
    async fn record_ban_hit(
        &self,
        guild_id: &str,
        user_id: &str,
        giveaway_id: &str,
    ) -> Result<bool> {
        let inserted = self
            .db
            .execute(
                "INSERT OR IGNORE INTO simple_giveaway_ban_hits (guild_id, user_id, giveaway_id, created_at) VALUES (?, ?, ?, ?)",
                &[
                    guild_id.into(),
                    user_id.into(),
                    giveaway_id.into(),
                    now_ms().into(),
                ],
            )
            .await?;
        Ok(changed(inserted))
    }

    async fn was_blocked_from(
        &self,
        guild_id: &str,
        user_id: &str,
        giveaway_id: &str,
    ) -> Result<bool> {
        Ok(self
            .db
            .query_one(
                "SELECT 1 AS one FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ? AND giveaway_id = ?",
                &[guild_id.into(), user_id.into(), giveaway_id.into()],
            )
            .await?
            .is_some())
    }

    /// Spend one giveaway of the penalty. Returns what is left (0 = lifted).
    ///
    /// Every giveaway already held against this member is remembered (not just
    /// the last one), so only a giveaway never seen before spends a unit:
    /// alternating A, B, A cannot burn the penalty down. The penalty row is
    /// deleted as soon as the counter reaches zero; the remembered giveaways
    /// stay, so the ones they sat out keep refusing them while still running.
    pub async fn consume_timeout_ban(
        &self,
        guild_id: &str,
        user_id: &str,
        giveaway_id: &str,
    ) -> Result<i64> {
        if self.record_ban_hit(guild_id, user_id, giveaway_id).await? {
            // last_blocked_giveaway_id is still honoured for rows written
            // before the hits table existed, whose trigger giveaway has no hit
            // row.
            self.db
                .execute(
                    "UPDATE simple_giveaway_bans SET giveaways_remaining = giveaways_remaining - 1, last_blocked_giveaway_id = ?, updated_at = ? WHERE guild_id = ? AND user_id = ? AND giveaways_remaining > 0 AND (last_blocked_giveaway_id IS NULL OR last_blocked_giveaway_id != ?)",
                    &[
                        giveaway_id.into(),
                        now_ms().into(),
                        guild_id.into(),
                        user_id.into(),
                        giveaway_id.into(),
                    ],
                )
                .await?;
        }
        let left = self.timeout_ban_remaining(guild_id, user_id).await?;
        if left > 0 {
            return Ok(left);
        }
        self.db
            .execute(
                "DELETE FROM simple_giveaway_bans WHERE guild_id = ? AND user_id = ? AND giveaways_remaining <= 0",
                &[guild_id.into(), user_id.into()],
            )
            .await?;
        Ok(0)
    }

    /// How many members of this guild are sitting out a penalty right now.
    ///
    /// The listing is capped, so the count is what lets a caller say how many it
    /// did not show instead of quietly reporting a short total.
    pub async fn count_timeout_bans(&self, guild_id: &str) -> Result<i64> {
        let row = self
            .db
            .query_one(
                "SELECT COUNT(*) AS n FROM simple_giveaway_bans WHERE guild_id = ? AND giveaways_remaining > 0",
                &[guild_id.into()],
            )
            .await?;
        Ok(row.and_then(|row| row.int("n")).unwrap_or(0))
    }

    /// Members of this guild still sitting out a penalty, longest first.
    ///
    /// Only this table and only this guild: the dashboard, the entry list and
    /// the winner draw never read it. A zero row cannot normally exist (it is
    /// deleted on the way to zero) but is filtered out anyway, so a row written
    /// by hand cannot show a member who is actually free to enter. The limit is
    /// a ceiling on one response, so pair it with count_timeout_bans() when the
    /// caller has to say whether anything was left out.
    pub async fn list_timeout_bans(&self, guild_id: &str, limit: i64) -> Result<Vec<Value>> {
        let rows = self
            .db
            .query(
                "SELECT user_id, giveaways_remaining, created_at FROM simple_giveaway_bans WHERE guild_id = ? AND giveaways_remaining > 0 ORDER BY giveaways_remaining DESC, user_id ASC LIMIT ?",
                &[guild_id.into(), limit.clamp(1, 500).into()],
            )
            .await?;
        let mut banned = Vec::with_capacity(rows.len());
        for row in &rows {
            let Some(remaining) = row.int("giveaways_remaining").map(|left| left.max(0)) else {
                continue;
            };
            if remaining <= 0 {
                continue;
            }
            banned.push(json!({
                "user_id": row.text("user_id").unwrap_or_else(|| "None".to_string()),
                "giveaways_remaining": remaining,
                "created_at": row.int_or("created_at", 0),
            }));
        }
        Ok(banned)
    }

    /// Refuse a timed-out member — or one still serving that penalty.
    ///
    /// Called by check_eligible() only once the giveaway is known to still be
    /// joinable, so a stale button never hands out a penalty.
    ///
    /// A member who is already sitting out the penalty spends one giveaway
    /// here: the giveaway they are blocked from *is* one of the ones they have
    /// to sit out. The timed-out test is skipped while a penalty is running,
    /// which is what stops a second penalty stacking on the first. Either way
    /// an entry they already had is dropped rather than left to win.
    pub async fn check_timeout_ban(
        &self,
        gw: &Giveaway,
        user_id: &str,
        timed_out: bool,
        snap_ban: Option<i64>,
        snap_hit: Option<bool>,
    ) -> Result<()> {
        if user_id.is_empty() {
            return Ok(());
        }
        // Snapshot staleness: the tuple was read in parallel with get(), before
        // this thread started. A penalty started/expired in between would make
        // a snapshot-based DELETE/consume wrong (false penalty or missed
        // spend). Happy path (snapshot says clean and member not timed out)
        // returns fast; the INSERT guard in join() still blocks a penalty that
        // landed after the snapshot (zero-insert path re-runs this gate live).
        // Anything the snapshot would refuse re-reads live before acting, so a
        // stale ban/hit can never cause a false DELETE/consume.
        let mut snap_ban = snap_ban;
        let mut snap_hit = snap_hit;
        if snap_ban.is_some() || snap_hit.is_some() {
            let snap_blocked = snap_ban.unwrap_or(0) > 0 || snap_hit.unwrap_or(false) || timed_out;
            if !snap_blocked {
                return Ok(());
            }
            snap_ban = None;
            snap_hit = None;
        }
        // Stale-giveaway precedence: gw was read before this thread started, so
        // end() may have flipped status since. Never hand out, spend, or keep a
        // penalty from a stale button on an ended giveaway -- the caller must
        // hear "ended", with no side effects. Happy-path joins already returned
        // above, so only refusal paths pay this trip.
        let live = match self.get(&gw.id).await {
            Ok(live) => live,
            Err(err) => {
                if err.downcast_ref::<ServiceError>().is_some() {
                    return Err(ServiceError::new("This giveaway has ended.").into());
                }
                return Err(err);
            }
        };
        if !live.active() || live.ends_at <= now_ms() {
            return Err(ServiceError::new("This giveaway has ended.").into());
        }
        let mut left = match snap_ban {
            Some(ban) => ban,
            None => self.timeout_ban_remaining(&gw.guild_id, user_id).await?,
        };
        if left <= 0 && !timed_out {
            // The penalty is over, but a giveaway it was served on is still one
            // they sat out: rejoining it (say, the one that spent the last
            // unit) would hand the penalty straight back.
            let was_blocked = match snap_hit {
                Some(hit) => hit,
                None => self.was_blocked_from(&gw.guild_id, user_id, &gw.id).await?,
            };
            if was_blocked {
                return Err(ServiceError::with_kind(
                    "You sat out this giveaway as part of your timed-out penalty.",
                    TIMEOUT_BAN_KIND,
                )
                .into());
            }
            return Ok(());
        }
        // Blocked, so they must not be in a giveaway at all: entries made
        // before Discord timed them out (or before the penalty started) are
        // dropped from every running giveaway in the server, exactly as
        // blacklist_add does. Dropping only the one they clicked would let them
        // win the giveaway they joined yesterday while sitting out this one.
        self.db
            .execute(
                "DELETE FROM simple_entries WHERE user_id = ? AND giveaway_id IN (SELECT id FROM simple_giveaways WHERE guild_id = ? AND status = 'active')",
                &[user_id.into(), gw.guild_id.as_str().into()],
            )
            .await?;
        for row in self.list_active(&gw.guild_id).await? {
            self.sync_counter(&row.id).await;
        }
        if left > 0 {
            left = self
                .consume_timeout_ban(&gw.guild_id, user_id, &gw.id)
                .await?;
            if left > 0 {
                return Err(ServiceError::with_kind(
                    format!(
                        "You are sitting out a penalty for joining while timed out. {left} giveaway(s) left."
                    ),
                    TIMEOUT_BAN_KIND,
                )
                .into());
            }
            return Err(ServiceError::with_kind(
                "That was the last giveaway of your timed-out penalty — you can enter the next one.",
                TIMEOUT_BAN_KIND,
            )
            .into());
        }
        let left = self
            .apply_timeout_penalty(&gw.guild_id, user_id, TIMEOUT_PENALTY, Some(&gw.id))
            .await?;
        Err(ServiceError::with_kind(
            format!(
                "You are currently timed out in this server, so you cannot enter giveaways. Timeout penalty: sit out the next {left} giveaway(s)."
            ),
            TIMEOUT_BAN_KIND,
        )
        .into())
    }

    pub async fn due(&self, now: Option<i64>) -> Result<Vec<Giveaway>> {
        let ts = now.unwrap_or_else(now_ms);
        let rows = self
            .db
            .query(
                "SELECT * FROM simple_giveaways WHERE status = 'active' AND ends_at <= ? ORDER BY ends_at ASC LIMIT 25",
                &[ts.into()],
            )
            .await?;
        Ok(rows.iter().map(Giveaway::from_row).collect())
    }
}
