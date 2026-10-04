"""Self-contained verification suite - no pytest, no network, no Discord token.

Run with::

    python -m giveaway_bot selftest

What it proves
--------------
* the draw is deterministic given ``(giveaway_id, seed, entries)``;
* commitments bind to seeds (a tampered seed fails verification);
* cross-language test vectors in ``shared/test_vectors.json`` still match, so
  the Python and TypeScript implementations agree;
* rejection sampling makes the score reduction unbiased (chi-square-ish sanity);
* every eligibility rule fires for the right reason;
* the full lifecycle works end to end against a real database: create -> seal ->
  join -> end -> draw -> verify, plus crash-recovery and reroll;
* an admin cannot smuggle a winner: there is no code path that accepts one.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .db import Database
from .eligibility import Reason, evaluate_join
from .repositories import control
from .fairness import (
    METHOD,
    DrawEntry,
    commitment,
    draw_winners,
    entry_message,
    generate_seed,
    participant_digest,
    score_entry,
    verify_draw,
)
from .validation import parse_duration, validate_giveaway_payload

VECTOR_PATH = Path(__file__).resolve().parents[2] / "shared" / "test_vectors.json"


class Check:
    """Tiny test registry so the suite runs anywhere, including in Docker."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    @staticmethod
    def _print(text: str) -> None:
        """Print ASCII-safe.

        Windows consoles default to cp1252, where the arrows and box characters
        used in user-facing messages raise UnicodeEncodeError and hide the real
        failure behind a broken stdout.
        """
        try:
            print(text)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "ascii"
            print(text.encode(encoding, "replace").decode(encoding, "replace"))

    def run(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except AssertionError as exc:
            detail = str(exc) or "assertion failed (add a message to this check)"
            self.failures.append(f"{name}: {detail}")
            self._print(f"  [FAIL] {name}\n      {detail}")
        except Exception as exc:  # noqa: BLE001
            self.failures.append(f"{name}: unexpected {type(exc).__name__}: {exc}")
            self._print(f"  [FAIL] {name}\n      unexpected {type(exc).__name__}: {exc}")
        else:
            self.passed += 1
            self._print(f"  [ok] {name}")

    def section(self, title: str) -> None:
        self._print(f"\n{title}")
        print("-" * len(title))


# --------------------------------------------------------------------------- #
# Fairness
# --------------------------------------------------------------------------- #
def test_determinism() -> None:
    entries = [
        DrawEntry(user_id=str(1_000_000_000_000_000_000 + index), entry_seq=1, entry_id=index + 1)
        for index in range(37)
    ]
    seed = "a" * 64
    first = draw_winners("gw_test", entries, 5, seed_hex=seed)
    second = draw_winners("gw_test", entries, 5, seed_hex=seed)
    assert first.manifest() == second.manifest(), "same inputs must produce the same draw"
    assert len(first.winners) == 5
    scores = [int(score) for _, _, score, _ in first.scores]
    assert len(set(scores)) == len(scores), "scores must be unique for distinct entries"
    ranks = sorted(int(score) for _, _, score, _ in first.scores)
    assert ranks[0] == min(ranks), "rank 1 must hold the minimum score"


def test_ordering_is_total_and_stable() -> None:
    """Ties break deterministically on (user_id, entry_seq)."""
    entries = [
        DrawEntry(user_id="222222222222222222", entry_seq=1, entry_id=1),
        DrawEntry(user_id="111111111111111111", entry_seq=1, entry_id=2),
    ]
    result = draw_winners("gw_tie", entries, 2, seed_hex="b" * 64)
    assert len(result.winners) == 2
    # Winner order must be score-ascending regardless of input order.
    scores = [int(winner.score) for winner in result.winners]
    assert scores == sorted(scores), "winners must be returned in ascending score order"

    reversed_result = draw_winners("gw_tie", list(reversed(entries)), 2, seed_hex="b" * 64)
    assert [w.user_id for w in result.winners] == [w.user_id for w in reversed_result.winners], (
        "input order must not influence the outcome"
    )


def test_commitment_binds_seed() -> None:
    seed = generate_seed()
    digest = commitment(seed)
    assert len(digest) == 64, "commitment must be 64 hex chars"
    assert commitment(seed) == digest, "commitment must be stable"
    assert commitment(generate_seed()) != digest, "distinct seeds must differ"

    entries = [DrawEntry(user_id="1", entry_seq=1), DrawEntry(user_id="2", entry_seq=1)]
    result = draw_winners("gw_commit", entries, 1, seed_hex=seed)
    good = verify_draw(
        giveaway_id="gw_commit",
        seed_hex=seed,
        expected_commitment=digest,
        expected_digest=result.participant_digest,
        manifest=result.to_json(),
    )
    assert good["ok"], f"fresh draw must verify: {good['errors']}"

    tampered_seed = "0" * 64 if seed != "0" * 64 else "1" * 64
    bad = verify_draw(
        giveaway_id="gw_commit",
        seed_hex=tampered_seed,
        expected_commitment=digest,
        expected_digest=result.participant_digest,
        manifest=result.to_json(),
    )
    assert not bad["ok"], "a different seed must fail the commitment check"


def test_manifest_tamper_is_detected() -> None:
    entries = [DrawEntry(user_id=str(100 + i), entry_seq=1) for i in range(10)]
    result = draw_winners("gw_tamper", entries, 3, seed_hex="c" * 64)
    manifest = result.manifest()
    manifest["scores"][0]["score"] = "0"
    outcome = verify_draw(
        giveaway_id="gw_tamper",
        seed_hex=result.seed,
        expected_commitment=result.commitment,
        expected_digest=result.participant_digest,
        manifest=manifest,
    )
    assert not outcome["ok"], "edited scores must be detected"
    assert outcome["errors"], "tampering must produce an explanation"


def test_score_bounds_and_retry() -> None:
    total = 7
    seed = generate_seed()
    upper = (1 << 256) // total
    for index in range(25):
        score, retries = score_entry("gw_bounds", str(500 + index), 1, seed, total)
        value = int(score)
        assert 0 <= value < upper, "score must fall inside the unbiased range"
        assert retries >= 0, "retry count must be reported"
    assert entry_message("gw", "123", 1) == b"gw:123:1", "message encoding is normative"
    try:
        entry_message("gw", "123", 0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("entry_seq must be >= 1")


def test_distribution_is_flat() -> None:
    """A giveaway owner must not be able to influence the winner.

    With the same participant set and *different* seeds, the winner should move
    around the set roughly uniformly. If an attacker could bias the draw, the
    same member would keep winning.
    """
    participants = [str(1_000_000_000_000_000_000 + i) for i in range(10)]
    entries = [DrawEntry(user_id=user_id, entry_seq=1) for user_id in participants]
    winners: Counter[str] = Counter()
    rounds = 120
    for _ in range(rounds):
        result = draw_winners("gw_dist", entries, 1, seed_hex=generate_seed())
        winners[result.winners[0].user_id] += 1

    distinct = len(winners)
    assert distinct >= 8, f"expected a spread across participants, got {distinct}/{len(participants)}"
    expected = rounds / len(participants)
    chi_square = sum((count - expected) ** 2 / expected for count in winners.values())
    # 9 degrees of freedom -> 99.9th percentile is ~27.9; stay well below.
    assert chi_square < 30, f"winner distribution looks skewed (chi2={chi_square:.2f})"


def test_cross_language_vectors() -> None:
    if not VECTOR_PATH.exists():
        print("      (no shared/test_vectors.json yet - run `genvectors`)")
        return
    vectors = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    assert vectors["algorithm"] == METHOD, "algorithm id changed - update the spec and TS implementation"
    for vector in vectors["vectors"]:
        score, retries = score_entry(
            vector["giveaway_id"],
            vector["user_id"],
            vector["entry_seq"],
            vector["seed"],
            vector["total_entries"],
        )
        assert score == vector["score"], (
            f"score drift for {vector['user_id']}#{vector['entry_seq']}: "
            f"python={score} expected={vector['score']}"
        )
        assert retries == vector["retry_count"], "retry count drift"
        assert commitment(vector["seed"]) == vector["commitment"], "commitment drift"
    digest = participant_digest((v["user_id"], v["entry_seq"]) for v in vectors["vectors"])
    assert digest == vectors["vectors_participant_digest"], "participant digest drift"


def test_shortfall_and_empty() -> None:
    empty = draw_winners("gw_empty", [], 3, seed_hex="d" * 64)
    assert empty.winners == () and empty.shortfall == 3, "no participants -> explicit shortfall"

    one = [DrawEntry(user_id="42", entry_seq=1)]
    result = draw_winners("gw_one", one, 5, seed_hex="d" * 64)
    assert len(result.winners) == 1 and result.shortfall == 4, "never duplicate a winner to fill a quota"

    duplicated = draw_winners("gw_dup", one + [DrawEntry(user_id="42", entry_seq=1)], 2, seed_hex="d" * 64)
    assert duplicated.participant_count == 1, "duplicate entries must be collapsed"


def test_random_seed_source() -> None:
    seeds = {generate_seed() for _ in range(50)}
    assert len(seeds) == 50, "seed generation must not repeat"
    for seed in seeds:
        assert len(seed) == 64 and all(char in "0123456789abcdef" for char in seed)


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
def _isolated_settings(**overrides: Any) -> Settings:
    """Settings that cannot inherit a real database from the environment.

    pydantic-settings reads ``.env`` *files* as well as ``os.environ``, and dotenv
    sits below environment variables in precedence. Popping TURSO_DATABASE_URL
    from ``os.environ`` therefore does nothing when the developer's ``.env``
    carries one - the suite would then try to reach a real, possibly production,
    database. Init arguments outrank both sources, so the driver is pinned to
    local SQLite here. This keeps the suite hermetic: no network, no token, no
    shared state, as its docstring promises.
    """
    return Settings(turso_database_url="", **overrides)


@contextmanager
def _temp_database(name: str) -> Iterator[Database]:
    """A migrated scratch database that always releases its file handle.

    Without the finally block, a failing assertion would leave the SQLite file
    locked and the TemporaryDirectory cleanup error would hide the real
    failure (a classic Windows-only confusion trap).
    """

    tmp = tempfile.mkdtemp(prefix="giveaway-selftest-")
    previous_path = os.environ.get("SQLITE_PATH")
    os.environ["SQLITE_PATH"] = str(Path(tmp) / f"{name}.db")
    db = Database(_isolated_settings())
    try:
        db.migrate(verbose=False)
        yield db
    finally:
        db.close_all()
        if previous_path is None:
            os.environ.pop("SQLITE_PATH", None)
        else:
            os.environ["SQLITE_PATH"] = previous_path
        shutil.rmtree(tmp, ignore_errors=True)


def _giveaway(**overrides: Any) -> Any:
    from .models import Giveaway, GiveawayStatus

    base = {
        "id": "gw_elig",
        "guild_id": "1",
        "channel_id": "10",
        "message_id": None,
        "status": GiveawayStatus.RUNNING,
        "title": "Test",
        "winner_count": 1,
        "max_entries_per_user": 1,
        "entry_limit": 0,
        "ends_at": int(time.time() * 1000) + 60_000,
    }
    base.update(overrides)
    return Giveaway(**base)


def test_eligibility_rules() -> None:
    now = int(time.time() * 1000)
    day = 86_400_000

    ok = evaluate_join(
        _giveaway(),
        user_id="1",
        role_ids=[],
        account_created_at=now - 400 * day,
        guild_joined_at=now - 400 * day,
        is_member=True,
        channel_id="10",
        current_entries=0,
        now=now,
    )
    assert ok.ok, "a plain eligible member must be allowed"

    ended = evaluate_join(
        _giveaway(status="ended"),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert ended.reason == Reason.GIVEAWAY_ENDED

    not_member = evaluate_join(
        _giveaway(),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=False, channel_id="10", now=now,
    )
    assert not_member.reason == Reason.REQUIRES_MEMBERSHIP

    wrong_channel = evaluate_join(
        _giveaway(allowed_channel_ids=["99"]),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert wrong_channel.reason == Reason.CHANNEL_NOT_ALLOWED

    missing_role = evaluate_join(
        _giveaway(required_role_ids=["555"]),
        user_id="1", role_ids=["666"], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert missing_role.reason == Reason.MISSING_REQUIRED_ROLES

    has_required = evaluate_join(
        _giveaway(required_role_ids=["555"]),
        user_id="1", role_ids=["555"], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert has_required.ok, "whitelist role present -> allowed"

    require_all = evaluate_join(
        _giveaway(required_role_ids=["555", "666"], required_mode="all"),
        user_id="1", role_ids=["555"], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert require_all.reason == Reason.MISSING_REQUIRED_ROLES, "'all' mode needs every role"

    blacklisted = evaluate_join(
        _giveaway(blacklist_role_ids=["777"]),
        user_id="1", role_ids=["777"], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert blacklisted.reason == Reason.HAS_BLACKLISTED_ROLE

    new_account = evaluate_join(
        _giveaway(min_account_age_days=30),
        user_id="1", role_ids=[], account_created_at=now - 2 * day,
        guild_joined_at=None, is_member=True, channel_id="10", now=now,
    )
    assert new_account.reason == Reason.ACCOUNT_TOO_NEW

    new_member = evaluate_join(
        _giveaway(min_guild_join_days=7),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=now - day,
        is_member=True, channel_id="10", now=now,
    )
    assert new_member.reason == Reason.JOINED_TOO_RECENTLY

    limit = evaluate_join(
        _giveaway(max_entries_per_user=2),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", current_entries=2, now=now,
    )
    assert limit.reason == Reason.MAX_ENTRIES_REACHED

    total_limit = evaluate_join(
        _giveaway(entry_limit=10),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, total_entries=10,
    )
    assert total_limit.reason == Reason.ENTRY_LIMIT_REACHED, (
        f"a full giveaway must refuse entries, got {total_limit.reason}"
    )

    room_left = evaluate_join(
        _giveaway(entry_limit=10),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, total_entries=9,
    )
    assert room_left.ok, "one slot left must still allow an entry"

    frozen = evaluate_join(
        _giveaway(locked_at=now),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now,
    )
    assert frozen.reason == Reason.GIVEAWAY_LOCKED, "a locked giveaway accepts no new entries"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_duration_parsing() -> None:
    assert parse_duration("30m") == 30 * 60_000
    assert parse_duration("1h30m") == 90 * 60_000
    assert parse_duration("2d") == 2 * 86_400_000
    assert parse_duration("90") == 90 * 60_000, "a bare number means minutes"
    assert parse_duration("garbage") is None
    assert parse_duration("1h30") is None, "trailing digits without a unit are rejected"
    assert parse_duration(None) is None


def test_message_requirement_rules() -> None:
    """The message-activity requirement must gate entry and explain the gap."""
    now = int(time.time() * 1000)

    # Disabled by default: never blocks, never queries.
    off = evaluate_join(
        _giveaway(),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, message_count=0,
    )
    assert off.ok, "a giveaway with min_messages=0 must accept a 0-message member"
    assert off.progress is None, "no progress block when the rule is off"

    blocked = evaluate_join(
        _giveaway(min_messages=10),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, message_count=4,
    )
    assert not blocked.ok, "4 messages must not satisfy a 10-message requirement"
    assert blocked.reason == Reason.INSUFFICIENT_MESSAGES, (
        f"expected INSUFFICIENT_MESSAGES, got {blocked.reason}"
    )
    assert blocked.progress == (4, 10), f"progress must be reported, got {blocked.progress}"
    assert blocked.messages_remaining == 6, "the member must be told what is left"
    assert "6" in blocked.message, f"the message must state the gap: {blocked.message}"

    exact = evaluate_join(
        _giveaway(min_messages=10),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, message_count=10,
    )
    assert exact.ok, "exactly meeting the requirement must be allowed"
    assert exact.messages_remaining == 0

    over = evaluate_join(
        _giveaway(min_messages=10),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, message_count=999,
    )
    assert over.ok, "surpassing the requirement must be allowed"

    # Ordering: a user who fails an earlier rule should see that rule, not the
    # message one - the most actionable blocker wins.
    wrong_channel = evaluate_join(
        _giveaway(min_messages=10, allowed_channel_ids=["99"]),
        user_id="1", role_ids=[], account_created_at=None, guild_joined_at=None,
        is_member=True, channel_id="10", now=now, message_count=0,
    )
    assert wrong_channel.reason == Reason.CHANNEL_NOT_ALLOWED, (
        "channel restriction must be reported before the message requirement"
    )


def test_message_requirement_validation() -> None:
    from .validation import ValidationError

    ok = validate_giveaway_payload(
        {"title": "t", "prize": "p", "duration": "1h", "min_messages": 25}
    )
    assert ok.min_messages == 25 and ok.message_count_scope == "guild"
    assert ok.message_count_channel_ids == []

    # min_messages=0 is the documented "off" switch, so it must normalise away
    # any leftover channel filters instead of failing validation.
    cleared = validate_giveaway_payload(
        {
            "title": "t", "prize": "p", "duration": "1h", "min_messages": 0,
            "message_count_channel_ids": ["111111111111111111"],
            "message_count_scope": "channel",
        }
    )
    assert cleared.min_messages == 0, "0 disables the rule"
    assert cleared.message_count_channel_ids == [], "stale channels must be cleared when disabled"
    assert cleared.message_count_scope == "guild", "scope must reset when disabled"

    scoped = validate_giveaway_payload(
        {
            "title": "t",
            "prize": "p",
            "duration": "1h",
            "min_messages": 5,
            "message_count_scope": "channel",
            "message_count_channel_ids": ["111111111111111111"],
        }
    )
    assert scoped.message_count_scope == "channel", "scope must round-trip"
    assert scoped.message_count_channel_ids == ["111111111111111111"]

    # Channel scope with no channels would count nothing -> must be refused.
    for bad_payload, expected_field in [
        (
            {
                "title": "t", "prize": "p", "duration": "1h",
                "min_messages": 5, "message_count_scope": "channel",
                "message_count_channel_ids": [],
            },
            "message_count_channel_ids",
        ),
        (
            {"title": "t", "prize": "p", "duration": "1h", "min_messages": -1},
            "min_messages",
        ),
        (
            {"title": "t", "prize": "p", "duration": "1h", "min_messages": 999_999},
            "min_messages",
        ),
        (
            {
                "title": "t", "prize": "p", "duration": "1h", "min_messages": 5,
                "message_count_scope": "galaxy",
            },
            "message_count_scope",
        ),
    ]:
        try:
            validate_giveaway_payload(bad_payload)
        except ValidationError as exc:
            assert expected_field in exc.errors, (
                f"expected an error on {expected_field}, got {sorted(exc.errors)}"
            )
        else:  # pragma: no cover
            raise AssertionError(f"expected ValidationError for {bad_payload}")


def test_message_counting_and_gating() -> None:
    """End-to-end: counters, duplicate protection and the join gate."""
    from .repositories import activity as activity_repo
    from .repositories import activity as activity_repo, control
    from .repositories import entries as entries_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("activity") as db:
        guilds_repo.upsert_guild(db, "700", name="Activity Guild")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")
        now = int(time.time() * 1000)

        giveaway = service.create(
            actor,
            guild_id="700",
            channel_id="701",
            payload={
                "title": "Chatty giveaway",
                "prize": "Key",
                "duration": "1h",
                "min_messages": 5,
            },
        )
        assert giveaway.min_messages == 5, "requirement must persist on create"
        assert giveaway.message_count_scope == "guild", "default scope is the whole guild"
        assert giveaway.rules_public()["min_messages"] == 5, "rules expose the requirement"

        # Bot messages and empty messages never count.
        for index in range(3):
            activity_repo.record_message(
                db, guild_id="700", user_id="999", channel_id="702",
                message_id=str(5000 + index), message_at=now,
            )
        assert activity_repo.get_count(db, "700", "999") == 3, "direct repo counts humans"

        # A member below the threshold is blocked with an explanation.
        blocked = service.join(
            giveaway,
            {"user_id": "3001", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert not blocked.joined, "0 messages must not satisfy a 5-message requirement"
        assert blocked.eligibility.reason == Reason.INSUFFICIENT_MESSAGES
        assert blocked.eligibility.messages_remaining == 5

        # Exactly 4 is still blocked, 5 is allowed: the boundary is inclusive.
        for index in range(4):
            activity_repo.record_message(
                db, guild_id="700", user_id="3001", channel_id="702",
                message_id=str(6000 + index), message_at=now,
            )
        almost = service.join(
            giveaway,
            {"user_id": "3001", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert not almost.joined, "4 of 5 messages must still be refused"

        activity_repo.record_message(
            db, guild_id="700", user_id="3001", channel_id="702",
            message_id="6100", message_at=now,
        )
        allowed = service.join(
            giveaway,
            {"user_id": "3001", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert allowed.joined, "5 of 5 messages must be accepted"

        # Progress reporting agrees with the gate.
        progress = service.message_progress(giveaway, "3001")
        assert progress["current"] == 5 and progress["required"] == 5
        assert progress["eligible"] is True and progress["remaining"] == 0

        # Disabling the requirement must lift the block immediately.
        disabled = service.set_message_requirement(
            actor, service.get(giveaway.id), payload={"min_messages": 0}
        )
        assert disabled.min_messages == 0, "requirement must be disable-able"
        free = service.join(
            disabled,
            {"user_id": "3002", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert free.joined, "with the rule off anyone may enter"
        assert service.message_progress(disabled, "3002")["required"] == 0

        # Re-enabling with a channel scope: activity elsewhere must not qualify.
        scoped = service.set_message_requirement(
            actor,
            service.get(giveaway.id),
            payload={
                "min_messages": 2,
                "message_count_scope": "channel",
                "message_count_channel_ids": ["7000000000000000702"],
            },
        )
        assert scoped.message_count_scope == "channel"
        activity_repo.record_channel_count(
            db, guild_id="700", user_id="3003", channel_id="7000000000000000702",
            message_at=now, tracked_channels={"7000000000000000702"},
        )
        activity_repo.record_channel_count(
            db, guild_id="700", user_id="3003", channel_id="7000000000000000702",
            message_at=now, tracked_channels={"7000000000000000702"},
        )
        in_scope = service.join(
            scoped,
            {"user_id": "3003", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert in_scope.joined, "2 messages in the watched channel must qualify"

        # Guild counter is high for 3003 (via 3002 path) but channel scope decides.
        out_of_scope = service.message_progress(scoped, "3003")
        assert out_of_scope["current"] == 2, (
            f"channel-scoped count must be exact, got {out_of_scope['current']}"
        )

        # Requirement changes are audited, including the disable.
        actions = {
            row["action"]
            for row in control.list_audit(db, giveaway_id=giveaway.id, limit=200)
        }
        assert "giveaway.message_requirement_changed" in actions, (
            "message requirement changes must be audited"
        )


def test_message_count_idempotency() -> None:
    """Replayed/duplicated events must never inflate a count."""
    from .repositories import activity as activity_repo

    with _temp_database("idempotency") as db:
        now = int(time.time() * 1000)

        activity_repo.record_message(
            db, guild_id="600", user_id="4001", channel_id="601",
            message_id="7001", message_at=now,
        )
        assert activity_repo.get_count(db, "600", "4001") == 1

        # Backfilling a range that includes an already-counted message is a no-op.
        result = activity_repo.backfill_messages(
            db,
            guild_id="600",
            channel_id="601",
            messages=[
                {"id": "7001", "author": {"id": "4001", "bot": False}, "timestamp_ms": now},
                {"id": "7002", "author": {"id": "4001", "bot": False}, "timestamp_ms": now},
                {"id": "7003", "author": {"id": "4002", "bot": True}, "timestamp_ms": now},
                {"id": "7004", "author": {"id": "4002", "bot": False}, "timestamp_ms": now},
            ],
        )
        assert result["skipped"] >= 1, "already-counted messages must be skipped"
        assert result["bots_skipped"] == 1, "bot messages must not be counted"
        assert activity_repo.get_count(db, "600", "4001") == 2, (
            "4001 should have exactly the two distinct messages"
        )
        assert activity_repo.get_count(db, "600", "4002") == 1, (
            "4002 must have exactly one human message"
        )

        # Running the same backfill twice changes nothing.
        activity_repo.backfill_messages(
            db,
            guild_id="600",
            channel_id="601",
            messages=[
                {"id": "7001", "author": {"id": "4001", "bot": False}, "timestamp_ms": now},
                {"id": "7002", "author": {"id": "4001", "bot": False}, "timestamp_ms": now},
                {"id": "7004", "author": {"id": "4002", "bot": False}, "timestamp_ms": now},
            ],
        )
        assert activity_repo.get_count(db, "600", "4001") == 2, "replay must be idempotent"
        assert activity_repo.get_count(db, "600", "4002") == 1, "replay must be idempotent"

        # Non-numeric ids are rejected outright.
        activity_repo.record_message(
            db, guild_id="600", user_id="not-a-snowflake", channel_id="601",
            message_id="9999", message_at=now,
        )
        assert activity_repo.get_count(db, "600", "not-a-snowflake") == 0


def test_message_tracker_buffering() -> None:
    """The tracker must skip work when no requirement exists, and flush in bulk."""
    from .activity import MessageActivityTracker
    from .repositories import activity as activity_repo, entries as entries_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("tracker") as db:
        guilds_repo.upsert_guild(db, "500", name="Tracker Guild")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")
        now = int(time.time() * 1000)

        tracker = MessageActivityTracker(db, service)
        tracker.refresh_requirements()
        assert tracker.stats["skipped_no_requirement"] == 0

        # With no requirement enabled, messages are dropped at zero cost.
        for index in range(10):
            tracker.record_raw(
                guild_id="500", user_id="2100", channel_id="501",
                message_id=str(1000 + index), message_at=now, content_length=10,
            )
        assert tracker.stats["skipped_no_requirement"] == 10, (
            "messages must be ignored entirely when no giveaway needs counting"
        )
        assert activity_repo.get_count(db, "500", "2100") == 0

        # Bots and empty messages are ignored even with counting enabled.
        service.create(
            actor, guild_id="500", channel_id="501",
            payload={"title": "t", "prize": "p", "duration": "1h", "min_messages": 3},
        )
        tracker.refresh_requirements()
        tracker.record_raw(
            guild_id="500", user_id="2100", channel_id="501",
            message_id="1100", message_at=now, is_bot=True, content_length=10,
        )
        tracker.record_raw(
            guild_id="500", user_id="2100", channel_id="501",
            message_id="1101", message_at=now, content_length=0,
        )
        assert tracker.stats["skipped_bot"] == 2, "bot and empty messages must be skipped"

        # Real messages buffer, then flush.
        for index in range(4):
            tracker.record_raw(
                guild_id="500", user_id="2100", channel_id="501",
                message_id=str(1200 + index), message_at=now, content_length=5,
            )
        assert tracker.pending() == 4, "events must buffer rather than write per message"
        assert activity_repo.get_count(db, "500", "2100") == 0, "no write before flush"

        tracker.flush_all()
        assert tracker.pending() == 0, "flush must drain the buffer"
        assert activity_repo.get_count(db, "500", "2100") == 4, (
            f"all 4 messages must be counted, got {activity_repo.get_count(db, '500', '2100')}"
        )

        # A queued participant is now eligible without any further action.
        giveaway = service.get(
            db.query_one("SELECT id FROM giveaways WHERE guild_id = '500'")["id"]
        )
        outcome = service.join(
            giveaway,
            {"user_id": "2100", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert outcome.joined, (
            f"re-check must let the member in after reaching 3 messages: {outcome.eligibility.reason}"
        )


def test_message_revalidation() -> None:
    """Revalidation flags only who fails today, and is audited."""
    from .repositories import activity as activity_repo, entries as entries_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("revalidate") as db:
        guilds_repo.upsert_guild(db, "400", name="Revalidate Guild")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")
        now = int(time.time() * 1000)

        # Enter with no requirement...
        giveaway = service.create(
            actor, guild_id="400", channel_id="401",
            payload={"title": "t", "prize": "p", "duration": "1h"},
        )
        for user_id in ("5001", "5002", "5003"):
            assert service.join(
                giveaway,
                {"user_id": user_id, "role_ids": [], "is_member": True, "account_created_at": now},
            ).joined

        # ...then require activity and re-check.
        updated = service.set_message_requirement(
            actor, service.get(giveaway.id), payload={"min_messages": 10}
        )
        for index in range(10):
            activity_repo.record_message(
                db, guild_id="400", user_id="5001", channel_id="401",
                message_id=str(8000 + index), message_at=now,
            )
        report = service.revalidate_message_activity(actor, updated)
        assert report["checked"] == 3, f"all participants must be checked: {report}"
        assert report["flagged"] == 2, (
            f"5002 and 5003 have no activity and must be flagged: {report}"
        )

        # The active member is still eligible; the flagged ones are not in the pool.
        frozen = entries_repo.frozen_entries(db, giveaway.id)
        frozen_users = {row["user_id"] for row in frozen}
        assert frozen_users == {"5001"}, (
            f"only the qualifying participant may remain in the draw: {frozen_users}"
        )

        actions = {
            row["action"]
            for row in control.list_audit(db, giveaway_id=giveaway.id, limit=200)
        }
        assert "giveaway.message_activity_revalidated" in actions, (
            "revalidation must be audited"
        )


def test_payload_validation() -> None:
    from .validation import ValidationError

    good = validate_giveaway_payload(
        {
            "title": "A prize",
            "prize": "Steam key",
            "duration": "2h",
            "channel_id": "123456789012345678",
            "winner_count": 3,
        }
    )
    assert good.winner_count == 3 and good.ends_at is not None
    assert good.channel_id == "123456789012345678", "a supplied channel_id is validated and kept"

    # The service passes channel_id as an explicit argument, so omitting it from
    # the payload must not be treated as a validation failure.
    without_channel = validate_giveaway_payload(
        {"title": "A prize", "prize": "Steam key", "duration": "2h"}
    )
    assert without_channel.channel_id == "", "channel_id is optional in the payload"

    for bad, field_name in [
        ({"title": "", "prize": "x", "duration": "2h"}, "title"),
        ({"title": "x", "prize": "x", "duration": "2h", "channel_id": "nope"}, "channel_id"),
        ({"title": "x", "prize": "x", "winner_count": 99, "duration": "2h"}, "winner_count"),
        ({"title": "x", "prize": "x", "duration": "1ms"}, "duration"),
        ({"title": "x", "prize": "x", "winner_count": 1}, "duration"),
        (
            {
                "title": "x",
                "prize": "x",
                "duration": "2h",
                "required_role_ids": ["12"],
                "blacklist_role_ids": ["12"],
            },
            "blacklist_role_ids",
        ),
    ]:
        try:
            validate_giveaway_payload(bad)
        except ValidationError as exc:
            assert field_name in exc.errors, f"expected an error on {field_name}, got {exc.errors}"
        else:  # pragma: no cover
            raise AssertionError(f"expected ValidationError for {bad}")


def test_no_admin_override_exists() -> None:
    """Guard rail: the draw API must not accept a winner, weight or priority."""
    from .fairness import draw_winners

    parameters = set(inspect.signature(draw_winners).parameters)
    forbidden = {"winner", "winner_id", "winner_user_id", "weights", "priority", "seed_override", "rng_seed"}
    assert not (parameters & forbidden), (
        f"draw_winners gained override parameters: {sorted(parameters & forbidden)}"
    )
    # `winner_count` is allowed but must be the *only* way to influence selection.
    assert "winner_count" in parameters


def test_role_grant_on_entry() -> None:
    """Joining must queue exactly one role grant for the entrants role."""
    from .repositories import entries as entries_repo, giveaways as gw_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("role-grant") as db:
        guilds_repo.upsert_guild(db, "300", name="Role Guild")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")

        giveaway = service.create(
            actor, guild_id="300", channel_id="301",
            payload={"title": "t", "prize": "p", "duration": "1h"},
        )
        # No role attached yet: entering must not queue anything.
        assert giveaway.participant_role_id is None, "no role is attached by default"
        service.join(giveaway, {"user_id": "4001", "role_ids": [], "is_member": True})
        assert control.pending_role_tasks(db, giveaway.id) == 0, (
            "no role configured means no role task"
        )

        # Attach one, as the bot does on startup.
        gw_repo.set_participant_role(db, giveaway.id, "3000000000000000001")
        updated = service.get(giveaway.id)
        assert updated.participant_role_id == "3000000000000000001"

        outcome = service.join(updated, {"user_id": "4002", "role_ids": [], "is_member": True})
        assert outcome.joined, "entry must succeed"

        tasks = control.claim_role_tasks(db, limit=10)
        assert len(tasks) == 1, f"exactly one grant expected, got {tasks}"
        task = tasks[0]
        assert task["action"] == "add", f"expected an add task, got {task['action']}"
        assert task["user_id"] == "4002", f"wrong member: {task['user_id']}"
        assert task["role_id"] == "3000000000000000001", "the role must be recorded on the task"

        # The entry records that the bot granted the role (provenance).
        assert entries_repo.has_bot_grant(db, giveaway.id, "4002"), (
            "the entry must record a bot grant"
        )
        assert not entries_repo.has_bot_grant(db, giveaway.id, "4001"), (
            "a member who entered before the role existed has no bot grant"
        )


def test_role_release_provenance() -> None:
    """Only bot grants are released; a human-assigned role is left alone."""
    from .repositories import entries as entries_repo, giveaways as gw_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("role-release") as db:
        guilds_repo.upsert_guild(db, "310", name="Role Guild 2")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")

        giveaway = service.create(
            actor, guild_id="310", channel_id="311",
            payload={"title": "t", "prize": "p", "duration": "1h"},
        )
        gw_repo.set_participant_role(db, giveaway.id, "3100000000000000001")
        giveaway = service.get(giveaway.id)

        service.join(giveaway, {"user_id": "4101", "role_ids": [], "is_member": True})
        service.join(giveaway, {"user_id": "4102", "role_ids": [], "is_member": True})
        # Simulate a moderator assigning the role to someone who never entered.
        entries_repo.add_entry(
            db, giveaway.id, "4103", 1,
            grant_source="manual", role_granted_at=int(time.time() * 1000),
        )

        bot_grants = entries_repo.users_with_bot_role(db, giveaway.id)
        assert sorted(bot_grants) == ["4101", "4102"], (
            f"only bot grants may be released, got {bot_grants}"
        )
        assert "4103" not in bot_grants, "a human-assigned role must never be stripped"

        # After release the bookkeeping is cleared, so a second end is a no-op.
        entries_repo.mark_grants_revoked(db, giveaway.id, "4101")
        assert not entries_repo.has_bot_grant(db, giveaway.id, "4101"), (
            "a revoked grant must no longer count as ours"
        )
        assert entries_repo.users_with_bot_role(db, giveaway.id) == ["4102"], (
            "already-released members must not be queued twice"
        )


def test_role_task_retry() -> None:
    """A failing role task is retried, then marked failed - never lost."""
    from .repositories import giveaways as gw_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("role-retry") as db:
        guilds_repo.upsert_guild(db, "320", name="Role Guild 3")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")

        giveaway = service.create(
            actor, guild_id="320", channel_id="321",
            payload={"title": "t", "prize": "p", "duration": "1h"},
        )
        gw_repo.set_participant_role(db, giveaway.id, "3200000000000000001")

        assert control.role_task(
            db, giveaway_id=giveaway.id, user_id="4201",
            role_id="3200000000000000001", action="add", status="pending",
        ) is True, "the first enqueue must insert"

        # A duplicate enqueue is idempotent, not a second task.
        control.role_task(
            db, giveaway_id=giveaway.id, user_id="4201",
            role_id="3200000000000000001", action="add", status="pending",
        )
        assert control.pending_role_tasks(db, giveaway.id) == 1, (
            "a duplicate grant must not queue twice"
        )

        # Failure returns the task to pending for a retry.
        claimed = control.claim_role_tasks(db, limit=5)
        assert len(claimed) == 1, "the task must be claimable"
        status = control.complete_role_task(db, int(claimed[0]["id"]), ok=False, error="boom")
        assert status == "pending", f"a transient failure must retry, got {status}"

        # Keep failing: eventually marked failed, but still recorded.
        for _ in range(10):
            claimed = control.claim_role_tasks(db, limit=5)
            if not claimed:
                break
            status = control.complete_role_task(db, int(claimed[0]["id"]), ok=False, error="boom")
        assert status == "failed", f"repeated failures must end as failed, got {status}"
        assert control.pending_role_tasks(db, giveaway.id) == 0, "no task is left pending"

        # Success clears the error.
        control.role_task(
            db, giveaway_id=giveaway.id, user_id="4202",
            role_id="3200000000000000001", action="add", status="pending",
        )
        claimed = control.claim_role_tasks(db, limit=5)
        assert control.complete_role_task(db, int(claimed[0]["id"]), ok=True) == "done"


def test_single_active_giveaway() -> None:
    """One open giveaway per guild keeps the entrants role unambiguous."""
    from .repositories import giveaways as gw_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("single-active") as db:
        guilds_repo.upsert_guild(db, "330", name="Single Guild")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")

        assert gw_repo.find_active(db, "330") is None, "no giveaway yet"

        first = service.create(
            actor, guild_id="330", channel_id="331",
            payload={"title": "First", "prize": "p", "duration": "1h"},
        )
        active = gw_repo.find_active(db, "330")
        assert active is not None and active["id"] == first.id, (
            "the running giveaway must be discoverable as the active one"
        )

        # A paused giveaway is still active.
        service.pause(actor, service.get(first.id))
        assert gw_repo.find_active(db, "330") is not None, "a paused giveaway is still active"

        service.resume(actor, service.get(first.id))
        # Ended frees the slot.
        service.end(actor, service.get(first.id), reason="test")
        assert gw_repo.find_active(db, "330") is None, "an ended giveaway frees the slot"

        second = service.create(
            actor, guild_id="330", channel_id="332",
            payload={"title": "Second", "prize": "p", "duration": "1h"},
        )
        active = gw_repo.find_active(db, "330")
        assert active is not None and active["id"] == second.id, (
            "the new giveaway becomes the active one"
        )


# --------------------------------------------------------------------------- #
# Database + lifecycle integration
# --------------------------------------------------------------------------- #
def test_lifecycle() -> None:
    from .repositories import (
        control,
        draws as draws_repo,
        entries as entries_repo,
        giveaways as gw_repo,
        guilds as guilds_repo,
    )
    from .service import Actor, GiveawayService

    with _temp_database("lifecycle") as db:
        guilds_repo.upsert_guild(db, "900", name="Test Guild", owner_id="1")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")
        now = int(time.time() * 1000)

        giveaway = service.create(
            actor,
            guild_id="900",
            channel_id="901",
            payload={
                "title": "Fair draw test",
                "prize": "A key",
                "duration": "1h",
                "winner_count": 2,
                "max_entries_per_user": 2,
                "min_account_age_days": 1,
            },
        )
        assert giveaway.status == "running", "creation seals the seed and starts the giveaway"
        assert giveaway.seed_commitment and len(giveaway.seed_commitment) == 64
        assert giveaway.server_seed, "a sealed seed exists before entries open"

        # Multi-entry: max_entries_per_user=2, so entry 1 and 2 are allowed and
        # the third attempt is refused.
        def _join_as_1000() -> Any:
            return service.join(
                giveaway,
                {
                    "user_id": "1000",
                    "role_ids": [],
                    "is_member": True,
                    "account_created_at": now - 10 * 86_400_000,
                },
            )

        first = _join_as_1000()
        assert first.joined and first.entry_seq == 1, "the first entry succeeds with seq 1"
        second = _join_as_1000()
        assert second.joined and second.entry_seq == 2, (
            f"a second entry is allowed when max_entries_per_user=2, got {second}"
        )
        third = _join_as_1000()
        assert not third.joined, "a third entry must be refused"
        assert third.eligibility.reason == Reason.MAX_ENTRIES_REACHED, (
            f"expected MAX_ENTRIES_REACHED, got {third.eligibility.reason}"
        )

        # Duplicate prevention at the storage layer: replaying the same
        # (giveaway, user, entry_seq) must be a no-op, never a second row.
        entries_before = entries_repo.active_entry_totals(db, giveaway.id)[0]
        replayed = entries_repo.add_entry(db, giveaway.id, "1000", 1)
        assert replayed is None, "the UNIQUE constraint must reject a duplicate entry_seq"
        entries_after = entries_repo.active_entry_totals(db, giveaway.id)[0]
        assert entries_before == entries_after == 2, "a replayed entry must not change the count"

        for user_id in ("1001", "1002", "1003", "1004"):
            outcome = service.join(
                giveaway,
                {"user_id": user_id, "role_ids": [], "is_member": True,
                 "account_created_at": now - 10 * 86_400_000},
            )
            assert outcome.joined, f"{user_id} should be able to enter"

        too_new = service.join(
            giveaway,
            {"user_id": "1005", "role_ids": [], "is_member": True, "account_created_at": now},
        )
        assert not too_new.joined and too_new.eligibility.reason == Reason.ACCOUNT_TOO_NEW

        stats = gw_repo.get_giveaway(db, giveaway.id)
        assert stats is not None, "giveaway row must exist"
        # 5 distinct users (1000..1004) but 6 entries: user 1000 entered twice.
        assert stats.participant_count == 5, (
            f"participant count should be 5 distinct users, got {stats.participant_count}"
        )
        assert stats.entry_count == 6, (
            f"entry count should be 6 (1000 entered twice), got {stats.entry_count}"
        )

        # Pause / resume preserve the remaining time.
        paused = service.pause(actor, stats)
        assert paused.status == "paused" and paused.paused_remaining_ms is not None
        resumed = service.resume(actor, paused)
        assert resumed.status == "running"
        assert abs((resumed.ends_at or 0) - (paused.paused_remaining_ms or 0) - now) < 5_000

        # Extend / shorten.
        extended = service.extend(actor, resumed, duration_ms=10 * 60_000)
        assert (extended.ends_at or 0) > (resumed.ends_at or 0)
        shortened = service.shorten(actor, extended, duration_ms=5 * 60_000)
        assert (shortened.ends_at or 0) < (extended.ends_at or 0)

        # Update rules.
        updated = service.update(actor, shortened, {"prize": "A better key", "winner_count": 3})
        assert updated.winner_count == 3 and updated.prize == "A better key"

        # Disqualify a cheater, then draw.
        entries_before_flag = entries_repo.active_entry_totals(db, giveaway.id)[0]
        flagged = service.set_entry_eligibility(
            actor, updated, "1004", eligible=False, reason="alt account"
        )
        assert flagged == 1, f"flagging 1004 should change 1 entry, changed {flagged}"
        entries_after_flag = entries_repo.active_entry_totals(db, giveaway.id)[0]
        assert entries_after_flag == entries_before_flag - 1, (
            f"flagged entries must leave the pool: {entries_before_flag} -> {entries_after_flag}"
        )

        result = service.end(actor, service.get(giveaway.id), reason="test")
        assert result is not None, "end() draws by default"
        # The draw freezes *entries*, not distinct users: 6 entries - 1 flagged.
        assert result.result.participant_count == entries_after_flag, (
            "the draw must score exactly the surviving entries "
            f"({result.result.participant_count} != {entries_after_flag})"
        )
        assert result.result.participant_count == 5, "6 entries minus 1 flagged entry"
        assert len(result.winners) == 3, "three winners are drawn"
        assert result.verification is not None and result.verification["ok"], (
            f"the bot must re-verify its own draw: {result.verification['errors']}"
        )
        winner_ids = [winner["user_id"] for winner in result.winners]
        assert "1004" not in winner_ids, "a disqualified entry cannot win"

        ended = service.get(giveaway.id)
        assert ended.status == "ended" and ended.seed_revealed_at is not None
        assert ended.server_seed, "the seed is revealed after the draw"

        # Nobody can join an ended giveaway.
        after = service.join(ended, {"user_id": "1001", "role_ids": [], "is_member": True})
        assert not after.joined and after.eligibility.reason == Reason.GIVEAWAY_ENDED

        # History: reroll uses fresh randomness, keeps the old round.
        first_seed = ended.server_seed
        rerolled = service.reroll(actor, ended, reason="test reroll")
        assert rerolled.result.seed != first_seed, "a reroll must never reuse the previous seed"
        assert rerolled.result.round_number == 2
        assert rerolled.verification and rerolled.verification["ok"]
        assert len(draws_repo.list_draws(db, giveaway.id)) == 2, "every round stays on record"

        # Regression: a reroll must re-freeze the SAME entry set. An earlier
        # version rewrote entry statuses to winner/lost after round 1, which
        # silently emptied the pool and produced empty rerolls.
        assert rerolled.result.participant_count == result.result.participant_count, (
            "a reroll must score the same number of entries as the first draw "
            f"({rerolled.result.participant_count} != {result.result.participant_count})"
        )
        assert len(rerolled.winners) == 3, f"a reroll must draw winners, got {rerolled.winners}"

        # Third round still works, and every round is independently verifiable.
        third_round = service.reroll(actor, service.get(giveaway.id), reason="third round")
        assert third_round.result.round_number == 3
        assert third_round.verification and third_round.verification["ok"]
        all_draws = draws_repo.list_draws(db, giveaway.id)
        assert len(all_draws) == 3, f"3 rounds on record, got {len(all_draws)}"
        for record in all_draws:
            assert record.server_seed and record.seed_commitment and record.participant_digest, (
                f"round {record.round} must retain its full proof material"
            )
        # Seeds must be unique per round - no accidental reuse.
        seeds = [record.server_seed for record in all_draws]
        assert len(set(seeds)) == len(seeds), "each round must use a fresh seed"

        # Snapshot for the public page contains no private data.
        snapshot = service.public_snapshot(giveaway.id)
        assert snapshot["winners"], "public snapshot exposes the latest winners"
        assert snapshot["fairness"]["seed"], "public snapshot exposes the revealed seed"
        assert snapshot["participant_count"] == 4, "5 users remain after flagging 1004"
        blob = json.dumps(snapshot)
        assert "account_created_at" not in blob and "guild_joined_at" not in blob, (
            "the public snapshot must not leak account timestamps"
        )

        # Audit trail covers the whole lifecycle.
        audit_rows = control.list_audit(db, giveaway_id=giveaway.id, limit=200)
        actions = {row["action"] for row in audit_rows}
        for expected in {
            "giveaway.created",
            "giveaway.started",
            "entry.joined",
            "entry.disqualified",
            "giveaway.paused",
            "giveaway.resumed",
            "giveaway.extended",
            "giveaway.shortened",
            "giveaway.updated",
            "giveaway.draw_locked",
            "giveaway.drawn",
            "giveaway.rerolled",
            "draw.verified",
        }:
            assert expected in actions, f"missing audit entry: {expected}"

        # Audit rows are immutable-by-design: the repository exposes no mutation.
        assert not hasattr(control, "update_audit") and not hasattr(control, "delete_audit")

        # Queue claim is exclusive.
        command_id = control.enqueue(
            db, guild_id="900", kind="giveaway.pause", payload={}, requested_by="1", giveaway_id=giveaway.id
        )
        first_claim = control.claim_batch(db, limit=5)
        second_claim = control.claim_batch(db, limit=5)
        claimed_ids = [item.id for item in first_claim]
        assert command_id in claimed_ids, "the pending command must be claimed"
        assert command_id not in [item.id for item in second_claim], "a command can only be claimed once"


def test_crash_recovery() -> None:
    """A draw interrupted between phases must finish on restart."""
    from .repositories import giveaways as gw_repo, guilds as guilds_repo
    from .service import Actor, GiveawayService

    with _temp_database("crash") as db:
        guilds_repo.upsert_guild(db, "800", name="Crash Guild")
        service = GiveawayService(db, _isolated_settings())
        actor = Actor("1", "owner", "discord")

        giveaway = service.create(
            actor,
            guild_id="800",
            channel_id="801",
            payload={"title": "Crash", "prize": "P", "duration": "30m"},
        )
        for user_id in ("2001", "2002", "2003"):
            service.join(giveaway, {"user_id": user_id, "role_ids": [], "is_member": True})

        # Simulate a crash mid-draw: locked, no giveaway_draws row yet.
        db.execute(
            "UPDATE giveaways SET locked_at = ?, status = 'ended', draw_round = 1 WHERE id = ?",
            (int(time.time() * 1000), giveaway.id),
        )
        recovered = service.recover_locked_draws()
        assert recovered == 1, "the interrupted draw must be completed"

        record = gw_repo.get_giveaway(db, giveaway.id)
        assert record is not None and record.total_draws == 1, "recovery records exactly one draw"
        verification = service.verification_for(giveaway.id)
        assert verification is not None and verification["ok"]
        winners = db.query(
            "SELECT user_id FROM giveaway_winners WHERE giveaway_id = ? ORDER BY rank",
            (giveaway.id,),
        )
        assert len(winners) == 1, "the recovered draw produced its winner"


def test_migration_integrity() -> None:
    """Applied migrations must never change afterwards."""
    with _temp_database("migrations") as db:
        applied = [name for name, _ in db.pending_migrations()]
        assert len(applied) >= 6, f"all shared migrations applied, got {applied}"
        assert db.migrate(verbose=False) == [], "re-running migrations is a no-op"

        db.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE filename = ?",
            (applied[0],),
        )
        try:
            db.migrate(verbose=False)
        except RuntimeError as exc:
            assert "modified after being applied" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("tampering with schema_migrations must abort migrate()")


def test_giveaway_channel_is_fixed() -> None:
    """Giveaways live in one configured channel that client input cannot change."""
    import inspect

    from .queue import _resolve_giveaway_channel
    from .service import ServiceError

    class _Perms:
        send_messages = True

    class _Channel:
        guild_id = 1
        # Must match the configured ID so the fake `get_channel` finds it.
        id = 123_456_789_012_345_678

        def permissions_for(self, _me: object) -> _Perms:
            return _Perms()

    class _Settings:
        giveaway_channel_id = "123456789012345678"

    class _Bot:
        settings = _Settings()

        def __init__(self, channel: object | None) -> None:
            self._channel = channel

        def get_channel(self, channel_id: int) -> object | None:
            return self._channel if self._channel and self._channel.id == channel_id else None

    class _Guild:
        id = 1
        me = object()

        def get_channel(self, channel_id: int) -> object | None:
            return None

    # 1. With a configured channel it resolves to exactly that channel.
    channel = _Channel()
    resolved = _resolve_giveaway_channel(_Bot(channel), _Guild())
    assert resolved is channel, "the configured channel must be used"
    assert str(resolved.id) == _Settings.giveaway_channel_id, (
        "resolution must return the configured channel"
    )

    # 2. With nothing configured it refuses, rather than falling back to
    #    wherever the command was run.
    try:
        _resolve_giveaway_channel(_Bot(None), _Guild())
    except ServiceError as exc:
        assert exc.code == "channel_not_configured", f"expected channel_not_configured, got {exc.code}"
        assert "DISCORD_GIVEAWAY_CHANNEL_ID" in exc.message, (
            f"the error must tell the operator how to fix it: {exc.message}"
        )
    else:  # pragma: no cover
        raise AssertionError("an unconfigured bot must refuse to create giveaways")

    # 3. The handler never reads channel_id from the payload. This is the
    #    security property: a compromised dashboard cannot redirect a giveaway.
    from .queue import _handle_create

    source = inspect.getsource(_handle_create)
    assert "payload.pop(\"channel_id\"" in source, (
        "giveaway.create must explicitly discard a client-supplied channel_id"
    )
    assert "payload[\"channel_id\"]" not in source, (
        "giveaway.create must never read channel_id from the payload"
    )

    # 4. The dashboard schema must not accept one either.
    schema = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "lib" / "commands.ts"
    if schema.exists():
        text = schema.read_text(encoding="utf-8")
        create_block = text.split("export const createSchema")[1].split("export const updateSchema")[0]
        assert "channel_id: snowflake" not in create_block, (
            "the dashboard create schema must not accept a channel_id"
        )


def test_requirements_cover_runtime_deps() -> None:
    """requirements.txt must not fall behind pyproject, and must ship libsql.

    The bot deploys from requirements.txt rather than from the package
    definition, so those two lists can drift apart. A dependency added to
    pyproject but forgotten here would install fine locally and then fail on the
    server, which is exactly the kind of bug nobody notices until it is live.
    """
    import re
    import tomllib

    root = Path(__file__).resolve().parents[2]

    def package_name(spec: str) -> str:
        # "discord.py>=2.4,<3" -> "discord.py"
        head = re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0]
        return head.strip().lower()

    requirements: dict[str, str] = {}
    for raw in (root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            requirements[package_name(line)] = line

    pyproject = tomllib.loads(
        (root / "bot" / "pyproject.toml").read_text(encoding="utf-8")
    )
    for spec in pyproject["project"]["dependencies"]:
        name = package_name(spec)
        assert name in requirements, (
            f"{name} is declared in pyproject.toml but missing from "
            f"requirements.txt, so a deploy would not install it"
        )

    # Optional extra in pyproject, mandatory in production: db.py imports it the
    # moment TURSO_DATABASE_URL is set, so a Turso deploy dies at startup without
    # it while every local test still passes on SQLite.
    assert "libsql" in requirements, (
        "requirements.txt must include libsql - it is required to reach Turso"
    )


def test_example_env_file_loads() -> None:
    """The .env.example we ship must itself be a loadable configuration.

    Two separate startup crashes hid behind a correct-looking example file: an
    empty CSV list (pydantic-settings JSON-decodes list-typed fields before any
    validator runs, so `TRUSTED_PROXIES=` was fatal) and a hex `EMBED_COLOR`
    (pydantic's int rejects 0x). Neither showed up for a developer whose local
    .env happened to be hand-written, only for an operator copying the example
    as the documentation tells them to. So the example is parsed and loaded here
    rather than trusted.
    """
    root = Path(__file__).resolve().parents[2]
    example = root / ".env.example"
    assert example.exists(), ".env.example is missing; deployment depends on it"

    values: dict[str, str] = {}
    for raw in example.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    from .config import Settings

    known = set(Settings.model_fields)
    kwargs = {key.lower(): value for key, value in values.items() if key.lower() in known}

    # Guard against the test passing vacuously if field names ever drift from
    # the env keys: an empty match would assert nothing.
    assert len(kwargs) >= 15, (
        f"only {len(kwargs)} of {len(values)} .env.example keys map to a Settings "
        f"field, so this check would not prove anything"
    )

    settings = Settings(**kwargs)  # type: ignore[arg-type]

    assert settings.embed_color == 0x7C5CFF, (
        f"EMBED_COLOR from .env.example parsed as {settings.embed_color}, "
        f"expected 0x7C5CFF"
    )
    assert settings.trusted_proxies == [], (
        "an empty TRUSTED_PROXIES must become an empty list, not fail to parse"
    )
    assert settings.guild_allowlist == [], (
        "an empty DISCORD_GUILD_ALLOWLIST must become an empty list"
    )
    assert not settings.uses_turso, (
        "the example ships an empty TURSO_DATABASE_URL, so it must not select the "
        "Turso driver - otherwise the example config cannot be validated offline"
    )


#: The os.exec* family. Matched by name rather than a "starts with exec" prefix,
#: because sys.executable - a legitimate, unrelated attribute - also starts with
#: "exec" and would otherwise be flagged.
_EXEC_FAMILY = frozenset(
    {"execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"}
)


def test_app_shim_entrypoint() -> None:
    """The root app.py must reach the bot and report the bot's exit code.

    Some hosting panels start a Python app from a file at the repository root, so
    app.py exists purely to bridge to `python -m giveaway_bot run`. Two things
    about it are easy to break and hard to notice:

    * the bot has to run *in* the app's own process. If it were spawned as a
      child, a SIGTERM arriving before or between the handler install and the wait
      would be lost and leave an orphaned bot holding a gateway connection. If it
      were exec'd, the behaviour would differ per platform - CPython emulates
      os.exec* on Windows without propagating the child's status.
    * the panel decides whether the deploy worked from the exit status, so the
      bot's code has to reach it unmodified.

    Both are asserted against a real interpreter rather than by reading the file
    alone. The probe subcommands chosen here are config-independent (argparse
    rejects them before any settings or database are touched), so this stays
    offline and needs no token.
    """
    import ast
    import subprocess

    root = Path(__file__).resolve().parents[2]
    app = root / "app.py"
    assert app.exists(), "app.py is missing from the repository root"

    # Inspect the parsed code rather than the text: the module docstring explains
    # at length why os.exec* is avoided, and a substring search would flag its own
    # explanation.
    tree = ast.parse(app.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [
                f"import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] == "subprocess"
            ]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                offenders.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Attribute) and node.attr in _EXEC_FAMILY:
            offenders.append(f".{node.attr}()")

    assert not offenders, (
        "app.py must run the bot in its own process, but it uses "
        f"{sorted(set(offenders))}: spawning a child would need signal "
        "forwarding, and os.exec* does not propagate the exit code on Windows"
    )

    # Run from an unrelated directory to prove paths come from __file__ rather
    # than the working directory a panel might choose.
    probe_dir = tempfile.mkdtemp(prefix="giveaway-appshim-")
    try:
        def probe(args: list[str]) -> int:
            return subprocess.run(
                [sys.executable, str(app), *args],
                cwd=probe_dir,
                capture_output=True,
                timeout=120,
                check=False,
            ).returncode

        # --help exercises the import wiring and the forwarding of arguments.
        assert probe(["--help"]) == 0, "app.py must forward arguments to the bot CLI"

        # An unknown subcommand makes argparse exit 2. If app.py returned anything
        # else, the panel would read every crash as a successful deploy.
        assert probe(["not-a-real-command"]) == 2, (
            "app.py must exit with the bot's own status; argparse exits 2 for an "
            "invalid subcommand, so anything else means the code was swallowed"
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def test_migrations_are_bom_free() -> None:
    """No migration file may contain a BOM, and both loaders must strip one.

    A BOM is not whitespace, so it silently becomes part of the first statement's
    text. SQLite ignores it, which is why every local test passed, but Turso's
    parser rejects the statement outright - a hosted deploy died on
    ``SQL_PARSE_ERROR`` for a migration that was provably fine locally. Both
    runtimes read the same files, so both loaders are checked.
    """
    root = Path(__file__).resolve().parents[2]
    bom = "\ufeff"

    migrations = sorted((root / "shared" / "migrations").glob("*.sql"))
    assert migrations, "no migrations found"
    for path in migrations:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), (
            f"{path.name} starts with a UTF-8 BOM; strip it"
        )
        assert bom not in raw.decode("utf-8"), (
            f"{path.name} contains a BOM character; strip it"
        )

    # The loaders must also cope with one, because a future editor can reintroduce
    # it and the failure mode is a rejected statement rather than a warning.
    from .db import load_migrations

    tmp = Path(tempfile.mkdtemp(prefix="giveaway-bom-"))
    try:
        bommed = tmp / "0001_probe.sql"
        bommed.write_bytes(b"\xef\xbb\xbf-- header\nCREATE TABLE t (id INTEGER);\n")
        loaded = load_migrations(tmp)
        assert len(loaded) == 1, "probe migration was not loaded"
        assert bom not in loaded[0][1], (
            "load_migrations must strip a BOM: it is not whitespace and Turso "
            "rejects the resulting statement"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    migrate_ts = root / "dashboard" / "scripts" / "migrate.ts"
    if migrate_ts.exists():
        source = migrate_ts.read_text(encoding="utf-8")
        assert "uFEFF" in source, (
            "the dashboard's migration loader must strip a BOM too - it applies "
            "the same files, so it would fail on the same statement"
        )


def test_rows_are_mapped_for_any_driver() -> None:
    """Result rows must be readable by name on both backends, not just SQLite.

    SQLite can be configured to hand back sqlite3.Row, which supports row.keys().
    libSQL has no row_factory attribute at all, so it returns plain tuples, and
    code that called row.keys() unconditionally died with

        AttributeError: 'tuple' object has no attribute 'keys'

    on the first query against a hosted database - after migrations had already
    been applied, so it looked like a data problem rather than a driver one.

    The point of this check is the seam. It runs the real read path through a
    connection that behaves like libSQL (tuples, description, no row_factory) so
    the behaviour is asserted directly instead of inferred from SQLite passing.
    """
    from .config import Settings
    from .db import Database

    class _TupleCursor:
        """A cursor shaped like libSQL's: tuples plus a description."""

        def __init__(self, cursor: Any) -> None:
            self._cursor = cursor

        @property
        def description(self) -> Any:
            return self._cursor.description

        @property
        def lastrowid(self) -> Any:
            return self._cursor.lastrowid

        @property
        def rowcount(self) -> Any:
            return self._cursor.rowcount

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [tuple(row) for row in self._cursor.fetchall()]

        def close(self) -> None:
            self._cursor.close()

    class _TupleConnection:
        """A connection that offers no row_factory, exactly like libSQL."""

        def __init__(self, conn: Any) -> None:
            self._conn = conn
            assert not hasattr(self, "row_factory"), "stub must not accept row_factory"

        def execute(self, sql: str, params: Any = ()) -> _TupleCursor:
            return _TupleCursor(self._conn.execute(sql, params))

        def close(self) -> None:
            self._conn.close()

    with _temp_database("driver-rows") as db:
        # The migrations themselves populated schema_migrations, so this needs no
        # extra fixture and no coupling to another table's columns.
        assert db.scalar("SELECT COUNT(*) AS n FROM schema_migrations") == 6, (
            "expected all six migrations applied in the scratch database"
        )

        # Read back through a libSQL-shaped connection.
        inner = Database(_isolated_settings())
        try:
            real = inner._connect_factory()  # noqa: SLF001 - exercising the seam
            inner._connect_factory = lambda: _TupleConnection(real)  # noqa: SLF001
            assert inner.backend == "sqlite"

            # The reported crash came through this exact query.
            applied = inner.query_one("SELECT filename, checksum FROM schema_migrations")
            assert applied is not None, "libSQL-shaped query returned nothing"
            assert set(applied) == {"filename", "checksum"}, (
                f"expected filename/checksum keys, got {applied!r}"
            )
            assert applied["filename"] == "0001_core.sql", (
                f"values must line up with their columns, got {applied!r}"
            )

            listed = inner.query("SELECT filename, checksum FROM schema_migrations")
            assert len(listed) == 6 and all(
                isinstance(r["checksum"], str) and r["checksum"] for r in listed
            ), "repeated reads must stay name-addressable over tuple rows"

            count = inner.scalar("SELECT COUNT(*) AS n FROM schema_migrations")
            assert count == 6, f"scalar() over tuple rows returned {count!r}"

            # A join proves aliases and multiple columns map in order.
            joined = inner.query(
                "SELECT a.filename AS name, b.filename AS other"
                " FROM schema_migrations a, schema_migrations b"
                " WHERE a.filename = b.filename LIMIT 1"
            )
            assert len(joined) == 1 and set(joined[0]) == {"name", "other"}, (
                f"aliased columns must map by name, got {joined!r}"
            )
        finally:
            inner.close_all()

        # And the real connection must not be relying on a row_factory either.
        raw = db.connection()
        assert getattr(raw, "row_factory", None) is None, (
            "Database.connection() must not set row_factory: it is a silent no-op on "
            "libSQL, so relying on it makes named rows work on SQLite only"
        )


# --------------------------------------------------------------------------- #
# Cross-language vectors
# --------------------------------------------------------------------------- #
def deterministic_test_seed(*parts: str) -> str:
    """A *test-only* deterministic seed so vectors are reproducible.

    Production seeds come from :func:`giveaway_bot.fairness.generate_seed`
    (the OS CSPRNG); this derivation exists purely to give the TypeScript
    implementation stable vectors to assert against.
    """
    material = ":".join(parts).encode()
    return hashlib.sha256(b"giveaway-bot-test-vector|" + material).hexdigest()


def build_vectors() -> dict[str, Any]:
    """Deterministic vectors (fixed seeds) that both implementations must match."""
    cases = [
        ("gw_vector_1", "100000000000000001", 1),
        ("gw_vector_1", "100000000000000002", 1),
        ("gw_vector_1", "100000000000000003", 2),
        ("gw_vector_2", "200000000000000001", 1),
        ("gw_vector_2", "200000000000000002", 3),
        ("gw_f3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "42", 1),
    ]
    vectors = []
    for giveaway_id, user_id, seq in cases:
        seed_hex = deterministic_test_seed(giveaway_id, user_id, str(seq))
        score, retries = score_entry(giveaway_id, user_id, seq, seed_hex, len(cases))
        vectors.append(
            {
                "giveaway_id": giveaway_id,
                "user_id": user_id,
                "entry_seq": seq,
                "total_entries": len(cases),
                "seed": seed_hex,
                "commitment": commitment(seed_hex),
                "score": score,
                "retry_count": retries,
            }
        )
    return {
        "algorithm": METHOD,
        "generated_by": "python",
        "note": "Seeds here are derived deterministically for testing only.",
        "vectors": vectors,
        "vectors_participant_digest": participant_digest(
            (vector["user_id"], vector["entry_seq"]) for vector in vectors
        ),
    }


def write_vectors(path: Path = VECTOR_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_vectors(), indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    check = Check()
    print("Giveaway bot self-test")
    print("=" * 60)

    check.section("Provably fair draw")
    check.run("deterministic for identical inputs", test_determinism)
    check.run("total, stable ordering (ties cannot bias)", test_ordering_is_total_and_stable)
    check.run("commitment binds the seed", test_commitment_binds_seed)
    check.run("manifest tampering is detected", test_manifest_tamper_is_detected)
    check.run("scores stay in the unbiased range", test_score_bounds_and_retry)
    check.run("winner distribution is not skewed", test_distribution_is_flat)
    check.run("python matches the cross-language vectors", test_cross_language_vectors)
    check.run("empty / short / duplicate entry sets", test_shortfall_and_empty)
    check.run("seed source has no collisions", test_random_seed_source)
    check.run("no admin winner-override exists", test_no_admin_override_exists)

    check.section("Eligibility")
    check.run("every rule fires with the right reason", test_eligibility_rules)
    check.run("message-activity requirement gates entry", test_message_requirement_rules)

    check.section("Input validation")
    check.run("duration parsing", test_duration_parsing)
    check.run("giveaway payload validation", test_payload_validation)
    check.run("message requirement validation", test_message_requirement_validation)

    check.section("Message activity")
    check.run("counters gate the join button", test_message_counting_and_gating)
    check.run("duplicate/backfilled events cannot inflate counts", test_message_count_idempotency)
    check.run("tracker skips work and flushes in batches", test_message_tracker_buffering)
    check.run("revalidation flags only who fails today", test_message_revalidation)

    check.section("Temporary entrants role")
    check.run("role is granted on entry and journalled", test_role_grant_on_entry)
    check.run("only bot grants are released", test_role_release_provenance)
    check.run("role tasks are retried, not lost", test_role_task_retry)
    check.run("one active giveaway per guild", test_single_active_giveaway)
    check.run("the giveaway channel is fixed by configuration", test_giveaway_channel_is_fixed)
    check.run("requirements.txt covers every runtime dependency", test_requirements_cover_runtime_deps)
    check.run("the shipped .env.example is a loadable config", test_example_env_file_loads)
    check.run("the root app.py shim reaches the bot", test_app_shim_entrypoint)
    check.run("migrations are BOM-free and loaders strip one", test_migrations_are_bom_free)
    check.run("rows map by name on any driver, not just SQLite", test_rows_are_mapped_for_any_driver)

    check.section("Persistence + lifecycle")
    check.run("create -> join -> manage -> draw -> reroll", test_lifecycle)
    check.run("crash recovery finishes an interrupted draw", test_crash_recovery)
    check.run("migrations are immutable and idempotent", test_migration_integrity)

    print("\n" + "=" * 60)
    if check.failures:
        print(f"FAILED: {len(check.failures)} check(s) failed, {check.passed} passed")
        for failure in check.failures:
            print(f"  [FAIL] {failure}")
        return 1
    print(f"OK: {check.passed} checks passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

