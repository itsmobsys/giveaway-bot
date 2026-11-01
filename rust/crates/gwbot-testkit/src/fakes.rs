//! In-memory Redis driver and manual cache clock for the cache tests.
//!
//! The Python suite injected a fake async client through `RedisCache(connect=...)`
//! so the cache's own behaviour -- budget, breaker, namespacing, the EVAL
//! fallback -- could be exercised without a server. This is the same fake on the
//! Rust side: it records every call, can be told to fail, and keeps values and
//! TTLs in memory.

use std::collections::BTreeMap;
use std::sync::Mutex;

use async_trait::async_trait;
use gwbot_core::cache::Driver;

/// One recorded driver call, in the shape the Python fake recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Call {
    pub name: String,
    pub args: Vec<String>,
    /// SET ... NX
    pub nx: bool,
    /// SET/INCR ... PX millis
    pub px: Option<i64>,
}

#[derive(Default)]
struct State {
    data: BTreeMap<String, String>,
    ttls: BTreeMap<String, i64>,
    calls: Vec<Call>,
    fail: Option<String>,
    eval_fails: bool,
}

/// A driver that keeps everything in memory and never opens a socket.
#[derive(Default)]
pub struct FakeRedis {
    state: Mutex<State>,
}

impl FakeRedis {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Seed a value as if it were already in Redis.
    pub fn put(&self, key: &str, value: &str) {
        self.lock().data.insert(key.to_string(), value.to_string());
    }

    pub fn get_raw(&self, key: &str) -> Option<String> {
        self.lock().data.get(key).cloned()
    }

    /// Number of keys currently held, so a test can tell "nothing cached" from
    /// "something cached".
    pub fn len(&self) -> usize {
        self.lock().data.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Every key currently held, sorted. Used to assert that a finished sweep
    /// released its lease, or that a background flush dropped the queued keys.
    pub fn keys(&self) -> Vec<String> {
        self.lock().data.keys().cloned().collect()
    }

    pub fn ttl(&self, key: &str) -> Option<i64> {
        self.lock().ttls.get(key).copied()
    }

    /// Make every command fail with `message`.
    pub fn fail_with(&self, message: &str) {
        self.lock().fail = Some(message.to_string());
    }

    pub fn clear_failure(&self) {
        self.lock().fail = None;
    }

    /// Fail only EVAL, to exercise the incr+pexpire fallback.
    pub fn fail_eval(&self, flag: bool) {
        self.lock().eval_fails = flag;
    }

    pub fn calls(&self) -> Vec<Call> {
        self.lock().calls.clone()
    }

    pub fn call_names(&self) -> Vec<String> {
        self.lock().calls.iter().map(|c| c.name.clone()).collect()
    }

    pub fn count(&self, name: &str) -> usize {
        self.lock().calls.iter().filter(|c| c.name == name).count()
    }

    fn record(
        &self,
        name: &str,
        args: Vec<String>,
        nx: bool,
        px: Option<i64>,
    ) -> Result<(), String> {
        let mut state = self.lock();
        state.calls.push(Call {
            name: name.to_string(),
            args,
            nx,
            px,
        });
        match &state.fail {
            Some(message) => Err(message.clone()),
            None => Ok(()),
        }
    }
}

#[async_trait]
impl Driver for FakeRedis {
    async fn ping(&self) -> Result<(), String> {
        self.record("ping", Vec::new(), false, None)
    }

    async fn get(&self, key: &str) -> Result<Option<String>, String> {
        self.record("get", vec![key.to_string()], false, None)?;
        Ok(self.lock().data.get(key).cloned())
    }

    async fn set(
        &self,
        key: &str,
        value: &str,
        millis: i64,
        nx: bool,
    ) -> Result<Option<String>, String> {
        self.record(
            "set",
            vec![key.to_string(), value.to_string()],
            nx,
            Some(millis),
        )?;
        let mut state = self.lock();
        if nx && state.data.contains_key(key) {
            return Ok(None);
        }
        state.data.insert(key.to_string(), value.to_string());
        state.ttls.insert(key.to_string(), millis);
        Ok(Some("OK".to_string()))
    }

    async fn del(&self, keys: &[String]) -> Result<i64, String> {
        self.record("del", keys.to_vec(), false, None)?;
        let mut state = self.lock();
        let mut removed = 0;
        for key in keys {
            if state.data.remove(key).is_some() {
                removed += 1;
            }
        }
        Ok(removed)
    }

    async fn eval(&self, script: &str, keys: &[String], args: &[String]) -> Result<i64, String> {
        let mut recorded = vec![script.to_string(), keys.len().to_string()];
        recorded.extend(keys.iter().cloned());
        recorded.extend(args.iter().cloned());
        {
            let mut state = self.lock();
            state.calls.push(Call {
                name: "eval".to_string(),
                args: recorded,
                nx: false,
                px: None,
            });
            if state.eval_fails {
                return Err("scripting disabled".to_string());
            }
            if let Some(message) = &state.fail {
                return Err(message.clone());
            }
        }

        let key = keys.first().cloned().unwrap_or_default();
        let payload = args.first().cloned().unwrap_or_default();
        let mut state = self.lock();
        if script.contains("pexpire") {
            let value = state
                .data
                .get(&key)
                .and_then(|raw| raw.parse::<i64>().ok())
                .unwrap_or(0)
                + 1;
            state.data.insert(key.clone(), value.to_string());
            state
                .ttls
                .entry(key)
                .or_insert_with(|| payload.parse::<i64>().unwrap_or(0));
            return Ok(value);
        }
        if state.data.get(&key).map(String::as_str) == Some(payload.as_str()) {
            state.data.remove(&key);
            return Ok(1);
        }
        Ok(0)
    }

    async fn incr(&self, key: &str) -> Result<i64, String> {
        self.record("incr", vec![key.to_string()], false, None)?;
        let mut state = self.lock();
        let value = state
            .data
            .get(key)
            .and_then(|raw| raw.parse::<i64>().ok())
            .unwrap_or(0)
            + 1;
        state.data.insert(key.to_string(), value.to_string());
        Ok(value)
    }

    async fn pexpire(&self, key: &str, millis: i64) -> Result<i64, String> {
        self.record(
            "pexpire",
            vec![key.to_string(), millis.to_string()],
            false,
            None,
        )?;
        self.lock().ttls.insert(key.to_string(), millis);
        Ok(1)
    }
}
