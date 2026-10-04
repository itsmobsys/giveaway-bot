# Deployment

Two processes share one Turso database:

```
   Discord              Render (worker)           Browser
  +----------+  gateway +--------------+        +--------+
  |  players | -------> | giveaway-bot |        | public |
  +----------+         +------+-------+        | pages  |
                            | SQL               +---+----+
                            v                        ^
                    +-------+-----------------------+
                    |        Turso / libSQL          |
                    +-------+-----------------------+
                            | SQL (command queue)
                    +-------v-----------------------+
                    |  giveaway-dashboard          |
                    |  (Render web / Vercel)       |
                    +-------------------------------+
```

The dashboard writes intents to `command_queue`; the bot executes them. Neither
needs to reach the other over HTTP.

---

## 1. Create the database

```bash
npm i -g @libsql/cli   # or: npx tursi
turso db create giveaway-bot
turso db tokens create --type jwt          # copy the token
```

Note the URL (`libsql://your-db.turso.io`) and token. The bot and the dashboard
must both use them.

### Apply migrations once

```bash
cd dashboard
export TURSO_DATABASE_URL="libsql://..."
export TURSO_AUTH_TOKEN="eyJ..."
npm run db:migrate
npm run db:status          # confirm all six are applied
```

The bot also applies migrations on startup (`python -m giveaway_bot run`), so
this step is only needed if you want the schema ready before the first deploy.
Both runners read the *same* `shared/migrations/*.sql`, so the schemas cannot
diverge.

---

## 2. Create the Discord application

1. <https://discord.com/developers/applications> -> **New Application**
2. **Bot** -> **Reset Token** -> copy it (`DISCORD_BOT_TOKEN`)
3. **Pick the giveaway channel.** Give a single channel over to the bot (e.g.
   `#giveaways`), then right-click it -> **Copy ID** and set
   `DISCORD_GIVEAWAY_CHANNEL_ID`. Every giveaway is posted there - admins never
   choose a channel, and the bot refuses to create one if this is unset or
   points somewhere it cannot post.
4. **Bot** -> enable **Server Members Intent** (under *Privileged Gateway
   Intents*), then press Save.

   This one is **mandatory**. Discord refuses the whole connection if the bot
   requests a privileged intent the portal has not enabled, and the bot has to
   request it: giveaway eligibility reads a member's roles and their server join
   date, and Discord omits the member object from interaction payloads without
   it. If you skip this the bot exits immediately with
   `PrivilegedIntentsRequired`.

   Do **not** enable Message Content Intent. The bot never reads message text -
   message counting uses gateway events - so it does not request it. discord.py
   logs "privileged message content intent is missing" at startup regardless; that
   warning is expected and harmless for a slash-command-only bot.

   `python -m giveaway_bot doctor` prints exactly which intents this build
   requests, read from the same code the client uses.
5. **OAuth2 -> URLs** -> add your dashboard's redirect:
   - Render: `https://<app>.onrender.com/api/auth/callback`
   - Vercel: `https://<app>.vercel.app/api/auth/callback`
   - Local: `http://localhost:3000/api/auth/callback`
6. Copy **Application ID** (`DISCORD_CLIENT_ID`) and **Client Secret**
   (`DISCORD_CLIENT_SECRET`)
7. Copy **Public Key** - not used by this project, but you will want it if you
   ever verify interactions server-side

### Invite the bot with the right permissions

`bot` + `applications.commands` scopes, and these permissions:

| Permission | Why |
| --- | --- |
| Manage Server | Create/edit/pause/end giveaways |
| Manage Roles | Grant and remove the temporary entrants role |
| View Channels / Send Messages | Post giveaway and winner messages |
| Read Message History | Backfill message counts after a gateway gap |
| Use External Emojis | The countdown/progress embeds |

Add the role **above** other member roles in the server's role order, otherwise
Discord will refuse role grants.

---

## 3. Generate secrets

```bash
# Session signing key (32+ bytes)
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"

# Optional: signed control API (only if you want HTTP instead of the queue)
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 4. Environment variables

**Bot** (Render worker, or `.env` locally):

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_GIVEAWAY_CHANNEL_ID=...        # the one channel giveaways are posted in
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://<app>.onrender.com/api/auth/callback
DISCORD_GUILD_ALLOWLIST=123456789012345678     # optional, comma separated
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...
DASHBOARD_URL=https://<app>.onrender.com
LOG_JSON=true
LOG_PRETTY=false
```

**Dashboard** (Render web / Vercel):

```dotenv
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://<app>.onrender.com/api/auth/callback
SESSION_SECRET=<32+ chars>
NEXT_PUBLIC_APP_URL=https://<app>.onrender.com
CSRF_TRUSTED_ORIGINS=https://<app>.onrender.com
```

`NEXT_PUBLIC_APP_URL` and `CSRF_TRUSTED_ORIGINS` must match the real hostname or
CSRF checks will (correctly) reject your own admin actions. On Vercel and Render
the deployment URL is picked up automatically, but setting them explicitly is
clearer.

---

## 5. Deploy

### Render (bot + dashboard together)

The repo root has `render.yaml` describing both services:

1. Push to GitHub, then in Render choose **New → Blueprint** and pick the repo.
2. Fill in every `sync: false` variable in both services.
3. Render gives the dashboard a `https://<name>.onrender.com` URL. Put that into
   `DISCORD_REDIRECT_URI`, `NEXT_PUBLIC_APP_URL`, `CSRF_TRUSTED_ORIGINS` and
   `DASHBOARD_URL`, and register it in the Discord Developer Portal.
4. Redeploy.

**Bot notes on Render**

The bot is containerised from the root `Dockerfile`. `render.yaml` sets:

| | |
| --- | --- |
| `runtime` | `docker` |
| `dockerfilePath` | `./Dockerfile` (build context: repository root) |
| health check | none - a worker has no HTTP port to probe |

The image is `python:3.13-slim` because the Turso driver (`libsql`) is a Rust
extension with wheels for CPython 3.11, 3.12 and 3.13 only; on 3.14 pip tries to
compile it from source and usually fails. Everything except Turso works on 3.14 if
you install everything except `libsql`.

`PYTHONPATH=/app/bot` runs the package from source rather than installing it,
because the default migrations directory is derived from the source tree
(`bot/giveaway_bot/../..` -> `shared/migrations`). An installed copy inside
`site-packages` would look for migrations that are not there, so `MIGRATIONS_DIR`
is deliberately not set for the bot.

Run migrations once before the first start - either run the image once, or use the
CLI from the repo:

```bash
docker compose run --rm bot python -m giveaway_bot migrate
# or
cd bot && python -m giveaway_bot migrate
```

**Bot notes on Render**

* It must be a **background worker**, not a web service - a worker keeps the
  gateway connection alive, which is what a Discord bot needs. A worker has no
  HTTP port, which is also why there is no health-check path for it.
* The free plan sleeps and has no persistent disk. Use **Starter** or above for a
  bot that must stay online. With Turso, no disk is needed at all, which is why
  the database is the hosted option.
* Keep `autoDeploy` off if you would rather deploy deliberately.

### Vercel (dashboard only)

```bash
cd dashboard
npx vercel            # preview
npx vercel --prod     # production
```

Set the variables in Project Settings → Environment Variables. Then update
`DISCORD_REDIRECT_URI` in Discord to the `*.vercel.app` URL.

Vercel caveat: the SSE endpoint (`/api/giveaways/[id]/stream`) closes after
`maxDuration`. The client reconnects automatically, so live updates continue with
a brief gap. If you want a gapless stream, deploy the dashboard to Render
instead — it is a long-lived process there.

---

## 6. Verify the deployment

```bash
# dashboard is alive and the database answers
curl -s https://<app>/api/health          # {"ok":true,"database":"reachable",...}

# OAuth2 is wired up
curl -sI https://<app>/api/auth/login | grep -i location
# -> 302 to discord.com/oauth2/authorize?...

# fairness implementations agree
cd dashboard && npm run fairness:verify
```

Then sign in, confirm your server appears, create a giveaway, and check that it
appears in Discord within a few seconds.

---

## Rollback

Nothing here needs a rollback to run: if a bot deploy misbehaves, redeploy the
previous commit. Schema changes are forward-only by design — migrations refuse to
run if an already-applied file changed, and a broken migration fails inside a
transaction, leaving the previous schema intact.

---

## Troubleshooting

These two lines on every start are **expected and harmless**:

`
WARNING discord.client  PyNaCl is not installed, voice will NOT be supported
WARNING discord.client  davey is not installed, voice will NOT be supported
`

They come from discord.py and mean only that voice channels are unavailable. This
bot never joins one, so neither package is a dependency. They are left unfixed on
purpose rather than silenced by installing an unused crypto library.

A third startup line is also expected, for the same kind of reason:

`
WARNING discord.ext.commands.bot  Privileged message content intent is missing
`

The bot uses slash commands only and never reads message text, so it does not
request that intent. This warning is harmless.

| Symptom | Cause |
| --- | --- |
| Dashboard shows "Not authorised" | Missing **Manage Server** in that server, or the bot is not in the server |
| Admin action says "request rejected" | `CSRF_TRUSTED_ORIGINS` / `NEXT_PUBLIC_APP_URL` mismatch |
| Giveaway creates in the dashboard but not in Discord | The bot is not polling, or it cannot post in the channel (check `/admin give sync`) |
| Entrants never receive the role | Bot lacks **Manage Roles**, or its role is below the entrants role |
| `PrivilegedIntentsRequired` on startup | **Server Members Intent** not enabled in the Developer Portal (Bot -> Privileged Gateway Intents). The bot exits immediately rather than running with broken eligibility |
| Message counts stay at zero | **Server Members Intent** not enabled, or the bot lacks **Read Message History** |
| `migration ... is malformed` | A `; statement-breakpoint` line is missing between two statements |
| Live updates stall on Vercel | Expected — SSE hits `maxDuration` and the client reconnects |
