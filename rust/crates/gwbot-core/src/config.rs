//! Environment configuration, with the same parsing rules as the Python bot.
//!
//! Every value is optional except the two the bot genuinely cannot run without
//! (`DISCORD_BOT_TOKEN`, `TURSO_DATABASE_URL`); `doctor` reports those instead
//! of panicking, so a misconfigured deploy prints one line rather than a
//! traceback.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// Default embed colour, matching the documented `EMBED_COLOR=0x7C5CFF`.
pub const DEFAULT_EMBED_COLOR: u32 = 0x007C_5CFF;

/// Default public dashboard, used only when `DASHBOARD_URL` is absent entirely.
/// An explicit empty value means "no Dashboard button".
pub const DEFAULT_DASHBOARD_URL: &str = "https://giveaway-bot-duggal.vercel.app/";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settings {
    pub bot_token: String,
    pub giveaway_channel_id: String,
    pub turso_url: String,
    pub turso_token: String,
    pub tick_seconds: i64,
    pub dashboard_url: String,
    pub port: u16,
    pub embed_color: u32,
    pub redis_url: String,
    pub redis_enabled: bool,
    /// Per-process ops/sec the Redis governor will spend. Kept configurable so
    /// a plan with a different ceiling does not need a rebuild, but it is never
    /// allowed to exceed the managed plan's own 100/s ceiling at runtime.
    pub redis_op_budget_per_sec: f64,
}

impl Settings {
    /// True when a Redis read-cache is both configured and switched on.
    ///
    /// A whitespace-only URL counts as unset: a stray space from a copy-paste
    /// would otherwise make the bot believe a cache exists and spend a
    /// connection attempt on it at every startup.
    pub fn uses_redis(&self) -> bool {
        !self.redis_url.trim().is_empty() && self.redis_enabled
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            bot_token: String::new(),
            giveaway_channel_id: String::new(),
            turso_url: String::new(),
            turso_token: String::new(),
            tick_seconds: 30,
            dashboard_url: DEFAULT_DASHBOARD_URL.to_string(),
            port: 10_000,
            embed_color: DEFAULT_EMBED_COLOR,
            redis_url: String::new(),
            redis_enabled: true,
            redis_op_budget_per_sec: 80.0,
        }
    }
}

/// Integer setting: blank or unparseable keeps the default, like the Python
/// helper, so a typo can never crash startup.
pub fn env_int(name: &str, default: i64) -> i64 {
    env_int_first(&[name], default)
}

/// Integer setting read from the first name that carries a usable value.
///
/// Only the renamed keys need this: an `.env` written before the port can hold
/// the older spelling and no new one, and silently falling back to the default
/// would change the cadence of a running deploy. Blank or unparseable values
/// fall through to the next name, then to the default, so a stray space cannot
/// hide the value behind it.
pub fn env_int_first(names: &[&str], default: i64) -> i64 {
    names
        .iter()
        .find_map(|name| match std::env::var(name) {
            Ok(raw) => {
                let trimmed = raw.trim();
                if trimmed.is_empty() {
                    None
                } else {
                    trimmed.parse::<i64>().ok()
                }
            }
            Err(_) => None,
        })
        .unwrap_or(default)
}

/// Yes/no setting in every spelling people actually write.
///
/// `1/true/yes/on` are true, `0/false/no/off` are false (case-insensitive).
/// Anything else — including empty — keeps the default, so a typo can never
/// silently flip a safety switch.
pub fn env_bool(name: &str, default: bool) -> bool {
    let raw = std::env::var(name)
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    match raw.as_str() {
        "1" | "true" | "yes" | "on" => true,
        "0" | "false" | "no" | "off" => false,
        _ => default,
    }
}

/// Float setting: blank or unparseable keeps the default, like [`env_int`].
pub fn env_float(name: &str, default: f64) -> f64 {
    match std::env::var(name) {
        Ok(raw) => {
            let trimmed = raw.trim();
            if trimmed.is_empty() {
                return default;
            }
            trimmed.parse::<f64>().unwrap_or(default)
        }
        Err(_) => default,
    }
}

/// Colour setting as a 24-bit int, in any spelling people actually write.
///
/// Accepts `0x7C5CFF`, `#7C5CFF`, `7C5CFF` and plain decimal. A `#` prefix, a
/// `0x` prefix or a bare six-character hex string is hex, so `#112233` and
/// `112233` are `0x112233`, not the decimal number 112233; any other bare
/// digits are decimal. Discord's colour field is 24 bits wide, so anything
/// wider — an 8-digit ARGB value pasted in — is masked down instead of being
/// sent for the API to reject. Anything unparseable keeps the default.
pub fn env_color(name: &str, default: u32) -> u32 {
    let raw = std::env::var(name).unwrap_or_default();
    let raw = raw.trim();
    let hashed = raw.starts_with('#');
    let raw = raw.trim_start_matches('#');
    if raw.is_empty() {
        return default;
    }
    let is_hex6 = raw.len() == 6 && raw.chars().all(|c| c.is_ascii_hexdigit());
    let lower = raw.to_lowercase();
    let parsed = if hashed || lower.starts_with("0x") || is_hex6 {
        u32::from_str_radix(raw.trim_start_matches("0x").trim_start_matches("0X"), 16).ok()
    } else {
        // Decimal first; hex only for odd lengths like an 8-digit ARGB.
        raw.parse::<u32>()
            .ok()
            .or_else(|| u32::from_str_radix(raw, 16).ok())
    };
    match parsed {
        Some(value) => value & 0x00FF_FFFF,
        None => default,
    }
}

/// Load `.env` the same way the Python package does: from the working
/// directory, then from the repository root (two levels above the crate).
pub fn load_dotenv() {
    // `dotenvy` ignores missing files, and never overrides an already-set
    // variable, which is exactly the precedence a container needs.
    let _ = dotenvy::dotenv();
    for candidate in repo_env_candidates() {
        if candidate.is_file() {
            let _ = dotenvy::from_path(&candidate);
        }
    }
}

fn repo_env_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        // target/debug/giveaway-bot -> rust/ -> repo root
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        for _ in 0..4 {
            if let Some(current) = dir {
                out.push(current.join(".env"));
                dir = current.parent().map(|p| p.to_path_buf());
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        let mut dir = Some(cwd);
        for _ in 0..3 {
            if let Some(current) = dir {
                out.push(current.join(".env"));
                dir = current.parent().map(|p| p.to_path_buf());
            }
        }
    }
    out
}

/// Read the process environment into `Settings`.
///
/// Mirrors `get_settings()`: the colour is parsed separately and `TICK_SECONDS`
/// is clamped to a 5-second floor so a busy loop cannot be configured by
/// accident. `TICK_INTERVAL_SECONDS` is the name the tick carried before the
/// port; it is still read so an existing `.env` needs no edit.
pub fn get_settings() -> Settings {
    let mut settings = Settings {
        bot_token: std::env::var("DISCORD_BOT_TOKEN")
            .unwrap_or_default()
            .trim()
            .to_string(),
        giveaway_channel_id: {
            let primary = std::env::var("DISCORD_GIVEAWAY_CHANNEL_ID").unwrap_or_default();
            let fallback = if primary.is_empty() {
                std::env::var("GIVEAWAY_CHANNEL_ID").unwrap_or_default()
            } else {
                primary
            };
            fallback.trim().to_string()
        },
        turso_url: std::env::var("TURSO_DATABASE_URL")
            .unwrap_or_default()
            .trim()
            .to_string(),
        turso_token: std::env::var("TURSO_AUTH_TOKEN")
            .unwrap_or_default()
            .trim()
            .to_string(),
        tick_seconds: env_int_first(&["TICK_SECONDS", "TICK_INTERVAL_SECONDS"], 30),
        dashboard_url: match std::env::var("DASHBOARD_URL") {
            Ok(value) => value.trim().to_string(),
            Err(_) => DEFAULT_DASHBOARD_URL.to_string(),
        },
        port: env_int("PORT", 10_000).clamp(1, 65_535) as u16,
        embed_color: DEFAULT_EMBED_COLOR,
        redis_url: std::env::var("REDIS_URL")
            .unwrap_or_default()
            .trim()
            .to_string(),
        redis_enabled: env_bool("REDIS_ENABLED", true),
        redis_op_budget_per_sec: env_float("REDIS_OP_BUDGET_PER_SEC", 80.0),
    };
    settings.embed_color = env_color("EMBED_COLOR", settings.embed_color);
    if settings.tick_seconds < 5 {
        settings.tick_seconds = 5;
    }
    settings
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with_var<T>(name: &str, value: &str, f: impl FnOnce() -> T) -> T {
        // SAFETY: tests in this module never run concurrently over the same key.
        unsafe { std::env::set_var(name, value) };
        let out = f();
        unsafe { std::env::remove_var(name) };
        out
    }

    #[test]
    fn color_accepts_every_documented_spelling() {
        assert_eq!(
            with_var("COLOR_A", "0x7C5CFF", || env_color("COLOR_A", 0)),
            0x7C5CFF
        );
        assert_eq!(
            with_var("COLOR_B", "#7C5CFF", || env_color("COLOR_B", 0)),
            0x7C5CFF
        );
        assert_eq!(
            with_var("COLOR_C", "7C5CFF", || env_color("COLOR_C", 0)),
            0x7C5CFF
        );
        // A bare six-character hex string is hex, not decimal (documented).
        assert_eq!(
            with_var("COLOR_D", "123456", || env_color("COLOR_D", 0)),
            0x12_3456
        );
        // 8-digit ARGB is masked to 24 bits rather than rejected by Discord.
        assert_eq!(
            with_var("COLOR_E", "FF112233", || env_color("COLOR_E", 0)),
            0x112233
        );
        assert_eq!(
            with_var("COLOR_F", "nonsense", || env_color("COLOR_F", 42)),
            42
        );
    }

    #[test]
    fn bool_keeps_default_for_typos() {
        assert!(with_var("BOOL_A", "YES", || env_bool("BOOL_A", false)));
        assert!(!with_var("BOOL_B", "off", || env_bool("BOOL_B", true)));
        assert!(with_var("BOOL_C", "banana", || env_bool("BOOL_C", true)));
    }

    #[test]
    fn int_keeps_default_for_blank_and_garbage() {
        assert_eq!(with_var("INT_A", "", || env_int("INT_A", 7)), 7);
        assert_eq!(with_var("INT_B", "abc", || env_int("INT_B", 7)), 7);
        assert_eq!(with_var("INT_C", " 12 ", || env_int("INT_C", 7)), 12);
    }

    /// A renamed key still configures the value it used to: an `.env` written
    /// before the port has the old spelling and no new one.
    #[test]
    fn the_old_tick_name_still_sets_the_tick() {
        let tick = with_var("TICK_SECONDS", "", || {
            with_var("TICK_INTERVAL_SECONDS", "45", || {
                get_settings().tick_seconds
            })
        });
        assert_eq!(tick, 45);
    }

    /// When both spellings are present the documented one wins, so a stale
    /// legacy line cannot quietly override a value someone just set.
    #[test]
    fn the_documented_tick_name_wins_over_the_old_one() {
        let names = ["ALIAS_PRIMARY", "ALIAS_LEGACY"];
        let value = with_var("ALIAS_PRIMARY", "12", || {
            with_var("ALIAS_LEGACY", "45", || env_int_first(&names, 30))
        });
        assert_eq!(value, 12);
        // Only the old name set, or only a blank one, still resolves.
        assert_eq!(
            with_var("ALIAS_LEGACY", "45", || env_int_first(&names, 30)),
            45
        );
        assert_eq!(
            with_var("ALIAS_PRIMARY", " ", || env_int_first(&names, 30)),
            30
        );
    }
}
