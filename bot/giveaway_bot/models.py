"""Domain models shared across the bot.

Plain dataclasses: no ORM, no lazy loading, no magic.  ``from_row`` helpers make
the mapping between SQL rows and domain objects explicit and reviewable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GiveawayStatus(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"

    @property
    def is_open(self) -> bool:
        return self in (GiveawayStatus.SCHEDULED, GiveawayStatus.RUNNING, GiveawayStatus.PAUSED)

    @property
    def accepts_entries(self) -> bool:
        return self is GiveawayStatus.RUNNING


class EntryStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    DISQUALIFIED = "disqualified"
    WINNER = "winner"
    LOST = "lost"


class CommandKind(StrEnum):
    """Whitelisted control commands (dashboard -> bot).

    Anything not in this enum is rejected by the dashboard *and* by the bot,
    so a compromised dashboard account cannot invent new bot capabilities.
    """

    CREATE = "giveaway.create"
    UPDATE = "giveaway.update"
    PAUSE = "giveaway.pause"
    RESUME = "giveaway.resume"
    EXTEND = "giveaway.extend"
    SHORTEN = "giveaway.shorten"
    END = "giveaway.end"
    CANCEL = "giveaway.cancel"
    REROLL = "giveaway.reroll"
    REVEAL = "giveaway.reveal"
    DISQUALIFY = "entry.disqualify"
    RESTORE = "entry.restore"
    ANNOUNCE = "giveaway.announce"
    #: Message-activity rule editing (dashboard -> bot).
    MESSAGE_REQUIREMENT = "giveaway.message_requirement"
    ACTIVITY_REVALIDATE = "giveaway.activity_revalidate"


#: Commands that must never be accepted for a running giveaway with a lock.
MUTATING_GIVEAWAY_COMMANDS = frozenset(
    {
        CommandKind.UPDATE,
        CommandKind.PAUSE,
        CommandKind.RESUME,
        CommandKind.EXTEND,
        CommandKind.SHORTEN,
        CommandKind.END,
        CommandKind.REROLL,
    }
)


def now_ms() -> int:
    return int(time.time() * 1000)


def normalise_status(value: GiveawayStatus | str | None) -> GiveawayStatus:
    """Coerce any status representation into the enum.

    Uses the lookup table rather than ``GiveawayStatus(value)`` so that an
    unrecognised value raises a clear error instead of a bare ``ValueError``
    inside an eligibility check.
    """
    if isinstance(value, GiveawayStatus):
        return value
    try:
        return _STATUS_LOOKUP[str(value or "").strip().lower()]
    except KeyError:
        raise ValueError(f"unknown giveaway status: {value!r}") from None


_STATUS_LOOKUP: dict[str, GiveawayStatus] = {member.value: member for member in GiveawayStatus}


def snowflake_to_ms(snowflake: str | int | None) -> int | None:
    """Discord epoch: (snowflake >> 22) + 1420070400000."""
    if snowflake in (None, "", 0):
        return None
    try:
        value = int(snowflake)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return (value >> 22) + 1_420_070_400_000


@dataclass(slots=True)
class Guild:
    id: str
    name: str = ""
    icon_url: str | None = None
    owner_id: str | None = None
    member_count: int = 0
    bot_present: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Guild:
        return cls(
            id=str(row["id"]),
            name=row.get("name") or "",
            icon_url=row.get("icon_url"),
            owner_id=row.get("owner_id"),
            member_count=int(row.get("member_count") or 0),
            bot_present=bool(row.get("bot_present")),
        )


@dataclass(slots=True)
class Giveaway:
    id: str
    guild_id: str
    channel_id: str
    message_id: str | None
    status: GiveawayStatus
    title: str
    description: str = ""
    prize: str = ""
    prize_image_url: str | None = None
    prize_count: int = 1
    winner_count: int = 1
    entry_limit: int = 0
    max_entries_per_user: int = 1
    starts_at: int | None = None
    ends_at: int | None = None
    original_ends_at: int | None = None
    paused_at: int | None = None
    paused_remaining_ms: int | None = None
    total_draws: int = 0
    required_role_ids: list[str] = field(default_factory=list)
    required_mode: str = "any"
    blacklist_role_ids: list[str] = field(default_factory=list)
    allowed_channel_ids: list[str] = field(default_factory=list)
    min_account_age_days: int = 0
    min_guild_join_days: int = 0
    entrants_require_membership: bool = True
    # --- message activity requirement ---
    min_messages: int = 0
    message_count_channel_ids: list[str] = field(default_factory=list)
    message_count_ignore_bots: bool = True
    message_count_since: int | None = None
    message_count_scope: str = "guild"
    #: Temporary "entrants" role granted on join and removed when the giveaway
    #: ends. None disables the feature for this giveaway.
    participant_role_id: str | None = None
    server_seed: str | None = None
    seed_commitment: str | None = None
    seed_sealed_at: int | None = None
    seed_revealed_at: int | None = None
    draw_round: int = 0
    locked_at: int | None = None
    created_by: str = ""
    updated_by: str | None = None
    version: int = 1
    created_at: int = 0
    updated_at: int = 0
    ended_reason: str | None = None
    # denormalised counters
    participant_count: int = 0
    entry_count: int = 0
    winner_count_total: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Giveaway:
        return cls(
            id=str(row["id"]),
            guild_id=str(row["guild_id"]),
            channel_id=str(row["channel_id"]),
            message_id=row.get("message_id"),
            status=GiveawayStatus(row.get("status") or "scheduled"),
            title=row.get("title") or "",
            description=row.get("description") or "",
            prize=row.get("prize") or "",
            prize_image_url=row.get("prize_image_url"),
            prize_count=int(row.get("prize_count") or 1),
            winner_count=int(row.get("winner_count") or 1),
            entry_limit=int(row.get("entry_limit") or 0),
            max_entries_per_user=int(row.get("max_entries_per_user") or 1),
            starts_at=row.get("starts_at"),
            ends_at=row.get("ends_at"),
            original_ends_at=row.get("original_ends_at"),
            paused_at=row.get("paused_at"),
            paused_remaining_ms=row.get("paused_remaining_ms"),
            total_draws=int(row.get("total_draws") or 0),
            required_role_ids=load_role_list(row.get("required_role_ids")),
            required_mode=row.get("required_mode") or "any",
            blacklist_role_ids=load_role_list(row.get("blacklist_role_ids")),
            allowed_channel_ids=load_role_list(row.get("allowed_channel_ids")),
            min_account_age_days=int(row.get("min_account_age_days") or 0),
            min_guild_join_days=int(row.get("min_guild_join_days") or 0),
            entrants_require_membership=bool(row.get("entrants_require_membership", 1)),
            min_messages=int(row.get("min_messages") or 0),
            message_count_channel_ids=load_role_list(row.get("message_count_channel_ids")),
            message_count_ignore_bots=bool(row.get("message_count_ignore_bots", 1)),
            message_count_since=row.get("message_count_since"),
            message_count_scope=row.get("message_count_scope") or "guild",
            participant_role_id=row.get("participant_role_id"),
            server_seed=row.get("server_seed"),
            seed_commitment=row.get("seed_commitment"),
            seed_sealed_at=row.get("seed_sealed_at"),
            seed_revealed_at=row.get("seed_revealed_at"),
            draw_round=int(row.get("draw_round") or 0),
            locked_at=row.get("locked_at"),
            created_by=str(row.get("created_by") or ""),
            updated_by=row.get("updated_by"),
            version=int(row.get("version") or 1),
            created_at=int(row.get("created_at") or 0),
            updated_at=int(row.get("updated_at") or 0),
            ended_reason=row.get("ended_reason"),
            participant_count=int(row.get("participant_count") or 0),
            entry_count=int(row.get("entry_count") or 0),
            winner_count_total=int(row.get("winner_count_total") or 0),
        )

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None

    def remaining_ms(self, *, now: int | None = None) -> int:
        current = now if now is not None else now_ms()
        if self.status is GiveawayStatus.PAUSED and self.paused_remaining_ms is not None:
            return max(0, self.paused_remaining_ms)
        if self.status is not GiveawayStatus.RUNNING or self.ends_at is None:
            return 0
        return max(0, self.ends_at - current)

    def rules_public(self) -> dict[str, Any]:
        """Rule summary safe for public display (no operator-only notes)."""
        return {
            "winner_count": self.winner_count,
            "prize_count": self.prize_count,
            "max_entries_per_user": self.max_entries_per_user,
            "entry_limit": self.entry_limit or None,
            "required_mode": self.required_mode,
            "required_role_count": len(self.required_role_ids),
            "blacklist_role_count": len(self.blacklist_role_ids),
            "channel_restricted": bool(self.allowed_channel_ids),
            "min_account_age_days": self.min_account_age_days,
            "min_guild_join_days": self.min_guild_join_days,
            "requires_membership": self.entrants_require_membership,
            "min_messages": self.min_messages,
            "message_count_scope": self.message_count_scope,
            "message_count_channel_count": len(self.message_count_channel_ids),
        }


def load_role_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def dump_role_list(values: list[str]) -> str:
    return json.dumps([str(value) for value in values], separators=(",", ":"))


@dataclass(slots=True)
class Participant:
    user_id: str
    entries: int = 0
    first_joined_at: int | None = None
    last_joined_at: int | None = None
    status: EntryStatus = EntryStatus.VALID
    account_created_at: int | None = None
    guild_joined_at: int | None = None
    invalid_reason: str | None = None
    display_name: str | None = None

    @property
    def has_entries(self) -> bool:
        return self.entries > 0 and self.status in (EntryStatus.VALID, EntryStatus.WINNER, EntryStatus.LOST)


@dataclass(slots=True)
class DrawRecord:
    id: str
    giveaway_id: str
    round: int
    method: str
    algorithm_version: str
    server_seed: str
    seed_commitment: str
    participant_digest: str
    participant_count: int
    eligible_count: int
    winner_count: int
    manifest_json: str
    triggered_by: str | None
    trigger_reason: str
    duration_ms: int | None
    created_at: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DrawRecord:
        return cls(
            id=str(row["id"]),
            giveaway_id=str(row["giveaway_id"]),
            round=int(row["round"]),
            method=row.get("method") or "",
            algorithm_version=row.get("algorithm_version") or "",
            server_seed=str(row["server_seed"]),
            seed_commitment=str(row["seed_commitment"]),
            participant_digest=str(row["participant_digest"]),
            participant_count=int(row["participant_count"]),
            eligible_count=int(row["eligible_count"]),
            winner_count=int(row["winner_count"]),
            manifest_json=row.get("manifest_json") or "{}",
            triggered_by=row.get("triggered_by"),
            trigger_reason=row.get("trigger_reason") or "ended",
            duration_ms=row.get("duration_ms"),
            created_at=int(row.get("created_at") or 0),
        )


@dataclass(slots=True)
class WinnerRecord:
    user_id: str
    rank: int
    round: int
    score: str
    entry_seq: int | None = None
    entry_id: int | None = None
    draw_id: str = ""
    server_seed: str = ""
    seed_commitment: str = ""
    awarded_at: int = 0
    display_name: str | None = None


@dataclass(slots=True)
class CommandRecord:
    id: int
    guild_id: str
    giveaway_id: str | None
    kind: str
    payload: dict[str, Any]
    requested_by: str
    requested_by_name: str | None
    source: str
    status: str
    attempts: int
    created_at: int
    last_error: str | None = None