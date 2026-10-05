# Giveaway bot (Python) — simple standalone v2

No dashboard. Just Discord slash commands + Join/Leave/Participants buttons + auto-draw timer.
Storage is **Turso only** — there is intentionally no local/SQLite fallback, so
a redeploy or restart can never wipe giveaways, entries, or settings.

```bash
python -m pip install -e ".[turso]"
python -m giveaway_bot doctor
python -m giveaway_bot run
```

## Env

| Var | Required | What |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | Bot token |
| `TURSO_DATABASE_URL` | yes | `libsql://...` — the only database. The bot refuses to start without it |
| `TURSO_AUTH_TOKEN` | yes | Turso auth token |
| `DISCORD_GIVEAWAY_CHANNEL_ID` | no | Force all giveaways into one channel |
| `TICK_SECONDS` | no | Auto-draw poll (default 30) |

Needs **Server Members Intent** on (Bot tab in the Developer Portal) for role checks.

## Commands

- `/giveaway_create prize winners minutes [required_role_1] [required_role_2] [required_role_3] [blocked_role] ...`
  - up to 3 required roles (member needs **any one**); all are pinged on create
  so eligible people see it
- `/giveaway_notifyer [role]` — **one-time setup** (Manage Server): the role
  pinged on every giveaway event (new, winners, rerolls, cancellations).
  Members opt in/out themselves with the **🔔 Notify me** button on any
  giveaway message. Run without a role to view the current one.
  - `min_messages`: members must have sent that many messages in the server (counted from when the bot is online)
  - `image`: http(s) photo URL shown on the embed — e.g. a Steam gift-card picture

Message counts start accumulating the moment the bot logs in (even with no
giveaway running), and **reset to 0 for everybody when a giveaway ends** —
so each giveaway measures fresh activity since the last one ended.

Joining grants a mentionable **🎉 \<prize\> entrants role** (created per
giveaway, so you can ping everyone in it). Leaving removes it; when the
giveaway ends or is cancelled it is taken from everyone — winners and losers
alike — and the role is deleted. The bot needs **Manage Roles** for this; if
it lacks the permission the giveaway still runs, just without the role.
- `/giveaway_list [giveaway_id]` — without an id: active giveaways; with
  an id (suggestions as you type): the full entrant list
- `/giveaway_ping giveaway_id [text]` — pings every entrant (Manage Server)
- `/giveaway_end giveaway_id` — suggestions appear as you type; with one live
  giveaway any id falls back to it
- `/giveaway_extend giveaway_id minutes` — add 1 minute to 60 days of time to
  a running giveaway
- `/giveaway_reroll giveaway_id [count]` — same suggestions + fallback; works
  on ended giveaways, drawing fresh winners that exclude previous ones
- `/giveaway_reroll giveaway_id [count]` — same suggestions + fallback
- `/giveaway_cancel giveaway_id` — same suggestions + fallback

The embed shows a live **⏳ Ends in ...** countdown, re-rendered every tick
(`TICK_SECONDS`, default 30). The **👥 Participants** button opens a paged
entrant list (10 per page, ◀ Previous | page | Next ▶) with totals and your
personal odds. The prize photo is shown on both the giveaway and winner messages.

## Layout

| File | Does |
| --- | --- |
| `config.py` | Env settings |
| `db.py` | SQLite/Turso wrapper + schema |
| `service.py` | Create/join/leave/end/reroll rules |
| `bot.py` | Discord wiring, buttons, timer |
| `embeds.py` / `views.py` | Messages + Join/Leave buttons |
| `cli.py` | `run` / `doctor` |
