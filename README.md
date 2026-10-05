# Giveaway Bot

Simple standalone Discord giveaway bot. MIT licensed.

```
.
├── bot/            Python Discord bot (discord.py), Turso-backed
├── dashboard/      Website + Vercel Node.js API (live + previous giveaways)
├── app.py          Root entry point for panels that start a file
├── requirements.txt  Bot dependencies
├── render.yaml     Render blueprint: bot web service
└── docker-compose.yml  Bot, locally
```

## What it does

- `/giveaway_create` — prize, winners, duration, up to 3 required roles, blocked
  role, min account age, min messages, host picker, prize photo
- Join / Leave / Participants buttons, live countdown embed, auto-draw timer
- `/giveaway_end`, `/giveaway_extend`, `/giveaway_reroll`, `/giveaway_cancel`
  (autocomplete + live-one fallback, no id typing)
- `/giveaway_list` (entrants + win odds), `/giveaway_ping`, `/giveaway_notifyer`
  (one-time notify-role setup, pinged on every event)
- Per-giveaway mentionable entrants role, granted on join, stripped on end
- Message counts reset for everyone when a giveaway ends
- Turso-only storage, so restarts never lose data
- Built-in `/health` server, so it runs on Render's free Web Service tier

Details: [bot/README.md](bot/README.md).

## Dashboard (website + API, Vercel)

Read-only view over the **same Turso DB** the bot writes. Shows only
**live + previous** giveaways, 4 fields per card: prize, entrants, win chance, timer.
No framework, no build step — `public/` is served as-is, `api/` runs as Node 20
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
Entry rows are wiped 5h after end by the bot, so old `entrants.count` decays
to 0 by design.

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

`/admin` (linked in the page footer) is a password-gated panel for deleting
previous giveaways. Live ones can't be deleted there — end them in Discord
first. The password defaults to the one in `api/admin.js` and can be
overridden with the `ADMIN_PASSWORD` env var; anyone with repo access can see
it, so it only keeps casual visitors out.

Deploy: import the repo on Vercel with **Root Directory = `dashboard`**,
set env vars `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` (same as the bot), deploy.

## Quick start

```bash
python -m pip install -r requirements.txt
python -m giveaway_bot doctor   # from bot/
python -m giveaway_bot run      # or: python app.py (from root)
```

## Env

| Var | Required | What |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | Bot token |
| `TURSO_DATABASE_URL` | yes | `libsql://...` — the only database |
| `TURSO_AUTH_TOKEN` | yes | Turso auth token |
| `DISCORD_GIVEAWAY_CHANNEL_ID` | no | Force all giveaways into one channel |
| `TICK_SECONDS` | no | Embed refresh + due checks (default 30) |
| `EMBED_COLOR` | no | e.g. `0x7C5CFF` |
| `PORT` | no | Health server port (Render sets it; default 10000) |

The bot needs **Server Members Intent** (Developer Portal → Bot) for role checks
and **Manage Roles** with its role above the ones it manages. On Render free
tier, ping `/health` from UptimeRobot every 5 minutes to stop it sleeping.

## License

MIT — see [LICENSE](LICENSE).
