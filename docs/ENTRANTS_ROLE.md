# Temporary entrants role

When someone enters a giveaway they are given a temporary **Giveaway Entrants**
role. When the giveaway ends, the bot removes it from everyone.

The point is practical: staff can ping one role to reach every entrant, instead
of copying a long list of mentions.

---

## Behaviour

| Event | What happens |
| --- | --- |
| Member enters | Receives the entrants role |
| Member leaves the giveaway | Role removed from them |
| Member is disqualified | Role removed from them |
| Giveaway ends (timer or manual) | Role removed from everyone the bot granted it to |
| Giveaway cancelled without a draw | Role removed from everyone |
| Bot restarts / crashes | Grants and revokes are retried automatically |

The role is created automatically the first time it is needed and reused for
later giveaways in the same server. It is **mentionable**, so staff can ping it.

### Requirements

The bot needs the **Manage Roles** permission. Without it the giveaway still
works end to end — entrants just do not receive the role, and the bot says so
plainly rather than failing silently. The role also needs to sit below the bot's
highest role, which is the case for roles it creates itself.

---

## Commands

```
/admin give entrants          # shows the current giveaway + the role to ping
/admin give end <id>          # ends it; the role is removed automatically
/admin give cancel <id>       # cancels it; the role is removed too
/admin give flag <id> user:X  # flags a member; their role is removed
```

---

## One giveaway at a time

Each server runs **a single giveaway at a time**. Starting a second one is
refused with a clear message naming the existing giveaway.

This is what makes the role unambiguous: "the entrants role" always means exactly
one giveaway's entrants, so `/admin give entrants` and a role ping are always
correct. Ended giveaways free the slot immediately, so the next one can start.

---

## Safety properties

These are the parts worth reviewing, because they are where this kind of feature
usually goes wrong.

### We only remove roles we added

Every entry records *how* the member got the role:

| `grant_source` | Meaning | Removed on end? |
| --- | --- | --- |
| `bot` | The bot added it after a successful entry | **Yes** |
| `manual` | A human assigned it themselves | **No** |
| `revoked` | We already removed it | — (no-op) |

This matters: stripping the role unconditionally would delete a permission a
moderator granted on purpose. `entries.users_with_bot_role()` returns only
`bot` rows, and that is the exact set released on end.

### Grants and revokes cannot be lost

Discord API calls are not transactional with our SQL, so they are journalled in
`giveaway_role_tasks` and retried until they succeed:

* A crash between "entry recorded" and "role added" is repaired on restart.
* A failed revoke is retried (up to 8 attempts), then recorded as `failed` —
  never silently dropped, which would strand members with a stale role.
* A duplicate enqueue is a no-op thanks to a unique index on
  `(giveaway_id, user_id, action)`, so a retried grant cannot double-apply.
* `_reconcile_roles()` runs on startup and re-applies the role to current
  entrants and removes it from anyone who no longer belongs.

### A role failure never blocks an entry

If the role cannot be granted, the member is **still entered**. The confirmation
embed says so explicitly:

> You are entered, but I could not give you the Giveaway Entrants role. That only
> affects how staff reach entrants — your entry is valid.

A cosmetic convenience must never cost someone their entry.

### Members who left the server

A grant or revoke for someone no longer in the guild is treated as complete
rather than retried forever.

---

## Where it lives

| Concern | File |
| --- | --- |
| Role resolution, grant, release, reconcile | `bot/giveaway_bot/roles.py` |
| Grant/revoke journal | `repositories/control.py` (`role_task`, `claim_role_tasks`, `complete_role_task`) |
| Entry provenance | `repositories/entries.py` (`users_with_bot_role`, `has_bot_grant`, `mark_grants_revoked`) |
| Single-active lookup | `repositories/giveaways.py` (`find_active`) |
| Wiring on entry/end/leave | `bot/giveaway_bot/bot.py` |
| Schema | `shared/migrations/0006_participant_role.sql` |

## Tests

`python -m giveaway_bot selftest` includes four checks:

| Check | Proves |
| --- | --- |
| `role is granted on entry and journalled` | Exactly one grant is queued, with the right member and role |
| `only bot grants are released` | A human-assigned role is never stripped; releases are not duplicated |
| `role tasks are retried, not lost` | Failures retry, then end as `failed`; duplicates are idempotent |
| `one active giveaway per guild` | Running/paused count as active; ending frees the slot |