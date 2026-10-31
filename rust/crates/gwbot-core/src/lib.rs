//! Giveaway domain: configuration, Turso storage, rules, embeds and cache.
//!
//! This crate holds everything that does not talk to Discord, so the rules can
//! be tested without a gateway connection and reused by the JSON harness.

pub mod cache;
pub mod components;
pub mod config;
pub mod db;
pub mod embeds;
pub mod health;
pub mod mentions;
pub mod models;
pub mod service;

pub use cache::RedisCache;
pub use components::{ButtonSpec, ButtonStyle, Click};
pub use config::{get_settings, load_dotenv, Settings};
pub use db::{Database, DbStats, Row, SqlValue};
pub use embeds::Embed;
pub use health::{start_health_server, HealthServer, HealthState};
pub use models::{now_ms, Giveaway, PartialFlush, ServiceError, MAX_WINNERS, TIMEOUT_PENALTY};

/// Exact refusal text for a missing token, shared by `doctor` and `run`.
pub const MISSING_TOKEN_HINT: &str =
    "DISCORD_BOT_TOKEN is not set. Set it in .env (see .env.example) and restart.";

/// Exact refusal text for a missing database URL.
pub const MISSING_DB_HINT: &str = "TURSO_DATABASE_URL is not set. This bot stores everything in \
     Turso so restarts never lose data — set TURSO_DATABASE_URL (and TURSO_AUTH_TOKEN) and restart.";
