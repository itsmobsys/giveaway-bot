# Dashboard — static page + Vercel Node.js API

Read-only view over the **same Turso DB** the bot writes. Shows only
**live + previous** giveaways, 4 fields per card: prize, entrants, win chance, timer.

No framework, no build step. Vercel serves `public/` as static assets and
`api/` as Node 20 functions; `vercel.json` rewrites every non-`/api` path to
`index.html`.

```
public/index.html   page shell (sections, status pill, refresh button)
public/styles.css   design tokens + all styling, dark/light via prefers-color-scheme
public/format.js    pure helpers — time formatting, escaping, cardHTML (unit-tested)
public/app.js       fetch loop, render, local countdown ticking
api/giveaways.js    live + previous queries, single card by ?id
api/health.js       Turso SELECT 1 probe
api/_lib/turso.js   shared Turso client, CORS/send helpers, card() shape
serve.js            local preview: serves public/ + mocks /api/giveaways
smoke.js            tests for format.js + the server card shape
```

## Routes

| Route | What |
| --- | --- |
| `GET /` | the dashboard page |
| `GET /api/health` | `{ ok, now }` — also verifies Turso `SELECT 1` |
| `GET /api/giveaways` | `{ now, live: [...], previous: [...] }` |
| `GET /api/giveaways?id=gw_xxx` | `{ now, giveaway: {...} }` — single card |

Query params: `guild_id` (optional filter), `previous_limit` (1–10, default 5).

## Card shape (the 4 fields)

```json
{
  "id": "gw_abc123",
  "status": "active",
  "prize": "Steam $20",
  "image_url": null,
  "host_name": "Mod",
  "entrants": { "count": 14, "usernames": ["Ann", "Zed"] },
  "chance": { "winners": 1, "entrants": 14, "percent": 7.14, "one_in": 14, "text": "1 winner / 14 entrants" },
  "timer": { "ends_at": 1760000000000, "ended_at": null, "ms_remaining": 3599000, "seconds_remaining": 3599, "is_live": true }
}
```

Privacy: usernames only, capped at 100 — **user ids never leave the DB**.
Previous giveaways keep prize/winners/timer; entry rows are wiped 5h after
end by the bot, so old `entrants.count` decays to 0 by design.

## Behaviour notes

- **Countdown** ticks locally every second from `timer.ends_at`, corrected by
  the server clock offset in the response `now` — so a skewed client clock does
  not show the wrong remaining time. Under 60s the timer turns amber.
- Refreshes every 15s, on tab focus, and on the Refresh button.
- Light/dark follows the OS setting; semantic colours have darker light-mode
  twins so body text, badges, and timers keep ≥4.5:1 contrast.
- The prize photo is decorative (`alt=""`) — the heading right below it already
  names the prize.
- The ticking clock is `aria-hidden` inside a `role="timer"` region with an
  absolute `<time datetime>` label, so a screen reader is never interrupted once
  a second.
- All untrusted text (prize, host, username) is HTML-escaped before insertion.

## Local preview

```bash
cd dashboard
npm i
node serve.js      # http://localhost:4321 — public/ + mocked /api/giveaways
node smoke.js      # helper + card-shape tests
```

`node serve.js` does not touch Turso, so it works without credentials. To run
against the real database use `npx vercel dev`.

## Deploy (Vercel)

1. Vercel → Add Project → import this repo → **Root Directory = `dashboard`**.
2. Env vars: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` (same as bot).
3. Deploy. Test: `https://<project>.vercel.app/api/health` then `/api/giveaways`.
