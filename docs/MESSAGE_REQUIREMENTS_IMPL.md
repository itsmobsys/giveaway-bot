"""Message-activity requirement — implementation notes and rationale.

Public specification for users: [`docs/MESSAGE_REQUIREMENTS.md`](../docs/MESSAGE_REQUIREMENTS.md).
Normative draw rules (unaffected by this feature): `shared/FAIRNESS_SPEC.md`.

---

## 1. What the rule is

Every giveaway carries an optional requirement:

| Setting | Meaning |
| --- | --- |
| `min_messages` | Messages a member must have sent to be eligible. `0` = **disabled**. |
| `message_count_scope` | `guild` (default) counts the whole server; `channel` counts only the listed channels. |
| `message_count_channel_ids` | The channels that count when scope is `channel`. |
| `message_count_ignore_bots` | Bots/webhooks never count. Always on in practice. |
| `message_count_since` | Optional clean-slate timestamp. |

A member qualifies when `messages counted >= min_messages`. The check runs in
`eligibility.evaluate_join`, the **single** gate used by the Join button, the
`/giveaway join` command and the dashboard — so they cannot disagree.

## 2. Why `min_messages` is checked last

`evaluate_join` returns the *first* failing rule. Ordering matters for the
message shown to the member:

```
status -> locked -> channel -> membership -> blacklist -> required roles
       -> account age -> join age -> per-user entry limit -> entry cap
       -> message requirement
```

Placing the activity rule last means a user who is in the wrong channel or
lacks a required role is told **that**, instead of being sent away to send
messages for a giveaway they could never enter anyway.

## 3. Storage: one row per user, never one row per message

A per-message table would grow without bound and make every count a `COUNT(*)`
over the whole server. Instead:

* `message_counters` — one row per `(guild_id, user_id)`, incremented with one
  UPSERT per flush.
* `message_counter_channels` — one row per `(guild_id, user_id, channel_id)`,
  populated **only** for channels a running giveaway is watching. This keeps the
  table bounded by *(watched channels × active users)* rather than by traffic,
  and it is what makes a channel-scoped requirement **exact** instead of an
  approximation.
* `message_channel_state` — a single high-water mark per channel.

Tracking thousands of users therefore costs one small indexed row each and no
per-message history.

## 4. Efficiency: buffering, and knowing when to do nothing

`activity.MessageActivityTracker` implements the hot path:

1. **Zero-cost when unused.** If no *running* giveaway in the guild has
   `min_messages > 0`, the message is dropped after a dict lookup — no lock, no
   allocation, no query. `refresh_requirements()` recomputes this set on
   startup, after every create/edit, and in a maintenance job.
2. **Batched writes.** Events buffer in memory and flush when 500 events or 5
   seconds accumulate. A busy channel does **not** produce one write per message.
3. **Off the gateway thread.** Flushes run on a 2-worker `ThreadPoolExecutor` so
   a slow database write can never delay a gateway event (which risks Discord
   dropping the connection).
4. **Filtered before counting.** Bots, webhooks, non-numeric IDs and empty
   messages never enter the buffer.
5. **The counter is not queried when the rule is off.** `service.join` fetches
   the count only if `giveaway.min_messages > 0`.

## 5. Reliability across restarts and duplicates

Discord may drop `MESSAGE_CREATE` events on a resume, so naive counting drifts
low and would *unfairly* block members. Three mechanisms prevent that:

* **Per-channel high-water mark** (`message_channel_state.last_message_id`),
  persisted on every flush. `on_resumed` flags the guild, and a scheduled job
  calls `backfill_channel()` to fetch recent history and repair the gap.
* **Idempotent ingestion.** `activity.backfill_messages` skips any message whose
  id is `<=` the stored high-water mark. Re-running an overlapping or replayed
  range **cannot** inflate a count — verified by
  `test_message_count_idempotency`, which replays a range twice.
* **Flush atomicity.** A batch is applied inside one transaction, so a crash
  leaves either all or none of it applied.

Every counter also carries an `exactness` value — `exact`, `backfilled` or
`estimated` — so the dashboard can be honest about how much a number can be
trusted.

## 6. Interaction with the draw (and with fairness)

The activity rule can only ever *remove* candidates, and only in ways that are
explicit and logged:

* **Lowering or removing the requirement never invalidates existing entries.**
  Participants are judged by the rule in force when they entered. Lowering a bar
  must never invalidate anyone's entry.
* **`revalidate_message_activity()`** is the only method that flags members for
  insufficient activity. It is always explicit, always records a reason, and
  writes a `giveaway.message_activity_revalidated` audit row. It is *never*
  triggered implicitly by a draw.
* When the giveaway ends with a requirement set, `queue._handle_end` runs that
  revalidation **before** entries are frozen, then publishes the outcome on the
  winner announcement ("N participants checked, M did not meet it").

This keeps the commit–reveal guarantee intact: entry eligibility is settled
*before* the seed is read, and the draw itself still ranks purely by
`score, user_id, entry_seq`.

## 7. Audit trail

Every change writes `audit_log` with `before`/`after` JSON:

| Action | Meaning |
| --- | --- |
| `giveaway.message_requirement_changed` | Rule edited, enabled or disabled (includes the disable) |
| `giveaway.message_activity_revalidated` | Participants re-checked; counts of flagged/restored |

Disabling the rule is logged just like enabling it, and
`metadata.changed` distinguishes a real edit from a no-op.

## 8. Privacy

* **Public pages** expose only the rule itself (`enabled`, `min_messages`,
  `scope`, `channel_count`) — never any individual's count.
* **Per-user counts** are admin-only data. `top_counters` exists for the
  dashboard's admin view and is not reachable from a public route.
* Participant lists continue to exclude account timestamps; the public snapshot
  is asserted not to contain `account_created_at` / `guild_joined_at`.

## 9. Commands

```
/giveaway create  …  min_messages:50          # set at creation
/admin give messages <id> min_messages:100     # change later
/admin give messages <id> min_messages:0       # disable
/admin give messages <id> min_messages:100 channels:<#123> revalidate:True
/admin give progress <id>                      # your own progress
```

The dashboard exposes the same operations through the command queue
(`giveaway.message_requirement`, `giveaway.activity_revalidate`).

## 10. Test coverage

`python -m giveaway_bot selftest` includes five dedicated checks:

| Check | Proves |
| --- | --- |
| `message-activity requirement gates entry` | Boundary is inclusive; earlier rules win; progress is reported |
| `message requirement validation` | `0` disables; channel scope needs channels; bad scope/limits rejected |
| `counters gate the join button` | 0/4/5 messages → blocked/blocked/allowed; disable lifts the block |
| `duplicate/backfilled events cannot inflate counts` | Replayed and bot messages never counted |
| `tracker skips work and flushes in batches` | No work when unused; buffering; batch flush |
| `revalidation flags only who fails today` | Only non-qualifiers leave the draw pool; audited |