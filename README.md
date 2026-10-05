# Giveaway Bot

Simple standalone Discord giveaway bot. MIT licensed.

```
.
├── bot/            Python Discord bot (discord.py), Turso-backed
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
