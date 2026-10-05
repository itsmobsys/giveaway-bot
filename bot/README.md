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
- `/giveaway_list`
- `/giveaway_end giveaway_id`
- `/giveaway_reroll giveaway_id [count]`
- `/giveaway_cancel giveaway_id`

## Layout

| File | Does |
| --- | --- |
| `config.py` | Env settings |
| `db.py` | SQLite/Turso wrapper + schema |
| `service.py` | Create/join/leave/end/reroll rules |
| `bot.py` | Discord wiring, buttons, timer |
| `embeds.py` / `views.py` | Messages + Join/Leave buttons |
| `cli.py` | `run` / `doctor` |
