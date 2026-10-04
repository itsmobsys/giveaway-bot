# Provably fair draw — normative specification

**Status:** normative. `bot/giveaway_bot/fairness.py` (Python) and
`dashboard/lib/fairness.ts` (TypeScript) are independent implementations of
*this* document. `shared/test_vectors.json` contains cross-language test
vectors produced by the Python implementation and asserted by both, so the two
cannot silently drift.

**Algorithm id:** `hmac-sha256-commit-reveal/v1`

---

## 1. Vocabulary

| Term | Meaning |
| --- | --- |
| `giveaway_id` | Public ULID-ish identifier of the giveaway. Fixed for the lifetime of the giveaway. |
| `user_id` | Discord snowflake, decimal ASCII string (no sign, no padding). |
| `entry_seq` | Which of the participant's entries this is, `1 .. max_entries_per_user`. Decimal ASCII, **no zero padding**. |
| `server_seed` | 32 bytes from the OS CSPRNG, hex encoded (64 lowercase hex chars). |

## 2. Commitment (before anybody can enter)

When a giveaway moves to `running`, the bot draws a fresh 32-byte `server_seed`
from the CSPRNG and publishes

```
seed_commitment = lowercase_hex(sha256(server_seed_bytes))
```

in three places: the `giveaways.seed_commitment` column, the Discord embed
footer, and the public dashboard page. The seed itself is written to
`giveaways.server_seed` but is treated as **sealed** — nothing reads it until
the draw executes, and no code path can replace it.

Rationale: because the commitment is a hash of a value the operator could in
principle have chosen after seeing entries, the commitment being published
*before* entries are accepted is what makes the seed unfalsifiable. Rerolls
always use a brand-new seed and a brand-new commitment (see §6).

## 3. Per-entry score

For each eligible entry:

```
key  = server_seed_bytes                        # 32 bytes
msg  = ascii( giveaway_id ":" user_id ":" entry_seq )
h0   = hmac_sha256(key, msg)                    # 32 bytes
```

Convert `h0` to a big-endian unsigned integer `r` (`r = int.from_bytes(h0, "big")`).

To make `r mod n` exactly uniform over the `n` frozen entries we use
**rejection sampling** against the largest multiple of `n` that fits in 2²⁵⁶:

```
n        = number of frozen entries (>= 1)
limit    = floor(2**256 / n) * n
attempt  = 0
while r >= limit:
    attempt += 1
    h = hmac_sha256(key, ascii(msg ":" str(attempt)))
    r  = int.from_bytes(h, "big")
score = r // n            # integer in [0, limit/n) ⊂ [0, 2**256)
```

`score` is stored as a decimal string so neither Python `int` nor JavaScript
`BigInt` can lose precision or disagree.

## 4. Ranking

1. Sort frozen entries by `score` **ascending**.
2. Ties (cryptographically improbable, but must be deterministic) are broken by
   `user_id` ascending as ASCII, then `entry_seq` ascending.
3. The first `winner_count` entries win, ranked `1 .. winner_count`.
4. If `n < winner_count`, every entry wins and the shortfall is stated in the
   announcement and audit record. There is no padding, no duplicate winner, and
   no substitute chosen from outside the participant set.

## 5. Participant freezing and the participant digest

Immediately before scoring, the entry set is frozen and hashed:

```
entries_sorted = sort by (user_id ASC, entry_seq ASC)
lines           = ["{user_id}:{entry_seq}" for each entry]
participant_digest = sha256( ascii( "\n".join(lines) ) )
```

Only rows in `giveaway_entries` with `status = 'valid'` are frozen. The digest,
the seed, the commitment, the counts and the full per-entry score manifest are
written to `giveaway_draws` **in the same transaction as the winners**, so a
draw is all-or-nothing.

## 6. Rerolls

A reroll is just another draw with `round = previous_round + 1`:

* a **new** CSPRNG `server_seed` is generated and a **new** commitment published
  before scoring;
* the previous seed is revealed in the announcement so a reroll is provably not
  a re-run of the same randomness;
* the same frozen entry set is reused (entries are never silently removed
  between rounds — see §8), and `giveaway_winners` keeps every previous round
  forever, so winner history is complete.

## 7. What the dashboard publishes

The public giveaway page and `GET /api/giveaways/{id}/verify` expose:

* `algorithm`, `algorithm_version`
* `seed_commitment` (from creation time)
* `server_seed` (revealed after the draw)
* `participant_digest`
* `participant_count`, `eligible_count`, `winner_count`
* `manifest`: the frozen entry list with each entry's `score` and `retry_count`

Anyone can recompute the scores from `server_seed` alone using the 20 lines of
code above. The TypeScript implementation shipped in this repo is one such
verifier; so is the "verify" button on the giveaway page.

## 8. Explicit non-goals — things the system will never do

These are absent **by design**, and their absence is what makes the draw fair:

* **No owner/admin override.** There is no code path anywhere that accepts a
  `winner_user_id`, a `weight`, a `priority`, or any other ordering input.
  Grep for `winner_count`, `score` and `ORDER BY` in `fairness.py` — the ranking
  is fully determined by `score, user_id, entry_seq`.
* **No seeded PRNG.** `random`, `Math.random`, `rand`, `secrets.randbelow`
  (biased) are never used for scoring. Only `secrets.token_bytes` / WebCrypto
  CSPRNG feed the seed.
* **No post-hoc seed selection.** The seed is committed before entries; the
  draw cannot choose a seed.
* **No silent entry removal.** With `FAIRNESS_FREEZE_ENTRIES_ON_DRAW=true`
  (default) once `locked_at` is set, entry rows can only be *flagged*
  (`valid -> disqualified`) with a recorded reason; hard deletion is refused by
  the repository layer. Flagging is itself audited and is visible in the draw
  manifest.
* **No cross-language ambiguity.** Scores are integer strings; sorting is over
  decimal digit strings of equal-arbitrary length compared numerically via
  `BigInt`/`int`, never via lexicographic string compare.

## 9. Threat model summary

| Adversary | Capability | Outcome |
| --- | --- | --- |
| Guild owner | Sees entries, controls the bot, can restart/patch it | Cannot choose a winner. Can end a giveaway, refuse to run it, or cancel it — all of which are logged and publicly visible, and are *visible refusals*, not bias. |
| Bot operator with DB write access | Can write to `giveaways.server_seed` | Can forge any draw they like. Mitigated by the *published pre-entry commitment*: the commitment embedded in the Discord message history was sent before entries existed, so a forged post-hoc seed cannot match it. Independent auditors compare commitments. |
| Participant | Owns many alts | Bounded by `max_entries_per_user`, `entry_limit`, min-account-age and min-join-age filters, all public. Buying alts raises `n` and therefore lowers each alts's probability — it does not improve an honest participant's odds beyond what the published rules say. |
| Network attacker | Observes Discord/dashboard traffic | Gains nothing: score inputs are public after the draw and were committed before it. |