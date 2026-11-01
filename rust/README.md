# Giveaway Bot — Rust workspace

The bot, ported from a Python implementation with behaviour, storage schema,
dashboard contract and fairness rules unchanged. That Python tree has been removed;
the Rust suite is now the only specification of the behaviour.

```
crates/
├── gwbot-core/      domain: config, Turso storage, rules, embeds, cache, health
├── gwbot-discord/   gateway adapter: commands, buttons, tick loop
├── gwbot-cli/       the `giveaway-bot` binary (run / doctor / healthcheck)
└── gwbot-testkit/   in-process harness and shared fixtures for the test suite
```

Only `gwbot-cli` produces a deployed binary. `gwbot-testkit` exists so the
integration tests can drive the real service without a Discord token, and is
never shipped.

## Build and run

```bash
cargo build --release -p gwbot-cli
./target/release/giveaway-bot doctor
./target/release/giveaway-bot run
```

A C linker is required: `rusqlite`'s bundled SQLite (used by the test suite)
compiles a small amount of C. On Windows that means MSVC Build Tools or a
mingw-w64 toolchain; on Linux, `build-essential` and `pkg-config`. The deployed
binary never opens a local database, so a production image does not need it.

## Layers

| Crate | Owns | Never does |
| --- | --- | --- |
| `gwbot-core` | rules, SQL, retries, caching, embed text, button ids | touch a gateway |
| `gwbot-discord` | slash commands, component dispatch, tick loop, login retry | write SQL directly |
| `gwbot-cli` | process lifecycle, `doctor`, health probing | decide a giveaway rule |
| `gwbot-testkit` | the test fixtures and fake clock | run in production |

`gwbot-core` is framework-neutral on purpose: embeds and buttons are plain
structs (`Embed`, `ButtonSpec`), so their exact wording and ids are unit-tested
without a Discord client. `gwbot-discord::convert` is the only place that knows
how serenity spells the same thing.

## Storage

Turso (libSQL) is the only production store. `libsql` is built with
`default-features = false, features = ["remote", "tls"]`; the local
`rusqlite` backend exists **only** for the test suite, and there is no code path
that falls back to a local file in production — Render wipes its disk on every
redeploy, so a local database would silently lose every giveaway.

Schema version 2, identical to the Python bot:

| Table | Purpose |
| --- | --- |
| `simple_giveaways` | one row per giveaway, `winners_json` appended by CAS |
| `simple_entries` | one row per entry, inserted by a single guarded INSERT |
| `simple_message_counts` | per-guild message grind counters |
| `simple_guild_settings` | notify role per guild |
| `simple_blacklist` | bans (permanent or timed) |
| `simple_giveaway_bans` | how many giveaways a timed-out member still sits out |
| `simple_giveaway_ban_hits` | one row per giveaway a penalty was spent on |
| `simple_claims` | prize claims and their replacements |

Invariants that must not be relaxed:

- an entry is one atomic guarded `INSERT`; never read-then-write;
- a winner is appended by compare-and-swap on `winners_json`;
- the status flip that ends a giveaway happens exactly once, and losing the
  race means not drawing;
- `TIMEOUT_PENALTY = 3` giveaways, tracked with per-giveaway hit rows so
  retries cannot stack or re-spend a penalty;
- `MAX_WINNERS = 25` (one announcement message);
- entry rows are wiped 5h after a giveaway ends, and the sweep never touches
  `simple_blacklist` or `simple_giveaway_bans`.

## Cache

`REDIS_URL` is optional. When set, `gwbot-core::cache` is a short-TTL read cache
and a concurrency damper: a token-bucket governor keeps a *sliding second* under
the managed plan's 100 ops/s ceiling (a skip looks exactly like a miss), and a
circuit breaker stops hammering a dead server. Turso stays the source of truth —
every write goes straight there, and losing Redis costs latency, never data.

## The test harness

`gwbot-testkit` is the harness, and it is a library, not a binary. One `Harness`
is one bot process: a fresh database, a `GiveawayService` over it, and a
`ManualClock` it owns.

```rust
let h = Harness::new().await;                       // fresh temp database
let gw = create(&h, json!({"prize": "Nitro", "minutes": 10})).await;
let joined = join(&h, &gw, "user_1", json!({})).await?;
```

Operations are still called by name — `h.call("join", args)` — so every test
ported from the old suite reads the same way, while the wire format and the
subprocess are gone. A refusal keeps its kind (`service_error`, `timeout_ban`, …)
so a test can assert on the exact reason.

Three rules keep the suite honest:

- every fixture starts a fresh database, so no test can be made green by another
  test's leftovers;
- the clock only moves when a test moves it (`advance`, `set`), so "expires in
  five minutes" never races a slow CI box;
- the clock belongs to the harness, not to the process, so tests run in parallel
  threads without moving each other's deadlines.

Storage assertions go straight at the test database, so a test can check what was
stored without trusting the service to report on itself. `run_concurrently` and
the `*_job` helpers stage the racing calls that the concurrency tests need.

Nothing here needs a Discord token, a network connection or a Turso account.

## Tests

```bash
cargo test --workspace                          # unit + integration tests
cargo clippy --workspace --all-targets          # lints
cargo fmt --all -- --check                      # formatting
```

`cargo test --workspace` is the only test entry point: there is no separate client
to build and no interpreter to install. The integration layer lives in
`crates/gwbot-testkit/tests/` and exercises rules, storage invariants, fairness,
concurrency, failure recovery and a load smoke test against a real SQLite
database using the real schema.

The gateway half lives in `crates/gwbot-discord/src/bot/tests.rs` plus
`src/bot/tests/`, in-crate because serenity only builds a `Context` inside its own
dispatch: those tests drive the same functions the handlers call and record the
answer instead of editing a message.
