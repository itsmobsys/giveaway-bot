//! `giveaway-bot run|doctor|healthcheck`.
//!
//! A hand-written argument parser: three subcommands and one flag do not need a
//! dependency, and this binary is the thing a deploy runs, so its startup path
//! stays as small as possible.

use std::sync::Arc;

use gwbot_core::config::get_settings;
use gwbot_core::db::Database;
use gwbot_core::health::{start_health_server, HealthState};
use gwbot_core::RedisCache;
use tokio::sync::RwLock;

/// Version reported by `--version`.
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// A bot token is three dot-separated parts. Only used to warn, never to block:
/// the format has changed before and a false alarm must not stop a working bot.
const TOKEN_PARTS: usize = 3;

fn usage() -> &'static str {
    "giveaway-bot <command>

Commands:
  run          start the Discord bot
  doctor       validate configuration
  healthcheck  probe this host's /health endpoint (exit 1 when unhealthy)

Options:
  -h, --help       show this message
  -V, --version    show the version"
}

fn main() -> std::process::ExitCode {
    let mut args = std::env::args().skip(1);
    let command = match args.next() {
        Some(flag) if flag == "-h" || flag == "--help" => {
            println!("{}", usage());
            return std::process::ExitCode::SUCCESS;
        }
        Some(flag) if flag == "-V" || flag == "--version" => {
            println!("giveaway-bot {VERSION}");
            return std::process::ExitCode::SUCCESS;
        }
        Some(command) => command,
        None => {
            eprintln!("{}", usage());
            return std::process::ExitCode::from(2);
        }
    };
    if args.next().is_some() {
        eprintln!("error: unexpected argument after `{command}`");
        eprintln!("{}", usage());
        return std::process::ExitCode::from(2);
    }

    // Loaded before dispatch, not inside `run`: the retired Python bot read
    // `.env` when its config module was imported, so `doctor` and `healthcheck`
    // saw the same settings the bot would. A host that already set a real
    // environment variable still wins -- dotenvy never overrides.
    gwbot_core::load_dotenv();

    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(e) => {
            eprintln!("error: could not start the async runtime: {e}");
            return std::process::ExitCode::FAILURE;
        }
    };

    let code = match command.as_str() {
        "run" => runtime.block_on(cmd_run()),
        "doctor" => runtime.block_on(cmd_doctor()),
        "healthcheck" => runtime.block_on(cmd_healthcheck()),
        other => {
            eprintln!("error: unknown command `{other}`");
            eprintln!("{}", usage());
            return std::process::ExitCode::from(2);
        }
    };
    std::process::ExitCode::from(code)
}

/// Run a future and turn a panic into an `Err`.
///
/// `Database::connect` reaches into the platform certificate store, and the TLS
/// stack `expect()`s on that call: a host whose store cannot be read (a locked
/// down container, a stripped image) aborts the process instead of reporting a
/// bad configuration. `doctor` must always report, and a deploy must always
/// explain itself, so the panic is contained here rather than escaping.
async fn caught<T, F>(future: F) -> anyhow::Result<T>
where
    F: std::future::Future<Output = anyhow::Result<T>>,
{
    use futures::FutureExt;
    match std::panic::AssertUnwindSafe(future).catch_unwind().await {
        Ok(result) => result,
        Err(payload) => Err(anyhow::anyhow!("{}", panic_text(&*payload))),
    }
}

/// The message out of a panic payload, without needing `Display` on `Any`.
fn panic_text(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        (*text).to_string()
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.clone()
    } else {
        "internal panic".to_string()
    }
}

/// Initialise logging once. `RUST_LOG` wins, then a sane default.
fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,gwbot_core=info,gwbot_discord=info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .try_init();
}

async fn cmd_run() -> u8 {
    init_tracing();
    let settings = get_settings();

    if settings.bot_token.trim().is_empty() {
        eprintln!("error: {}", gwbot_core::MISSING_TOKEN_HINT);
        return 1;
    }
    if settings.turso_url.trim().is_empty() {
        eprintln!("error: {}", gwbot_core::MISSING_DB_HINT);
        return 1;
    }

    let db = match caught(Database::connect(&settings)).await {
        Ok(db) => db,
        Err(e) => {
            eprintln!("error: could not open the database: {e:#}");
            return 1;
        }
    };

    // Schema first: the health server and the gateway both assume the tables
    // exist, and a fresh database has none.
    if let Err(e) = caught(db.init_schema()).await {
        eprintln!("error: could not initialise the schema: {e:#}");
        return 1;
    }

    // The cache is an accelerator only: an unset or unreachable Redis must never
    // stop the bot, so this is a log line rather than a failure path.
    let redis = RedisCache::from_settings(&settings);
    if redis.configured() {
        if redis.start().await {
            tracing::info!("redis cache: {}", redis.describe().await);
        } else {
            tracing::warn!("redis configured but unreachable; every read will use the database");
        }
    } else {
        tracing::info!("redis cache: disabled");
    }

    let state = Arc::new(RwLock::new(HealthState::default()));
    // Render's health check hits /health, so this must be listening before the
    // gateway is even attempted.
    if let Err(e) = start_health_server(settings.port, Arc::clone(&state)).await {
        eprintln!("error: {e:#}");
        return 1;
    }

    // `run_bot` owns the database now: it flushes the message buffer and closes
    // the connection itself, so a shutdown cannot drop buffered counts.
    let result = gwbot_discord::run_bot(settings, db, redis, state).await;
    match result {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("error: {e:#}");
            1
        }
    }
}

async fn cmd_doctor() -> u8 {
    let settings = get_settings();
    let mut problems: Vec<String> = Vec::new();
    let mut notes: Vec<String> = Vec::new();

    if settings.bot_token.trim().is_empty() {
        problems.push("DISCORD_BOT_TOKEN is not set.".to_string());
    } else if settings.bot_token.matches('.').count() != TOKEN_PARTS - 1 {
        notes.push(
            "DISCORD_BOT_TOKEN does not look like a bot token (expected three dot-separated \
             parts). The bot would start and then fail to log in."
                .to_string(),
        );
    }

    if settings.turso_url.trim().is_empty() {
        problems.push(
            "TURSO_DATABASE_URL is not set. This bot is Turso-only so restarts never lose \
             data — set it (plus TURSO_AUTH_TOKEN) and restart."
                .to_string(),
        );
    } else {
        if settings.turso_url.starts_with("file:") {
            problems.push(
                "TURSO_DATABASE_URL is a file: URL, which Render wipes on every redeploy. \
                 Point it at the remote Turso database instead."
                    .to_string(),
            );
        } else if !(settings.turso_url.starts_with("libsql://")
            || settings.turso_url.starts_with("https://")
            || settings.turso_url.starts_with("http://"))
        {
            notes.push(
                "TURSO_DATABASE_URL does not start with libsql:// or https:// — check for a \
                 stray space or a copy-paste slip."
                    .to_string(),
            );
        }
        if settings.turso_token.trim().is_empty() {
            notes.push("TURSO_AUTH_TOKEN is empty; a remote Turso URL needs one.".to_string());
        }
        match caught(Database::connect(&settings)).await {
            Err(e) => problems.push(format!("database driver unavailable: {e:#}")),
            Ok(db) => {
                let probe: anyhow::Result<(i64, i64)> = caught(async {
                    db.init_schema().await?;
                    let total = db
                        .query_one("SELECT COUNT(*) AS n FROM simple_giveaways", &[])
                        .await?
                        .and_then(|row| row.int("n"))
                        .unwrap_or(0);
                    let live = db
                        .query_one(
                            "SELECT COUNT(*) AS n FROM simple_giveaways WHERE status = 'active'",
                            &[],
                        )
                        .await?
                        .and_then(|row| row.int("n"))
                        .unwrap_or(0);
                    Ok((total, live))
                })
                .await;
                match probe {
                    Ok((total, live)) => {
                        println!("database backend: {}", db.backend());
                        println!("giveaways on record: {total} ({live} active)");
                    }
                    Err(e) => problems.push(format!("database unreachable: {e:#}")),
                }
                db.close().await;
            }
        }
    }

    // Redis: reported, never required. The bot runs straight to Turso with no
    // cache at all, so a missing or unreachable Redis is a note, not a problem —
    // doctor must not fail a working deployment over an optional accelerator.
    if !settings.uses_redis() {
        notes.push(
            "REDIS_URL is empty (or REDIS_ENABLED=false), so every read goes straight to \
             Turso. Correct, just slower under load."
                .to_string(),
        );
    } else {
        notes.push(
            "REDIS_URL is set: a short-TTL read cache is active. It is never a source of \
             truth — entries are still confirmed by Turso."
                .to_string(),
        );
    }

    println!("tick: every {}s", settings.tick_seconds);
    println!(
        "giveaway channel: {}",
        if settings.giveaway_channel_id.is_empty() {
            "the channel the command runs in"
        } else {
            &settings.giveaway_channel_id
        }
    );
    println!(
        "dashboard button: {}",
        if settings.dashboard_url.is_empty() {
            "disabled"
        } else {
            &settings.dashboard_url
        }
    );

    for note in &notes {
        println!("  [!] {note}");
    }
    if !problems.is_empty() {
        println!("Problems:");
        for problem in &problems {
            println!("  [x] {problem}");
        }
        return 1;
    }
    println!("[ok] configuration looks good");
    0
}

/// Probe this host's own `/health`, the way a container health check would.
///
/// Kept in the binary so the runtime image needs no curl or wget, and so the
/// probe uses the same `PORT` default the server does.
async fn cmd_healthcheck() -> u8 {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let port = gwbot_core::config::env_int("PORT", 10000).clamp(1, 65_535) as u16;
    let address = format!("127.0.0.1:{port}");
    let Ok(mut stream) = tokio::net::TcpStream::connect(&address).await else {
        eprintln!("unhealthy: nothing listening on {address}");
        return 1;
    };
    let request = format!("GET /health HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).await.is_err() {
        eprintln!("unhealthy: could not send the probe to {address}");
        return 1;
    }
    let mut raw = String::new();
    // A probe that hangs is worse than one that reports failure: bound the read
    // so a wedged server still exits 1 inside the container's own timeout.
    let _ = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        stream.read_to_string(&mut raw),
    )
    .await;
    match raw
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
    {
        Some(200) => 0,
        Some(code) => {
            eprintln!("unhealthy: /health answered {code}");
            1
        }
        None => {
            eprintln!("unhealthy: /health did not answer with a status line");
            1
        }
    }
}
