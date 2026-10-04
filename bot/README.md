# Giveaway bot (Python)

Discord giveaway bot with a **provably fair** winner draw. See the
[repository README](../README.md) for the full picture and
[`shared/FAIRNESS_SPEC.md`](../shared/FAIRNESS_SPEC.md) for the draw algorithm.

```bash
python -m pip install -e ".[dev]"
python -m giveaway_bot migrate
python -m giveaway_bot selftest     # 20+ checks, no token or network required
python -m giveaway_bot doctor
python -m giveaway_bot run
```

## Layout

| Path | Responsibility |
| --- | --- |
| `giveaway_bot/fairness.py` | The entire randomness surface. Auditable in isolation. |
| `giveaway_bot/eligibility.py` | Pure eligibility rules (roles, age, channel, limits). |
| `giveaway_bot/service.py` | Business logic, state machine, three-phase draw. |
| `giveaway_bot/validation.py` | Strict validation of every untrusted input. |
| `giveaway_bot/repositories/` | Explicit SQL. No ORM. |
| `giveaway_bot/queue.py` | Executes dashboard-originated commands. |
| `giveaway_bot/bot.py` | discord.py plumbing and rendering only. |
| `giveaway_bot/selftest.py` | Verification suite + cross-language test vectors. |

## Design rules

1. No randomness outside `fairness.py`. No `random`/`Math.random` in selection.
2. No place accepts a winner id, weight or priority.
3. Every mutation writes an audit row in the same transaction.
4. The bot re-validates everything the dashboard sends (defence in depth).
5. The commit–reveal seed is sealed *before* entries open.