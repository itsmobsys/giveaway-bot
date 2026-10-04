# Giveaway Bot

**Open-source Discord giveaways with a provably fair, independently verifiable
draw** — a Python bot and a Next.js dashboard, sharing one Turso/libSQL database.

Everything here is MIT licensed and auditable. The randomness lives in two small
files, and `npm run fairness:verify` proves the two implementations agree.

```
.
├── bot/            Python Discord bot (discord.py) + command-queue worker
├── dashboard/      Next.js dashboard (Render or Vercel) with Turso
├── shared/         Cross-language contract: SQL migrations, fairness spec, test vectors
├── docs/           Architecture · Fairness · Security · Deployment
├── docker-compose.yml
├── render.yaml     Render blueprint: bot (worker) + dashboard (web)
└── Dockerfile
```

## How the two halves work together

```
   Discord                Render                    Browser
  ┌─────────┐   gateway  ┌──────────────┐          ┌────────┐
  │ players │ ─────────► │  giveaway-bot│          │public  │
  └─────────┘            │  (worker)    │          │pages   │
                         └──────┬───────┘          └────┬───┘
                                │ SQL                   │ OAuth2
                                ▼                       ▼
                         ┌───────────────────────────────────┐
                         │        Turso / libSQL             │
                         └────────────────▲──────────────────┘
                                            │ SQL (command queue)
                         ┌──────────────────┴─────────┐
                         │  giveaway-dashboard        │
                         │  (Render web / Vercel)    │
                         └────────────────────────────┘
```

The dashboard never calls Discord. Admin actions are written to a
`command_queue` table; the bot claims them atomically, **re-validates everything
against live Discord state**, executes, and writes an audit row.

## Features

**Giveaways** — create, edit, pause, resume, extend, shorten, end, cancel, reroll.
Discord buttons for join/leave with live counts. Animated embeds with countdowns,
winner announcements and reroll buttons. Multiple winners, configurable prizes,
duration or explicit end time, entry limits.

**Eligibility** — whitelist roles (any/all), blacklist roles, channel
restrictions, minimum account age, minimum server-join age, membership required.
Duplicate entries prevented; every rule re-checked on every attempt.

**Message activity** — optional per-giveaway minimum message count, scoped to
the whole server or specific channels, with a live progress bar for members.

**Temporary entrants role** — entrants get a role so staff can ping one target
instead of a long list; it is removed automatically when the giveaway ends. Only
roles the bot granted are removed, never ones a human assigned.

**Provably fair draws** — commit–reveal, rejection sampling, no owner override,
full manifest published, independently verifiable.

**Dashboard** — public giveaway pages with winner proof, Discord OAuth2 admin
panel with server-level RBAC, participant management, analytics, audit log, SSE
live updates, dark mode.

## Quick start

```bash
# 1. Bot: install, migrate, verify
cd bot
python -m pip install -e ".[dev]"
python -m giveaway_bot migrate
python -m giveaway_bot selftest      # 26 checks, no token or network needed

# 2. Dashboard
cd ../dashboard
npm install
cp .env.example .env.local
npm run db:migrate
npm run db:seed                     # optional demo data
npm run dev

# 3. Cross-language fairness check
npm run fairness:verify
```

Open <http://localhost:3000/giveaways>. The admin panel needs real Discord OAuth2
credentials; everything else works offline against a local SQLite file.

## Deploy

Bot on **Render** (background worker), dashboard on **Render** or **Vercel** —
both supported, `render.yaml` wires up the bot and dashboard together.

Full walkthrough, including Discord application setup, bot permissions and the
intents required for message counting: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

```bash
docker compose up -d --build    # bot + dashboard locally
```

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Processes, schema, state machine, three-phase draw, jobs |
| [FAIRNESS.md](docs/FAIRNESS.md) | The draw algorithm, what an operator can and cannot do, how to verify |
| [SECURITY.md](docs/SECURITY.md) | AuthN/Z, CSRF, rate limits, validation, threat model, limitations |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Turso, Discord portal, Render, Vercel, troubleshooting |
| [MESSAGE_REQUIREMENTS.md](docs/MESSAGE_REQUIREMENTS.md) | Message-activity feature, for users and admins |
| [MESSAGE_REQUIREMENTS_IMPL.md](docs/MESSAGE_REQUIREMENTS_IMPL.md) | Design rationale, efficiency, reliability |
| [ENTRANTS_ROLE.md](docs/ENTRANTS_ROLE.md) | Temporary role behaviour and safety properties |
| [shared/FAIRNESS_SPEC.md](shared/FAIRNESS_SPEC.md) | Normative cross-language algorithm spec |

## Why the draw is trustworthy

1. A SHA-256 **commitment to the random seed is published before anyone can
   enter**, in the Discord message and on the public page.
2. Scores are `HMAC-SHA256(seed, giveaway:user:entry)` reduced with **rejection
   sampling**, so every entrant has an identical chance.
3. The draw reads the sealed seed **back out of the database**; it cannot mint a
   convenient one at draw time.
4. Ranking has **no other inputs** — no weight, no priority, no owner override.
   A self-test asserts `draw_winners()` has not grown such a parameter.
5. After the draw, the seed and every participant's score are public. Recompute
   the winner yourself in about twenty lines at `/verify`, or fetch
   `/api/giveaways/<id>/verify`.

Two independent implementations (`bot/giveaway_bot/fairness.py` and
`dashboard/src/lib/fairness.ts`) are cross-checked against
`shared/test_vectors.json` on every run.

## License

MIT — see [LICENSE](LICENSE).