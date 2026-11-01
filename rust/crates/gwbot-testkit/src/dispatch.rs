//! The operation table the suite drives the real service through.
//!
//! This used to be a JSON-line binary: one request object in, one response
//! object out. The tests are Rust now, so the same table is called directly and
//! the wire format is gone -- but the *shape* is deliberately unchanged, because
//! every ported test names its operation and arguments exactly as the Python
//! suite did. One table also keeps a new service method from being unreachable
//! from a test.
//!
//! Operations fall into three families:
//!
//! * every `GiveawayService` method, by its Rust name (`join`, `end`, ...);
//! * `sql` / `exec`, which reach the database directly so a test can assert
//!   storage state without trusting the service to report on itself;
//! * `clock.*`, which read or move this harness's clock.
//!
//! Storage is always local SQLite here. Production is Turso-only; nothing in
//! this crate is shipped.

use std::collections::BTreeMap;

use anyhow::{anyhow, Result};
use gwbot_core::db::{Row, SqlValue};
use gwbot_core::models::{Giveaway, ManualClock};
use gwbot_core::service::{FlushReceipt, GiveawayService};
use serde_json::{json, Map, Value};

/// The four eligibility subqueries, as the service reads them.
type Snapshot = Option<(bool, i64, i64, bool)>;

// -- argument reading ----------------------------------------------------

/// Typed, lenient readers over one request's `args` object.
///
/// Every getter has a documented default so a test only has to name the fields
/// it cares about; a typo therefore shows up as a wrong value in an assertion,
/// never as a protocol error that hides the rule under test.
struct Args<'a> {
    map: &'a Map<String, Value>,
}

impl<'a> Args<'a> {
    fn new(map: &'a Map<String, Value>) -> Self {
        Self { map }
    }

    fn raw(&self, key: &str) -> Option<&Value> {
        self.map.get(key).filter(|value| !value.is_null())
    }

    /// Required string. A missing one is a test bug, and saying which field is
    /// missing is far more useful than a defaulted empty id.
    fn s(&self, key: &str) -> Result<String> {
        match self.raw(key) {
            Some(Value::String(text)) => Ok(text.clone()),
            Some(other) => Ok(match other {
                Value::Number(n) => n.to_string(),
                other => other.to_string(),
            }),
            None => Err(anyhow!("missing argument `{key}`")),
        }
    }

    fn os(&self, key: &str) -> Option<String> {
        self.raw(key).map(|value| match value {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        })
    }

    fn i(&self, key: &str) -> Result<i64> {
        self.oi(key)
            .ok_or_else(|| anyhow!("missing argument `{key}`"))
    }

    fn oi(&self, key: &str) -> Option<i64> {
        match self.raw(key)? {
            Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
            Value::String(text) => text.trim().parse().ok(),
            Value::Bool(flag) => Some(i64::from(*flag)),
            _ => None,
        }
    }

    fn i_or(&self, key: &str, default: i64) -> i64 {
        self.oi(key).unwrap_or(default)
    }

    fn of(&self, key: &str) -> Option<f64> {
        match self.raw(key)? {
            Value::Number(n) => n.as_f64(),
            Value::String(text) => text.trim().parse().ok(),
            _ => None,
        }
    }

    fn b(&self, key: &str, default: bool) -> bool {
        match self.raw(key) {
            Some(Value::Bool(flag)) => *flag,
            Some(Value::Number(n)) => n.as_i64().map(|v| v != 0).unwrap_or(default),
            Some(Value::String(text)) => {
                matches!(
                    text.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            }
            _ => default,
        }
    }

    fn strs(&self, key: &str) -> Vec<String> {
        match self.raw(key) {
            Some(Value::Array(items)) => items
                .iter()
                .map(|item| match item {
                    Value::String(text) => text.clone(),
                    other => other.to_string(),
                })
                .collect(),
            _ => Vec::new(),
        }
    }

    fn ostrs(&self, key: &str) -> Option<Vec<String>> {
        self.raw(key).map(|_| self.strs(key))
    }

    /// A giveaway: either a full object (as returned by `create`/`get`) or a
    /// bare id, so a test can pass `gw` straight through.
    fn gw(&self, key: &str) -> Result<Giveaway> {
        self.ogw(key)
            .ok_or_else(|| anyhow!("missing argument `{key}`"))
    }

    fn ogw(&self, key: &str) -> Option<Giveaway> {
        match self.raw(key)? {
            Value::String(id) => Some(Giveaway {
                id: id.clone(),
                ..Giveaway::default()
            }),
            Value::Object(_) => serde_json::from_value(self.raw(key)?.clone()).ok(),
            _ => None,
        }
    }

    /// The eligibility snapshot: `[banned, message_count, account_age_days,
    /// hit]`, or null when the caller has none.
    fn snapshot(&self, key: &str) -> Snapshot {
        let items = self.raw(key)?.as_array()?;
        if items.len() != 4 {
            return None;
        }
        let flag = |index: usize| items[index].as_bool().unwrap_or(false);
        let int = |index: usize| items[index].as_i64().unwrap_or(0);
        Some((flag(0), int(1), int(2), flag(3)))
    }

    /// Message-count rows as `[guild_id, user_id, count]` triples.
    fn counts(&self, key: &str) -> Vec<(String, String, i64)> {
        let Some(items) = self.raw(key).and_then(Value::as_array) else {
            return Vec::new();
        };
        items
            .iter()
            .filter_map(|item| {
                let triple = item.as_array()?;
                if triple.len() != 3 {
                    return None;
                }
                Some((
                    triple[0].as_str()?.to_string(),
                    triple[1].as_str()?.to_string(),
                    triple[2].as_i64()?,
                ))
            })
            .collect()
    }

    /// The pre-flush counts a receipt captured, as a map.
    fn before(&self, key: &str) -> BTreeMap<(String, String), i64> {
        self.counts(key)
            .into_iter()
            .map(|(guild, user, count)| ((guild, user), count))
            .collect()
    }

    /// SQL parameters. Bools become 0/1 because SQLite has no boolean type.
    fn params(&self, key: &str) -> Vec<SqlValue> {
        let Some(items) = self.raw(key).and_then(Value::as_array) else {
            return Vec::new();
        };
        items
            .iter()
            .map(|item| match item {
                Value::Null => SqlValue::Null,
                Value::Bool(flag) => SqlValue::Integer(i64::from(*flag)),
                Value::Number(n) => match n.as_i64() {
                    Some(int) => SqlValue::Integer(int),
                    None => SqlValue::Real(n.as_f64().unwrap_or(0.0)),
                },
                Value::String(text) => SqlValue::Text(text.clone()),
                other => SqlValue::Text(other.to_string()),
            })
            .collect()
    }
}

// -- result shaping ------------------------------------------------------

fn cell(value: &SqlValue) -> Value {
    match value {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(int) => json!(int),
        SqlValue::Real(real) => json!(real),
        SqlValue::Text(text) => json!(text),
        // No column the bot reads is a blob; surfacing bytes as a number array
        // is still better than pretending the row has one fewer column.
        SqlValue::Blob(bytes) => json!(bytes),
    }
}

fn row_json(row: &Row) -> Value {
    let mut map = Map::new();
    for (name, value) in row.columns().iter().zip(row.values()) {
        map.insert(name.clone(), cell(value));
    }
    Value::Object(map)
}

fn rows_json(rows: &[Row]) -> Value {
    Value::Array(rows.iter().map(row_json).collect())
}

fn gws_json(items: &[Giveaway]) -> Value {
    Value::Array(items.iter().map(|gw| json!(gw)).collect())
}

// -- dispatch ------------------------------------------------------------

pub async fn dispatch(
    service: &GiveawayService,
    clock: &ManualClock,
    op: &str,
    args: &Map<String, Value>,
) -> Result<Value> {
    let a = Args::new(args);
    let db = service.db();

    Ok(match op {
        // -- plumbing ----------------------------------------------------
        "ping" => json!("pong"),
        "clock.now" => json!(clock.now()),
        "clock.advance" => json!(clock.advance(a.i("ms")?)),
        "clock.set" => json!(clock.set(a.i("ms")?)),
        "schema_version" => {
            let row = db
                .query_one("PRAGMA user_version", &[])
                .await?
                .map(|row| row_json(&row));
            row.unwrap_or(Value::Null)
        }
        "sql" => rows_json(&db.query(&a.s("sql")?, &a.params("params")).await?),
        "exec" => json!(db.execute(&a.s("sql")?, &a.params("params")).await?),
        "db_stats" => {
            let stats = db.stats();
            json!({
                "reads": stats.reads,
                "writes": stats.writes,
                "read_ms": stats.read_ms,
                "write_ms": stats.write_ms,
            })
        }
        "reset_stats" => {
            db.reset_stats();
            Value::Null
        }

        // -- cache -------------------------------------------------------
        "cache_get" => json!(service.cache_get(&a.s("key")?).await),
        "cache_get_int" => json!(service.cache_get_int(&a.s("key")?).await),
        "cache_put" => {
            let key = a.s("key")?;
            let ttl = a.of("ttl").unwrap_or(0.0);
            service
                .cache_put(&key, a.raw("value").unwrap_or(&Value::Null), ttl)
                .await;
            Value::Null
        }
        "cache_drop" => {
            service.cache_drop(&a.s("suffix")?).await;
            Value::Null
        }
        "mark_stale" => {
            let suffixes = a.strs("suffixes");
            let refs: Vec<&str> = suffixes.iter().map(String::as_str).collect();
            service.mark_stale(&refs);
            Value::Null
        }
        "drain_invalidations" => json!(service.drain_invalidations()),
        "flush_invalidations" => json!(service.flush_invalidations().await),
        "dump" => GiveawayService::dump(&a.gw("gw")?),
        "load_gw" => match GiveawayService::load_gw(a.raw("blob").unwrap_or(&Value::Null)) {
            Some(gw) => json!(gw),
            None => Value::Null,
        },

        // -- create and settings -----------------------------------------
        "create" => {
            let gw = service
                .create(
                    &a.s("guild_id")?,
                    &a.s("channel_id")?,
                    &a.s("prize")?,
                    a.i("winner_count")?,
                    a.i("duration_seconds")?,
                    &a.s("created_by")?,
                    a.os("required_role_id").as_deref(),
                    a.ostrs("required_role_ids").as_deref(),
                    a.os("blocked_role_id").as_deref(),
                    a.i_or("min_account_age_days", 0),
                    a.i_or("min_messages", 0),
                    a.os("image_url").as_deref(),
                    a.os("host_id").as_deref(),
                    a.os("host_name").as_deref(),
                    a.i_or("claim_timeout_seconds", 0),
                )
                .await?;
            json!(gw)
        }
        "get_notify_role" => json!(service.get_notify_role(&a.s("guild_id")?).await?),
        "get_notify_role_cached" => {
            json!(service.get_notify_role_cached(&a.s("guild_id")?).await)
        }
        "set_notify_role" => {
            service
                .set_notify_role(&a.s("guild_id")?, a.os("role_id").as_deref())
                .await?;
            Value::Null
        }
        "invalidate_notify_role" => {
            service.invalidate_notify_role(&a.s("guild_id")?).await;
            Value::Null
        }

        // -- reads -------------------------------------------------------
        "get" => json!(service.get(&a.s("giveaway_id")?).await?),
        "get_by_message" => json!(service.get_by_message(&a.s("message_id")?).await?),
        "list_active" => gws_json(&service.list_active(&a.s("guild_id")?).await?),
        "list_all_active" => gws_json(&service.list_all_active(a.i_or("limit", 25)).await?),
        "list_active_cached" => gws_json(&service.list_active_cached(&a.s("guild_id")?).await?),
        "resolve" => json!(service.resolve(&a.s("guild_id")?, &a.s("raw_id")?).await?),
        "list_ended" => gws_json(
            &service
                .list_ended(&a.s("guild_id")?, a.i_or("limit", 25))
                .await?,
        ),
        "list_all_ended" => gws_json(&service.list_all_ended(a.i_or("limit", 200)).await?),
        "resolve_ended" => json!(
            service
                .resolve_ended(&a.s("guild_id")?, &a.s("raw_id")?)
                .await?
        ),

        // -- entries -----------------------------------------------------
        "entry_count" => json!(
            service
                .entry_count(&a.s("giveaway_id")?, a.b("exact", false))
                .await?
        ),
        "entry_count_cached" => json!(service.entry_count_cached(&a.s("giveaway_id")?).await?),
        "sync_counter" => {
            service.sync_counter(&a.s("giveaway_id")?).await;
            Value::Null
        }
        "entries_snapshot" => {
            let snapshot = service
                .entries_snapshot(
                    &a.s("giveaway_id")?,
                    a.oi("limit"),
                    &a.os("user_id").unwrap_or_default(),
                )
                .await;
            match snapshot {
                Some((total, rows, chance)) => json!([total, rows, chance]),
                None => Value::Null,
            }
        }
        "entries" => json!(service.entries(&a.s("giveaway_id")?, a.oi("limit")).await?),
        "has_entry" => json!(
            service
                .has_entry(&a.s("giveaway_id")?, &a.s("user_id")?)
                .await?
        ),
        "check_eligible" => {
            let gw = a.gw("gw")?;
            service
                .check_eligible(
                    &gw,
                    &a.strs("member_roles"),
                    a.of("account_created_ts"),
                    &a.s("user_id")?,
                    a.i_or("pending_messages", 0),
                    a.b("timed_out", false),
                    a.snapshot("snapshot"),
                )
                .await?;
            Value::Null
        }
        "message_count" => json!(
            service
                .message_count(&a.s("guild_id")?, &a.s("user_id")?)
                .await?
        ),
        "get_status" => json!(service.get_status(&a.s("giveaway_id")?).await?),
        "refresh_snapshot" => {
            let (status, count) = service.refresh_snapshot(&a.s("giveaway_id")?).await;
            json!([status, count])
        }
        "join_snapshot" => {
            let (gw, snapshot) = service
                .join_snapshot(&a.s("giveaway_id")?, &a.s("guild_id")?, &a.s("user_id")?)
                .await?;
            let snap = match snapshot {
                Some((banned, messages, age, hit)) => json!([banned, messages, age, hit]),
                None => Value::Null,
            };
            json!([gw, snap])
        }
        "eligibility_snapshot" => {
            let snapshot = service
                .eligibility_snapshot(
                    &a.s("guild_id")?,
                    &a.s("user_id")?,
                    a.os("giveaway_id").as_deref(),
                )
                .await;
            match snapshot {
                Some((banned, messages, age, hit)) => json!([banned, messages, age, hit]),
                None => Value::Null,
            }
        }

        // -- message counts ----------------------------------------------
        "record_message" => {
            service
                .record_message(&a.s("guild_id")?, &a.s("user_id")?)
                .await?;
            Value::Null
        }
        "add_message_counts" => {
            let mut rows = a.counts("rows");
            let receipt = FlushReceipt::new();
            let applied = service
                .add_message_counts(&mut rows, Some(&receipt))
                .await?;
            json!({"applied": applied, "before": receipt.before().len()})
        }
        "reconcile_message_counts" => {
            let rows = a.counts("rows");
            let before = a.before("before");
            let left = service.reconcile_message_counts(&rows, &before).await;
            json!(left)
        }

        // -- join / leave ------------------------------------------------
        "join" => {
            let gw = a.gw("gw")?;
            json!(
                service
                    .join(
                        &gw,
                        &a.s("user_id")?,
                        &a.s("username")?,
                        &a.strs("member_roles"),
                        a.of("account_created_ts"),
                        a.i_or("pending_messages", 0),
                        a.b("timed_out", false),
                        a.snapshot("snapshot"),
                        a.b("fetch_count", true),
                    )
                    .await?
            )
        }
        "join_and_refresh" => {
            let gw = a.gw("gw")?;
            let (status, count) = service
                .join_and_refresh(
                    &gw,
                    &a.s("user_id")?,
                    &a.s("username")?,
                    &a.strs("member_roles"),
                    a.of("account_created_ts"),
                    a.i_or("pending_messages", 0),
                    a.b("timed_out", false),
                    a.snapshot("snapshot"),
                )
                .await?;
            json!([status, count])
        }
        "leave" => json!(
            service
                .leave(&a.s("giveaway_id")?, &a.s("user_id")?)
                .await?
        ),
        "leave_with_count" => {
            let (left, count, prize) = service
                .leave_with_count(&a.s("giveaway_id")?, &a.s("user_id")?)
                .await?;
            json!([left, count, prize])
        }

        // -- draws -------------------------------------------------------
        "pick" => json!(
            service
                .pick(
                    &a.s("giveaway_id")?,
                    a.i("n")?,
                    a.ostrs("exclude").as_deref(),
                )
                .await?
        ),
        "end" => {
            let known = a.ogw("known");
            let (gw, winners) = service
                .end(
                    &a.s("giveaway_id")?,
                    a.oi("now"),
                    a.b("require_due", false),
                    known.as_ref(),
                )
                .await?;
            json!([gw, winners])
        }
        "reroll" => {
            let (gw, winners) = service
                .reroll(
                    &a.s("giveaway_id")?,
                    a.i_or("count", 1),
                    a.os("replace_user_id").as_deref(),
                )
                .await?;
            json!([gw, winners])
        }
        "due" => gws_json(&service.due(a.oi("now")).await?),

        // -- claims ------------------------------------------------------
        "start_claims" => {
            let gw = a.gw("gw")?;
            service
                .start_claims(&gw, &a.strs("winners"), a.oi("now"))
                .await?;
            Value::Null
        }
        "repair_missing_claims" => json!(
            service
                .repair_missing_claims(&a.s("giveaway_id")?, a.oi("now"))
                .await?
        ),
        "repair_all_missing_claims" => json!(
            service
                .repair_all_missing_claims(a.oi("now"), a.i_or("limit", 200))
                .await?
        ),
        "pending_claims" => json!(service.pending_claims(&a.s("giveaway_id")?).await?),
        "claim_status" => json!(service.claim_status(&a.s("giveaway_id")?).await?),
        "due_claims" => json!(
            service
                .due_claims(a.oi("now"), a.i_or("limit", 200))
                .await?
        ),
        "claim" => json!(
            service
                .claim(&a.s("giveaway_id")?, &a.s("user_id")?)
                .await?
        ),
        "skip_claim" => json!(
            service
                .skip_claim(&a.s("giveaway_id")?, &a.s("user_id")?, &a.s("staff_id")?,)
                .await?
        ),
        "expire_claim" => {
            let (gw, winners) = service
                .expire_claim(&a.s("giveaway_id")?, &a.s("user_id")?, a.oi("now"))
                .await?;
            json!([gw, winners])
        }

        // -- lifecycle ---------------------------------------------------
        "discard" => {
            service.discard(&a.s("giveaway_id")?).await?;
            Value::Null
        }
        "cancel" => json!(service.cancel(&a.s("giveaway_id")?).await?),
        "set_message" => {
            service
                .set_message(&a.s("giveaway_id")?, &a.s("message_id")?)
                .await?;
            Value::Null
        }
        "extend" => json!(
            service
                .extend(&a.s("giveaway_id")?, a.i("extra_seconds")?)
                .await?
        ),
        "set_entrants_role" => {
            service
                .set_entrants_role(&a.s("giveaway_id")?, a.os("role_id").as_deref())
                .await?;
            Value::Null
        }
        "ended_with_roles" => gws_json(&service.ended_with_roles(a.i_or("limit", 50)).await?),
        "wipe_stale_entries" => json!(service.wipe_stale_entries(a.oi("now")).await?),

        // -- blacklist ---------------------------------------------------
        "ban_info" => json!(
            service
                .ban_info(&a.s("guild_id")?, &a.s("user_id")?, a.oi("now"))
                .await?
        ),
        "ban_message" => json!(
            service
                .ban_message(&a.s("guild_id")?, &a.s("user_id")?, a.oi("now"))
                .await?
        ),
        "is_blacklisted" => json!(
            service
                .is_blacklisted(&a.s("guild_id")?, &a.s("user_id")?, a.oi("now"))
                .await?
        ),
        "blacklist_add" => json!(
            service
                .blacklist_add(
                    &a.s("guild_id")?,
                    &a.s("user_id")?,
                    a.i_or("hours", 0),
                    &a.os("reason").unwrap_or_default(),
                    &a.os("banned_by").unwrap_or_default(),
                    a.oi("now"),
                )
                .await?
        ),
        "blacklist_remove" => json!(
            service
                .blacklist_remove(&a.s("guild_id")?, &a.s("user_id")?)
                .await?
        ),
        "purge_expired_bans" => json!(service.purge_expired_bans(a.oi("now")).await?),
        "count_blacklisted" => json!(
            service
                .count_blacklisted(&a.s("guild_id")?, a.oi("now"))
                .await?
        ),
        "blacklist_list" => json!(
            service
                .blacklist_list(&a.s("guild_id")?, a.i_or("limit", 50), a.oi("now"))
                .await?
        ),
        "blacklist_list_detailed" => json!(
            service
                .blacklist_list_detailed(&a.s("guild_id")?, a.i_or("limit", 50), a.oi("now"))
                .await?
        ),

        // -- timeout penalty ---------------------------------------------
        "timeout_ban_remaining" => json!(
            service
                .timeout_ban_remaining(&a.s("guild_id")?, &a.s("user_id")?)
                .await?
        ),
        "apply_timeout_penalty" => json!(
            service
                .apply_timeout_penalty(
                    &a.s("guild_id")?,
                    &a.s("user_id")?,
                    a.i_or("giveaways", 3),
                    a.os("giveaway_id").as_deref(),
                )
                .await?
        ),
        "consume_timeout_ban" => json!(
            service
                .consume_timeout_ban(&a.s("guild_id")?, &a.s("user_id")?, &a.s("giveaway_id")?,)
                .await?
        ),
        "count_timeout_bans" => json!(service.count_timeout_bans(&a.s("guild_id")?).await?),
        "list_timeout_bans" => json!(
            service
                .list_timeout_bans(&a.s("guild_id")?, a.i_or("limit", 50))
                .await?
        ),
        "check_timeout_ban" => {
            let gw = a.gw("gw")?;
            service
                .check_timeout_ban(
                    &gw,
                    &a.s("user_id")?,
                    a.b("timed_out", false),
                    a.oi("snap_ban"),
                    match a.raw("snap_hit") {
                        Some(Value::Bool(flag)) => Some(*flag),
                        _ => None,
                    },
                )
                .await?;
            Value::Null
        }

        other => return Err(anyhow!("unknown op `{other}`")),
    })
}
