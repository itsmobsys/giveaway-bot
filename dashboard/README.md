# Dashboard backend (Vercel, Node.js) — API only, no frontend yet

Read-only HTTP layer over the **same Turso DB** the bot writes. Shows only
**live + previous** giveaways, 4 fields per card: prize, entrants, win chance, timer.

## Routes

| Route | What |
| --- | --- |
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

## Deploy (Vercel)

1. Vercel → Add Project → import this repo → **Root Directory = `dashboard`**.
2. Env vars: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` (same as bot).
3. Deploy. Test: `https://<project>.vercel.app/api/health` then `/api/giveaways`.

Local: `cd dashboard && npm i && vercel dev` (or `npx vercel dev`).
