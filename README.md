# Giveaway Bot

A standalone Discord giveaway bot written in Rust, with a read-only web dashboard
over the same database. MIT licensed.

Python is no longer part of the product. It survives only as the automated
black-box test client: `tests/` starts the compiled harness binary and drives the
real Rust service over a JSON-line protocol. The original Python bot under `bot/`
is kept as a behavioural reference and is never deployed.

**Why Rust:** one binary, no interpreter or virtualenv on the host, a footprint
that fits Render's free web-service tier, and a single typed service layer where
the giveaway rules cannot drift between handlers.

---

## Contents

- [Layout](#layout)
- [Features](#features)
- [Commands](#commands)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Testing](#testing)
- [Dashboard](#dashboard)
- [Deployment](#deployment)
- [Python's role](#pythons-role)
- [License](#license)

---

## Layout

```
.
├── rust/                       Rust workspace: the bot
│   ├── crates/
│   │   ├── gwbot-core/         config, Turso storage, giveaway rules, embeds,
│   │   │                       components, Redis cache, health server
│   │   ├── gwbot-discord/      serenity gateway: commands, buttons, tick loops
│   │   ├── gwbot-cli/          the `giveaway-bot` binary (run/doctor/healthcheck)
│   │   └── gwbot-harness/      JSON-line driver used by the Python suite
│   └── README.md               workspace-internal notes
├── tests/                      Python black-box suite (test-only, no credentials)
├── bot/                        original Python bot — reference only, not deployed
├── dashboard/                  read-only site + Vercel Node 20 API
├── app.py                      root launcher that execs the built binary
├── Dockerfile                  multi-stage Rust image (deployed to Render)
├── render.yaml                 Render blueprint: bot web service
├── docker-compose.yml          local container run
└── .env.example                every setting, with defaults
```

---

## Features

### Giveaway lifecycle

- **Create** — prize, 1–25 winners, duration from 30 seconds to 60 days, up to
  five **required roles** (a member needs *any one* of them), one **blocked
  role**, minimum Discord account age, minimum messages sent in the server, an
  optional host shown on the embed, an optional prize photo, and an optional
  winner claim window.
- **Live embed** — a countdown that refreshes on a timer, the current entrant
  count, the entry requirements, and the host. Buttons below it: **Join**,
  **Leave**, **Participants**, and an optional blue **Dashboard** link.
- **Auto-draw** — the tick loop ends a giveaway the moment it is due and draws
  winners. A restart does not miss the draw: deadlines are durable rows, so the
  next tick simply picks up whatever is due.
- **Manual control** — end now, extend, reroll, cancel, all from slash commands
  with autocomplete over the live giveaways.
- **Pagination** — the participants list pages 500 entrants at a time in an
  ephemeral message, with Prev/Next buttons scoped to the clicking member.

### Entry rules

Checked in a fixed order on every click, before anything is written:

1. the giveaway is still active;
2. the member is not timed out (see below);
3. the member has one of the required roles, if any are set;
4. the member does not have the blocked role;
5. the account is old enough, when a minimum age is configured;
6. the member has sent enough messages in this server, when configured.

A failure returns a specific, member-facing reason rather than a generic error.
An account-age lookup that cannot be completed **refuses** the entry instead of
waving it through, so a transient Discord failure cannot silently disable the
alt-account filter.

Entries are recorded with one atomic guarded `INSERT`, so a double-click, two
gateway events racing, or a replayed interaction can only ever produce one row.

### Winners, claims and rerolls

- Winners are drawn uniformly at random from the entry set; winner selection is
  never asserted in tests, only the fairness invariants around it.
- **Claim window** (`claim_minutes`, `0` disables it): winners get a **Claim**
  button and a deadline. An expired claim automatically rerolls that one winner
  and announces the replacement.
- `/giveaway_reroll` draws new winner(s), or replaces exactly one winner with
  `user:` — the usual path when someone never claimed.
- `/skipclaim` marks a claim verified without replacing the winner.
- The winner append is a compare-and-swap on the stored winner list, and the
  status flip that ends a giveaway is a single guarded update, so two processes
  racing a draw cannot both announce.

### Moderation

Three distinct mechanisms, deliberately not merged:

| Mechanism | Scope | Where it lives |
| --- | --- | --- |
| **Blacklist** | Permanent block from every giveaway | `simple_blacklist` |
| **Ban** | Timed (1–720 h) or permanent, with a reason shown to the member and staff | `simple_giveaway_bans` |
| **Timeout penalty** | Automatic: a member Discord has timed out sits out the next **3** giveaways | `simple_giveaway_bans` + `simple_giveaway_ban_hits` |

The timeout penalty is applied when a timed-out member tries to join: the attempt
is refused, any existing entry is removed, and one hit is recorded against the
next three giveaways. `/giveaway_timeout_bans` lists who is currently sitting
out.

The entry-wipe sweep that prunes stale rows **never** touches `simple_blacklist`
or `simple_giveaway_bans` — moderation state outlives giveaway data by design.

### Roles and notifications

- **Entrants role** — each giveaway can create a mentionable role, granted on
  join and stripped on end. Grants and strips are concurrency-limited so a
  1000-entrant giveaway does not trip Discord's per-route rate limit, and the
  role is deleted five minutes after the strip. A grant that completes after the
  giveaway ended is rolled back immediately (the grant-after-end race).
- **Notify role** — `/giveaway_notifyer` sets one role per server that is pinged
  on every giveaway event. It is validated against the guild cache, so a deleted
  role degrades to no mention instead of a broken ping.
- **`/giveaway_ping`** mentions everyone who entered, split across messages to
  stay inside Discord's mention limits.

### Message counts

Message counts feed the `min_messages` requirement. They are buffered in memory
and written in one batched statement every 15 seconds (chunks of 300 rows), so a
busy server does not pay a database round-trip per message. Counts for a server
reset for everyone when a giveaway in that server ends.

### Reliability

- **Turso-only storage** in production. No local file fallback: Render wipes the
  filesystem on redeploy, so a local database would silently lose every giveaway.
- **Retries** — transient libSQL failures are retried with backoff; a hung call
  is bounded by an 8-second timeout so a stuck stream cannot park a click or a
  tick forever.
- **Login retry** — the gateway connection retries on a 30/60/120/300 s schedule
  with jitter instead of exiting.
- **Health server** — `/health` is liveness (process alive, always 200),
  `/ready` is readiness (503 until the gateway is online).
- **Panic containment** — a broken certificate store yields a clean
  `database driver unavailable: …` and exit code 1 instead of an abort.

---

## Commands

All commands are global and registered on the first `ready`. Admin commands
require **Manage Server** or **Administrator**; the check reads the interaction's
own permission bits, with no cache round-trip.

### Giveaway commands

| Command | Options | What it does |
| --- | --- | --- |
| `/giveaway_create` | `prize` (required), `winners`, `minutes`, `required_role_1`…`_5`, `blocked_role`, `min_account_age_days`, `min_messages`, `host`, `image`, `claim_minutes` | Start a giveaway |
| `/giveaway_end` | `giveaway_id` | End now and draw |
| `/giveaway_reroll` | `giveaway_id`, `count`, `user` | Draw new winner(s), or replace one |
| `/skipclaim` | `giveaway_id`, `user` | Mark a claim verified, no replacement |
| `/giveaway_cancel` | `giveaway_id` | Cancel an active giveaway |
| `/giveaway_extend` | `giveaway_id`, `minutes` (1–43200) | Add time to a running giveaway |
| `/giveaway_list` | `giveaway_id` (optional) | Entrants and win odds for one, or the active list |
| `/giveaway_notifyer` | `role` (optional) | Set or view the server's notify role |
| `/giveaway_ping` | `giveaway_id`, `text` | Ping everyone who joined |

### Admin commands

| Command | Options | What it does |
| --- | --- | --- |
| `/giveaway_blacklist_add` | `user` | Block a user from all giveaways |
| `/giveaway_blacklist_remove` | `user` | Unblock a user |
| `/giveaway_blacklist_list` | — | Show blocked users |
| `/giveaway_ban` | `user`, `hours` (0 = permanent), `reason` | Ban a user for N hours |
| `/giveaway_unban` | `user` | Unban a user |
| `/giveaway_ban_list` | — | Banned users with expiry and reason |
| `/giveaway_timeout_bans` | — | Members sitting out the timeout penalty |
| `/gw_request_time` | — | Time a real giveaway request (Redis + Discord legs) |

`/gw_request_time` is a diagnostic: it reports each leg separately and shows
`not measured` or `n/a` rather than a fast-looking zero when a leg never ran.

---

## Architecture

### Crates

| Crate | Responsibility |
| --- | --- |
| `gwbot-core` | Everything framework-neutral: settings, storage, rules, embeds, buttons, cache, health |
| `gwbot-discord` | serenity gateway wiring only — it translates events into service calls |
| `gwbot-cli` | The `giveaway-bot` binary: `run`, `doctor`, `healthcheck` |
| `gwbot-harness` | A JSON-line driver over the same service, used by the Python tests |

The dependency direction is one-way: `gwbot-discord` and `gwbot-cli` depend on
`gwbot-core`, never the reverse. Embeds and buttons are plain structs in
`gwbot-core` (`Embed`, `ButtonSpec`); `gwbot-discord::convert` is the only place
that knows about serenity's types.

### The service layer

`gwbot-core/src/service.rs` is the single source of runtime truth — 84 public
methods covering create, join, leave, end, reroll, cancel, extend, claims,
blacklist, bans, timeout penalties, settings and message counts. Handlers and
loops do not reimplement rules; they call the service and translate the result.

Two error types are deliberately downcastable so callers can react precisely:
`ServiceError` for a rule violation with a member-facing message, and
`PartialFlush` for a batch write that only partly landed.

### Storage

Turso (libSQL over HTTPS) only. `Database::connect` rejects any non-remote URL,
which is what stops a local file from quietly becoming the production database.

Schema version **2**, eight tables:

| Table | Holds |
| --- | --- |
| `simple_giveaways` | One row per giveaway: prize, status, deadlines, requirements, host, claim window |
| `simple_entries` | One row per (giveaway, user), unique by construction |
| `simple_message_counts` | Per-guild, per-user message totals |
| `simple_guild_settings` | Per-guild notify role |
| `simple_blacklist` | Permanent blocks |
| `simple_giveaway_bans` | Timed/permanent bans and timeout penalties |
| `simple_giveaway_ban_hits` | Per-giveaway rows that make the 3-giveaway penalty countable |
| `simple_claims` | Claim deadlines and outcomes (`pending`, `claimed`, `expired`, `skipped`, `replaced`) |

Indexes cover the queries that actually run: `(status, ends_at)`,
`(status, ended_at)`, their per-guild variants, `(giveaway_id, entered_at)`,
`user_id`, and pending claim deadlines. Entry rows are wiped five hours after a
giveaway ends; the final entrant count is stored on the giveaway row, so the
dashboard keeps its numbers after the entries are gone.

`rusqlite` (bundled SQLite) exists **only** for the test oracle and the harness.
It is never reachable from the production path.

### Cache

An optional Redis read cache sits in front of the database with short TTLs
(list 8 s, settings 10 s, giveaway 6 s, count 4 s). Everything cached is
derivable from Turso, so a stale hit costs a few seconds of cosmetic staleness —
a list one giveaway behind, a count one entry behind — never a wrong outcome.
Writes queue the keys they invalidated and flush them in the background, so a
slow Redis `DEL` never delays an announcement. A per-process operations budget
(80/s by default) keeps the cache from becoming its own outage.

Unset `REDIS_URL` means no cache at all: every read goes straight to Turso.

### Concurrency model

`Bot` is a cheap cloneable handle over `Arc<BotState>`. serenity 0.12 only builds
a `Context` inside its own dispatch, so the tick loops have none; both paths
share one `Gateway` carrier (`Arc<Cache>` + `Arc<Http>`) and one implementation
of every side effect. That is why a fix to the winner announcement or the
entrants-role strip lands once, not twice.

Per-tick work is bounded and rate-limited:

- live embeds refresh 10 immediately plus a rotating 5, at most 5 concurrently,
  every 120 s (a cosmetic half separate from the due/claim half);
- role strips and grants run at 5 concurrent, in batches of 200;
- a duplicate-entry latch absorbs burst taps for 2 minutes and a 2-second refresh
  debounce collapses repeat renders; the database remains the real guard.

---

## Quick start

Requires Rust 1.80+. A C linker is needed for the bundled SQLite that the test
oracle and the harness link: `gcc`/`cc` on Linux, MSVC Build Tools or a mingw-w64
toolchain on Windows.

```bash
# from rust/
cargo build --release -p gwbot-cli

# from the repo root
cp .env.example .env          # then fill in the token and Turso credentials
./rust/target/release/giveaway-bot doctor    # validate configuration
./rust/target/release/giveaway-bot run       # start the bot
```

`app.py` is only a launcher: it locates the built binary and `exec`s it (on
POSIX) or hands over to it (on Windows, where `execv` would lose the exit code),
so a host that can only start a Python file still works. Nothing about the bot
runs in Python.

### The `giveaway-bot` binary

```
giveaway-bot <command>

Commands:
  run          start the Discord bot
  doctor       validate configuration
  healthcheck  probe this host's /health endpoint (exit 1 when unhealthy)

Options:
  -h, --help       show this message
  -V, --version    show the version
```

`doctor` separates **problems** from **notes**. A problem (such as a missing
`TURSO_DATABASE_URL`) is a hard failure and exits 1; a note (such as an
odd-looking token) is a warning and still exits 0. `healthcheck` exits 0 only
when the local `/health` endpoint answers 200.

---

## Configuration

Copy `.env.example` to `.env`. Settings are read once at startup.

| Variable | Required | Default | What |
| --- | --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | — | Bot token |
| `TURSO_DATABASE_URL` | yes | — | `libsql://…`; the only database |
| `TURSO_AUTH_TOKEN` | yes | — | Turso auth token |
| `DISCORD_GIVEAWAY_CHANNEL_ID` | no | — | Force every giveaway into one channel (`GIVEAWAY_CHANNEL_ID` also accepted) |
| `TICK_SECONDS` | no | `30` | Due-check and refresh cadence, minimum 5 |
| `EMBED_COLOR` | no | `0x7C5CFF` | `0xRRGGBB`, `#RRGGBB`, bare hex or decimal; masked to 24 bits |
| `DASHBOARD_URL` | no | public site | Blue Dashboard link button; empty string removes the button |
| `PORT` | no | `10000` | Health server port (Render sets this) |
| `REDIS_URL` | no | — | Short-TTL read cache; unset disables it |
| `REDIS_ENABLED` | no | `true` | Switch the cache off without clearing the URL |
| `REDIS_OP_BUDGET_PER_SEC` | no | `80` | Per-process Redis operations ceiling |
| `ADMIN_PASSWORD` | dashboard admin | — | Enables `/api/admin`; unset or empty means every admin request returns 503 |

Parsing is deliberately forgiving in the same way the Python bot was: a blank or
unparseable integer/float keeps the default, and a boolean only accepts an
explicit true/false spelling — a typo can never silently flip a switch.

The Discord application needs **Server Members Intent** (Developer Portal → Bot)
for role checks, and **Manage Roles** with the bot's role above any role it
manages.

---

## Testing

Three independent suites, none of which needs a Discord token, network access or
a Turso account.

```bash
# Rust unit and integration tests — from rust/
cargo test --workspace
cargo clippy --workspace --all-targets     # must be warning-free
cargo fmt --all -- --check

# Python black-box suite — from the repo root, after building the harness
cargo build -p gwbot-harness --manifest-path rust/Cargo.toml
python -m unittest discover -s tests -t .

# Dashboard helpers and card shape — from dashboard/
node smoke.js
```

### What the Python suite covers

`tests/` starts the compiled `gwbot-harness` binary and drives the real Rust
service against an in-memory SQLite database, over a JSON-line protocol. It is a
black box: it cannot reach inside the service, only send operations and inspect
the resulting database state.

| Module | Focus |
| --- | --- |
| `test_schema.py` | Table/column/index shape, `SCHEMA_VERSION`, migration behaviour |
| `test_service.py` | Create/join/leave/end/reroll/cancel/extend, validation order, error text |
| `test_fairness.py` | Uniform winner selection, no duplicates, entrant-set invariants |
| `test_concurrency.py` | Racing joins, double draws, duplicate clicks, claim races |
| `test_recovery.py` | Restart mid-draw, partial flush, due giveaways after downtime |
| `test_load.py` | Bulk entry and draw smoke test |

Winner selection cannot match Python bit-for-bit, so parity is asserted on
database state and fairness invariants — never on a specific winner.

The harness protocol is documented by its own op list in
`rust/crates/gwbot-harness/src/main.rs`, which is the authority for argument
names; `tests/harness.py` is the Python client for it.

---

## Dashboard

A read-only view over the **same Turso database** the bot writes. It shows live
and previous giveaways with four fields per card: prize, entrants, win chance and
timer. No framework and no bundler — `public/` is baked into `api/page.js`, and
`api/` runs as Node 20 serverless functions (`framework: null` in `vercel.json`
forces Vercel's "Other" preset, so it never asks for Next.js).

| Route | Response |
| --- | --- |
| `GET /` | The dashboard page |
| `GET /api/health` | `{ ok, now }`, also verifying Turso with `SELECT 1` |
| `GET /api/giveaways` | `{ now, live: [...], previous: [...] }` |
| `GET /api/giveaways?id=gw_xxx` | `{ now, giveaway: {...} }` — a single card |

Query parameters: `guild_id` (optional filter), `previous_limit` (1–10, default 5).

**Privacy:** usernames only, capped at 100; user ids never leave the database.
Entry rows are wiped five hours after a giveaway ends. Finished giveaways keep
their stored final entrant count; older rows without one fall back to live
entries.

Behaviour notes:

- The countdown ticks locally every second from `timer.ends_at`, corrected by the
  server clock offset in the response `now`; under 60 s it turns amber.
- Refreshes every 15 s, on tab focus, and on the Refresh button.
- Light/dark follows the OS setting; the ticking clock is screen-reader-safe
  (`role="timer"`, absolute `<time>` label); all untrusted text is escaped.

### `/admin`

A password-gated panel for clearing **finished** giveaways. Live ones cannot be
deleted there — end them in Discord first. It pages through the whole table (the
public API only returns the last 10) with search, status filters, multi-select
and bulk delete behind a confirm dialog. It is fail-closed: without a nonempty
`ADMIN_PASSWORD`, every `/api/admin` request returns 503.

### Local preview

The API is mocked, so no Turso credentials are needed:

```bash
cd dashboard
npm i
node serve.js      # http://localhost:4321
```

`serve.js` uses `ADMIN_PASSWORD` if set, otherwise a non-secret preview-only
password (`local-preview-only`) that the deployed API never accepts.

`api/page.js` has the `public/` files baked in. **After editing anything in
`public/`, run `node build-page.js` from `dashboard/` before committing**, or the
deployed page will not pick up the change.

---

## Deployment

### Render (bot)

`render.yaml` defines one Docker web service with `autoDeploy: true`. The
`Dockerfile` is a multi-stage build: it compiles the workspace in a Rust stage and
copies only the stripped binary into a slim runtime image, so the final image
carries no toolchain.

Set these in the Render dashboard (or let the blueprint prompt for them):
`DISCORD_BOT_TOKEN`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and optionally
`DISCORD_GIVEAWAY_CHANNEL_ID`, `EMBED_COLOR`, `DASHBOARD_URL`, `REDIS_URL`,
`TICK_SECONDS`. Render injects `PORT`.

The container exposes `/health` and `/ready`. On the free tier, ping `/health`
from an uptime monitor every five minutes to stop the service sleeping.

### Vercel (dashboard)

Import the repository with **Root Directory = `dashboard`**, set
`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` (the same values as the bot) plus a
strong, unique `ADMIN_PASSWORD`, then deploy. No build step runs.

### Docker Compose (local)

`docker compose up` runs the bot from the same image. It reads `.env`, so a real
token and Turso credentials are required — there is no local database mode.

### Turso

The bot refuses to start without `TURSO_DATABASE_URL`. Schema creation and
migration run automatically on first connect, and are idempotent.

---

## Python's role

Python is **test-only**. `tests/` is the only Python that ships as part of the
workflow; it is a black-box client for the Rust binary and is not imported by the
bot.

The `bot/` tree is the original Python implementation, kept because it is the
behavioural specification the Rust port was checked against. It is not built, not
deployed and not part of the runtime path, but it is kept runnable for reference:

```bash
cd bot
python -m unittest discover -s tests -t .
```

Do not add features there. Changes belong in `rust/`.

---

## License

MIT — see [LICENSE](LICENSE).
