# Giveaway Bot

Simple standalone Discord giveaway bot, written in Rust. MIT licensed.

```
.
├── rust/               Rust workspace: the bot
│   └── crates/
│       ├── gwbot-core/     config, Turso storage, rules, embeds, cache
│       ├── gwbot-discord/  gateway wiring: commands, buttons, tick loop
│       ├── gwbot-cli/      the `giveaway-bot` binary (run / doctor / healthcheck)
│       └── gwbot-harness/  JSON-line driver used by the Python test suite
├── tests/              Python test suite (drives the harness, no gateway needed)
├── dashboard/          Website + Vercel Node.js API (live + previous giveaways)
├── app.py              Root launcher for panels that start a file
├── Dockerfile          Multi-stage Rust image (deployed to Render)
├── render.yaml         Render blueprint: bot web service
└── docker-compose.yml  Bot, locally
```

## What it does

- `/giveaway_create` — prize, winners, duration, up to 5 required roles, blocked
  role, min account age, min messages, host picker, prize photo
- Join / Leave / Participants buttons, live countdown embed, auto-draw timer
- `/giveaway_end`, `/giveaway_extend`, `/giveaway_reroll`, `/giveaway_cancel`
  (autocomplete + live-one fallback, no id typing)
- `/giveaway_list` (entrants + win odds), `/giveaway_ping`, `/giveaway_notifyer`
  (one-time notify-role setup, pinged on every event)
- Members Discord has timed out (native `/mute`) cannot join: the attempt is
  refused, their entry is removed, and they sit out the next 3 giveaways.
  `/giveaway_timeout_bans` lists who is currently sitting out
- Per-giveaway mentionable entrants role, granted on join, stripped on end
- Message counts reset for everyone **in that server** when a giveaway ends.
  They are kept in memory and written in one batched statement every 10s, so a
  busy server does not pay a database round-trip per message
- Turso-only storage, so restarts never lose data
- Built-in `/health` and `/ready` endpoints, so it runs on Render's free Web
  Service tier

Details: [rust/README.md](rust/README.md).

## Dashboard (website + API, Vercel)

Read-only view over the **same Turso DB** the bot writes. Shows only
**live + previous** giveaways, 4 fields per card: prize, entrants, win chance, timer.
No framework or bundler — `public/` is baked into `api/page.js`; `api/` runs as Node 20
functions (`framework: null` in `vercel.json` forces the "Other" preset, so
Vercel never asks for Next.js).

| Route | What |
| --- | --- |
| `GET /` | the dashboard page |
| `GET /api/health` | `{ ok, now }` — also verifies Turso `SELECT 1` |
| `GET /api/giveaways` | `{ now, live: [...], previous: [...] }` |
| `GET /api/giveaways?id=gw_xxx` | `{ now, giveaway: {...} }` — single card |

Query params: `guild_id` (optional filter), `previous_limit` (1–10, default 5).

Privacy: usernames only, capped at 100 — **user ids never leave the DB**.
Entry rows are wiped 5h after end by the bot. Finished giveaways keep
their final `entrants.count`; older rows without a stored count use live entries.

Behaviour notes:

- Countdown ticks locally every second from `timer.ends_at`, corrected by the
  server clock offset in the response `now`. Under 60s the timer turns amber.
- Refreshes every 15s, on tab focus, and on the Refresh button.
- Light/dark follows the OS setting; the ticking clock is screen-reader-safe
  (`role="timer"`, absolute `<time>` label); all untrusted text is escaped.

Local preview (no Turso credentials needed — the API is mocked):

```bash
cd dashboard
npm i
node serve.js      # http://localhost:4321
node smoke.js      # helper + card-shape tests
```

The page is served by `api/page.js`, which has the `public/` files baked in —
after editing anything in `public/`, run `node build-page.js` (from
`dashboard/`) before committing, or the live page won't pick up the change.

`/admin` (linked in the page footer) is a password-gated panel for clearing
finished giveaways. Live ones can't be deleted there — end them in Discord
first. It pages through the whole table (the public API only returns the last
10), with search, status filters, multi-select and bulk delete behind a
confirm dialog. Configure `ADMIN_PASSWORD` before deploying; without a nonempty
value every admin API request returns 503. The local `node serve.js` mock uses
`ADMIN_PASSWORD` if supplied, otherwise a non-secret preview-only password
(`local-preview-only`) that is not used by the deployed API.

Deploy: import the repo on Vercel with **Root Directory = `dashboard`**,
set env vars `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` (same as the bot)
and a strong, unique `ADMIN_PASSWORD` (required for admin access), then deploy.

## Quick start

Requires a Rust toolchain (1.80+). A C linker is needed for the bundled SQLite
that the test oracle and the harness link: `gcc`/`cc` on Linux, MSVC Build Tools
or a mingw-w64 toolchain on Windows.

```bash
cargo build --release -p gwbot-cli          # from rust/
./rust/target/release/giveaway-bot doctor   # check the environment
./rust/target/release/giveaway-bot run      # or: python app.py (from the root)
```

`app.py` is only a launcher: it finds the built binary and `exec`s it, so hosts
that can only start a Python file still work. Nothing about the bot runs in
Python.

## Tests

```bash
cargo test --workspace                                   # from rust/
python -m unittest discover -s tests -t .                # from the root
cd dashboard && node smoke.js                            # page + card shape
```

The Python suite is the black-box suite: it starts the compiled harness binary
and drives the real Rust service over a JSON-line protocol, against an
in-memory SQLite database. It covers the rules, the storage invariants,
fairness, concurrency, failure recovery and a load smoke test. No Discord
token, network or Turso account is needed.

## Env

| Var | Required | What |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | Bot token |
| `TURSO_DATABASE_URL` | yes | `libsql://...` — the only database |
| `TURSO_AUTH_TOKEN` | yes | Turso auth token |
| `ADMIN_PASSWORD` | dashboard admin | Required to enable `/api/admin`; unset/empty disables it (503) |
| `DISCORD_GIVEAWAY_CHANNEL_ID` | no | Force all giveaways into one channel |
| `TICK_SECONDS` | no | Embed refresh + due checks (default 30) |
| `EMBED_COLOR` | no | e.g. `0x7C5CFF`, `#7C5CFF` or `7C5CFF`; masked to 24 bits |
| `DASHBOARD_URL` | no | Blue "Dashboard" link button on every giveaway message (empty = no button) |
| `REDIS_URL` | no | Optional short-TTL read cache; unset disables it |
| `REDIS_ENABLED` | no | Switch the cache off without clearing the URL (default true) |
| `REDIS_OP_BUDGET_PER_SEC` | no | Per-process Redis ops/sec ceiling (default 80) |
| `PORT` | no | Health server port (Render sets it; default 10000) |

The bot needs **Server Members Intent** (Developer Portal → Bot) for role checks
and **Manage Roles** with its role above the ones it manages. On Render free
tier, ping `/health` from UptimeRobot every 5 minutes to stop it sleeping.

## License

MIT — see [LICENSE](LICENSE).
