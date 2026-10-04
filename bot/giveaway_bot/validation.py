"""Input validation and normalisation for every user- or dashboard-supplied value.

The dashboard validates with Zod *and* the bot re-validates here.  The bot is
the trust boundary: a compromised dashboard (or a hand-written curl) cannot push
a winner id, an unbounded duration, or a malformed role list into the draw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_TITLE = 256
MAX_DESCRIPTION = 4000
MAX_PRIZE = 512
MAX_WINNERS = 20
MAX_ENTRIES_PER_USER = 100
MAX_DURATION_MS = 365 * 24 * 60 * 60 * 1000  # 1 year
MIN_DURATION_MS = 30 * 1000  # 30 seconds
MAX_ROLES = 50
MAX_CHANNELS = 50
#: Upper bound on the message requirement. High enough for real anti-spam
#: rules, low enough that nobody can demand a number no member could reach.
MAX_MIN_MESSAGES = 100_000
#: Cap on channel-scoped counting (a giveaway may not watch the whole server
#: twice over; the guild scope already covers "count everything").
MAX_MESSAGE_CHANNELS = 200

_SNOWFLAKE = re.compile(r"^[0-9]{15,25}$")
_URL = re.compile(r"^https://(cdn\.discordapp\.com|media\.discordapp\.net)/[^\s]+$", re.IGNORECASE)


class ValidationError(ValueError):
    """Raised with a field -> message map for clean API responses."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{key}: {value}" for key, value in errors.items()))


@dataclass(slots=True)
class GiveawayInput:
    """Validated, normalised giveaway configuration."""

    title: str
    description: str = ""
    prize: str = ""
    prize_image_url: str | None = None
    prize_count: int = 1
    winner_count: int = 1
    entry_limit: int = 0
    max_entries_per_user: int = 1
    duration_ms: int | None = None
    ends_at: int | None = None
    required_role_ids: list[str] = field(default_factory=list)
    required_mode: str = "any"
    blacklist_role_ids: list[str] = field(default_factory=list)
    allowed_channel_ids: list[str] = field(default_factory=list)
    min_account_age_days: int = 0
    min_guild_join_days: int = 0
    entrants_require_membership: bool = True
    min_messages: int = 0
    message_count_channel_ids: list[str] = field(default_factory=list)
    message_count_ignore_bots: bool = True
    message_count_since: int | None = None
    message_count_scope: str = "guild"
    channel_id: str = ""

    def as_columns(self) -> dict[str, Any]:
        """Columns for ``repositories.giveaways.update_fields``."""
        data: dict[str, Any] = {
            "title": self.title,
            "description": self.description,
            "prize": self.prize,
            "prize_image_url": self.prize_image_url,
            "prize_count": self.prize_count,
            "winner_count": self.winner_count,
            "entry_limit": self.entry_limit,
            "max_entries_per_user": self.max_entries_per_user,
            "required_role_ids": self.required_role_ids,
            "required_mode": self.required_mode,
            "blacklist_role_ids": self.blacklist_role_ids,
            "allowed_channel_ids": self.allowed_channel_ids,
            "min_account_age_days": self.min_account_age_days,
            "min_guild_join_days": self.min_guild_join_days,
            "entrants_require_membership": int(self.entrants_require_membership),
            "min_messages": self.min_messages,
            "message_count_channel_ids": self.message_count_channel_ids,
            "message_count_ignore_bots": int(self.message_count_ignore_bots),
            "message_count_scope": self.message_count_scope,
        }
        if self.message_count_since is not None:
            data["message_count_since"] = self.message_count_since
        if self.ends_at is not None:
            data["ends_at"] = self.ends_at
        if self.duration_ms is not None:
            data["ends_at"] = self.ends_at
        return data


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_snowflake(value: Any, field_name: str, errors: dict[str, str]) -> str | None:
    text = _as_str(value).strip()
    if not text:
        errors[field_name] = "Required."
        return None
    if not _SNOWFLAKE.match(text):
        errors[field_name] = "Not a valid Discord ID."
        return None
    return text


def validate_snowflake_list(
    value: Any, field_name: str, errors: dict[str, str], *, limit: int
) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw = [item for item in re.split(r"[,\s]+", value) if item]
    elif isinstance(value, (list, tuple)):
        raw = [item for item in value if str(item).strip()]
    else:
        errors[field_name] = "Expected a list of Discord IDs."
        return []

    out: list[str] = []
    for item in raw[: limit + 5]:
        text = _as_str(item).strip()
        if not _SNOWFLAKE.match(text):
            errors[field_name] = f"{text!r} is not a valid Discord ID."
            return []
        if text not in out:
            out.append(text)
    if len(out) > limit:
        errors[field_name] = f"At most {limit} entries allowed."
        return []
    return out


def parse_duration(value: Any) -> int | None:
    """Parse ``30m`` / ``2h30m`` / ``1d`` / ``90`` (minutes) into milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value * 60_000)
    text = _as_str(value).strip().lower()
    if text.isdigit():
        return int(text) * 60_000
    pattern = re.compile(r"(\d+)\s*([smhdw])")
    matches = pattern.findall(text)
    if not matches:
        return None
    units = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    total = sum(int(amount) * units[unit] for amount, unit in matches)
    # Reject things like "1h30" (trailing unit-less digits).
    consumed = sum(len(amount) + len(unit) for amount, unit in matches)
    if consumed < len(re.sub(r"\s+", "", text)):
        return None
    return total


def validate_giveaway_payload(payload: dict[str, Any], *, partial: bool = False) -> GiveawayInput:
    """Validate a create/update payload. Raises :class:`ValidationError`."""
    errors: dict[str, str] = {}

    title = _as_str(payload.get("title")).strip()
    if not partial or "title" in payload:
        if not title:
            errors["title"] = "Title is required."
        elif len(title) > MAX_TITLE:
            errors["title"] = f"Title must be {MAX_TITLE} characters or fewer."

    description = _as_str(payload.get("description")).strip() if "description" in payload or not partial else ""
    if len(description) > MAX_DESCRIPTION:
        errors["description"] = f"Description must be {MAX_DESCRIPTION} characters or fewer."

    prize = _as_str(payload.get("prize")).strip() if "prize" in payload or not partial else ""
    if len(prize) > MAX_PRIZE:
        errors["prize"] = f"Prize must be {MAX_PRIZE} characters or fewer."

    image = payload.get("prize_image_url")
    image_url: str | None = None
    if image not in (None, "", "null"):
        candidate = _as_str(image).strip()
        if not _URL.match(candidate):
            errors["prize_image_url"] = "Must be a discord CDN image URL."
        else:
            image_url = candidate

    winner_count = _int(payload.get("winner_count"), 1)
    if not 1 <= winner_count <= MAX_WINNERS:
        errors["winner_count"] = f"Choose between 1 and {MAX_WINNERS} winners."

    prize_count = _int(payload.get("prize_count"), 1)
    if not 1 <= prize_count <= MAX_WINNERS:
        errors["prize_count"] = f"Choose between 1 and {MAX_WINNERS} prizes."

    entry_limit = _int(payload.get("entry_limit"), 0)
    if entry_limit < 0 or entry_limit > 1_000_000:
        errors["entry_limit"] = "Entry limit must be between 0 and 1000000."

    max_entries = _int(payload.get("max_entries_per_user"), 1)
    if not 1 <= max_entries <= MAX_ENTRIES_PER_USER:
        errors["max_entries_per_user"] = f"Must be between 1 and {MAX_ENTRIES_PER_USER}."

    min_account_age = _int(payload.get("min_account_age_days"), 0)
    if not 0 <= min_account_age <= 3650:
        errors["min_account_age_days"] = "Must be between 0 and 3650 days."

    min_join_age = _int(payload.get("min_guild_join_days"), 0)
    if not 0 <= min_join_age <= 3650:
        errors["min_guild_join_days"] = "Must be between 0 and 3650 days."

    # --- message activity requirement ---
    # 0 disables the rule entirely, which is the default for every giveaway.
    min_messages = _int(payload.get("min_messages"), 0)
    if not 0 <= min_messages <= MAX_MIN_MESSAGES:
        errors["min_messages"] = f"Choose between 0 (disabled) and {MAX_MIN_MESSAGES} messages."

    message_channels = validate_snowflake_list(
        payload.get("message_count_channel_ids"),
        "message_count_channel_ids",
        errors,
        limit=MAX_MESSAGE_CHANNELS,
    )
    message_scope = _as_str(payload.get("message_count_scope") or "guild").strip().lower()
    if message_scope not in {"guild", "channel"}:
        errors["message_count_scope"] = "Must be 'guild' or 'channel'."
        message_scope = "guild"
    # A disabled requirement must not keep stale channel filters, since
    # they would silently reactivate if the rule were turned back on.
    # This is normalisation, not validation: `0` is the documented way to turn
    # the rule off, and it should not fail because a UI still had channels set.
    if min_messages == 0:
        message_channels = []
        message_scope = "guild"
    elif message_scope == "channel" and not message_channels:
        # Channel scope with no channels would count nothing, quietly making
        # the giveaway unenterable - reject rather than silently widen to guild.
        errors["message_count_channel_ids"] = (
            "Pick at least one channel, or set the scope to 'guild'."
        )

    required_mode = _as_str(payload.get("required_mode") or "any").strip().lower()
    if required_mode not in {"any", "all"}:
        errors["required_mode"] = "Must be 'any' or 'all'."

    required_roles = validate_snowflake_list(
        payload.get("required_role_ids"), "required_role_ids", errors, limit=MAX_ROLES
    )
    blacklist_roles = validate_snowflake_list(
        payload.get("blacklist_role_ids"), "blacklist_role_ids", errors, limit=MAX_ROLES
    )
    channels = validate_snowflake_list(
        payload.get("allowed_channel_ids"), "allowed_channel_ids", errors, limit=MAX_CHANNELS
    )
    overlap = set(required_roles) & set(blacklist_roles)
    if overlap:
        errors["blacklist_role_ids"] = (
            "A role cannot be both required and blacklisted: " + ", ".join(sorted(overlap))
        )

    # `channel_id` is validated separately when it arrives in the payload (e.g.
    # from the dashboard create form). The service passes it as an explicit
    # argument, so absence here is not an error.
    channel_id = ""
    if payload.get("channel_id"):
        channel_id = validate_snowflake(payload.get("channel_id"), "channel_id", errors) or ""

    # Timing: either an explicit end timestamp or a duration.
    duration_ms: int | None = None
    ends_at: int | None = None
    raw_duration = payload.get("duration_ms")
    raw_duration_text = payload.get("duration")
    if raw_duration_text not in (None, "") and raw_duration in (None, ""):
        duration_ms = parse_duration(raw_duration_text)
        if duration_ms is None:
            errors["duration"] = "Use formats like 30m, 12h, 3d or 90 (minutes)."
    elif raw_duration not in (None, ""):
        duration_ms = _int(raw_duration, 0) or None

    raw_ends_at = payload.get("ends_at")
    if raw_ends_at not in (None, "", 0):
        ends_at = _int(raw_ends_at, 0)
        if ends_at <= 0:
            errors["ends_at"] = "Invalid end timestamp."

    if ends_at is None and duration_ms is None and not partial:
        errors["duration"] = "Provide a duration or an end time."

    if duration_ms is not None:
        if not MIN_DURATION_MS <= duration_ms <= MAX_DURATION_MS:
            errors["duration"] = "Duration must be between 30 seconds and 1 year."
        elif ends_at is None:
            from .db import now_ms  # local import avoids a cycle at module load

            ends_at = now_ms() + duration_ms

    if errors:
        raise ValidationError(errors)

    return GiveawayInput(
        title=title,
        description=description,
        prize=prize,
        prize_image_url=image_url,
        prize_count=prize_count,
        winner_count=winner_count,
        entry_limit=entry_limit,
        max_entries_per_user=max_entries,
        duration_ms=duration_ms,
        ends_at=ends_at,
        required_role_ids=required_roles,
        required_mode=required_mode,
        blacklist_role_ids=blacklist_roles,
        allowed_channel_ids=channels,
        min_account_age_days=min_account_age,
        min_guild_join_days=min_join_age,
        entrants_require_membership=_bool(payload.get("entrants_require_membership"), True),
        min_messages=min_messages,
        message_count_channel_ids=message_channels,
        message_count_ignore_bots=_bool(payload.get("message_count_ignore_bots"), True),
        message_count_since=_int(payload.get("message_count_since"), 0) or None,
        message_count_scope=message_scope,
        channel_id=channel_id,
    )


def validate_mutation_action(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the numeric arguments of pause/resume/extend/shorten/end/reroll."""
    errors: dict[str, str] = {}
    result: dict[str, Any] = {}

    minutes = payload.get("minutes")
    duration_text = payload.get("duration")
    duration_ms: int | None = None
    if duration_text not in (None, ""):
        duration_ms = parse_duration(duration_text)
        if duration_ms is None:
            errors["duration"] = "Use formats like 30m, 12h or 3d."
    elif minutes not in (None, ""):
        duration_ms = _int(minutes, 0) * 60_000
    if duration_ms is not None:
        if not MIN_DURATION_MS <= duration_ms <= MAX_DURATION_MS:
            errors["duration"] = "Duration must be between 30 seconds and 1 year."
        result["duration_ms"] = duration_ms

    new_winner_count = payload.get("winner_count")
    if new_winner_count not in (None, ""):
        count = _int(new_winner_count, 0)
        if not 1 <= count <= MAX_WINNERS:
            errors["winner_count"] = f"Choose between 1 and {MAX_WINNERS} winners."
        result["winner_count"] = count

    reason = _as_str(payload.get("reason")).strip()
    if reason:
        if len(reason) > 200:
            errors["reason"] = "Reason must be 200 characters or fewer."
        result["reason"] = reason

    if errors:
        raise ValidationError(errors)
    return result