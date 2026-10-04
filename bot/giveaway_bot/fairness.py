"""Provably fair winner selection.

This module is the *entire* randomness surface of the project.  It is
deliberately small, dependency-free and dependency-injectable so that it can be
read, audited and unit tested in isolation.

Normative specification: ``shared/FAIRNESS_SPEC.md``.

Guarantees implemented here
--------------------------
1. The 32-byte seed comes from the OS CSPRNG (``secrets.token_bytes``).
2. The seed's SHA-256 commitment is published **before** entries are accepted,
   so an operator cannot pick a favourable seed after seeing participants.
3. Scores are ``HMAC-SHA256(server_seed, giveaway_id:user_id:entry_seq)``
   reduced modulo the entry count with rejection sampling, making the reduction
   exactly uniform (no modulo bias).
4. Ranking is a total order on ``(score, user_id, entry_seq)``.  Nothing else is
   an input - there is no weight, no priority, no owner override.
5. Scores are exchanged as decimal strings so Python and JavaScript agree bit
   for bit (see the test vectors in ``shared/test_vectors.json``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Bumped whenever the derivation changes; stored on every draw row.
ALGORITHM_VERSION = "v1"
ALGORITHM_ID = "hmac-sha256-commit-reveal"
METHOD = f"{ALGORITHM_ID}/{ALGORITHM_VERSION}"

#: 2**256, used for the rejection-sampling bound.
_UINT256 = 1 << 256

SEED_BYTES = 32


# --------------------------------------------------------------------------- #
# Seeds and commitments
# --------------------------------------------------------------------------- #
def generate_seed(rng: Callable[[int], bytes] | None = None) -> str:
    """Return 32 CSPRNG bytes as lowercase hex.

    ``rng`` exists only so tests can inject a deterministic source; production
    code must never pass it.
    """
    raw = (rng or secrets.token_bytes)(SEED_BYTES)
    if len(raw) != SEED_BYTES:  # pragma: no cover - defensive
        raise ValueError(f"seed source produced {len(raw)} bytes, expected {SEED_BYTES}")
    return raw.hex()


def seed_bytes(seed_hex: str) -> bytes:
    """Decode a hex seed, validating length and charset."""
    if not isinstance(seed_hex, str) or len(seed_hex) != SEED_BYTES * 2:
        raise ValueError("seed must be a 64 character hex string")
    try:
        raw = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise ValueError("seed is not valid hex") from exc
    if len(raw) != SEED_BYTES:
        raise ValueError("seed must decode to 32 bytes")
    return raw


def commitment(seed_hex: str) -> str:
    """``sha256(server_seed)`` in lowercase hex - published before entries open."""
    return hashlib.sha256(seed_bytes(seed_hex)).hexdigest()


def verify_commitment(seed_hex: str, expected: str) -> bool:
    """Constant-time commitment check."""
    return hmac.compare_digest(commitment(seed_hex), expected.strip().lower())


def entry_message(giveaway_id: str, user_id: str, entry_seq: int) -> bytes:
    """Canonical byte string that gets HMAC'd.

    ``entry_seq`` is rendered as a bare decimal integer with **no** zero
    padding, so ``1`` never becomes ``01`` on one side of the wire.
    """
    if not giveaway_id or ":" in giveaway_id:
        raise ValueError("giveaway_id must be non-empty and contain no ':'")
    if not user_id.isdigit():
        raise ValueError("user_id must be a decimal string")
    if entry_seq < 1:
        raise ValueError("entry_seq must be >= 1")
    return f"{giveaway_id}:{user_id}:{entry_seq}".encode()


def score_entry(
    giveaway_id: str,
    user_id: str,
    entry_seq: int,
    seed_hex: str,
    total_entries: int,
) -> tuple[str, int]:
    """Return ``(score_decimal_string, rejection_attempts)`` for one entry.

    ``total_entries`` (``n``) must be the size of the *entire* frozen entry set;
    every participant must derive against the same ``n`` or the result is not
    verifiable.
    """
    if total_entries < 1:
        raise ValueError("total_entries must be >= 1")

    key = seed_bytes(seed_hex)
    msg = entry_message(giveaway_id, user_id, entry_seq)

    # Largest multiple of n that fits in 2**256 -> unbiased reduction bound.
    limit = (_UINT256 // total_entries) * total_entries

    digest = hmac.new(key, msg, hashlib.sha256).digest()
    value = int.from_bytes(digest, "big")
    attempts = 0
    while value >= limit:
        attempts += 1
        if attempts > 256:  # pragma: no cover - ~2**-256 probability
            raise RuntimeError("rejection sampling failed to converge")
        retry_msg = msg + b":" + str(attempts).encode()
        digest = hmac.new(key, retry_msg, hashlib.sha256).digest()
        value = int.from_bytes(digest, "big")

    return str(value // total_entries), attempts


def participant_digest(entries: Iterable[tuple[str, int]]) -> str:
    """SHA-256 over the frozen entry set, sorted for canonical ordering.

    Each line is ``"{user_id}:{entry_seq}"``; lines are sorted by (user_id,
    entry_seq) numerically/ASCII and joined with ``\\n``.
    """
    normalised = sorted(
        ((str(user_id), int(seq)) for user_id, seq in entries),
        key=lambda item: (item[0], item[1]),
    )
    payload = "\n".join(f"{user_id}:{seq}" for user_id, seq in normalised)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DrawEntry:
    """One frozen entry participating in a draw."""

    user_id: str
    entry_seq: int
    entry_id: int | None = None
    display_name: str | None = None  # never used for ordering, presentation only

    def as_public(self) -> dict[str, Any]:
        """Public projection: no private account data crosses the wire."""
        return {
            "entry_id": self.entry_id,
            "user_id": self.user_id,
            "entry_seq": self.entry_seq,
            "display_name": self.display_name,
        }


@dataclass(frozen=True, slots=True)
class Winner:
    rank: int
    user_id: str
    entry_seq: int
    entry_id: int | None
    score: str

    def as_public(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "user_id": self.user_id,
            "entry_seq": self.entry_seq,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class DrawResult:
    """Fully reproducible result of one draw round."""

    giveaway_id: str
    round_number: int
    seed: str
    commitment: str
    participant_count: int
    winner_count_requested: int
    winners: tuple[Winner, ...]
    scores: tuple[tuple[str, int, str, int], ...]  # (user_id, entry_seq, score, attempts)
    participant_digest: str
    shortfall: int = 0
    algorithm: str = METHOD
    metadata: dict[str, Any] = field(default_factory=dict)

    def manifest(self) -> dict[str, Any]:
        """Everything a third party needs to recompute this draw offline."""
        return {
            "algorithm": self.algorithm,
            "algorithm_version": ALGORITHM_VERSION,
            "giveaway_id": self.giveaway_id,
            "round": self.round_number,
            "seed": self.seed,
            "seed_commitment": self.commitment,
            "participant_digest": self.participant_digest,
            "participant_count": self.participant_count,
            "winner_count": len(self.winners),
            "winner_count_requested": self.winner_count_requested,
            "shortfall": self.shortfall,
            "winners": [winner.as_public() for winner in self.winners],
            "scores": [
                {"user_id": user_id, "entry_seq": seq, "score": score, "retry_count": retries}
                for user_id, seq, score, retries in self.scores
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.manifest(), separators=(",", ":"), sort_keys=True)


def draw_winners(
    giveaway_id: str,
    entries: Sequence[DrawEntry],
    winner_count: int,
    *,
    seed_hex: str | None = None,
    round_number: int = 1,
    rng: Callable[[int], bytes] | None = None,
    with_scores: bool = True,
) -> DrawResult:
    """Run one draw round.

    Parameters
    ----------
    entries:
        The frozen, eligibility-validated entry set.  Duplicates are impossible
        (``UNIQUE(giveaway_id, user_id, entry_seq)``) and are defensively
        de-duplicated here anyway so that a bug upstream cannot silently skew
        the distribution.
    winner_count:
        How many winners to select.  Clamped to ``[0, len(entries)]``.
    seed_hex:
        Reuse an existing (already revealed) seed.  Production callers pass
        ``None`` for every round, which mints a fresh seed; rerolls therefore
        always get new randomness.
    """
    if winner_count < 0:
        raise ValueError("winner_count must be >= 0")

    seed_hex = seed_hex or generate_seed(rng)
    seed_commitment = commitment(seed_hex)

    # Canonical, duplicate-free participant set.
    seen: set[tuple[str, int]] = set()
    frozen: list[DrawEntry] = []
    for entry in entries:
        key = (str(entry.user_id), int(entry.entry_seq))
        if key in seen:
            continue
        # Fail loudly on malformed ids rather than letting a non-numeric user_id
        # reach the HMAC, where it would raise deep inside scoring.
        if not key[0].isdigit():
            raise ValueError(f"entry user_id must be a decimal string, got {key[0]!r}")
        seen.add(key)
        frozen.append(entry)
    frozen.sort(key=lambda item: (item.user_id, item.entry_seq))

    total = len(frozen)
    digest = participant_digest((entry.user_id, entry.entry_seq) for entry in frozen)

    if total == 0:
        return DrawResult(
            giveaway_id=giveaway_id,
            round_number=round_number,
            seed=seed_hex,
            commitment=seed_commitment,
            participant_count=0,
            winner_count_requested=winner_count,
            winners=(),
            scores=(),
            participant_digest=digest,
            shortfall=winner_count,
        )

    scores: list[tuple[str, int, str, int]] = []
    for entry in frozen:
        score, retries = score_entry(
            giveaway_id, entry.user_id, entry.entry_seq, seed_hex, total
        )
        scores.append((entry.user_id, entry.entry_seq, score, retries))

    # Total order: score first, then deterministic tie-breakers.
    ordered = sorted(
        zip(scores, frozen, strict=True),
        key=lambda pair: (int(pair[0][2]), pair[0][0], pair[0][1]),
    )

    take = min(winner_count, total)
    winners = tuple(
        Winner(
            rank=index + 1,
            user_id=scored[0],
            entry_seq=scored[1],
            entry_id=entry.entry_id,
            score=scored[2],
        )
        for index, (scored, entry) in enumerate(ordered[:take])
    )

    return DrawResult(
        giveaway_id=giveaway_id,
        round_number=round_number,
        seed=seed_hex,
        commitment=seed_commitment,
        participant_count=total,
        winner_count_requested=winner_count,
        winners=winners,
        scores=tuple(scores) if with_scores else (),
        participant_digest=digest,
        shortfall=max(0, winner_count - total),
    )


# --------------------------------------------------------------------------- #
# Independent verification (used by the bot's /verify and the dashboard)
# --------------------------------------------------------------------------- #
def verify_draw(
    *,
    giveaway_id: str,
    seed_hex: str,
    expected_commitment: str,
    expected_digest: str,
    manifest: dict[str, Any] | str,
) -> dict[str, Any]:
    """Recompute a stored manifest and report every check performed.

    Returns ``{"ok": bool, "checks": [...], "errors": [...], "recomputed": {...}}``.
    Never raises for a *bad* manifest - a verification failure is a result, not
    an exception, because this is exactly what auditors call.
    """
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "checks": [],
                "errors": [f"manifest is not valid JSON: {exc}"],
                "recomputed": None,
            }

    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def record(name: str, expected: Any, actual: Any) -> None:
        ok = expected == actual
        checks.append({"name": name, "expected": expected, "actual": actual, "ok": ok})
        if not ok:
            errors.append(f"{name} mismatch")

    record("seed_commitment", expected_commitment.lower(), commitment(seed_hex))
    record("participant_digest", expected_digest.lower(), manifest.get("participant_digest"))

    entries = [
        DrawEntry(user_id=str(item["user_id"]), entry_seq=int(item["entry_seq"]))
        for item in manifest.get("scores", [])
    ]
    total = len(entries)
    if total != int(manifest.get("participant_count", -1)):
        errors.append("participant_count does not match the number of scores")
        checks.append(
            {
                "name": "participant_count",
                "expected": manifest.get("participant_count"),
                "actual": total,
                "ok": False,
            }
        )

    recomputed: list[Winner] = []
    if total >= 1:
        for entry in entries:
            try:
                score, _ = score_entry(
                    giveaway_id, entry.user_id, entry.entry_seq, seed_hex, total
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            declared = next(
                (
                    item
                    for item in manifest["scores"]
                    if str(item["user_id"]) == entry.user_id
                    and int(item["entry_seq"]) == entry.entry_seq
                ),
                None,
            )
            if declared is None:
                errors.append(f"missing score for {entry.user_id}#{entry.entry_seq}")
                continue
            record(f"score[{entry.user_id}#{entry.entry_seq}]", score, str(declared["score"]))

        ranked = sorted(
            zip(entries, (str(item["score"]) for item in manifest["scores"]), strict=True),
            key=lambda pair: (int(pair[1]), pair[0].user_id, pair[0].entry_seq),
        )
        take = min(len(ranked), len(manifest.get("winners", [])))
        recomputed = [
            Winner(
                rank=index + 1,
                user_id=entry.user_id,
                entry_seq=entry.entry_seq,
                entry_id=None,
                score=score,
            )
            for index, (entry, score) in enumerate(ranked[:take])
        ]

        declared_winners = [
            (str(item["user_id"]), int(item["entry_seq"]), str(item["score"]))
            for item in manifest.get("winners", [])
        ]
        actual_winners = [(w.user_id, w.entry_seq, w.score) for w in recomputed]
        record("winners", declared_winners, actual_winners)

    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "recomputed": {
            "winners": [winner.as_public() for winner in recomputed],
            "participant_digest": participant_digest(
                (entry.user_id, entry.entry_seq) for entry in entries
            ),
        },
    }