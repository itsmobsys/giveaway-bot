"""Eligibility evaluation - pure functions, no Discord objects required.

The functions here take plain values (role ids, timestamps) so they can be unit
tested exhaustively without a Discord connection.  The Discord layer's only job
is to *collect* those values from a ``discord.Member`` and pass them in.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .models import Giveaway, GiveawayStatus

DAY_MS = 86_400_000


class Reason:
    """Stable machine-readable reason codes (also surfaced in the UI)."""

    OK = "ok"
    GIVEAWAY_NOT_RUNNING = "giveaway_not_running"
    GIVEAWAY_ENDED = "giveaway_ended"
    GIVEAWAY_LOCKED = "giveaway_locked"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    REQUIRES_MEMBERSHIP = "requires_membership"
    MISSING_REQUIRED_ROLES = "missing_required_roles"
    HAS_BLACKLISTED_ROLE = "has_blacklisted_role"
    ACCOUNT_TOO_NEW = "account_too_new"
    JOINED_TOO_RECENTLY = "joined_too_recently"
    ENTRY_LIMIT_REACHED = "entry_limit_reached"
    MAX_ENTRIES_REACHED = "max_entries_reached"
    INVALID_ROLE_CONFIG = "invalid_role_config"
    INSUFFICIENT_MESSAGES = "insufficient_messages"


@dataclass(frozen=True, slots=True)
class Eligibility:
    ok: bool
    reason: str
    message: str
    #: For role failures, the roles that were missing / offending.
    role_ids: tuple[str, ...] = ()
    #: Message-activity progress: (current, required). ``None`` when the rule is
    #: not applicable, so the UI can hide the block entirely.
    progress: tuple[int, int] | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok

    @property
    def messages_remaining(self) -> int:
        """Messages still needed before this member qualifies (0 when done)."""
        if self.progress is None:
            return 0
        current, required = self.progress
        return max(0, required - current)


ALLOWED = Eligibility(True, Reason.OK, "Eligible.")


def evaluate_join(
    giveaway: Giveaway,
    *,
    user_id: str,
    role_ids: Iterable[str],
    account_created_at: int | None,
    guild_joined_at: int | None,
    is_member: bool,
    channel_id: str,
    current_entries: int = 0,
    now: int,
    total_entries: int = 0,
    message_count: int = 0,
) -> Eligibility:
    """Decide whether ``user_id`` may add an entry right now.

    This is the *only* gate used by the join button, by slash commands and by
    the dashboard's "eligibility validation" panel, so the three can never
    disagree.

    ``total_entries`` is an explicit parameter rather than a field read off the
    giveaway object: it is live data owned by the caller. A denormalised counter
    on the row can be stale, and quietly letting a user past an entry cap is a
    worse failure than one extra query.
    """
    # Normalise defensively: a raw string status must behave exactly like the
    # enum, otherwise a malformed row could bypass every state check.
    status = GiveawayStatus(giveaway.status)

    if status is GiveawayStatus.ENDED:
        return Eligibility(False, Reason.GIVEAWAY_ENDED, "This giveaway has ended.")
    if status is not GiveawayStatus.RUNNING:
        return Eligibility(
            False, Reason.GIVEAWAY_NOT_RUNNING, "This giveaway is not accepting entries."
        )
    if giveaway.locked_at is not None:
        return Eligibility(
            False, Reason.GIVEAWAY_LOCKED, "The draw for this giveaway is being finalised."
        )
    if giveaway.ends_at is not None and now >= giveaway.ends_at:
        return Eligibility(False, Reason.GIVEAWAY_ENDED, "This giveaway has ended.")

    if giveaway.allowed_channel_ids and channel_id not in giveaway.allowed_channel_ids:
        return Eligibility(
            False,
            Reason.CHANNEL_NOT_ALLOWED,
            "This giveaway can only be entered from specific channels.",
        )

    if giveaway.entrants_require_membership and not is_member:
        return Eligibility(
            False,
            Reason.REQUIRES_MEMBERSHIP,
            "You must be a member of this server to enter.",
        )

    role_ids = {str(role) for role in role_ids}

    blacklist = {str(role) for role in giveaway.blacklist_role_ids}
    if blacklist & role_ids:
        return Eligibility(
            False,
            Reason.HAS_BLACKLISTED_ROLE,
            "One of your roles is excluded from this giveaway.",
            tuple(sorted(blacklist & role_ids)),
        )

    required = {str(role) for role in giveaway.required_role_ids}
    if required:
        if giveaway.required_mode == "all":
            missing = required - role_ids
            ok = not missing
        else:
            missing = set() if required & role_ids else required
            ok = not missing
        if not ok:
            return Eligibility(
                False,
                Reason.MISSING_REQUIRED_ROLES,
                "You do not have the role required to enter this giveaway.",
                tuple(sorted(missing)),
            )

    if giveaway.min_account_age_days > 0:
        if account_created_at is None:
            return Eligibility(
                False,
                Reason.ACCOUNT_TOO_NEW,
                f"Your account must be at least {giveaway.min_account_age_days} day(s) old.",
            )
        if now - account_created_at < giveaway.min_account_age_days * DAY_MS:
            return Eligibility(
                False,
                Reason.ACCOUNT_TOO_NEW,
                f"Your account must be at least {giveaway.min_account_age_days} day(s) old.",
            )

    if giveaway.min_guild_join_days > 0:
        if guild_joined_at is None:
            return Eligibility(
                False,
                Reason.JOINED_TOO_RECENTLY,
                f"You must have been in this server for {giveaway.min_guild_join_days} day(s).",
            )
        if now - guild_joined_at < giveaway.min_guild_join_days * DAY_MS:
            return Eligibility(
                False,
                Reason.JOINED_TOO_RECENTLY,
                f"You must have been in this server for {giveaway.min_guild_join_days} day(s).",
            )

    if giveaway.max_entries_per_user and current_entries >= giveaway.max_entries_per_user:
        return Eligibility(
            False,
            Reason.MAX_ENTRIES_REACHED,
            f"You have already used all {giveaway.max_entries_per_user} entr"
            + ("y." if giveaway.max_entries_per_user == 1 else "ies."),
        )

    if giveaway.entry_limit and total_entries >= giveaway.entry_limit:
        return Eligibility(
            False,
            Reason.ENTRY_LIMIT_REACHED,
            "This giveaway has reached its entry limit.",
        )

    # Message activity requirement, evaluated last so a user is always told the
    # most actionable blocker first (and never "passes" the activity check by
    # default when the requirement is off).
    if giveaway.min_messages > 0:
        current = int(message_count)
        required = int(giveaway.min_messages)
        if current < required:
            return Eligibility(
                False,
                Reason.INSUFFICIENT_MESSAGES,
                (
                    f"You need **{required}** messages to enter, and you have "
                    f"sent **{current}**. Keep chatting and try again - "
                    f"**{required - current}** to go."
                ),
                progress=(current, required),
            )

    return ALLOWED


def revalidate_snapshot(
    giveaway: Giveaway, entry: dict[str, Any], *, is_member: bool, role_ids: Iterable[str], now: int
) -> Eligibility:
    """Re-check a stored entry against current guild state.

    Used by the "eligibility validation" pass that administrators can trigger
    from the dashboard: participants who lost a required role (or gained a
    blacklisted one) can be flagged so the next draw is honest.
    """
    return evaluate_join(
        giveaway,
        user_id=str(entry["user_id"]),
        role_ids=role_ids,
        account_created_at=entry.get("account_created_at"),
        guild_joined_at=entry.get("guild_joined_at"),
        is_member=is_member,
        channel_id=giveaway.channel_id,
        current_entries=0,
        now=now,
    )


def summarise_rules(giveaway: Giveaway, *, role_names: dict[str, str] | None = None) -> list[str]:
    """Human readable rule bullets for the embed / public page."""
    names = role_names or {}
    bullets: list[str] = []
    if giveaway.winner_count > 1:
        bullets.append(f"**{giveaway.winner_count}** winners will be drawn")
    else:
        bullets.append("**1** winner will be drawn")
    if giveaway.max_entries_per_user > 1:
        bullets.append(f"Up to **{giveaway.max_entries_per_user}** entries per person")
    else:
        bullets.append("**1** entry per person")
    if giveaway.entry_limit:
        bullets.append(f"Entry cap: **{giveaway.entry_limit}** total entries")
    if giveaway.required_role_ids:
        if giveaway.required_mode == "all":
            rendered = " + ".join(
                f"`{names.get(role, role)}`" for role in giveaway.required_role_ids
            )
            bullets.append(f"Requires **all** of: {rendered}")
        else:
            rendered = " or ".join(f"`{names.get(role, role)}`" for role in giveaway.required_role_ids)
            bullets.append(f"Requires one of: {rendered}")
    if giveaway.blacklist_role_ids:
        rendered = ", ".join(f"`{names.get(role, role)}`" for role in giveaway.blacklist_role_ids)
        bullets.append(f"Excluded roles: {rendered}")
    if giveaway.allowed_channel_ids:
        bullets.append(f"Only entries from **{len(giveaway.allowed_channel_ids)}** channel(s)")
    if giveaway.min_account_age_days:
        bullets.append(f"Account must be **{giveaway.min_account_age_days}** day(s) old")
    if giveaway.min_guild_join_days:
        bullets.append(f"Must have joined this server **{giveaway.min_guild_join_days}** day(s) ago")
    if giveaway.min_messages:
        scope = (
            f"{len(giveaway.message_count_channel_ids)} specific channel(s)"
            if giveaway.message_count_scope == "channel" and giveaway.message_count_channel_ids
            else "this server"
        )
        bullets.append(f"Requires **{giveaway.min_messages}** messages in {scope}")
    return bullets