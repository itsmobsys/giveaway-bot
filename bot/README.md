# Giveaway bot (Python) — simple standalone v2

No dashboard. Just Discord slash commands + Join/Leave buttons + auto-draw timer.
Storage is Turso when `TURSO_DATABASE_URL` is set, otherwise a local SQLite file.

```bash
python -m pip install -e ".[turso]"
python -m giveaway_bot doctor
python -m giveaway_bot run
```

## Env

| Var | Required | What |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | Bot token |
| `TURSO_DATABASE_URL` | no | `libsql://...` — without it, uses SQLite |
| `TURSO_AUTH_TOKEN` | no | Turso auth token |
| `SQLITE_PATH` | no | Default `./data/giveaways.db` |
| `DISCORD_GIVEAWAY_CHANNEL_ID` | no | Force all giveaways into one channel |
| `TICK_SECONDS` | no | Auto-draw poll (default 30) |

Needs **Server Members Intent** on (Bot tab in the Developer Portal) for role checks.

## Commands

- `/giveaway_create prize winners minutes [required_role] [blocked_role] [min_account_age_days] [min_messages] [image]`
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
- `/giveaway_reroll giveaway_id [count]` — same suggestions + fallback
- `/giveaway_cancel giveaway_id` — same suggestions + fallback

The embed shows a live **⏳ Ends in ...** countdown, re-rendered every tick
(`TICK_SECONDS`, default 30).

## Layout

| File | Does |
| --- | --- |
| `config.py` | Env settings |
| `db.py` | SQLite/Turso wrapper + schema |
| `service.py` | Create/join/leave/end/reroll rules |
| `bot.py` | Discord wiring, buttons, timer |
| `embeds.py` / `views.py` | Messages + Join/Leave buttons |
| `cli.py` | `run` / `doctor` |
