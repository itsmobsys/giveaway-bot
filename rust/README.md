# Giveaway Bot — Rust workspace

The bot, ported from the Python implementation with behaviour, storage schema,
dashboard contract and fairness rules unchanged.

```
crates/
├── gwbot-core/      domain: config, Turso storage, rules, embeds, cache, health
├── gwbot-discord/   gateway adapter: commands, buttons, tick loop
├── gwbot-cli/       the `giveaway-bot` binary (run / doctor / healthcheck)
└── gwbot-harness/   JSON-line driver used by the Python test suite
```

Only `gwbot-cli` produces a deployed binary. `gwbot-harness` exists so the
Python suite can drive the real service without a Discord token, and is never
shipped.

## Build and run

```bash
cargo build --release -p gwbot-cli
./target/release/giveaway-bot doctor
./target/release/giveaway-bot run
```

A C linker is required: `rusqlite`'s bundled SQLite (used by the test oracle
and the harness) compiles a small amount of C. On Windows that means MSVC Build
Tools or a mingw-w64 toolchain; on Linux, `build-essential` and `pkg-config`.

## Layers

| Crate | Owns | Never does |
| --- | --- | --- |
| `gwbot-core` | rules, SQL, retries, caching, embed text, button ids | touch a gateway |
| `gwbot-discord` | slash commands, component dispatch, tick loop, login retry | write SQL directly |
| `gwbot-cli` | process lifecycle, `doctor`, health probing | decide a giveaway rule |
| `gwbot-harness` | the test protocol | run in production |

`gwbot-core` is framework-neutral on purpose: embeds and buttons are plain
structs (`Embed`, `ButtonSpec`), so their exact wording and ids are unit-tested
without a Discord client. `gwbot-discord::convert` is the only place that knows
how serenity spells the same thing.

## Storage

Turso (libSQL) is the only production store. `libsql` is built with
`default-features = false, features = ["remote", "tls"]`; the local
`rusqlite` backend exists **only** for the test oracle and the harness, and
there is no code path that falls back to a local file in production — Render
wipes its disk on every redeploy, so a local database would silently lose
every giveaway.

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

## Harness protocol

`gwbot-harness` reads one JSON object per line on stdin and writes one per line
on stdout:

```json
{"id": 1, "op": "create", "args": {"guild_id": "...", "prize": "Nitro", ...}}
{"id": 1, "ok": true, "result": {"id": "gw_ab12", ...}}
```

A refusal keeps its kind so the Python suite can assert on it:

```json
{"id": 2, "ok": false, "kind": "service_error", "detail": "timeout_ban", "error": "..."}
```

`sql` runs a statement straight against the test database so a test can assert
storage state without trusting the service to report on itself. `clock.now`,
`clock.advance` and `clock.set` drive a fake clock, so "expires in five
minutes" never races a slow CI box.

Environment:

| Var | What |
| --- | --- |
| `GWBOT_HARNESS_DB` | database file (default `:memory:`) |
| `GWBOT_HARNESS_FAKE_CLOCK` | `1` to start the clock at a fixed point |
| `GWBOT_HARNESS_START_MS` | the fixed point, in epoch milliseconds |

## Tests

```bash
cargo test --workspace                          # Rust unit tests
cargo clippy --workspace --all-targets          # lints
cargo fmt --all -- --check                      # formatting

python -m unittest discover -s tests -t .       # from the repository root
```

The Python suite is the integration layer: it builds nothing, starts the
compiled harness, and exercises rules, storage invariants, fairness,
concurrency, failure recovery and a load smoke test against a real SQLite
database using the real schema.
