# Security

What is actually enforced, and where. Anything claimed here should be findable in
the code by the path named.

## Authentication

| Layer | Mechanism | Code |
| --- | --- | --- |
| Discord login | OAuth2 authorization code, `identify guilds` | `src/app/api/auth/login`, `.../callback` |
| Login CSRF | Single-use random `state`, 10-minute TTL, consumed on use | `oauth_states` table |
| Session | `jose` JWE (HS256 + A256GCM), `httpOnly`, `secure`, `sameSite=lax` | `src/lib/session.ts` |
| Sign out | POST-only, so an `<img>` cannot log a user out | `src/app/api/auth/logout` |

The session carries identity only. It grants nothing on its own.

### Why the state parameter matters

Without a single-use `state`, an attacker can start an OAuth flow with *their*
code and feed it to the callback; the victim's browser would then adopt the
attacker's account. `oauth_states` is consumed atomically on first use, so a
replayed state fails.

## Authorization

**Every** privileged action re-verifies guild administration against Discord:

```
requireGuildAdmin(guildId)
  → cached guild_admins row younger than 5 minutes?  use it
  → else GET /guilds/{guild}/members/{user} with the BOT token
  → accept only if permissions & (ADMINISTRATOR | MANAGE_GUILD)
```

`src/lib/auth.ts`. Cached decisions expire in 5 minutes, so a demotion takes
effect promptly. A user who is not a member, or a guild the bot cannot see
members in, is refused rather than trusted from cache.

Read-only admin pages call the same function *before* querying giveaway data, so
an unauthorised request never reaches the data.

## CSRF

Mutations are Next.js Server Actions, which are same-origin POSTs — already
resistant to cross-site form posts. On top of that:

* an explicit **origin allowlist** check (`checkOrigin`), falling back to
  `Referer`, with `CSRF_TRUSTED_ORIGINS` / `NEXT_PUBLIC_APP_URL` /
  `VERCEL_URL` / `RENDER_EXTERNAL_URL` as sources
* a **double-submit CSRF token** (`gw_csrf`, `httpOnly: false` so JS can echo it,
  compared with `timingSafeEqual`)
* if the allowlist resolves to empty, the check **fails closed**

Every origin failure returns an identical 403 so the response reveals nothing
about which check failed.

## Rate limiting

Shared, database-backed fixed-window limiter (`rate_limits`), incremented with a
single UPSERT. Because the dashboard and the bot write to the same table, limits
hold across serverless instances rather than per-instance.

Defaults: 30 mutations per 60s per admin.

## Input validation

Two independent layers, and the bot is the trust boundary:

1. **Dashboard** — zod schemas in `src/lib/commands.ts`. Duration strings
   (`30m`, `2h30m`, `3d`) are parsed and range-checked; Discord IDs must match
   `[0-9]{15,25}`; prize images must be Discord CDN URLs.
2. **Bot** — `validation.py`, re-validating everything it receives. A dashboard
   bypass still cannot send a winner id, a role list that does not exist, an
   unbounded duration, or a channel the bot cannot post in
   (`queue.py::_validate_roles_exist`, `_validate_channel`).

All SQL uses bound parameters. Identifiers are restricted to a whitelist
(`CommandKind`), never interpolated from input.

## Secrets

* `SESSION_SECRET` must be ≥32 characters; the session code refuses to run
  otherwise
* `CONTROL_API_SECRET` (bot) requires ≥32 characters when the control API is
  enabled — enforced in `Settings`, so startup fails loudly
* Discord tokens and the Turso token are read from the environment only and are
  never logged; `repr=False` on the pydantic fields keeps them out of tracebacks
* IPs are stored only as a salted digest (`hash_ip`), never raw
* `.env` is git-ignored; `.env.example` contains no real values

## Response headers

Set in `next.config.mjs` for every route:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(), microphone=(), geolocation=()
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
```

## Privacy

Public pages and public APIs expose **only**: title, prize, rules (as counts),
entry/participant totals, status, timestamps, the seed commitment, and — after a
draw — the revealed seed, digest, manifest and winners.

They never expose: account timestamps, join timestamps, per-user message counts,
participant identities before a draw, or the actual role/channel ID lists (only
how many).

Participant lists, message counters and audit logs are admin-only and behind
`requireGuildAdmin`. The public snapshot is asserted in the bot's self-test to
contain no `account_created_at` / `guild_joined_at`.

## Fairness as a security property

An unrigged draw is part of the security story, so the relevant properties are
enforced structurally rather than by convention — see
[`FAIRNESS.md`](FAIRNESS.md). Briefly:

* the seed is sealed and its commitment published **before** entries open
* phase 2 of the draw reads the seed **from the database**, aborting if absent,
  so no code path can mint a convenient seed at draw time
* no function accepts a winner id, weight or priority — a self-test asserts the
  signature of `draw_winners` has not grown one

## Known limitations

Stated plainly rather than glossed over:

* An operator with direct database write access could forge a draw. The published
  pre-entry commitment makes that **detectable** (it was sent to Discord before
  any entries existed), not impossible.
* An operator can end or cancel a giveaway, or refuse to run one. Those are
  visible refusals in the audit log.
* An operator can disqualify participants (with a recorded reason) or reroll.
  Rerolls always publish a new seed and every round stays on record.
* Discord IDs are public information and appear on winner pages. That is
  unavoidable when announcing a Discord winner, and no other user data is shown.
* Server-side rendering trusts the database. If Turso is compromised, so is the
  data — mitigated for fairness specifically by the commitment check.

## Hardening checklist

- [ ] `SESSION_SECRET` is ≥32 random characters
- [ ] `DISCORD_REDIRECT_URI` matches the registered redirect exactly (no trailing slash drift)
- [ ] `NEXT_PUBLIC_APP_URL` and `CSRF_TRUSTED_ORIGINS` match the real hostname
- [ ] `DISCORD_BOT_TOKEN` has least privilege, and the bot's role is above the entrants role
- [ ] Server Members Intent is enabled

  Message Content Intent is deliberately NOT required and should stay off: the bot
  counts messages from gateway events and never reads message text. See
  docs/DEPLOYMENT.md section 2.
- [ ] Turso auth token is scoped to the one database, and rotated if leaked
- [ ] `discord.com/developers/applications` → the app is private/internal if you do not want it listed
- [ ] `npm audit --omit=dev` is clean
- [ ] `python -m giveaway_bot selftest` passes
- [ ] `npm run fairness:verify` passes