# Message activity requirements

A giveaway can require members to have posted a minimum number of messages
before they are allowed to enter. The rule is **optional** — when it is off, the
bot does no extra work at all.

---

## For server administrators

### Setting a requirement

**From Discord** when creating a giveaway:

```
/giveaway create
  title: Steam key giveaway
  prize: 3 game keys
  duration: 24h
  min_messages: 50
  message_channels:            # blank = count the whole server
```

**Changing it later:**

```
/admin give messages <giveaway_id> min_messages:100
/admin give messages <giveaway_id> min_messages:100 channels:<#general> <#chat>
/admin give messages <giveaway_id> min_messages:0          # disable
```

Add `revalidate:True` to also re-check everyone who is already entered:

```
/admin give messages <giveaway_id> min_messages:100 revalidate:True
```

> **Important:** revalidation is the only way this rule can remove an existing
> participant, and it always records who and why. Lowering or disabling the
> requirement never invalidates entries.

### Checking your own progress

```
/admin give progress <giveaway_id>
```

Shows a progress bar, your current count and exactly how many messages are left.
Anyone can run this on themselves at any time.

### How members experience it

Pressing **Enter giveaway** re-checks every rule, including the activity
requirement. If they have not reached the minimum, they get a progress bar with
their exact count and the number of messages remaining — no need to ask anyone,
and it updates automatically as they chat. Pressing the button again works the
instant they qualify.

The live giveaway embed shows the requirement, so the rule is never hidden.

### What counts

* **Human messages only.** Bots, webhooks and automated posts never count, and
  empty messages do not count either.
* **Guild-wide by default.** With `channels:` set, only those channels count.
* Counts are tracked **live** and survive bot restarts. If the bot misses events
  during a connection hiccup, it repairs the gap from message history
  automatically so nobody is unfairly blocked.

---

## For members

You cannot see other people's counts, and there is no way to farm the counter:
bot messages, webhooks and empty posts are ignored, and the requirement is
re-checked live against real server messages.

---

## For auditors

The rule is implemented in a small, readable set of places:

| Concern | File |
| --- | --- |
| The gate itself | `bot/giveaway_bot/eligibility.py` (`INSUFFICIENT_MESSAGES`) |
| Count storage | `bot/giveaway_bot/repositories/activity.py` |
| Runtime buffering | `bot/giveaway_bot/activity.py` |
| Rule changes | `service.set_message_requirement()` in `bot/giveaway_bot/service.py` |
| Schema | `shared/migrations/0005_message_activity.sql` |
| Audit | `audit_log` actions `giveaway.message_requirement_changed`, `giveaway.message_activity_revalidated` |

Two properties are worth stating explicitly:

1. **The rule cannot bias the winner selection.** It only filters *who is
   eligible*; the draw still ranks purely by
   `HMAC(seed, giveaway:user:entry)` as specified in
   [`shared/FAIRNESS_SPEC.md`](../shared/FAIRNESS_SPEC.md). Eligibility is
   settled *before* the seed is read.
2. **Counts cannot be inflated by replay.** Backfills skip any message at or
   below the stored high-water mark, so re-running a range is a no-op.

The full design rationale, efficiency argument and test coverage are in
[`MESSAGE_REQUIREMENTS_IMPL.md`](MESSAGE_REQUIREMENTS_IMPL.md).