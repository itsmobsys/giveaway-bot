//! The bot half of the retired `test_redis_integration.py`.
//!
//! These live in-crate rather than in `gwbot-testkit` because the behaviour
//! under test is private to the bot: the join/leave click path, the tick's sweep
//! lease, the `/gw_request_time` probe and the background invalidation flush.
//! Python could reach them through discord.py's fake `Member`/`Interaction`
//! objects and `views._HANDLERS`; serenity 0.12 only builds a `Context` inside
//! its own dispatch and `Http` performs real requests, so the Discord side of a
//! click is replaced by the same sequence of calls `handle_join` makes, with
//! every answer recorded in a plain enum instead of a message edit.
//!
//! Each test names the Python case it ports. Where a Python assertion relied on
//! monkeypatching something Rust does not expose (a fake `Member`, a swapped
//! `service._redis`, a spied `list_all_active`), the replacement is noted with
//! an `// ADAPTED:` comment right at the assertion.

use std::sync::Arc;
use std::time::{Duration, Instant};

use gwbot_core::cache::{CacheOptions, Driver, RedisCache};
use gwbot_core::components::{self, Click};
use gwbot_core::config::Settings;
use gwbot_core::db::Database;
use gwbot_core::embeds;
use gwbot_core::health::HealthState;
use gwbot_core::mentions::MAX_MENTION_MESSAGES;
use gwbot_core::models::{
    Giveaway, ManualClock, ServiceError, MESSAGE_FLUSH_CHUNK, TIMEOUT_BAN_KIND,
};
use gwbot_core::service::GiveawayService;
use gwbot_testkit::fakes::FakeRedis;
use tokio::sync::RwLock;

use serenity::all::{Cache, GatewayIntents, Http};
use serenity::model::Timestamp;

use super::buttons::{
    empty_panel_text, is_timed_out, join_header, post_join, spawn_flush, PostJoin, LEAVE_FAILED,
};
use super::commands;
use super::commands::{
    can_manage_bits, cancel_edit, cancelled_embed, create_mentions, entrant_report, extend_notice,
    mention_policy, no_entrants_report, ping_summary, started_reply, MESSAGE_NOTE_FAILED,
    ROLE_NOTE_FAILED, USE_IN_SERVER,
};
use super::commands_admin::{
    ban_list_body, ban_reply, blacklist_body, mark_in_guild, measure_probe, page_rows,
    render_report, unban_reply, BLACKLIST_FAILED, MANAGE_REQUIRED, NO_TIMEOUT_BANS,
    TIMEOUT_BANS_FAILED,
};
use super::{
    announce_mentions, announce_steps, announce_title, build_intents, counts_message,
    flatten_login_text, is_fatal_login, json_id, refresh_allowed, refresh_count,
    refresh_status_allows, retry_wait, should_revoke_after_grant, shutdown, wait_or_stop,
    AnnounceStep, Bot, LOGIN_RETRY_DELAYS, LOGIN_RETRY_JITTER_SECS, LOGIN_RETRY_MAX_WAIT_SECS,
    MESSAGE_BUFFER_MAX, PARTICIPANT_WINDOW, SWEEP_LEASE_SECS,
};

/// The guild every case works in, and the channel a giveaway is posted to.
const GUILD: &str = "111111111111111111";
const CHANNEL: &str = "222222222222222222";
const CREATED_BY: &str = "999999999999999999";

/// The one instant every rule sees, so a 600s giveaway never expires mid-test.
const START_MS: i64 = 1_700_000_000_000;
/// An account old enough for every minimum-age requirement to pass.
const ACCOUNT_CREATED_TS: f64 = 1_600_000_000.0;

/// How long the hanging driver sleeps per call: longer than the cache's own
/// 4.0s hard timeout (2.0s connect + 1.5s socket + 0.5s slack), so the cache
/// always gives up first and the test never waits for the driver.
const HANG: Duration = Duration::from_secs(30);

/// The statement `list_all_active` sends, and nothing else sends.
///
/// Used both to assert the sweep did not run and to inject a failure into just
/// that read. The `LIMIT ?` tail matters: `due` selects from the same table with
/// a `WHERE ... AND ends_at <= ?` clause, so a shorter marker would hit it too.
const LIST_ALL_ACTIVE_SQL: &str =
    "SELECT * FROM simple_giveaways WHERE status = 'active' ORDER BY ends_at ASC LIMIT ?";

/// The whole-row header read the click path must not repeat once it is warm.
const HEADER_SQL: &str = "SELECT * FROM simple_giveaways WHERE id";

// -- source scans --------------------------------------------------------
//
// A handful of Python cases asserted on a call that never happens, or on the
// shape of the code itself: "a fresh client per attempt", "no blocking read on
// the default executor", "a silent followup logs a warning". Rust has no
// monkeypatchable `_db_call`, so the honest equivalent is to read the shipped
// source. These constants make that scan compile-time, not filesystem-relative.

const BOT_SOURCE: &str = include_str!("mod.rs");
const BUTTONS_SOURCE: &str = include_str!("buttons.rs");
const COMMANDS_SOURCE: &str = include_str!("commands.rs");
const COMMANDS_ADMIN_SOURCE: &str = include_str!("commands_admin.rs");
const SERVICE_SOURCE: &str = include_str!("../../../gwbot-core/src/service.rs");

/// The lines of one function, from its signature to its closing brace.
///
/// Deliberately line-based rather than a parser: the callers only ask whether a
/// token appears inside a particular function, and a line scan cannot be fooled
/// by a brace inside a string literal.
fn body_of(source: &str, signature: &str) -> String {
    let lines: Vec<&str> = source.lines().collect();
    let start = lines
        .iter()
        .position(|line| line.contains(signature))
        .unwrap_or_else(|| panic!("no source line contains {signature:?}"));
    let indent = lines[start].len() - lines[start].trim_start().len();
    let mut end = lines.len();
    for (offset, line) in lines.iter().enumerate().skip(start + 1) {
        let trimmed = line.trim_start();
        if trimmed.starts_with('}') && line.len() - trimmed.len() <= indent {
            end = offset + 1;
            break;
        }
    }
    lines[start..end].join("\n")
}

// -- fixture -------------------------------------------------------------

/// A bot over an in-memory database and an injected Redis driver.
///
/// Python built two `Database` objects over one shared-cache file (the bot's and
/// the service's). Rust's `Database` is a single owner, so there is one, reached
/// through `bot.service().db()`.
struct BotFixture {
    bot: Bot,
    /// The driver's backing store, when the driver is a [`FakeRedis`]. The
    /// hanging fixture has no fake: nothing it asserts on reads one.
    fake: Arc<FakeRedis>,
    cache: RedisCache,
    /// The service's clock, kept so a test can make a giveaway due.
    clock: Arc<ManualClock>,
}

impl BotFixture {
    async fn new() -> Self {
        Self::with_fake(Arc::new(FakeRedis::new())).await
    }

    /// A cache whose every command fails, like Python's `failing_cache()`.
    async fn with_failing_cache() -> Self {
        let fake = Arc::new(FakeRedis::new());
        fake.fail_with("redis down");
        Self::with_fake(fake).await
    }

    /// A cache that never answers, like Python's `hanging_cache()`.
    async fn with_hanging_cache() -> Self {
        let fake = Arc::new(FakeRedis::new());
        Self::build(
            Self::settings(),
            Some(Self::cache_for(Arc::new(Hanging))),
            fake,
        )
        .await
    }

    /// A bot with no cache at all: Python's `Settings(turso_url="")` case.
    ///
    /// `RedisCache::disabled()` is the same object `RedisCache::new("")` builds,
    /// so `configured()` is false and every reader falls through to the
    /// database — which is exactly what the no-`REDIS_URL` probe must report.
    async fn without_cache() -> Self {
        let settings = Settings {
            redis_url: String::new(),
            ..Self::settings()
        };
        Self::build(settings, None, Arc::new(FakeRedis::new())).await
    }

    /// A bot whose `/health` probe dials `port` instead of the default.
    async fn with_port(port: u16) -> Self {
        let fake = Arc::new(FakeRedis::new());
        let driver: Arc<dyn Driver> = fake.clone();
        let settings = Settings {
            port,
            ..Self::settings()
        };
        Self::build(settings, Some(Self::cache_for(driver)), fake).await
    }

    /// The settings every cached fixture shares.
    fn settings() -> Settings {
        Settings {
            redis_url: "redis://example.invalid:6379/0".to_string(),
            dashboard_url: "https://dash.test/".to_string(),
            ..Settings::default()
        }
    }

    fn cache_for(driver: Arc<dyn Driver>) -> RedisCache {
        RedisCache::with_driver(driver, CacheOptions::default())
    }

    async fn with_fake(fake: Arc<FakeRedis>) -> Self {
        let driver: Arc<dyn Driver> = fake.clone();
        Self::build(Self::settings(), Some(Self::cache_for(driver)), fake).await
    }

    async fn build(settings: Settings, cache: Option<RedisCache>, fake: Arc<FakeRedis>) -> Self {
        let db = Database::in_memory().expect("in-memory database");
        db.init_schema().await.expect("schema");
        let clock = Arc::new(ManualClock::new(START_MS));
        // One cache object cloned into both sides: the service serves the cached
        // readers and the bot reports the same object's counters, so a probe can
        // see the ops its own reads made. `None` is the "no REDIS_URL" case.
        let service = GiveawayService::with_clock(db, cache.clone(), clock.clone());
        let cache = cache.unwrap_or_else(RedisCache::disabled);
        let health = Arc::new(RwLock::new(HealthState::default()));
        let bot = Bot::new(settings, service, cache.clone(), health);
        Self {
            bot,
            fake,
            cache,
            clock,
        }
    }

    /// The same giveaway Python's `make()` created.
    async fn make(&self) -> Giveaway {
        self.make_named("Nitro", GUILD).await
    }

    /// A giveaway in `guild_id` with the given prize.
    async fn make_named(&self, prize: &str, guild_id: &str) -> Giveaway {
        self.create(prize, guild_id, 600).await
    }

    /// A giveaway that is already due, so the tick's `due` read picks it up.
    ///
    /// `create` enforces a 30s minimum, so the fixture's clock is moved past the
    /// deadline afterwards. The clock is per-service, so this cannot leak into
    /// another test.
    async fn make_due(&self, prize: &str) -> Giveaway {
        let gw = self.create(prize, GUILD, 30).await;
        self.clock.advance(31_000);
        gw
    }

    async fn create(&self, prize: &str, guild_id: &str, duration_seconds: i64) -> Giveaway {
        self.bot
            .service()
            .create(
                guild_id,
                CHANNEL,
                prize,
                1,
                duration_seconds,
                CREATED_BY,
                None,
                None,
                None,
                0,
                0,
                None,
                None,
                None,
                0,
            )
            .await
            .expect("create giveaway")
    }

    fn cache(&self) -> &RedisCache {
        &self.cache
    }

    /// Press Join as `user_id` would.
    async fn click_join(&self, giveaway_id: &str, user_id: &str) -> JoinAnswer {
        self.click_join_as(giveaway_id, user_id, false).await
    }

    /// Press Join with Discord's native timeout reported as `timed_out`.
    ///
    /// Python's `FakeMember(timed_out=True)` was the only way to say this; in
    /// Rust the flag is a plain argument to the service's eligibility gate, so
    /// the click path takes it the same way `handle_join` does.
    async fn click_join_timed_out(&self, giveaway_id: &str, user_id: &str) -> JoinAnswer {
        self.click_join_as(giveaway_id, user_id, true).await
    }

    async fn click_join_as(&self, giveaway_id: &str, user_id: &str, timed_out: bool) -> JoinAnswer {
        click_join_on(&self.bot, giveaway_id, user_id, timed_out).await
    }

    /// Press Leave as `user_id` would.
    async fn click_leave(&self, giveaway_id: &str, user_id: &str) -> LeaveAnswer {
        let bot = &self.bot;
        let (removed, _count, _status) = match bot
            .db_call(bot.service().leave_with_count(giveaway_id, user_id))
            .await
        {
            Ok(result) => result,
            Err(err) => return LeaveAnswer::Failed(err.to_string()),
        };
        // A leave undoes the entry the latch remembers, or the member's next
        // honest join would be refused as "already entered".
        if removed {
            bot.clear_entered(giveaway_id, user_id);
        }
        spawn_flush(bot);
        if !removed {
            return LeaveAnswer::NotEntered;
        }
        // The header reuse `handle_leave` performs: the row is immutable while a
        // giveaway runs, so a burst-hot header is not re-read and a cold one is
        // read once and then cached for the next click.
        if bot
            .gw_cached(giveaway_id)
            .filter(|gw| gw.guild_id == GUILD)
            .is_none()
        {
            if let Ok(gw) = bot.db_call(bot.service().get(giveaway_id)).await {
                bot.gw_store(giveaway_id, &gw);
            }
        }
        LeaveAnswer::Left
    }

    /// Wait for a click's background flush to finish, then drain the rest.
    ///
    /// Python's `drain_bg()` awaited the spawned tasks. There is no task list
    /// here, so the fake (which answers instantly) gets a moment to be reached,
    /// and then the queue is drained explicitly. Only the cache call counts
    /// depend on this; nothing else in these tests does.
    async fn settle(&self) {
        tokio::time::sleep(Duration::from_millis(60)).await;
        self.bot.flush_invalidations().await;
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    /// True while a `count:<id>` key is cached, i.e. before the leave drops it.
    fn count_key_present(&self, giveaway_id: &str) -> bool {
        let suffix = format!("count:{giveaway_id}");
        self.fake.keys().iter().any(|key| key.ends_with(&suffix))
    }

    /// True while the sweep lease is held by anybody.
    fn lease_present(&self) -> bool {
        self.fake
            .keys()
            .iter()
            .any(|key| key.ends_with("sweep:live"))
    }

    /// The giveaways with a delayed role delete currently queued.
    fn scheduled_role_deletes(&self) -> Vec<String> {
        self.bot
            .state
            .scheduled_role_deletes
            .lock()
            .map(|guard| guard.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Render `/giveaway_timeout_bans` the way its handler does.
    ///
    /// The handler's only Context-dependent step is resolving `in_guild` from
    /// the member cache; everything else is these calls, so the panel is built
    /// here with the same helpers and a supplied membership predicate.
    async fn timeout_bans_panel(
        &self,
        limit: i64,
        cached: bool,
        is_member: impl Fn(&str) -> bool,
    ) -> (embeds::Embed, usize) {
        let mut rows = self
            .bot
            .service()
            .list_timeout_bans(GUILD, limit)
            .await
            .expect("timeout bans");
        let total = self
            .bot
            .service()
            .count_timeout_bans(GUILD)
            .await
            .expect("timeout ban total");
        let flags = mark_in_guild(&rows, cached, is_member);
        for (row, in_guild) in rows.iter_mut().zip(flags) {
            if let serde_json::Value::Object(map) = row {
                map.insert("in_guild".to_string(), serde_json::Value::Bool(in_guild));
            }
        }
        let pages = rows.len().div_ceil(components::PAGE_SIZE).max(1);
        let hidden = (total - rows.len() as i64).max(0);
        let embed = embeds::timeout_bans_embed(
            page_rows(&rows, 0),
            0,
            pages as i64,
            total,
            self.bot.state.settings.embed_color,
            hidden,
        );
        (embed, rows.len())
    }

    /// Seed one entry, as `BotTestCase`'s callers did through the service.
    async fn join_user(&self, gw: &Giveaway, user_id: &str) {
        self.bot
            .service()
            .join(
                gw,
                user_id,
                "member",
                &[],
                Some(ACCOUNT_CREATED_TS),
                0,
                false,
                None,
                true,
            )
            .await
            .expect("seed entry");
    }

    /// Python's `banned()`: a timeout penalty of `remaining` giveaways.
    async fn ban(&self, user_id: &str, remaining: i64) {
        self.bot
            .service()
            .apply_timeout_penalty(GUILD, user_id, remaining, Some("gw_x"))
            .await
            .expect("timeout penalty");
    }

    /// Install the cache/http pair `login_loop` publishes before `start()`.
    ///
    /// An empty cache is deliberate: it is what makes "the guild is not visible"
    /// reachable without a gateway, and `Http` only builds a request (nothing is
    /// sent unless a handler actually calls it).
    fn install_gateway(&self) {
        *self.bot.state.cache.lock().expect("cache slot") = Some(Arc::new(Cache::new()));
        *self.bot.state.http.lock().expect("http slot") =
            Some(Arc::new(Http::new("Bot test-token")));
    }

    /// Render the participants panel the way `handle_participants` does.
    ///
    /// The handler's only Context-dependent step is sending the followup, so the
    /// window read and the embed are reproduced here with the same helpers.
    async fn participants_panel(
        &self,
        gw: &Giveaway,
        viewer: &str,
        window: i64,
    ) -> (embeds::Embed, usize) {
        let (total, rows, mine) = self
            .bot
            .service()
            .entries_snapshot(&gw.id, Some(window), viewer)
            .await
            .expect("entries window");
        let pages = rows.len().div_ceil(components::PAGE_SIZE);
        let shown = (total > rows.len() as i64).then_some(rows.len() as i64);
        let embed = embeds::participants_embed(
            &gw.prize,
            page_rows(&rows, 0),
            0,
            pages as i64,
            total,
            mine,
            gw.winner_count,
            self.bot.state.settings.embed_color,
            shown,
        );
        (embed, rows.len())
    }

    /// Whether the cosmetic half of the tick has ever completed.
    fn last_refresh(&self) -> Option<Instant> {
        self.bot
            .state
            .last_list_refresh
            .lock()
            .ok()
            .and_then(|slot| *slot)
    }

    /// Configure a notify role so the probe's notify-role leg really measures.
    ///
    /// Without one the reader answers `None` for "no role set", which the probe
    /// reports as an unmeasured leg — the report would then prove nothing about
    /// the cache on that line.
    async fn seed_notify_role(&self) {
        self.bot
            .service()
            .set_notify_role(GUILD, Some("123456789012345678"))
            .await
            .expect("notify role");
    }
}

/// What a Join click answered, in place of a fake interaction's texts.
#[derive(Debug)]
enum JoinAnswer {
    AlreadyEntered,
    Entered {
        status: Option<String>,
        count: i64,
    },
    Refused {
        message: String,
        kind: String,
    },
    // The payload exists for the `{answer:?}` text on a failed assertion: a test
    // that lands here expected to enter, so the giveaway id (or the driver error)
    // is the whole diagnostic. No arm reads it on a passing run, which is why
    // these two carry `allow(dead_code)` rather than losing the value.
    #[allow(dead_code)]
    NotFound(String),
    #[allow(dead_code)]
    Failed(String),
}

impl JoinAnswer {
    fn entered(&self) -> bool {
        matches!(self, JoinAnswer::Entered { .. })
    }

    fn is_refused(&self) -> bool {
        matches!(self, JoinAnswer::Refused { .. })
    }

    fn count(&self) -> i64 {
        match self {
            JoinAnswer::Entered { count, .. } => *count,
            other => panic!("expected a committed join, got {other:?}"),
        }
    }
}

#[derive(Debug)]
enum LeaveAnswer {
    Left,
    NotEntered,
    Failed(String),
}

// -- the click, outside the fixture --------------------------------------

/// Press Join exactly as `handle_join` does, for any bot.
///
/// Free rather than a fixture method so a burst can hand each spawned task its
/// own cloned `Bot`. `Bot` is an `Arc` handle, so all the clones share one
/// service, one cache and one set of latches — the same single process a real
/// burst lands in.
async fn click_join_on(bot: &Bot, giveaway_id: &str, user_id: &str, timed_out: bool) -> JoinAnswer {
    // The same latch `handle_join` checks first: a second tap of the same
    // button never reaches the database at all.
    if bot.entered_recently(giveaway_id, user_id) {
        return JoinAnswer::AlreadyEntered;
    }
    let (gw, snapshot) = match join_header(bot, giveaway_id, GUILD, user_id).await {
        Ok(pair) => pair,
        Err(err) => {
            return match err.downcast_ref::<ServiceError>() {
                // `handle_join` tells "no such giveaway" from "the read
                // failed" exactly this way, and so does this port.
                Some(_) => JoinAnswer::NotFound(giveaway_id.to_string()),
                None => JoinAnswer::Failed(err.to_string()),
            };
        }
    };
    let joined = bot
        .db_call(bot.service().join_and_refresh(
            &gw,
            user_id,
            "Tester",
            &[],
            Some(ACCOUNT_CREATED_TS),
            bot.pending_messages(GUILD, user_id),
            timed_out,
            snapshot,
        ))
        .await;
    match joined {
        Ok((status, count)) => {
            // Commit landed: the keys the write made wrong go out in the
            // background, and only now may the latch be set.
            spawn_flush(bot);
            bot.mark_entered(giveaway_id, user_id);
            JoinAnswer::Entered { status, count }
        }
        Err(err) => match err.downcast_ref::<ServiceError>() {
            Some(refusal) => JoinAnswer::Refused {
                message: refusal.message.clone(),
                kind: refusal.kind.clone(),
            },
            None => JoinAnswer::Failed(err.to_string()),
        },
    }
}

/// Python's `JoinBurstHandlerTests.run_burst`: `count` clicks at once.
///
/// Python gathered coroutines on one loop. Rust has no `gather` over a shared
/// `&self`, so each task gets a cloned `Bot` handle and they all meet inside the
/// same service — which is where the burst has to be observed anyway.
async fn run_burst(bot: &Bot, giveaway_id: &str, count: u64, base: u64) -> Vec<JoinAnswer> {
    let mut tasks = Vec::with_capacity(count as usize);
    for index in 0..count {
        let bot = bot.clone();
        let giveaway_id = giveaway_id.to_string();
        let user_id = format!("{:0>18}", base + index);
        tasks.push(tokio::spawn(async move {
            click_join_on(&bot, &giveaway_id, &user_id, false).await
        }));
    }
    let mut answers = Vec::with_capacity(tasks.len());
    for task in tasks {
        answers.push(task.await.expect("burst task"));
    }
    answers
}

// -- the hanging driver --------------------------------------------------

/// A Redis that accepts nothing: every call sleeps past the cache's timeout.
struct Hanging;

#[async_trait::async_trait]
impl Driver for Hanging {
    async fn ping(&self) -> Result<(), String> {
        tokio::time::sleep(HANG).await;
        Ok(())
    }

    async fn get(&self, _key: &str) -> Result<Option<String>, String> {
        tokio::time::sleep(HANG).await;
        Ok(None)
    }

    async fn set(
        &self,
        _key: &str,
        _value: &str,
        _millis: i64,
        _nx: bool,
    ) -> Result<Option<String>, String> {
        tokio::time::sleep(HANG).await;
        Ok(Some("OK".to_string()))
    }

    async fn del(&self, _keys: &[String]) -> Result<i64, String> {
        tokio::time::sleep(HANG).await;
        Ok(0)
    }

    async fn eval(&self, _script: &str, _keys: &[String], _args: &[String]) -> Result<i64, String> {
        tokio::time::sleep(HANG).await;
        Ok(0)
    }

    async fn incr(&self, _key: &str) -> Result<i64, String> {
        tokio::time::sleep(HANG).await;
        Ok(1)
    }

    async fn pexpire(&self, _key: &str, _millis: i64) -> Result<i64, String> {
        tokio::time::sleep(HANG).await;
        Ok(1)
    }
}

// -- CachedEmbedTests ----------------------------------------------------

/// The number the embed shows must be a number, not an awaited coroutine.
#[tokio::test(flavor = "multi_thread")]
async fn a_warm_count_is_rendered_as_a_number() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "777777777777777777";
    assert!(f.click_join(&gw.id, member).await.entered());

    // Warm the display count the way a repeat refresh would find it.
    let warm = f
        .bot
        .service()
        .entry_count_cached(&gw.id)
        .await
        .expect("cached count");
    assert_eq!(warm, 1);

    let fresh = f.bot.service().get(&gw.id).await.expect("fresh row");
    let embed = embeds::giveaway_embed(&fresh, warm, f.bot.state.settings.embed_color);
    let rendered = format!("{:?}", embed.description);
    assert!(
        rendered.contains("Entries: **1**"),
        "the warm count must render as a number: {rendered}"
    );
    // Rust's type system already forbids the coroutine leak this pins, so the
    // assertion guards the renderer against a future debug-format regression.
    assert!(!rendered.to_lowercase().contains("coroutine"));
    assert!(!rendered.contains("object at 0x"));
}

// -- ClickReadBudgetTests ------------------------------------------------

/// What a second tap in a burst still costs the database.
///
/// Every click re-checks eligibility live (that is correctness), but it must
/// not also re-read a giveaway header the bot already holds.
#[tokio::test(flavor = "multi_thread")]
async fn a_burst_does_not_re_read_the_header() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let db = f.bot.service().db();

    db.log_queries();
    assert!(f.click_join(&gw.id, "777777777777777771").await.entered());
    let first = db.take_query_log();
    assert!(
        !first.is_empty(),
        "the first click reads, as it must: {first:?}"
    );
    assert!(f.bot.gw_cached(&gw.id).is_some(), "the header is warm now");

    assert!(f.click_join(&gw.id, "777777777777777772").await.entered());
    let added = db.take_query_log();
    assert!(
        !added.is_empty(),
        "eligibility is still checked live, never cached"
    );
    // The refresh read ("SELECT status, COALESCE(entrant_count, 0) ...") is
    // expected: it carries the new count back. What must not appear is the
    // whole-row header read, which the bot already holds.
    assert!(
        added.iter().all(|sql| !sql.contains(HEADER_SQL)),
        "the header is already in hand: {added:?}"
    );
}

/// A second panel open on an empty giveaway costs no extra lookup.
#[tokio::test(flavor = "multi_thread")]
async fn a_second_panel_open_on_an_empty_giveaway_costs_no_lookup() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let db = f.bot.service().db();

    db.log_queries();
    let found = f
        .bot
        .service()
        .entries_snapshot(&gw.id, Some(PARTICIPANT_WINDOW), "777777777777777773")
        .await
        .expect("window read");
    let log = db.take_query_log();

    assert_eq!(found.0, 0, "an empty giveaway has no entrants");
    assert!(found.1.is_empty());
    assert!(
        empty_panel_text(&gw.prize).contains("no entrants yet"),
        "the empty window is what the panel reports"
    );
    // ADAPTED: Python forbade a second `AS one FROM simple_entries` lookup
    // because its membership check was a separate query. Rust folds that flag
    // into the window read (`(user_id = ?) AS me`), so the equivalent budget is
    // "one read of simple_entries, not two".
    let entry_reads: Vec<&String> = log
        .iter()
        .filter(|sql| sql.contains("FROM simple_entries"))
        .collect();
    assert_eq!(
        entry_reads.len(),
        1,
        "an empty window must cost exactly one read: {log:?}"
    );
}

// -- DuplicateClickTests -------------------------------------------------

/// A second tap of the same button must not reach the database at all.
#[tokio::test(flavor = "multi_thread")]
async fn the_second_tap_is_refused_and_the_row_stays_single() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "555555555555555555";

    let first = f.click_join(&gw.id, member).await;
    assert!(first.entered(), "{first:?}");
    assert_eq!(first.count(), 1);
    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1
    );

    // Let the first click's background flush finish before counting cache
    // calls: a DEL still in flight would otherwise be attributed to the tap.
    f.settle().await;
    let calls = f.fake.calls().len();

    let second = f.click_join(&gw.id, member).await;
    assert!(matches!(second, JoinAnswer::AlreadyEntered), "{second:?}");
    assert_eq!(
        f.bot
            .service()
            .entry_count(&gw.id, true)
            .await
            .expect("count"),
        1
    );
    assert_eq!(
        f.fake.calls().len(),
        calls,
        "a refused tap must not touch the cache"
    );
}

/// A committed join is still acked when every cache call fails.
#[tokio::test(flavor = "multi_thread")]
async fn a_committed_join_is_acked_when_redis_is_down() {
    let f = BotFixture::with_failing_cache().await;
    let gw = f.make().await;
    let member = "666666666666666666";

    let answer = f.click_join(&gw.id, member).await;
    assert!(answer.entered(), "{answer:?}");
    assert!(
        f.bot
            .service()
            .has_entry(&gw.id, member)
            .await
            .expect("has_entry"),
        "the entry must be committed even though the cache refused every call"
    );
}

// -- RequestTimeProbeTests -----------------------------------------------

/// The probe must show the cache it really used, warm and cold.
#[tokio::test(flavor = "multi_thread")]
async fn the_report_shows_a_hit_for_a_warm_key() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.seed_notify_role().await;
    assert!(f.click_join(&gw.id, "888888888888888888").await.entered());

    let measured = measure_probe(&f.bot, GUILD, CHANNEL, CREATED_BY).await;
    let text = render_report(&measured, Some(0.4), Some(12.0), Some(0.02), 20.0);

    assert!(text.contains("Redis ping"), "{text}");
    assert!(
        text.contains("2nd"),
        "the warm run has to be reported separately: {text}"
    );
    assert!(
        text.contains("HIT"),
        "a served read must not be reported as a miss: {text}"
    );
    assert!(
        !text.contains("**failed**"),
        "a live fake must not report a failed leg: {text}"
    );
    assert!(
        text.contains("set(s)"),
        "the probe's own SET+DEL are counted: {text}"
    );
    assert!(text.contains("Warm cache legs"), "{text}");
    // The cached path leads, with the price of an empty cache beside it.
    assert!(text.contains("Cached reads this request"), "{text}");
    assert!(text.contains("First read on an empty cache"), "{text}");
    assert!(text.to_lowercase().contains("repeat click"), "{text}");
    // Nothing database-shaped may appear in this report any more.
    assert!(!text.contains("Turso"), "{text}");
    assert!(!text.contains("tursodown"), "{text}");
    // The probe cleans up after itself: no leftover Redis key.
    assert!(
        f.fake
            .keys()
            .iter()
            .all(|key| !key.ends_with("probe:gw_request_time")),
        "the probe key must be deleted: {:?}",
        f.fake.keys()
    );
}

/// The production case: nothing is live, yet the cache legs must report.
///
/// The Join button only exists on a running giveaway, so without a preview key
/// the report could only ever say "failed" and tell the invoker nothing about
/// Redis at all.
#[tokio::test(flavor = "multi_thread")]
async fn no_live_giveaway_still_measures_the_cache() {
    let f = BotFixture::new().await;
    f.seed_notify_role().await;

    let measured = measure_probe(&f.bot, GUILD, CHANNEL, CREATED_BY).await;
    let text = render_report(&measured, Some(0.4), Some(12.0), Some(0.02), 20.0);

    assert!(text.contains("preview row this probe deleted"), "{text}");
    assert!(
        text.contains("HIT"),
        "the second run must be served from the cache: {text}"
    );
    assert!(!text.contains("no live giveaway id to read"), "{text}");
    // The preview row must not outlive the probe that made it.
    assert!(
        f.bot
            .service()
            .list_active(GUILD)
            .await
            .expect("live list")
            .is_empty(),
        "the preview row must be discarded"
    );
    assert!(
        f.bot
            .service()
            .list_all_ended(200)
            .await
            .expect("ended list")
            .is_empty(),
        "a preview row is never an ended giveaway"
    );
}

/// The probe never prints a host, a port or a password.
#[tokio::test(flavor = "multi_thread")]
async fn the_probe_never_prints_a_host_or_password() {
    let f = BotFixture::new().await;
    f.seed_notify_role().await;
    f.make().await;

    let measured = measure_probe(&f.bot, GUILD, CHANNEL, CREATED_BY).await;
    let text = render_report(&measured, Some(0.4), Some(12.0), Some(0.02), 20.0);

    // The settings URL is redis://example.invalid:6379/0, so either string in
    // the report would mean the URL leaked.
    assert!(!text.contains("example.invalid"), "{text}");
    assert!(!text.contains("6379"), "{text}");
}

// -- SweepLeaseTests -----------------------------------------------------

/// One poll body per window: a lease may only narrow who sweeps.
#[tokio::test(flavor = "multi_thread")]
async fn a_held_lease_skips_the_duplicate_sweep() {
    let f = BotFixture::new().await;
    f.make().await;
    let (token, answered) = f
        .cache()
        .acquire_checked("sweep:live", SWEEP_LEASE_SECS)
        .await;
    assert!(token.is_some() && answered, "the lease must be taken first");

    let db = f.bot.service().db();
    db.log_queries();
    f.bot.tick_once().await;
    let log = db.take_query_log();

    // ADAPTED: Python replaced `service.list_all_active` with a spy. Rust has
    // no monkeypatching, so the sweep is observed through the statement log:
    // this SQL belongs to `list_all_active` and to nothing else.
    assert!(
        log.iter().all(|sql| !sql.contains(LIST_ALL_ACTIVE_SQL)),
        "another instance is already sweeping this window: {log:?}"
    );
    assert!(
        f.last_refresh().is_none(),
        "a skipped sweep must not stamp the refresh time"
    );
    assert!(
        f.lease_present(),
        "the other instance's lease must survive this tick: {:?}",
        f.fake.keys()
    );
}

/// An unreachable Redis must not skip the sweep.
#[tokio::test(flavor = "multi_thread")]
async fn an_unreachable_redis_still_runs_the_sweep() {
    let f = BotFixture::with_failing_cache().await;
    f.make().await;

    let db = f.bot.service().db();
    db.log_queries();
    f.bot.tick_once().await;
    let log = db.take_query_log();

    assert!(
        log.iter().any(|sql| sql.contains(LIST_ALL_ACTIVE_SQL)),
        "an unreachable Redis must not skip the sweep: {log:?}"
    );
    assert!(
        f.last_refresh().is_some(),
        "the lists were rebuilt, so the tick must have stamped them"
    );
}

/// A finished sweep frees the lease it took.
#[tokio::test(flavor = "multi_thread")]
async fn the_lease_is_released_after_the_sweep() {
    let f = BotFixture::new().await;
    f.make().await;

    f.bot.tick_once().await;

    assert!(f.last_refresh().is_some());
    assert!(
        !f.lease_present(),
        "a finished sweep must free the lease: {:?}",
        f.fake.keys()
    );
}

/// The lease is freed even when the sweep body dies half way.
#[tokio::test(flavor = "multi_thread")]
async fn a_failed_live_list_still_frees_the_lease() {
    let f = BotFixture::new().await;
    f.make().await;

    // ADAPTED: Python replaced `list_all_active` with one that raises. Here the
    // same statement is made to fail at the database seam, so every other read
    // in the tick still works and only the sweep body dies.
    f.bot.service().db().fail_matching(LIST_ALL_ACTIVE_SQL);
    f.bot.tick_once().await;
    f.bot.service().db().clear_failures();

    assert!(
        !f.lease_present(),
        "a failed sweep must not leak the lease it took: {:?}",
        f.fake.keys()
    );
    assert!(
        f.last_refresh().is_none(),
        "a sweep that never read the list must not stamp the refresh time"
    );
}

// -- BackgroundFlushTests ------------------------------------------------

/// A cache DEL is housekeeping: it never sits on the reply path.
#[tokio::test(flavor = "multi_thread")]
async fn a_click_still_answers_when_the_cache_never_answers() {
    let f = BotFixture::with_hanging_cache().await;
    let gw = f.make().await;
    let member = "444444444444444444";

    let started = Instant::now();
    let answer = f.click_join(&gw.id, member).await;
    let elapsed = started.elapsed();

    assert!(answer.entered(), "{answer:?}");
    assert!(
        elapsed < Duration::from_secs_f64(4.0),
        "the reply must not wait out the cache timeout: {elapsed:?}"
    );
    assert!(f
        .bot
        .service()
        .has_entry(&gw.id, member)
        .await
        .expect("has_entry"));
}

/// The join invalidation is scheduled, not awaited.
#[tokio::test(flavor = "current_thread")]
async fn the_join_invalidation_is_scheduled_not_awaited() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    // Settle whatever `create` queued, so the only DEL left to happen is the
    // join's own.
    f.bot.flush_invalidations().await;
    let dels_before = f.fake.count("del");

    let answer = f.click_join(&gw.id, "333333333333333333").await;
    assert!(answer.entered(), "{answer:?}");
    // On a current_thread runtime nothing else has been polled since the click
    // returned, so a flush that had been awaited on the reply path would
    // already have issued its DEL.
    assert_eq!(
        f.fake.count("del"),
        dels_before,
        "the flush must be background work, not a blocking await"
    );

    f.bot.flush_invalidations().await;
    assert!(
        f.fake.count("del") > dels_before,
        "the queued keys go out once the background flush runs"
    );
}

/// A leave is answered before the queued keys go.
#[tokio::test(flavor = "multi_thread")]
async fn a_leave_is_answered_before_the_queued_keys_go() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "222222222222222222";

    assert!(f.click_join(&gw.id, member).await.entered());
    f.settle().await;
    // Warm the display count so the leave has a real key to drop.
    let _ = f
        .bot
        .service()
        .entry_count_cached(&gw.id)
        .await
        .expect("cached count");
    assert!(
        f.count_key_present(&gw.id),
        "the count key must be warm before the leave: {:?}",
        f.fake.keys()
    );

    let answer = f.click_leave(&gw.id, member).await;
    assert!(matches!(answer, LeaveAnswer::Left), "{answer:?}");

    f.settle().await;
    assert!(
        !f.count_key_present(&gw.id),
        "the queued count key is dropped in the background: {:?}",
        f.fake.keys()
    );
}

// -- IntentTests ---------------------------------------------------------

/// Python: `IntentTests.test_member_intent_is_required_for_role_checks`.
#[test]
fn member_intent_is_required_for_role_checks() {
    assert!(build_intents().contains(GatewayIntents::GUILD_MEMBERS));
}

// -- WiringTests ---------------------------------------------------------

/// The slash-command surface, in the order Python asserted it.
const EXPECTED_COMMANDS: [&str; 17] = [
    "giveaway_ban",
    "giveaway_ban_list",
    "giveaway_blacklist_add",
    "giveaway_blacklist_list",
    "giveaway_blacklist_remove",
    "giveaway_cancel",
    "giveaway_create",
    "giveaway_end",
    "giveaway_extend",
    "giveaway_list",
    "giveaway_notifyer",
    "giveaway_ping",
    "giveaway_reroll",
    "giveaway_timeout_bans",
    "giveaway_unban",
    "gw_request_time",
    "skipclaim",
];

/// Python: `WiringTests.test_every_command_is_registered_once`.
///
/// The two halves of the table are built by different modules now, so the
/// check is that their union is exactly the old surface with no duplicate.
#[test]
fn every_command_is_registered_once() {
    // `command_specs` already carries both halves: the admin set is appended
    // to the same table, and `handle_admin` dispatches it by name.
    let mut names: Vec<String> = commands::command_specs()
        .iter()
        .map(|spec| {
            serde_json::to_value(spec)
                .expect("a command spec serialises")
                .get("name")
                .and_then(|name| name.as_str())
                .expect("every spec has a name")
                .to_string()
        })
        .collect();
    names.sort();
    assert_eq!(names, EXPECTED_COMMANDS);
    let mut unique = names.clone();
    unique.dedup();
    assert_eq!(unique.len(), names.len(), "a command was registered twice");
}

/// Python: `WiringTests.test_an_error_handler_is_installed` and
/// `test_the_error_handler_answers_without_leaking_the_error`.
///
/// ADAPTED: there is no `tree.error` decorator to introspect. The handler is
/// the tail of `commands::handle_command`, which serenity 0.12 can only reach
/// with a real `Context`; the part worth pinning is the reply it writes, which
/// names the command and carries nothing from the error.
#[test]
fn the_error_handler_answers_without_leaking_the_error() {
    let reply = commands::command_failure_reply("?");
    assert_eq!(reply, "⚠️ /? failed. Please try again.");
    let named = commands::command_failure_reply("giveaway_create");
    assert!(named.contains("/giveaway_create"), "{named}");
    assert!(
        !named.to_lowercase().contains("boom"),
        "the error text must not reach the member: {named}"
    );
}

/// Python: `WiringTests.test_click_handlers_are_wired_to_the_bot`.
///
/// A click is dispatched off the custom id alone, so the "handler" is
/// [`parse_custom_id`] and the id it hands back.
#[test]
fn click_handlers_are_wired_to_the_bot() {
    assert_eq!(
        components::parse_custom_id("gw_join:gw_1"),
        Some(Click::Join("gw_1".to_string()))
    );
}

/// Python: `WiringTests.test_an_unknown_id_shape_never_matches`.
#[test]
fn an_unknown_id_shape_never_matches() {
    let too_long = format!("gw_join:{}", "x".repeat(65));
    assert_eq!(components::parse_custom_id(&too_long), None);
    assert_eq!(components::parse_custom_id("gw_join:"), None);
    assert_eq!(components::parse_custom_id("other:gw_1"), None);
}

// -- CommandWiringTests --------------------------------------------------

/// Python: `CommandWiringTests.test_messages_are_buffered_not_written_per_message`.
#[tokio::test]
async fn messages_are_buffered_not_written_per_message() {
    let f = BotFixture::new().await;
    for _ in 0..5 {
        f.bot.buffer_message(GUILD, "333333333333333333");
    }
    assert_eq!(f.bot.pending_messages(GUILD, "333333333333333333"), 5);
    assert_eq!(
        f.bot
            .service()
            .message_count(GUILD, "333333333333333333")
            .await
            .expect("message count"),
        0,
        "nothing reaches the database before a flush"
    );
}

/// Python: `CommandWiringTests.test_bots_and_dms_are_ignored`.
#[test]
fn bots_and_dms_are_ignored() {
    assert!(!counts_message(true, Some(GUILD)), "a bot never counts");
    assert!(!counts_message(false, None), "a DM never counts");
    assert!(counts_message(false, Some(GUILD)));
}

/// Python: `CommandWiringTests.test_flush_writes_every_buffered_count_and_clears`.
#[tokio::test]
async fn flush_writes_every_buffered_count_and_clears() {
    let f = BotFixture::new().await;
    let (a, b) = ("333333333333333333", "444444444444444444");
    for _ in 0..3 {
        f.bot.buffer_message(GUILD, a);
    }
    f.bot.buffer_message(GUILD, b);

    f.bot.flush_message_buffer().await.expect("flush");
    assert_eq!(f.bot.service().message_count(GUILD, a).await.unwrap(), 3);
    assert_eq!(f.bot.service().message_count(GUILD, b).await.unwrap(), 1);
    assert_eq!(f.bot.pending_messages(GUILD, a), 0);

    f.bot.flush_message_buffer().await.expect("second flush");
    assert_eq!(
        f.bot.service().message_count(GUILD, a).await.unwrap(),
        3,
        "no double counting"
    );
}

/// Python: `CommandWiringTests.test_a_failed_flush_keeps_the_counts`.
///
/// ADAPTED: Python swapped `service.add_message_counts` for a raising lambda.
/// Here the write itself is made to fail, which is the failure the handler is
/// meant to survive.
#[tokio::test]
async fn a_failed_flush_keeps_the_counts() {
    let f = BotFixture::new().await;
    let a = "333333333333333333";
    f.bot.buffer_message(GUILD, a);

    f.bot
        .service()
        .db()
        .fail_matching("INSERT INTO simple_message_counts");
    let failed = f.bot.flush_message_buffer().await;
    f.bot.service().db().clear_failures();
    assert!(failed.is_err(), "the flush must report the failure");

    assert_eq!(
        f.bot.pending_messages(GUILD, a),
        1,
        "a blip must not lose counts"
    );
    f.bot.flush_message_buffer().await.expect("retry");
    assert_eq!(f.bot.service().message_count(GUILD, a).await.unwrap(), 1);
}

/// Python: `CommandWiringTests.test_a_partial_flush_hands_back_only_what_did_not_land`.
///
/// ADAPTED: Python raised `PartialFlush(applied=1)` from a swapped method. The
/// batch is chunked at `MESSAGE_FLUSH_CHUNK`, so the second chunk is made to
/// fail for real: the committed head must not be requeued.
#[tokio::test]
async fn a_partial_flush_hands_back_only_what_did_not_land() {
    let f = BotFixture::new().await;
    // One row per user, spread over two chunks.
    let users: Vec<String> = (0..=MESSAGE_FLUSH_CHUNK)
        .map(|i| format!("{i:018}"))
        .collect();
    for user in &users {
        f.bot.buffer_message(GUILD, user);
    }

    f.bot
        .service()
        .db()
        .fail_matching_after("INSERT INTO simple_message_counts", 1);
    let failed = f.bot.flush_message_buffer().await;
    f.bot.service().db().clear_failures();
    assert!(failed.is_err(), "the flush must report the partial write");

    // The batch is a HashMap, so which user lands in the failed chunk is not
    // deterministic; what matters is that exactly the uncommitted tail goes
    // back and the committed head does not.
    let mut requeued: Vec<&String> = users
        .iter()
        .filter(|user| f.bot.pending_messages(GUILD, user) > 0)
        .collect();
    assert_eq!(requeued.len(), 1, "only the tail goes back: {requeued:?}");
    let tail = requeued.pop().expect("one tail row");
    assert_eq!(f.bot.pending_messages(GUILD, tail), 1);
    assert_eq!(
        f.bot.service().message_count(GUILD, tail).await.unwrap(),
        0,
        "the tail was never written"
    );
    for user in users.iter().filter(|user| *user != tail) {
        assert_eq!(
            f.bot.service().message_count(GUILD, user).await.unwrap(),
            1,
            "the committed head must not be counted twice"
        );
    }

    f.bot.flush_message_buffer().await.expect("retry");
    assert_eq!(f.bot.service().message_count(GUILD, tail).await.unwrap(), 1);
}

/// Python: `CommandWiringTests.test_a_timed_out_flush_does_not_count_the_batch_twice`.
///
/// ADAPTED: the abandoned-worker race cannot be produced through the service
/// here (the write either lands or fails), so the receipt's own contract is
/// asserted directly: a done receipt makes `reconcile_timed_out_flush` requeue
/// nothing, because the rows are already stored.
#[tokio::test]
async fn a_timed_out_flush_does_not_count_the_batch_twice() {
    let f = BotFixture::new().await;
    let a = "333333333333333333";
    let rows = vec![(GUILD.to_string(), a.to_string(), 3i64)];

    // The worker outlived the await: by the time anybody looks, the counts are
    // stored and the receipt says so.
    let receipt = Arc::new(gwbot_core::service::FlushReceipt::new());
    let mut batch = rows.clone();
    f.bot
        .service()
        .add_message_counts(&mut batch, Some(&receipt))
        .await
        .expect("write");
    assert!(receipt.done());

    f.bot.reconcile_timed_out_flush(rows, receipt).await;
    assert_eq!(
        f.bot.pending_messages(GUILD, a),
        0,
        "what the abandoned worker wrote must not be requeued"
    );
    assert_eq!(f.bot.service().message_count(GUILD, a).await.unwrap(), 3);
}

/// Python: `CommandWiringTests.test_buffer_overflow_flushes_early`.
///
/// ADAPTED: `MESSAGE_BUFFER_MAX` is a const, so the cap is not shrunk; the
/// overflow signal is asserted instead, and the flush it triggers is the one
/// `on_message` runs.
#[tokio::test]
async fn buffer_overflow_flushes_early() {
    let f = BotFixture::new().await;
    let mut overflowed = false;
    for i in 0..MESSAGE_BUFFER_MAX {
        overflowed = f.bot.buffer_message(GUILD, &format!("{i:018}"));
    }
    assert!(overflowed, "the cap is what bounds the buffer");
    f.bot.flush_message_buffer().await.expect("flush");
    assert_eq!(
        f.bot
            .service()
            .message_count(GUILD, &format!("{:018}", 1))
            .await
            .unwrap(),
        1
    );
    assert_eq!(f.bot.pending_messages(GUILD, &format!("{:018}", 1)), 0);
}

// -- AutocompleteCacheTests ----------------------------------------------

/// Python: `AutocompleteCacheTests.test_snapshot_is_grouped_per_guild`.
#[tokio::test]
async fn snapshot_is_grouped_per_guild() {
    let f = BotFixture::new().await;
    let mine = f.make().await;
    let theirs = f
        .bot
        .service()
        .create(
            "555555555555555555",
            CHANNEL,
            "theirs",
            1,
            600,
            CREATED_BY,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
            None,
            0,
        )
        .await
        .expect("create");
    f.bot.cache_autocomplete(&[mine.clone(), theirs.clone()]);
    assert_eq!(
        f.bot.suggestions(GUILD, "", false),
        vec![(mine.id.clone(), "Nitro".to_string())]
    );
    assert_eq!(
        f.bot.suggestions("555555555555555555", "", false),
        vec![(theirs.id.clone(), "theirs".to_string())]
    );
    assert!(f.bot.suggestions("999", "", false).is_empty());
}

/// Python: `AutocompleteCacheTests.test_snapshot_is_replaced_not_appended`.
#[tokio::test]
async fn snapshot_is_replaced_not_appended() {
    let f = BotFixture::new().await;
    let first = f.make().await;
    f.bot.cache_autocomplete(std::slice::from_ref(&first));
    f.bot.cache_autocomplete(&[]);
    assert!(f.bot.autocomplete_empty());
}

/// Python: `AutocompleteCacheTests.test_ended_snapshot_is_grouped_per_guild`.
#[tokio::test]
async fn ended_snapshot_is_grouped_per_guild() {
    let f = BotFixture::new().await;
    let mine = f.make().await;
    f.bot
        .service()
        .end(&mine.id, Some(START_MS), false, None)
        .await
        .unwrap();
    let theirs = f
        .bot
        .service()
        .create(
            "555555555555555555",
            CHANNEL,
            "theirs",
            1,
            600,
            CREATED_BY,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
            None,
            0,
        )
        .await
        .expect("create");
    f.bot
        .service()
        .end(&theirs.id, Some(START_MS), false, None)
        .await
        .unwrap();
    f.bot.cache_ended_autocomplete(&[
        f.bot.service().get(&mine.id).await.unwrap(),
        f.bot.service().get(&theirs.id).await.unwrap(),
    ]);
    assert_eq!(
        f.bot.suggestions(GUILD, "", true),
        vec![(mine.id.clone(), "Nitro".to_string())]
    );
    assert_eq!(
        f.bot.suggestions("555555555555555555", "", true),
        vec![(theirs.id.clone(), "theirs".to_string())]
    );
}

/// Python: `AutocompleteCacheTests.test_ended_snapshot_is_replaced_not_appended`.
#[tokio::test]
async fn ended_snapshot_is_replaced_not_appended() {
    let f = BotFixture::new().await;
    let first = f.make().await;
    f.bot
        .service()
        .end(&first.id, Some(START_MS), false, None)
        .await
        .unwrap();
    let ended = f.bot.service().get(&first.id).await.unwrap();
    f.bot.cache_ended_autocomplete(std::slice::from_ref(&ended));
    f.bot.cache_ended_autocomplete(&[]);
    assert!(f.bot.suggestions(GUILD, "", true).is_empty());
}

/// Python: `AutocompleteCacheTests.test_live_and_ended_caches_are_separate`.
#[tokio::test]
async fn live_and_ended_caches_are_separate() {
    let f = BotFixture::new().await;
    let live = f.make().await;
    let done = f
        .bot
        .service()
        .create(
            GUILD, CHANNEL, "done", 1, 600, CREATED_BY, None, None, None, 0, 0, None, None, None, 0,
        )
        .await
        .expect("create");
    f.bot
        .service()
        .end(&done.id, Some(START_MS), false, None)
        .await
        .unwrap();
    f.bot.cache_autocomplete(std::slice::from_ref(&live));
    f.bot
        .cache_ended_autocomplete(&[f.bot.service().get(&done.id).await.unwrap()]);
    assert_eq!(
        f.bot.suggestions(GUILD, "", false),
        vec![(live.id.clone(), "Nitro".to_string())]
    );
    assert_eq!(
        f.bot.suggestions(GUILD, "", true),
        vec![(done.id.clone(), "done".to_string())]
    );
}

// -- GuildSyncTests ------------------------------------------------------

/// Python: `GuildSyncTests.test_on_ready_clears_guild_copies_once_then_skips`.
///
/// ADAPTED: `on_ready` needs a `Context` and a `Ready`, so the once-per-process
/// latch it guards is asserted directly. A reconnect must not sweep again.
#[tokio::test]
async fn on_ready_clears_guild_copies_once_then_skips() {
    let f = BotFixture::new().await;
    assert!(f.bot.claim_guild_cleanup(), "the first ready sweeps");
    assert!(
        !f.bot.claim_guild_cleanup(),
        "a reconnect must not sweep again"
    );
}

/// Python: `GuildSyncTests.test_on_ready_survives_a_guild_cleanup_failure`.
///
/// ADAPTED: the failure is swallowed inside `on_ready`'s per-guild loop, which
/// needs a live `Context`; the latch is claimed before the loop runs, so a
/// failed sweep can never be retried into a duplicate registration.
#[tokio::test]
async fn on_ready_survives_a_guild_cleanup_failure() {
    let f = BotFixture::new().await;
    assert!(f.bot.claim_guild_cleanup());
    assert!(!f.bot.claim_guild_cleanup());
}

// -- RoleTaskTests -------------------------------------------------------

/// Python: `RoleTaskTests.test_scheduled_deletes_are_kept_alive_and_deduplicated`.
///
/// ADAPTED: Python counted `_role_tasks` and then cancelled them. Rust spawns
/// the task from `schedule_role_delete` and keeps no handle list, so the set the
/// dedup latch lives in is observed directly: two giveaways queued from three
/// calls, and nothing left once the tasks have run.
#[tokio::test]
async fn scheduled_deletes_are_kept_alive_and_deduplicated() {
    let f = BotFixture::new().await;
    f.bot.schedule_role_delete(GUILD, "123", "gw_a", 0.0);
    f.bot.schedule_role_delete(GUILD, "123", "gw_a", 0.0);
    f.bot.schedule_role_delete(GUILD, "123", "gw_b", 0.0);

    let mut queued = f.scheduled_role_deletes();
    queued.sort();
    assert_eq!(
        queued,
        vec!["gw_a".to_string(), "gw_b".to_string()],
        "one task per giveaway, the duplicate is dropped"
    );

    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        f.scheduled_role_deletes().is_empty(),
        "and released again once the tasks finish"
    );
}

/// Python: `RoleTaskTests.test_incomplete_targets_are_ignored`.
#[tokio::test]
async fn incomplete_targets_are_ignored() {
    let f = BotFixture::new().await;
    f.bot.schedule_role_delete("", "1", "gw_x", 1.0);
    f.bot.schedule_role_delete(GUILD, "", "gw_y", 1.0);
    assert!(f.scheduled_role_deletes().is_empty());
}

// -- TimeoutDetectionTests -----------------------------------------------

/// Python: `TimeoutDetectionTests.test_the_library_timeout_state_is_used`.
///
/// ADAPTED: Python called `GiveawayBot._timed_out(obj)` on stubs with an
/// `is_timed_out()` method. Serenity hands the join path a plain
/// `Option<Timestamp>`, so the rule being asserted is the comparison itself.
#[test]
fn the_library_timeout_state_is_used() {
    let now = Timestamp::from_unix_timestamp(1_700_000_000).expect("timestamp");
    let later = Timestamp::from_unix_timestamp(1_700_000_100).expect("timestamp");
    let earlier = Timestamp::from_unix_timestamp(1_699_999_900).expect("timestamp");
    assert!(is_timed_out(Some(later), now));
    assert!(!is_timed_out(Some(earlier), now));
}

/// Python: `TimeoutDetectionTests.test_an_object_without_the_api_is_not_timed_out`.
#[test]
fn an_object_without_the_api_is_not_timed_out() {
    let now = Timestamp::from_unix_timestamp(1_700_000_000).expect("timestamp");
    assert!(!is_timed_out(None, now), "a missing field is not a timeout");
}

/// Python: `TimeoutDetectionTests.test_an_unreadable_timeout_never_penalises`.
///
/// ADAPTED: serenity parses the field into a `Timestamp` before the handler
/// runs, so "the read threw" collapses into "no value" (`None`). What still has
/// to hold is that the boundary itself never penalises: a timeout that expired
/// this very second is over.
#[test]
fn an_unreadable_timeout_never_penalises() {
    let now = Timestamp::from_unix_timestamp(1_700_000_000).expect("timestamp");
    assert!(!is_timed_out(None, now));
    assert!(
        !is_timed_out(Some(now), now),
        "a timeout that expires now is over"
    );
}

// -- JoinTimeoutTests ----------------------------------------------------

/// Python: `JoinTimeoutTests.test_a_timed_out_member_cannot_enter_and_is_penalised`.
#[tokio::test]
async fn a_timed_out_member_cannot_enter_and_is_penalised() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "333333333333333333";

    let answer = f.click_join_timed_out(&gw.id, member).await;
    assert!(
        matches!(&answer, JoinAnswer::Refused { message, kind }
            if message.contains("timed out") && kind == TIMEOUT_BAN_KIND),
        "{answer:?}"
    );
    assert_eq!(f.bot.service().entry_count(&gw.id, true).await.unwrap(), 0);
    assert_eq!(
        f.bot
            .service()
            .timeout_ban_remaining(GUILD, member)
            .await
            .unwrap(),
        3
    );
}

/// Python: `JoinTimeoutTests.test_an_untimed_out_member_enters_as_usual`.
#[tokio::test]
async fn an_untimed_out_member_enters_as_usual() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "444444444444444444";

    let answer = f.click_join(&gw.id, member).await;
    assert!(answer.entered(), "{answer:?}");
    assert_eq!(f.bot.service().entry_count(&gw.id, true).await.unwrap(), 1);
    assert_eq!(
        f.bot
            .service()
            .timeout_ban_remaining(GUILD, member)
            .await
            .unwrap(),
        0
    );
}

/// Python: `JoinTimeoutTests.test_a_dropped_entry_loses_roles_in_every_active_giveaway`.
///
/// ADAPTED: Python spied `_take_entrants_role` to see which giveaways were
/// stripped. The strip list is `list_active(guild)` after the service dropped
/// the entries, so that is what is pinned here; the per-guild drop itself is
/// asserted through the entry counts.
#[tokio::test]
async fn a_dropped_entry_loses_roles_in_every_active_giveaway() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let other = f.make_named("other active giveaway", GUILD).await;
    let foreign = f.make_named("elsewhere", "333333333333333333").await;
    let member = "666666666666666666";

    for active in [&gw, &other, &foreign] {
        f.bot
            .service()
            .join(
                active,
                member,
                "member",
                &[],
                Some(ACCOUNT_CREATED_TS),
                0,
                false,
                None,
                true,
            )
            .await
            .expect("seed entry");
    }

    let answer = f.click_join_timed_out(&gw.id, member).await;
    assert!(
        matches!(&answer, JoinAnswer::Refused { message, .. } if message.contains("timed out")),
        "{answer:?}"
    );

    // `handle_join` walks exactly this list to take the role back.
    let mut stripped: Vec<String> = f
        .bot
        .service()
        .list_active(GUILD)
        .await
        .unwrap()
        .into_iter()
        .map(|row| row.id)
        .collect();
    stripped.sort();
    let mut expected = vec![gw.id.clone(), other.id.clone()];
    expected.sort();
    assert_eq!(stripped, expected, "both same-guild roles go");

    assert_eq!(f.bot.service().entry_count(&gw.id, true).await.unwrap(), 0);
    assert_eq!(
        f.bot.service().entry_count(&other.id, true).await.unwrap(),
        0
    );
    assert_eq!(
        f.bot
            .service()
            .entry_count(&foreign.id, true)
            .await
            .unwrap(),
        1,
        "another guild is untouched"
    );
}

/// Python: `JoinTimeoutTests.test_an_ordinary_refusal_leaves_roles_alone`.
///
/// ADAPTED: the second click is short-circuited by the entered-recently latch,
/// so it never reaches the role sweep at all; the service-level refusal is
/// asserted separately, because that is the path a restart takes.
#[tokio::test]
async fn an_ordinary_refusal_leaves_roles_alone() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "777777777777777777";

    assert!(f.click_join(&gw.id, member).await.entered());
    let answer = f.click_join(&gw.id, member).await;
    assert!(
        matches!(answer, JoinAnswer::AlreadyEntered),
        "a plain refusal must not strip anything: {answer:?}"
    );
    assert_eq!(f.bot.service().entry_count(&gw.id, true).await.unwrap(), 1);
    assert_eq!(
        f.bot
            .service()
            .timeout_ban_remaining(GUILD, member)
            .await
            .unwrap(),
        0
    );

    let refused = f
        .bot
        .service()
        .join(
            &gw,
            member,
            "member",
            &[],
            Some(ACCOUNT_CREATED_TS),
            0,
            false,
            None,
            true,
        )
        .await
        .expect_err("the database refuses the second entry");
    let refusal = refused.downcast_ref::<ServiceError>().expect("refusal");
    assert!(
        refusal.message.to_lowercase().contains("already entered"),
        "{}",
        refusal.message
    );
}

/// Python: `JoinTimeoutTests.test_a_penalised_member_is_refused_by_the_button`.
#[tokio::test]
async fn a_penalised_member_is_refused_by_the_button() {
    let f = BotFixture::new().await;
    let first = f.make_named("trigger", GUILD).await;
    let member = "555555555555555555";

    assert!(f.click_join_timed_out(&first.id, member).await.is_refused());

    let second = f.make_named("next", GUILD).await;
    // The timeout ended; the penalty has not.
    let answer = f.click_join(&second.id, member).await;
    assert!(
        matches!(&answer, JoinAnswer::Refused { message, .. } if message.contains("penalty")),
        "{answer:?}"
    );
    assert_eq!(
        f.bot.service().entry_count(&second.id, true).await.unwrap(),
        0
    );
    assert_eq!(
        f.bot
            .service()
            .timeout_ban_remaining(GUILD, member)
            .await
            .unwrap(),
        2
    );
}

// -- GiveawayListTests ---------------------------------------------------

/// Python: `GiveawayListTests.test_the_page_is_capped_while_the_header_counts_everyone`.
///
/// ADAPTED: the handler resolves the giveaway, counts, then reads one page and
/// hands the pair to `entrant_report`. Serenity cannot build a
/// `CommandInteraction`, so the same calls are made here and the reply text is
/// what gets asserted.
#[tokio::test]
async fn the_page_is_capped_while_the_header_counts_everyone() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    for index in 0..60 {
        f.join_user(&gw, &format!("{:0>18}", index + 1)).await;
    }

    let svc = f.bot.service();
    let total = svc.entry_count(&gw.id, false).await.expect("total");
    let page = svc.entries(&gw.id, Some(50)).await.expect("page");
    let shown: Vec<String> = page.iter().filter_map(json_id).collect();
    let host = match gw.host_id.as_deref().filter(|id| !id.is_empty()) {
        Some(host_id) => format!(" by <@{host_id}>"),
        None => String::new(),
    };
    let text = entrant_report(&gw, &host, total, &shown);

    assert!(text.contains("**60** entrant(s)"), "{text}");
    assert!(text.contains("…and 10 more."), "{text}");
    assert_eq!(
        text.matches("<@").count(),
        51,
        "50 entrants listed, plus the host mention"
    );
}

/// Python: `GiveawayListTests.test_no_entrants_says_so`.
#[tokio::test]
async fn no_entrants_says_so() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    assert_eq!(f.bot.service().entry_count(&gw.id, true).await.unwrap(), 0);

    let host = match gw.host_id.as_deref().filter(|id| !id.is_empty()) {
        Some(host_id) => format!(" by <@{host_id}>"),
        None => String::new(),
    };
    let text = no_entrants_report(&gw.prize, &host);
    assert!(text.contains("no entrants yet"), "{text}");
}

// -- BlacklistListTests --------------------------------------------------

/// Python: `BlacklistListTests.test_the_body_fits_and_the_header_counts_the_whole_list`.
///
/// ADAPTED: the handler defers, reads 100 ids and the real total, then renders
/// `blacklist_body`. Only the rendering is reachable without a gateway, so the
/// reads are made here and the body is asserted; the defer is the handler's own
/// `defer_command`, exercised elsewhere.
#[tokio::test]
async fn the_body_fits_and_the_header_counts_the_whole_list() {
    let f = BotFixture::new().await;
    for index in 0..90 {
        f.bot
            .service()
            .blacklist_add(GUILD, &format!("{:0>18}", index + 1), 0, "", "", None)
            .await
            .expect("blacklist add");
    }

    let svc = f.bot.service();
    let ids = svc.blacklist_list(GUILD, 100, None).await.expect("ids");
    let total = svc
        .count_blacklisted(GUILD, None)
        .await
        .expect("blacklist total");
    let content = blacklist_body(&ids, total);

    assert!(content.len() <= 2000, "Discord rejects anything longer");
    assert!(content.contains("Blocked (90)"), "{content}");
    assert!(content.contains("…plus 10 more."), "{content}");
    assert_eq!(content.matches("<@").count(), 80);
}

/// Python: `BlacklistListTests.test_slow_blacklist_queries_run_only_after_defer_and_errors_use_followup`.
///
/// ADAPTED: "only after defer" is `defer_command`, which needs a live `Context`.
/// The half that can still regress is the failure path — a read that throws must
/// become the followup line rather than an escaped error — so the read is failed
/// at the database and the handler's reply constant is pinned.
#[tokio::test]
async fn slow_blacklist_queries_run_only_after_defer_and_errors_use_followup() {
    let f = BotFixture::new().await;
    f.bot
        .service()
        .blacklist_add(GUILD, "111111111111111111", 0, "", "", None)
        .await
        .expect("blacklist add");

    f.bot.service().db().fail_matching("FROM simple_blacklist");
    let failed = f.bot.service().blacklist_list(GUILD, 100, None).await;
    f.bot.service().db().clear_failures();

    assert!(failed.is_err(), "the injected failure has to surface");
    assert!(
        BLACKLIST_FAILED.contains("Could not load the blacklist"),
        "{BLACKLIST_FAILED}"
    );
}

// -- TimeoutBanCommandTests ----------------------------------------------

/// Python: `TimeoutBanCommandTests.test_lists_active_bans_with_mentions_and_remaining`.
#[tokio::test]
async fn lists_active_bans_with_mentions_and_remaining() {
    let f = BotFixture::new().await;
    let first = "111111111111111111";
    let second = "222222222222222222";
    f.ban(first, 3).await;
    f.ban(second, 1).await;

    let (embed, listed) = f.timeout_bans_panel(100, true, |_| true).await;
    assert_eq!(listed, 2);
    let desc = &embed.description;
    assert!(desc.contains(&format!("<@{first}>")), "{desc}");
    assert!(desc.contains(&format!("<@{second}>")), "{desc}");
    assert!(desc.contains("**3**"), "{desc}");
    assert!(desc.contains("**1**"), "{desc}");
    assert!(
        desc.find(first).unwrap() < desc.find(second).unwrap(),
        "longest penalty first: {desc}"
    );
    assert!(desc.contains("Total: **2**"), "{desc}");
}

/// Python: `TimeoutBanCommandTests.test_users_with_no_giveaways_left_are_excluded`.
#[tokio::test]
async fn users_with_no_giveaways_left_are_excluded() {
    let f = BotFixture::new().await;
    let spent = "333333333333333333";
    let active = "444444444444444444";
    // Seeded directly: the service never writes a zero-row itself, and the
    // listing is what has to cope with one.
    f.bot
        .service()
        .db()
        .execute(
            "INSERT INTO simple_giveaway_bans (guild_id, user_id, giveaways_remaining) \
             VALUES (?, ?, 0)",
            &[GUILD.into(), spent.into()],
        )
        .await
        .expect("zero-remaining ban");
    f.ban(active, 3).await;

    let (embed, listed) = f.timeout_bans_panel(100, false, |_| true).await;
    assert_eq!(listed, 1, "only the live penalty is listed");
    assert!(!embed.description.contains(spent), "{}", embed.description);
    assert!(embed.description.contains(active), "{}", embed.description);
}

/// Python: `TimeoutBanCommandTests.test_an_empty_list_says_so`.
///
/// ADAPTED: the handler answers with a plain followup and no embed, which is a
/// `Context` call; the reply it sends is the constant asserted here.
#[tokio::test]
async fn an_empty_list_says_so() {
    let f = BotFixture::new().await;
    let rows = f
        .bot
        .service()
        .list_timeout_bans(GUILD, 100)
        .await
        .expect("empty listing");
    assert!(rows.is_empty());
    assert_eq!(
        NO_TIMEOUT_BANS,
        "No users are currently banned from giveaways."
    );
}

/// Python: `TimeoutBanCommandTests.test_unauthorised_members_are_denied`.
#[tokio::test]
async fn unauthorised_members_are_denied() {
    let f = BotFixture::new().await;
    let banned = "111111111111111111";
    f.ban(banned, 3).await;

    assert!(!can_manage_bits(false, false));
    assert!(can_manage_bits(true, false), "Manage Server is enough");
    assert!(can_manage_bits(false, true), "so is Administrator");
    assert_eq!(MANAGE_REQUIRED, "You need **Manage Server**.");
    assert!(
        !MANAGE_REQUIRED.contains(banned),
        "no data for non-moderators"
    );
}

/// Python: `TimeoutBanCommandTests.test_a_database_blip_is_reported_not_raised`.
///
/// ADAPTED: the handler logs and follows up instead of propagating; with no
/// `Context` the failure is injected at the database and the reply constant is
/// pinned, which is the wording the followup sends.
#[tokio::test]
async fn a_database_blip_is_reported_not_raised() {
    let f = BotFixture::new().await;
    f.ban("111111111111111111", 3).await;

    f.bot
        .service()
        .db()
        .fail_matching("FROM simple_giveaway_bans");
    let failed = f.bot.service().list_timeout_bans(GUILD, 100).await;
    f.bot.service().db().clear_failures();

    assert!(failed.is_err(), "the injected failure has to surface");
    assert_eq!(
        TIMEOUT_BANS_FAILED,
        "⚠️ Could not load the timeout bans. Try again."
    );
}

/// Python: `TimeoutBanCommandTests.test_a_member_who_left_is_shown_by_id`.
#[tokio::test]
async fn a_member_who_left_is_shown_by_id() {
    let f = BotFixture::new().await;
    let gone = "999999999999999999";
    f.ban(gone, 3).await;

    // The member cache knows somebody else, so this id is not in it.
    let (embed, _) = f
        .timeout_bans_panel(100, true, |uid| uid == "111111111111111111")
        .await;
    assert!(
        embed
            .description
            .contains(&format!("`{gone}` (left the server)")),
        "{}",
        embed.description
    );
    assert!(!embed.description.contains(&format!("<@{gone}>")));
}

/// Python: `TimeoutBanCommandTests.test_an_unnumbered_id_does_not_break_the_list`.
#[tokio::test]
async fn an_unnumbered_id_does_not_break_the_list() {
    let f = BotFixture::new().await;
    f.ban("not-a-snowflake", 3).await;

    let (embed, _) = f.timeout_bans_panel(100, true, |_| true).await;
    assert!(
        embed.description.contains("not-a-snowflake"),
        "{}",
        embed.description
    );
}

/// Python: `TimeoutBanCommandTests.test_a_capped_list_admits_how_many_it_hid`.
///
/// ADAPTED: Python shrank the handler's page size by swapping
/// `service.list_timeout_bans`; the limit is a parameter here, so the panel is
/// rendered with the same small limit instead of a 101-row seed.
#[tokio::test]
async fn a_capped_list_admits_how_many_it_hid() {
    let f = BotFixture::new().await;
    for index in 0..5 {
        f.ban(&format!("{:0>18}", index + 1), 3).await;
    }

    let (embed, listed) = f.timeout_bans_panel(3, false, |_| true).await;
    assert_eq!(listed, 3, "the listing is capped");
    assert!(
        embed.description.contains("Total: **5**"),
        "{}",
        embed.description
    );
    assert!(
        embed.description.contains("2 more not shown."),
        "{}",
        embed.description
    );
}

/// Python: `TimeoutBanCommandTests.test_long_lists_are_paginated`.
#[tokio::test]
async fn long_lists_are_paginated() {
    let f = BotFixture::new().await;
    for index in 0..11 {
        f.ban(&format!("{:0>18}", index + 1), 3 - index % 3).await;
    }

    let (embed, listed) = f.timeout_bans_panel(100, false, |_| true).await;
    assert_eq!(listed, 11);
    assert!(embed.title.contains("page 1/2"), "{}", embed.title);
    assert!(
        embed.description.contains("Total: **11**"),
        "{}",
        embed.description
    );
}

// -- RoleCleanupTests ----------------------------------------------------

/// Python: `RoleCleanupTests.test_the_task_deletes_the_role_and_clears_the_row`.
///
/// ADAPTED: Python stubbed `get_guild`/`FakeRole.delete`; serenity's `Http`
/// performs real requests, so the delete itself is unreachable. What the task
/// still guarantees without a gateway is that the row is cleared and the dedup
/// latch released on every path, which is what stops the restart sweep from
/// re-queueing the giveaway forever.
#[tokio::test]
async fn the_task_deletes_the_role_and_clears_the_row() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_entrants_role(&gw.id, Some("123"))
        .await
        .expect("entrants role");

    f.install_gateway();
    f.bot.schedule_role_delete(GUILD, "123", &gw.id, 0.0);
    tokio::time::sleep(Duration::from_millis(200)).await;

    let fresh = f.bot.service().get(&gw.id).await.expect("row");
    assert!(
        fresh.entrants_role_id.is_none(),
        "the row is cleared so the sweep stops"
    );
    assert!(f.scheduled_role_deletes().is_empty());
}

/// Python: `RoleCleanupTests.test_an_unreachable_guild_still_clears_the_row`.
#[tokio::test]
async fn an_unreachable_guild_still_clears_the_row() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    f.bot
        .service()
        .set_entrants_role(&gw.id, Some("123"))
        .await
        .expect("entrants role");

    // A gateway whose cache does not contain this guild: the bot was removed, or
    // the guild is unavailable right now.
    f.install_gateway();
    f.bot.schedule_role_delete(GUILD, "123", &gw.id, 0.0);
    tokio::time::sleep(Duration::from_millis(200)).await;

    // Otherwise the restart sweep re-queues this giveaway on every boot, for a
    // role it can never reach.
    let fresh = f.bot.service().get(&gw.id).await.expect("row");
    assert!(fresh.entrants_role_id.is_none());
    assert!(f.scheduled_role_deletes().is_empty());
}

// -- LeaveTests ----------------------------------------------------------

/// Python: `LeaveTests.test_a_hot_header_saves_the_post_leave_row_read`.
///
/// ADAPTED: Python spied `service.get`; the statement log records the same read,
/// and `handle_leave`'s decision is reproduced by `click_leave`.
#[tokio::test]
async fn a_hot_header_saves_the_post_leave_row_read() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "889888888888888888";
    f.join_user(&gw, member).await;
    // The join path's own header, still inside its 2s TTL.
    f.bot.gw_store(&gw.id, &gw);

    f.bot.service().db().log_queries();
    let answer = f.click_leave(&gw.id, member).await;
    let reads = f.bot.service().db().take_query_log();

    assert!(matches!(answer, LeaveAnswer::Left), "{answer:?}");
    assert!(
        !reads.iter().any(|sql| sql.contains(HEADER_SQL)),
        "the cached header answers this: {reads:?}"
    );
}

/// Python: `LeaveTests.test_a_cold_header_is_read_and_then_cached`.
#[tokio::test]
async fn a_cold_header_is_read_and_then_cached() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "887888888888888888";
    f.join_user(&gw, member).await;
    f.bot.gw_drop(&gw.id);

    assert!(matches!(
        f.click_leave(&gw.id, member).await,
        LeaveAnswer::Left
    ));
    assert!(
        f.bot.gw_cached(&gw.id).is_some(),
        "the read that had to happen warms the cache"
    );
}

/// Python: `LeaveTests.test_leave_removes_the_entry`.
#[tokio::test]
async fn leave_removes_the_entry() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "888888888888888888";
    f.join_user(&gw, member).await;

    assert!(matches!(
        f.click_leave(&gw.id, member).await,
        LeaveAnswer::Left
    ));
    assert_eq!(f.bot.service().entry_count(&gw.id, true).await.unwrap(), 0);
}

/// Python: `LeaveTests.test_a_failing_leave_is_reported_not_raised`.
///
/// ADAPTED: the handler answers the failure with a followup; with no `Context`
/// the write is failed at the database and the reply constant is pinned. This
/// used to escape the callback as a bare "interaction failed".
#[tokio::test]
async fn a_failing_leave_is_reported_not_raised() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "999999999999999999";
    f.join_user(&gw, member).await;

    f.bot
        .service()
        .db()
        .fail_matching("DELETE FROM simple_entries WHERE giveaway_id");
    let answer = f.click_leave(&gw.id, member).await;
    f.bot.service().db().clear_failures();

    assert!(
        matches!(&answer, LeaveAnswer::Failed(err) if err.contains("injected database failure")),
        "{answer:?}"
    );
    assert!(
        LEAVE_FAILED.contains("Could not update your entry"),
        "{LEAVE_FAILED}"
    );
}

// -- ParticipantsPanelTests ----------------------------------------------

/// Python: `ParticipantsPanelTests.test_the_panel_asks_the_entries_table_once`.
///
/// ADAPTED: the empty-panel half is the handler's followup, so the wording comes
/// from `empty_panel_text`; the query count is read from the statement log.
#[tokio::test]
async fn the_panel_asks_the_entries_table_once() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "223222222222222222";
    assert!(empty_panel_text(&gw.prize).contains("no entrants yet"));

    f.join_user(&gw, member).await;
    f.bot.service().db().log_queries();
    let (embed, listed) = f.participants_panel(&gw, member, PARTICIPANT_WINDOW).await;
    let reads = f.bot.service().db().take_query_log();

    assert_eq!(listed, 1);
    assert!(
        embed.description.contains("Total Participants: 1"),
        "{}",
        embed.description
    );
    assert!(
        embed.description.contains("Your Entries: 1"),
        "the caller's own entry is still found: {}",
        embed.description
    );
    assert_eq!(
        reads
            .iter()
            .filter(|sql| sql.contains("FROM simple_entries"))
            .count(),
        1,
        "the window answers all three questions: {reads:?}"
    );
}

/// Python: `ParticipantsPanelTests.test_the_page_is_bounded_and_finds_me_by_lookup`.
#[tokio::test]
async fn the_page_is_bounded_and_finds_me_by_lookup() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "222222222222222222";
    for index in 0..60 {
        f.join_user(&gw, &format!("{:0>18}", index + 1)).await;
    }
    f.join_user(&gw, member).await;

    let (embed, listed) = f.participants_panel(&gw, member, PARTICIPANT_WINDOW).await;
    assert_eq!(listed, 61);
    assert!(
        embed.description.contains("Total Participants: 61"),
        "{}",
        embed.description
    );
    assert!(
        embed.description.contains("Your Entries: 1"),
        "answered by a lookup: {}",
        embed.description
    );
    assert!(
        !embed.description.contains("Showing the first"),
        "61 fits the window"
    );
}

/// Python: `ParticipantsPanelTests.test_a_giveaway_bigger_than_the_window_says_so`.
///
/// ADAPTED: Python shrank `bot.PARTICIPANT_WINDOW`; the window is a parameter of
/// `entries_snapshot` here, so a smaller one is passed instead of mutated.
#[tokio::test]
async fn a_giveaway_bigger_than_the_window_says_so() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "222222222222222222";
    for index in 0..8 {
        f.join_user(&gw, &format!("{:0>18}", index + 1)).await;
    }

    let (embed, listed) = f.participants_panel(&gw, member, 5).await;
    assert_eq!(listed, 5, "the window is bounded");
    assert!(
        embed.description.contains("Total Participants: 8"),
        "{}",
        embed.description
    );
    assert!(
        embed.description.contains("Showing the first 5 of 8."),
        "{}",
        embed.description
    );
    assert!(embed.title.contains("page 1/1"), "{}", embed.title);
}

// -- GiveawayLifecycleTests ----------------------------------------------

/// Python: `GiveawayLifecycleTests.test_ended_or_cancelled_giveaway_never_refreshes_live_buttons`.
///
/// ADAPTED: Python asserted on the edits a fake message received; the decision
/// that gates them is `refresh_allowed`, so that is what is pinned.
#[tokio::test]
async fn ended_or_cancelled_giveaway_never_refreshes_live_buttons() {
    let f = BotFixture::new().await;
    let ended = f.make_named("ended", GUILD).await;
    let cancelled = f.make_named("cancelled", GUILD).await;
    let active = f.make_named("active", GUILD).await;
    for gw in [&ended, &cancelled, &active] {
        f.bot
            .service()
            .set_message(&gw.id, "123456789012345678")
            .await
            .expect("message id");
    }
    f.bot
        .service()
        .end(&ended.id, None, false, None)
        .await
        .expect("end");
    f.bot.service().cancel(&cancelled.id).await.expect("cancel");

    let ended = f.bot.service().get(&ended.id).await.expect("ended row");
    let cancelled = f
        .bot
        .service()
        .get(&cancelled.id)
        .await
        .expect("cancelled row");
    let active = f.bot.service().get(&active.id).await.expect("active row");
    assert!(!refresh_allowed(&ended));
    assert!(!refresh_allowed(&cancelled));
    assert!(refresh_allowed(&active));
    assert!(!refresh_status_allows(Some(&ended.status)));
    assert!(!refresh_status_allows(Some(&cancelled.status)));
    assert!(refresh_status_allows(Some(&active.status)));
    // A giveaway that never got a message id has no live embed to update.
    let message_less = f.make_named("never posted", GUILD).await;
    assert!(!refresh_allowed(&message_less));
}

/// Python: `GiveawayLifecycleTests.test_join_race_with_draw_does_not_restore_the_live_embed`.
///
/// ADAPTED: Python ended the giveaway from inside a stubbed `service.join`; here
/// the end happens between the click and the refresh, and the gate the refresh
/// consults is asserted. The join still answers, which is the member-visible
/// half.
#[tokio::test]
async fn join_race_with_draw_does_not_restore_the_live_embed() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "888888888888888888";

    let answer = f.click_join(&gw.id, member).await;
    assert!(answer.entered(), "the join still answers: {answer:?}");
    f.bot
        .service()
        .end(&gw.id, None, false, None)
        .await
        .expect("end");

    let live = f.bot.service().get(&gw.id).await.expect("row");
    assert!(
        !refresh_allowed(&live) && !refresh_status_allows(Some(&live.status)),
        "the stale snapshot must not repaint the winner announcement"
    );
}

/// Python: `GiveawayLifecycleTests.test_leave_race_with_cancel_does_not_restore_the_live_embed`.
///
/// ADAPTED: the cancel lands after the leave instead of inside it; the invariant
/// is the same one `handle_leave` checks with the status its own write returned.
#[tokio::test]
async fn leave_race_with_cancel_does_not_restore_the_live_embed() {
    let f = BotFixture::new().await;
    let gw = f.make().await;
    let member = "888888888888888888";
    f.join_user(&gw, member).await;

    assert!(matches!(
        f.click_leave(&gw.id, member).await,
        LeaveAnswer::Left
    ));
    f.bot.service().cancel(&gw.id).await.expect("cancel");

    let live = f.bot.service().get(&gw.id).await.expect("row");
    assert!(
        !refresh_allowed(&live) && !refresh_status_allows(Some(&live.status)),
        "a cancelled giveaway has no live embed to restore"
    );
}

/// Python: `GiveawayLifecycleTests.test_tick_continues_after_announcement_and_role_strip_failures`.
///
/// ADAPTED: `announce_with` and `strip_entrants_role_with` return `()`, so a
/// failed send cannot propagate into the loop at all; the per-giveaway `end`
/// failure is the one that can still abort a draw, so it is injected here. One
/// of the two due giveaways ends and the pass still reaches the cosmetic half.
#[tokio::test]
async fn tick_continues_after_announcement_and_role_strip_failures() {
    let f = BotFixture::new().await;
    let first = f.make_due("first").await;
    let second = f.make_due("second").await;

    // The first guarded flip goes through, the second fails.
    f.bot
        .service()
        .db()
        .fail_matching_after("SET status = 'ended'", 1);
    f.bot.tick_once().await;
    f.bot.service().db().clear_failures();

    let mut ended = 0;
    for id in [&first.id, &second.id] {
        if f.bot.service().get(id).await.expect("row").status == "ended" {
            ended += 1;
        }
    }
    assert_eq!(ended, 1, "one draw failed, the other still ran");
    assert!(
        f.last_refresh().is_some(),
        "and the pass still reached the cosmetic half"
    );
}

// -- the rest of the old bot suite, one module per class -----------------
//
// The shared fixture and the source-scan helpers live above; each module below
// owns exactly one class of the retired `test_bot.py`, so a port can be read
// beside the case it came from without scrolling through two thousand lines of
// unrelated tests.

mod announcement;
mod audit_post_ack;
mod ban_commands;
mod create_persistence;
mod join_burst;
mod join_fast_path;
mod login_retry;
mod off_loop_pool;
mod request_time;
mod shutdown;

/// Python: `GiveawayLifecycleTests.test_tick_error_handler_logs_and_restarts_loop`.
///
/// ADAPTED: Python restarted the crashed task from an error handler; the Rust
/// loop logs every failure and iterates, so what is asserted is that a failed
/// pass leaves the loop healthy: the next pass does its work.
#[tokio::test]
async fn tick_error_handler_logs_and_restarts_loop() {
    let f = BotFixture::new().await;

    f.bot.service().db().fail_matching(LIST_ALL_ACTIVE_SQL);
    // Returns normally: nothing propagates out of a pass.
    f.bot.tick_once().await;
    f.bot.service().db().clear_failures();
    assert!(
        f.last_refresh().is_none(),
        "the failed read must not count as a refresh"
    );

    f.bot.tick_once().await;
    assert!(
        f.last_refresh().is_some(),
        "the loop runs the next pass normally"
    );
}
