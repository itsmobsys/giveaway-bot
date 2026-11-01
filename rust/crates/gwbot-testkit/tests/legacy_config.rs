//! Port of the retired Python `test_config.py`: settings parsing for the values that
//! reach Discord unchanged.
//!
//! Every case goes through [`get_settings`], not through the individual
//! `env_*` helpers, because that is what the Python suite did: the point is
//! that the value a caller receives is the parsed one.
//!
//! Environment variables are process-wide, so this binary serialises its tests
//! on one lock and restores every key it touches. Other test binaries run in
//! their own process and never see these writes.

use std::sync::Mutex;

use gwbot_core::config::{get_settings, DEFAULT_DASHBOARD_URL, DEFAULT_EMBED_COLOR};

/// Guards every read/write of the process environment in this binary.
static ENV_LOCK: Mutex<()> = Mutex::new(());

/// Sets one variable for the duration of a test and puts it back afterwards.
struct EnvVar {
    key: String,
    saved: Option<String>,
}

impl EnvVar {
    fn set(key: &str, value: &str) -> Self {
        let saved = std::env::var(key).ok();
        // SAFETY: every test in this binary holds `ENV_LOCK`, so no other
        // thread here reads or writes the environment concurrently.
        unsafe { std::env::set_var(key, value) };
        Self {
            key: key.to_string(),
            saved,
        }
    }

    fn unset(key: &str) -> Self {
        let saved = std::env::var(key).ok();
        // SAFETY: see `set`.
        unsafe { std::env::remove_var(key) };
        Self {
            key: key.to_string(),
            saved,
        }
    }
}

impl Drop for EnvVar {
    fn drop(&mut self) {
        // SAFETY: see `EnvVar::set`.
        unsafe {
            match &self.saved {
                Some(value) => std::env::set_var(&self.key, value),
                None => std::env::remove_var(&self.key),
            }
        }
    }
}

fn lock() -> std::sync::MutexGuard<'static, ()> {
    ENV_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

const VAR: &str = "EMBED_COLOR";

/// The colour `get_settings()` reports for one raw `EMBED_COLOR` spelling.
fn color(raw: &str) -> u32 {
    let _guard = EnvVar::set(VAR, raw);
    get_settings().embed_color
}

#[test]
fn every_documented_spelling_works() {
    let _lock = lock();
    for raw in ["0x7C5CFF", "#7C5CFF", "7c5cff", "8150271"] {
        assert_eq!(0x007C_5CFF, color(raw), "{raw}");
    }
}

#[test]
fn hash_and_bare_six_digit_hex_are_hex_not_decimal() {
    let _lock = lock();
    // '#112233' / '112233' used to be read as decimal 112233 (0x01B669).
    for raw in ["#112233", "112233", "0x112233", "#123456"] {
        let expected =
            u32::from_str_radix(raw.trim_start_matches('#').trim_start_matches("0x"), 16)
                .expect("hex literal");
        assert_eq!(expected, color(raw), "{raw}");
    }
}

#[test]
fn other_bare_digits_stay_decimal() {
    let _lock = lock();
    assert_eq!(11223, color("11223"));
    assert_eq!(0x007C_5CFF, color("8150271"));
}

#[test]
fn junk_keeps_the_default() {
    let _lock = lock();
    for raw in ["", "   ", "#", "purple"] {
        assert_eq!(DEFAULT_EMBED_COLOR, color(raw), "{raw:?}");
    }
}

#[test]
fn an_over_wide_value_is_masked_to_24_bits() {
    let _lock = lock();
    // Discord's colour field is 24 bits: an 8-digit ARGB value pasted in must
    // not be sent as 4294900991 for the API to reject.
    assert_eq!(0x007C_5CFF, color("0xFF7C5CFF"));
}

#[test]
fn unset_dashboard_url_uses_the_default() {
    let _lock = lock();
    let _guard = EnvVar::unset("DASHBOARD_URL");
    let url = get_settings().dashboard_url;
    assert!(url.starts_with("https://"), "{url}");
    assert_eq!(DEFAULT_DASHBOARD_URL, url);
}

#[test]
fn explicit_empty_disables_the_button() {
    let _lock = lock();
    for raw in ["", "   "] {
        let _guard = EnvVar::set("DASHBOARD_URL", raw);
        assert_eq!("", get_settings().dashboard_url, "{raw:?}");
    }
}

#[test]
fn custom_value_is_used() {
    let _lock = lock();
    let _guard = EnvVar::set("DASHBOARD_URL", " https://example.com/ ");
    assert_eq!("https://example.com/", get_settings().dashboard_url);
}
