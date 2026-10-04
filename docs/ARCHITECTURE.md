# Architecture

## Processes

| Process | Runtime | Responsibility |
| --- | --- | --- |
| **Bot** | Python + discord.py, long-running | Discord state: messages, buttons, embeds, eligibility, draws, role grants |
| **Dashboard** | Next.js on Render/Vercel | Presentation and administration; OAuth2, RBAC, live updates |

They never call each other over HTTP. They share one database, and the bot polls
a queue the dashboard writes to.

```
 admin clicks "End giveaway"
        │
        ▼
 INSERT INTO command_queue (kind='giveaway.end', status='pending')
        │
        │   bot polls every 2s
        ▼
 UPDATE … SET status='claimed' WHERE id=? AND status='pending'   ← atomic, exclusive
        │
        ▼
 service.end()  →  lock → score → reveal  (three phases, one transaction each)
        │
        ▼
 status='succeeded' + audit_log row + giveaway_events row
        │
        ▼
 dashboard reads the result (or sees it over SSE)
```

Why a queue rather than HTTP:

* a compromised dashboard token cannot invent new bot capabilities — the bot only
  accepts its whitelisted command kinds
* Discord operations and SQL writes are not transactional together, so the queue
  makes the gap recoverable: a claimed-but-failed command is retried, and a crash
  mid-draw is finished by `recover_locked_draws()` on restart
* nothing needs an inbound port on the bot

## Database

One SQLite-dialect schema, `shared/migrations/*.sql`, applied by **both**
runtimes. Migrations are immutable: a file that changes after being applied aborts
`migrate()` rather than being silently skipped.

| File | Contents |
| --- | --- |
| `0001_core` | `guilds`, `guild_admins`, `giveaways`, `bot_state` |
| `0002_entries_draws` | `giveaway_entries`, `giveaway_draws`, `giveaway_winners`, `giveaway_stats` |
| `0003_control_plane` | `command_queue`, `audit_log`, `giveaway_events`, `oauth_states`, `rate_limits` |
| `0004_indexes` | hot-path indexes |
| `0005_message_activity` | `message_counters`, `message_counter_channels`, `message_channel_state` |
| `0006_participant_role` | `giveaway_role_tasks`, role columns on giveaways/entries |

Statements are separated by a `; statement-breakpoint` line. Both runners split
on that marker and both verify no chunk contains more than one statement, so a
missing separator is a loud error instead of a confusing one later.

Turso/libSQL in production; a local SQLite file when `TURSO_DATABASE_URL` is
unset.

## Giveaway state machine

```
        create                start                 end / timer
  ───────────────► scheduled ────────► running ───────────────► ended
                      │                 │  ▲                      ▲  │
                      │                 ▼  │                      │  │
                      └────────────► paused┘  └──── resume ─────────┘  │
                                                                        │
                                          reroll (new seed, same entries)┘
```

* `create` seals the seed and moves straight to `running` if no end time is
  required to be scheduled — the commitment is published **before entries open**.
* `paused` stores the remaining milliseconds, so resuming does not extend the
  giveaway.
* Exactly one giveaway may be `scheduled`/`running`/`paused` per guild. That is
  what makes the temporary entrants role unambiguous.

### The draw is three phases

1. **lock** — transaction 1: freeze the entry set, set `locked_at`, mark ended.
   For a reroll this also mints and publishes a *new* seed and commitment.
2. **score** — pure computation, using the seed read back **out of the
   database** rather than from memory.
3. **reveal** — transaction 2: write the draw, the winners, and the revealed
   seed.

Reading the seed back from storage is what makes "the seed was fixed before
entries existed" a structural property rather than a convention: phase 2 aborts
with `LookupError` if there is no sealed seed, instead of quietly minting a fresh
one.

A crash between phases leaves `locked_at` set with no `giveaway_draws` row;
`recover_locked_draws()` finishes it on the next startup.

## Fairness

See [`FAIRNESS.md`](FAIRNESS.md) and the normative
[`../shared/FAIRNESS_SPEC.md`](../shared/FAIRNESS_SPEC.md). Summary: the seed's
SHA-256 commitment is published before entries open, scores are
`HMAC-SHA256(seed, giveaway:user:entry)` reduced with rejection sampling, and
ranking is a total order on `(score, user_id, entry_seq)` with no other inputs.

Two implementations exist — `bot/giveaway_bot/fairness.py` and
`dashboard/src/lib/fairness.ts` — and `npm run fairness:verify` proves they
agree against `shared/test_vectors.json`.

## Message-activity counting

See [`MESSAGE_REQUIREMENTS_IMPL.md`](MESSAGE_REQUIREMENTS_IMPL.md). One row per
user, batched flushes on a thread pool, zero work when no giveaway needs it, and
idempotent backfill after gateway gaps.

## Entrants role

See [`ENTRANTS_ROLE.md`](ENTRANTS_ROLE.md). Grants and revokes are journalled in
`giveaway_role_tasks` and retried, because a Discord call is not transactional
with our SQL. Only roles the bot granted are removed.

## Scheduled jobs (bot)

| Job | Interval | Purpose |
| --- | --- | --- |
| `end_due` | 5s | draw giveaways whose deadline passed |
| `queue` | 2s | claim and execute dashboard commands |
| `refresh` | 30s | update countdown embeds (budgeted) |
| `activity_flush` | 5s | flush buffered message counters |
| `activity_backfill` | 60s | repair counts after a gateway resume |
| `role_tasks` | 15s | retry pending entrants-role grants/revokes |
| `maintenance` | 5m | prune oauth states, rate limits, old events |

Each job runs in isolation: a failing job logs and retries without affecting the
others.

## Request flows

**Public giveaway page** — server-rendered, `force-dynamic`, 15s CDN cache.
Reads `giveaways` + `giveaway_stats` + `guilds`. Exposes counts, never identity.

**Public JSON** — `GET /api/giveaways/{id}`, plus `/verify` returning the seed,
commitment, digest, manifest and a recomputed verification, and `/stream`
(SSE, 55s cap, heartbeat, auto-reconnect).

**Admin action** — Server Action → `guard()` (origin/CSRF → session →
rate limit → Manage Server) → zod validation → `INSERT INTO command_queue`.

**Bot button** — `on_interaction` → `service.join/leave` →
`eligibility.evaluate_join` → entry + audit + event in one transaction →
role grant journalled → embed re-rendered.