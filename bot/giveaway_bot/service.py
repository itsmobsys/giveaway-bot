"""Business logic for giveaways.

Every mutating operation follows the same shape:

1. load the giveaway,
2. check the state machine and the caller's permissions (the caller is
   responsible for Discord-side permission checks; the service re-checks the
   *state* unconditionally),
3. perform the write **inside a single transaction** together with its audit
   row and its dashboard event,
4. return the fresh domain object.

The draw is deliberately split into two phases (``seal`` -> ``score`` ->
``reveal``) so the fairness contract in ``shared/FAIRNESS_SPEC.md`` is enforced
by structure rather than by convention.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, get_settings
from .db import Database, now_ms
from .eligibility import Eligibility, Reason, evaluate_join
from .fairness import (
    DrawEntry,
    DrawResult,
    commitment,
    draw_winners,
    generate_seed,
    verify_draw,
)
from .models import CommandKind, EntryStatus, Giveaway, GiveawayStatus
from .repositories import (
    activity as activity_repo,
)
from .repositories import (
    control,
)
from .repositories import (
    draws as draws_repo,
)
from .repositories import (
    entries as entries_repo,
)
from .repositories import (
    giveaways as gw_repo,
)
from .validation import (
    ValidationError,
    validate_giveaway_payload,
)

log = logging.getLogger("giveaway_bot.service")

MINUTE = 60_000


class ServiceError(Exception):
    """Domain error with a stable code, safe to show to the operator."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


@dataclass(slots=True)
class Actor:
    """Who is performing an action (Discord user id + where it came from)."""

    user_id: str
    username: str | None = None
    source: str = "discord"

    @property
    def is_system(self) -> bool:
        return self.user_id in {"system", "scheduler"}


@dataclass(slots=True)
class DrawOutcome:
    """Everything the presentation layer needs after a draw."""

    result: DrawResult
    draw_id: str
    giveaway: Giveaway
    winners: list[dict[str, Any]] = field(default_factory=list)
    reroll: bool = False
    verification: dict[str, Any] | None = None
    #: How many members the temporary entrants role was released from.
    role_released: int = 0


@dataclass(slots=True)
class JoinOutcome:
    joined: bool
    eligibility: Eligibility
    entry_seq: int | None = None
    duplicate: bool = False


class GiveawayService:
    """Stateless facade over the repositories (one instance per bot process)."""

    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ reads
    def get(self, giveaway_id: str) -> Giveaway:
        giveaway = gw_repo.get_giveaway(self.db, giveaway_id)
        if giveaway is None:
            raise ServiceError("not_found", "Giveaway not found.")
        return giveaway

    # ----------------------------------------------------------------- create
    def create(
        self, actor: Actor, *, guild_id: str, channel_id: str, payload: dict[str, Any]
    ) -> Giveaway:
        data = validate_giveaway_payload(payload, partial=False)
        giveaway_id = f"gw_{secrets.token_hex(9)}"
        timestamp = now_ms()

        with self.db.transaction() as tx:
            giveaway = gw_repo.create_giveaway(
                tx,
                giveaway_id=giveaway_id,
                guild_id=guild_id,
                channel_id=channel_id,
                title=data.title,
                description=data.description,
                prize=data.prize,
                created_by=actor.user_id,
                winner_count=data.winner_count,
                prize_count=data.prize_count,
                prize_image_url=data.prize_image_url,
                max_entries_per_user=data.max_entries_per_user,
                entry_limit=data.entry_limit,
                starts_at=timestamp,
                ends_at=data.ends_at,
                required_role_ids=data.required_role_ids,
                required_mode=data.required_mode,
                blacklist_role_ids=data.blacklist_role_ids,
                allowed_channel_ids=data.allowed_channel_ids,
                min_account_age_days=data.min_account_age_days,
                min_guild_join_days=data.min_guild_join_days,
                entrants_require_membership=data.entrants_require_membership,
                min_messages=data.min_messages,
                message_count_channel_ids=data.message_count_channel_ids,
                message_count_ignore_bots=data.message_count_ignore_bots,
                message_count_since=data.message_count_since,
                message_count_scope=data.message_count_scope,
            )
            control.audit(
                tx,
                guild_id=guild_id,
                giveaway_id=giveaway_id,
                action="giveaway.created",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                after=_giveaway_audit_view(giveaway),
            )
            control.emit(
                tx,
                guild_id=guild_id,
                giveaway_id=giveaway_id,
                event_type="giveaway.created",
                payload={"title": giveaway.title, "status": giveaway.status.value},
            )

        # A giveaway with no explicit start time starts immediately: seal the
        # seed before anybody can enter.
        self.start(actor, giveaway)
        return self.get(giveaway_id)

    # ------------------------------------------------------------------ start
    def start(self, actor: Actor, giveaway: Giveaway) -> Giveaway:
        """Move ``scheduled`` -> ``running``, publishing the seed commitment."""
        if giveaway.status is GiveawayStatus.RUNNING:
            return giveaway
        if giveaway.status is GiveawayStatus.ENDED:
            raise ServiceError("already_ended", "This giveaway has already ended.")
        if giveaway.status is GiveawayStatus.PAUSED:
            return self.resume(actor, giveaway)
        if giveaway.ends_at is None:
            raise ServiceError("no_end_time", "Set an end time before starting the giveaway.")

        seed = generate_seed()
        digest = commitment(seed)

        with self.db.transaction() as tx:
            gw_repo.seal_seed(tx, giveaway.id, seed, digest, giveaway.starts_at or now_ms())
            gw_repo.set_status(tx, giveaway.id, GiveawayStatus.RUNNING, actor_id=actor.user_id)
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.started",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                after={"status": "running", "seed_commitment": digest},
                metadata={"note": "commitment published before entries opened"},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.started",
                payload={"status": "running", "seed_commitment": digest},
            )
        return self.get(giveaway.id)

    # ----------------------------------------------------------------- update
    def update(
        self, actor: Actor, giveaway: Giveaway, payload: dict[str, Any]
    ) -> Giveaway:
        if giveaway.status is GiveawayStatus.ENDED:
            raise ServiceError("already_ended", "Ended giveaways cannot be edited.")
        if giveaway.locked_at is not None:
            raise ServiceError("locked", "A draw is in progress for this giveaway.")

        before = _giveaway_audit_view(giveaway)
        data = validate_giveaway_payload(payload, partial=True)
        columns = data.as_columns()
        if not columns:
            return giveaway

        with self.db.transaction() as tx:
            updated = gw_repo.update_fields(tx, giveaway.id, columns, actor_id=actor.user_id)
            if not updated:
                raise ServiceError("conflict", "Giveaway changed while you were editing it.")
            fresh = gw_repo.get_giveaway(tx, giveaway.id)
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.updated",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                before=before,
                after=_giveaway_audit_view(fresh) if fresh else None,
                metadata={"fields": sorted(columns)},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.updated",
                payload={"fields": sorted(columns)},
            )
        return self.get(giveaway.id)

    # ------------------------------------------------------------ pause/resume
    def pause(self, actor: Actor, giveaway: Giveaway, *, reason: str | None = None) -> Giveaway:
        if giveaway.status is not GiveawayStatus.RUNNING:
            raise ServiceError("not_running", "Only running giveaways can be paused.")
        if giveaway.locked_at is not None:
            raise ServiceError("locked", "A draw is in progress for this giveaway.")

        remaining = giveaway.remaining_ms()
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE giveaways
                SET status = ?, paused_at = ?, paused_remaining_ms = ?,
                    updated_by = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    GiveawayStatus.PAUSED.value,
                    now_ms(),
                    remaining,
                    actor.user_id,
                    now_ms(),
                    giveaway.id,
                    GiveawayStatus.RUNNING.value,
                ),
            )
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.paused",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                before={"status": "running", "ends_at": giveaway.ends_at},
                after={"status": "paused", "remaining_ms": remaining},
                metadata={"reason": reason} if reason else None,
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.paused",
                payload={"remaining_ms": remaining, "reason": reason},
            )
        return self.get(giveaway.id)

    def resume(self, actor: Actor, giveaway: Giveaway) -> Giveaway:
        if giveaway.status is not GiveawayStatus.PAUSED:
            raise ServiceError("not_paused", "Only paused giveaways can be resumed.")

        remaining = giveaway.paused_remaining_ms or 0
        ends_at = now_ms() + max(remaining, MINUTE)
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE giveaways
                SET status = ?, ends_at = ?, paused_at = NULL, paused_remaining_ms = NULL,
                    updated_by = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    GiveawayStatus.RUNNING.value,
                    ends_at,
                    actor.user_id,
                    now_ms(),
                    giveaway.id,
                    GiveawayStatus.PAUSED.value,
                ),
            )
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.resumed",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                before={"status": "paused"},
                after={"status": "running", "ends_at": ends_at},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.resumed",
                payload={"ends_at": ends_at},
            )
        return self.get(giveaway.id)

    # --------------------------------------------------------- extend / shorten
    def extend(self, actor: Actor, giveaway: Giveaway, *, duration_ms: int) -> Giveaway:
        if giveaway.status is GiveawayStatus.ENDED:
            raise ServiceError("already_ended", "Ended giveaways cannot be extended.")
        if giveaway.status is GiveawayStatus.PAUSED:
            new_remaining = (giveaway.paused_remaining_ms or 0) + duration_ms
            return self._set_remaining(
                actor, giveaway, new_remaining, action="giveaway.extended", extra={"duration_ms": duration_ms}
            )

        base = max(now_ms(), giveaway.ends_at or now_ms())
        new_end = base + duration_ms
        return self._set_end(actor, giveaway, new_end, "giveaway.extended", {"duration_ms": duration_ms})

    def shorten(self, actor: Actor, giveaway: Giveaway, *, duration_ms: int) -> Giveaway:
        if giveaway.status is GiveawayStatus.ENDED:
            raise ServiceError("already_ended", "Ended giveaways cannot be shortened.")
        if giveaway.status is GiveawayStatus.PAUSED:
            new_remaining = max(MINUTE, (giveaway.paused_remaining_ms or 0) - duration_ms)
            return self._set_remaining(
                actor,
                giveaway,
                new_remaining,
                action="giveaway.shortened",
                extra={"duration_ms": duration_ms},
            )

        current = giveaway.remaining_ms()
        new_remaining = max(MINUTE, current - duration_ms)
        new_end = now_ms() + new_remaining
        return self._set_end(
            actor,
            giveaway,
            new_end,
            "giveaway.shortened",
            {"duration_ms": duration_ms, "new_remaining_ms": new_remaining},
        )

    def _set_end(
        self, actor: Actor, giveaway: Giveaway, new_end: int, action: str, metadata: dict[str, Any]
    ) -> Giveaway:
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE giveaways
                SET ends_at = ?, original_ends_at = COALESCE(original_ends_at, ends_at),
                    updated_by = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (new_end, actor.user_id, now_ms(), giveaway.id),
            )
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action=action,
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                before={"ends_at": giveaway.ends_at},
                after={"ends_at": new_end},
                metadata=metadata,
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type=action.split(".", 1)[1],
                payload={"ends_at": new_end, **metadata},
            )
        return self.get(giveaway.id)

    def _set_remaining(
        self, actor: Actor, giveaway: Giveaway, remaining_ms: int, *, action: str, extra: dict[str, Any]
    ) -> Giveaway:
        with self.db.transaction() as tx:
            tx.execute(
                """
                UPDATE giveaways
                SET paused_remaining_ms = ?, updated_by = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (remaining_ms, actor.user_id, now_ms(), giveaway.id),
            )
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action=action,
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                before={"paused_remaining_ms": giveaway.paused_remaining_ms},
                after={"paused_remaining_ms": remaining_ms},
                metadata=extra,
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type=action.split(".", 1)[1],
                payload={"paused_remaining_ms": remaining_ms, **extra},
            )
        return self.get(giveaway.id)

    # ---------------------------------------------------------- join / leave
    def join(self, giveaway: Giveaway, member: dict[str, Any]) -> JoinOutcome:
        """Add one entry for a member, enforcing every rule exactly once."""
        user_id = str(member["user_id"])
        role_ids = [str(role) for role in member.get("role_ids", [])]
        channel_id = str(member.get("channel_id") or giveaway.channel_id)
        is_member = bool(member.get("is_member", True))

        current_entries = entries_repo.count_user_entries(
            self.db, giveaway.id, user_id, max_entries_per_user=giveaway.max_entries_per_user
        )
        totals = entries_repo.active_entry_totals(self.db, giveaway.id)

        # Live message count for this member. Only fetched when the giveaway
        # actually has the requirement, so the common case costs zero queries.
        message_count = 0
        if giveaway.min_messages > 0:
            message_count = self.message_activity_for(giveaway, user_id)

        # `total_entries` and `message_count` are passed explicitly so limits are
        # always evaluated against live data rather than stale counters.
        verdict = evaluate_join(
            giveaway,
            user_id=user_id,
            role_ids=role_ids,
            account_created_at=member.get("account_created_at"),
            guild_joined_at=member.get("guild_joined_at"),
            is_member=is_member,
            channel_id=channel_id,
            current_entries=current_entries,
            now=now_ms(),
            total_entries=totals[0],
            message_count=message_count,
        )
        if not verdict.ok:
            return JoinOutcome(joined=False, eligibility=verdict)

        # MAX(entry_seq) + 1 rather than count + 1: see entries.max_entry_seq.
        next_seq = (entries_repo.max_entry_seq(self.db, giveaway.id, user_id) or 0) + 1
        # The temporary entrants role is *intent*, not a side effect: the row
        # records that the role should be applied, and the Discord layer performs
        # (and retries) the actual grant. A failed grant therefore never loses
        # the entry, and reconciliation can repair it later.
        role_id = giveaway.participant_role_id
        try:
            with self.db.transaction() as tx:
                entry_id = entries_repo.add_entry(
                    tx,
                    giveaway.id,
                    user_id,
                    next_seq,
                    account_created_at=member.get("account_created_at"),
                    guild_joined_at=member.get("guild_joined_at"),
                    snapshot={
                        "role_ids": sorted(role_ids),
                        "channel_id": channel_id,
                        "is_member": is_member,
                        "eligibility": verdict.reason,
                    },
                    role_granted_at=now_ms() if role_id else None,
                    grant_source="bot" if role_id else "none",
                )
                if entry_id is None:
                    return JoinOutcome(
                        joined=False,
                        eligibility=Eligibility(True, Reason.OK, "You are already entered."),
                        duplicate=True,
                    )
                # Re-enforce both caps inside the transaction. The pre-checks above
                # read outside any transaction, so two concurrent joins could both
                # pass and over-fill. Raising rolls the insert back; the caller
                # sees the same refusal as the pre-check.
                inside = entries_repo.count_user_entries(
                    tx, giveaway.id, user_id,
                    max_entries_per_user=giveaway.max_entries_per_user,
                )
                if giveaway.max_entries_per_user and inside > giveaway.max_entries_per_user:
                    raise ServiceError(
                        "max_entries_reached",
                        f"You have already used all {giveaway.max_entries_per_user} entr"
                        + ("y." if giveaway.max_entries_per_user == 1 else "ies."),
                    )
                if giveaway.entry_limit:
                    total_inside = entries_repo.active_entry_totals(tx, giveaway.id)[0]
                    if total_inside > giveaway.entry_limit:
                        raise ServiceError(
                            "entry_limit_reached",
                            "This giveaway has reached its entry limit.",
                        )
                if role_id:
                    # Journal the grant. The Discord call happens in the bot layer and
                    # is retried from this queue, so neither a Discord failure nor a
                    # crash can silently lose the role for a member who entered.
                    control.role_task(
                        tx,
                        giveaway_id=giveaway.id,
                        user_id=user_id,
                        role_id=role_id,
                        action="add",
                        status="pending",
                    )

                gw_repo.refresh_stats(tx, giveaway.id)
                fresh_totals = entries_repo.active_entry_totals(tx, giveaway.id)
                control.audit(
                    tx,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    action="entry.joined",
                    actor_id=user_id,
                    actor_name=member.get("username"),
                    source="discord",
                    target_id=user_id,
                    after={"entry_seq": next_seq, "entry_id": entry_id},
                )
                control.emit(
                    tx,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    event_type="entry.joined",
                    payload={
                        "entry_count": fresh_totals[0],
                        "participant_count": fresh_totals[1],
                    },
                )
        except ServiceError as exc:
            # Only the in-transaction cap re-checks raise these; anything else
            # is a genuine error and propagates. The insert was rolled back,
            # so returning a refusal keeps join()'s contract (no exceptions
            # for eligibility outcomes).
            if exc.code == "max_entries_reached":
                return JoinOutcome(
                    joined=False,
                    eligibility=Eligibility(False, Reason.MAX_ENTRIES_REACHED, exc.message),
                )
            if exc.code == "entry_limit_reached":
                return JoinOutcome(
                    joined=False,
                    eligibility=Eligibility(False, Reason.ENTRY_LIMIT_REACHED, exc.message),
                )
            raise
        return JoinOutcome(joined=True, eligibility=verdict, entry_seq=next_seq)

    def leave(self, giveaway: Giveaway, user_id: str) -> bool:
        """Voluntary withdrawal. Refused once a draw is locked."""
        if giveaway.locked_at is not None:
            raise ServiceError("locked", "Entries are frozen for the draw.")
        if giveaway.status is not GiveawayStatus.RUNNING:
            raise ServiceError("not_running", "This giveaway is not accepting entries.")

        with self.db.transaction() as tx:
            removed = entries_repo.delete_entries(tx, giveaway.id, user_id)
            if not removed:
                return False
            gw_repo.refresh_stats(tx, giveaway.id)
            totals = entries_repo.active_entry_totals(tx, giveaway.id)
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="entry.left",
                actor_id=user_id,
                source="discord",
                target_id=user_id,
                metadata={"entries_removed": removed},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="entry.left",
                payload={"entry_count": totals[0], "participant_count": totals[1]},
            )
        return True

    # ------------------------------------------------------- message activity
    def message_activity_for(self, giveaway: Giveaway, user_id: str) -> int:
        """Messages from ``user_id`` that count toward this giveaway's rule.

        The stored counter is guild-wide, so a channel-scoped requirement is
        narrowed by the per-channel breakdown where one exists. We never
        *estimate upward*: if the breakdown is unavailable the guild total is
        used and the caller sees ``exactness`` so the UI can be honest about it.
        """
        if giveaway.min_messages <= 0:
            return 0

        guild_id = giveaway.guild_id
        if giveaway.message_count_scope == "channel" and giveaway.message_count_channel_ids:
            return activity_repo.counts_in_channels(
                self.db,
                guild_id=guild_id,
                user_id=user_id,
                channel_ids=giveaway.message_count_channel_ids,
                since=giveaway.message_count_since,
            )

        return activity_repo.count_since(
            self.db,
            guild_id=guild_id,
            user_id=user_id,
            since=giveaway.message_count_since,
        )

    def message_progress(self, giveaway: Giveaway, user_id: str) -> dict[str, Any]:
        """A user's status against the message requirement (for UI + API)."""
        if giveaway.min_messages <= 0:
            return {"required": 0, "current": 0, "eligible": True, "remaining": 0}
        current = self.message_activity_for(giveaway, user_id)
        return {
            "required": giveaway.min_messages,
            "current": current,
            "remaining": max(0, giveaway.min_messages - current),
            "eligible": current >= giveaway.min_messages,
            "scope": giveaway.message_count_scope,
            "channel_count": len(giveaway.message_count_channel_ids),
            "since": giveaway.message_count_since,
        }

    def record_message_activity(
        self,
        *,
        guild_id: str,
        user_id: str,
        channel_id: str,
        message_id: str,
        message_at: int,
        is_bot: bool = False,
        distinct_channel: bool = False,
    ) -> None:
        """Record one message against the guild-wide counter.

        Cheap by design: a single UPSERT, only for humans, only when at least one
        open giveaway in the guild could care (checked by the caller).
        """
        if is_bot:
            return
        activity_repo.record_message(
            self.db,
            guild_id=guild_id,
            user_id=user_id,
            channel_id=channel_id,
            message_id=message_id,
            message_at=message_at,
            distinct_channel=distinct_channel,
            ignore_bots=True,
        )

    def set_message_requirement(
        self, actor: Actor, giveaway: Giveaway, *, payload: dict[str, Any]
    ) -> Giveaway:
        """Change the message requirement. Fully audited, including disables.

        Lowering or removing the requirement never invalidates entries that were
        already accepted under the stricter rule: participants are judged by the
        rule in force when they entered, and any admin-driven removal is an
        explicit, logged action.
        """
        if giveaway.status is GiveawayStatus.ENDED:
            raise ServiceError("already_ended", "Ended giveaways cannot be edited.")
        if giveaway.locked_at is not None:
            raise ServiceError("locked", "A draw is in progress for this giveaway.")

        # On a partial update, unspecified fields inherit their current value so
        # that changing only min_messages does not silently clear a channel list.
        channels = payload.get("message_count_channel_ids")
        if channels is None:
            channels = giveaway.message_count_channel_ids
        scope = payload.get("message_count_scope") or giveaway.message_count_scope

        data = validate_giveaway_payload(
            {
                "min_messages": payload.get("min_messages", giveaway.min_messages),
                "message_count_channel_ids": list(channels),
                "message_count_scope": scope,
                "message_count_ignore_bots": payload.get(
                    "message_count_ignore_bots", giveaway.message_count_ignore_bots
                ),
            },
            partial=True,
        )

        columns: dict[str, Any] = {
            "min_messages": data.min_messages,
            "message_count_channel_ids": data.message_count_channel_ids,
            "message_count_scope": data.message_count_scope,
            "message_count_ignore_bots": int(data.message_count_ignore_bots),
        }
        if "message_count_since" in payload:
            columns["message_count_since"] = data.message_count_since

        with self.db.transaction() as tx:
            changed = gw_repo.update_fields(tx, giveaway.id, columns, actor_id=actor.user_id)
            if not changed:
                raise ServiceError("conflict", "Giveaway changed while you were editing it.")
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.message_requirement_changed",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                before={
                    "min_messages": giveaway.min_messages,
                    "message_count_scope": giveaway.message_count_scope,
                    "message_count_channel_ids": giveaway.message_count_channel_ids,
                    "message_count_ignore_bots": giveaway.message_count_ignore_bots,
                },
                after={
                    "min_messages": data.min_messages,
                    "message_count_scope": data.message_count_scope,
                    "message_count_channel_ids": data.message_count_channel_ids,
                    "message_count_ignore_bots": data.message_count_ignore_bots,
                },
                metadata={
                    "enabled": data.min_messages > 0,
                    "was_enabled": giveaway.min_messages > 0,
                    "changed": giveaway.min_messages != data.min_messages,
                },
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.message_requirement_changed",
                payload={
                    "min_messages": data.min_messages,
                    "scope": data.message_count_scope,
                    "enabled": data.min_messages > 0,
                },
            )
        return self.get(giveaway.id)

    def participants_needing_messages(self, giveaway: Giveaway, *, limit: int = 50) -> list[dict[str, Any]]:
        """Participants whose activity requirement is not (yet) satisfied.

        Read-only: it reports who *would* be blocked today so an admin can see
        the impact before doing anything.
        """
        if giveaway.min_messages <= 0:
            return []
        rows = entries_repo.list_participants(self.db, giveaway.id, limit=limit)
        required = giveaway.min_messages
        results: list[dict[str, Any]] = []
        user_ids = [str(row["user_id"]) for row in rows]
        counts = self._bulk_message_counts(giveaway, user_ids)
        for row in rows:
            user_id = str(row["user_id"])
            current = counts.get(user_id, 0)
            results.append(
                {
                    "user_id": user_id,
                    "entries": int(row.get("entries") or 0),
                    "current": current,
                    "required": required,
                    "remaining": max(0, required - current),
                    "satisfied": current >= required,
                }
            )
        results.sort(key=lambda item: (item["satisfied"], -item["current"]))
        return results

    def _bulk_message_counts(self, giveaway: Giveaway, user_ids: list[str]) -> dict[str, int]:
        """Counts for many users in a bounded number of queries."""
        if not user_ids:
            return {}
        if giveaway.message_count_scope == "channel" and giveaway.message_count_channel_ids:
            return activity_repo.counts_in_channels_bulk(
                self.db,
                guild_id=giveaway.guild_id,
                user_ids=user_ids,
                channel_ids=giveaway.message_count_channel_ids,
                since=giveaway.message_count_since,
            )
        return activity_repo.counts_since_bulk(
            self.db,
            guild_id=giveaway.guild_id,
            user_ids=user_ids,
            since=giveaway.message_count_since,
        )

    def revalidate_message_activity(self, actor: Actor, giveaway: Giveaway) -> dict[str, int]:
        """Re-check every participant against the current message requirement.

        This is the only way the requirement can *remove* someone, it is always
        explicit, and it records a reason - never a silent side effect of a draw.
        """
        if giveaway.min_messages <= 0:
            return {"checked": 0, "flagged": 0, "restored": 0}

        rows = entries_repo.list_participants(self.db, giveaway.id, limit=1000)
        user_ids = [str(row["user_id"]) for row in rows]
        counts = self._bulk_message_counts(giveaway, user_ids)
        checked = flagged = restored = 0
        reason = f"message activity revalidation (needs {giveaway.min_messages} messages)"

        with self.db.transaction() as tx:
            for user_id in user_ids:
                current = counts.get(user_id, 0)
                checked += 1
                if current >= giveaway.min_messages:
                    changed = entries_repo.set_status(
                        tx, giveaway.id, user_id, EntryStatus.VALID, actor_id=actor.user_id
                    )
                    restored += changed
                else:
                    changed = entries_repo.set_status(
                        tx,
                        giveaway.id,
                        user_id,
                        EntryStatus.DISQUALIFIED,
                        reason=reason,
                        actor_id=actor.user_id,
                    )
                    flagged += changed
            if checked:
                gw_repo.refresh_stats(tx, giveaway.id)
                control.audit(
                    tx,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    action="giveaway.message_activity_revalidated",
                    actor_id=actor.user_id,
                    actor_name=actor.username,
                    source=actor.source,
                    after={"checked": checked, "flagged": flagged, "restored": restored},
                    metadata={"required": giveaway.min_messages},
                )
                control.emit(
                    tx,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    event_type="giveaway.message_activity_revalidated",
                    payload={"checked": checked, "flagged": flagged, "restored": restored},
                )
        return {"checked": checked, "flagged": flagged, "restored": restored}

    # --------------------------------------------------------- admin entry ops
    def set_entry_eligibility(
        self, actor: Actor, giveaway: Giveaway, user_id: str, *, eligible: bool, reason: str
    ) -> int:
        """Flag or restore a participant's entries.

        When entries are frozen (a draw has run or is locked) this *flags*
        rather than deletes, and the flag applies to future rounds only - it is
        recorded with the reason so the published manifest always matches.
        """
        status = EntryStatus.VALID if eligible else EntryStatus.DISQUALIFIED
        action = "entry.restored" if eligible else "entry.disqualified"

        if giveaway.is_locked and self.settings.fairness_freeze_entries_on_draw and not eligible:
            log.info(
                "disqualification on locked giveaway %s recorded as a flag (entry %s)",
                giveaway.id,
                user_id,
            )

        with self.db.transaction() as tx:
            changed = entries_repo.set_status(
                tx, giveaway.id, user_id, status, reason=reason if not eligible else None,
                actor_id=actor.user_id, only_valid=True,
            )
            if changed:
                gw_repo.refresh_stats(tx, giveaway.id)
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action=action,
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                target_id=user_id,
                outcome="success" if changed else "noop",
                after={"status": status.value, "reason": reason, "entries_changed": changed},
                metadata={"giveaway_locked": giveaway.is_locked},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type=action,
                # Public SSE carries this payload unauthenticated: no user IDs
                # or reasons. The audit row above keeps the full detail for admins.
                payload={"entries_changed": changed},
            )
        return changed

    def sync_entry(
        self, giveaway: Giveaway, user_id: str, *, account_created_at: int | None, guild_joined_at: int | None
    ) -> int:
        """Refresh the stored eligibility snapshot for a re-entering user."""
        with self.db.transaction() as tx:
            cursor = tx.execute(
                "UPDATE giveaway_entries SET account_created_at = ?, guild_joined_at = ?,"
                " updated_at = ? WHERE giveaway_id = ? AND user_id = ?",
                (account_created_at, guild_joined_at, now_ms(), giveaway.id, user_id),
            )
            changed = int(getattr(cursor, "rowcount", 0) or 0)
            cursor.close()
        return changed

    # ----------------------------------------------------------------- draws
    def end(
        self, actor: Actor, giveaway: Giveaway, *, reason: str = "manual", draw: bool = True
    ) -> DrawOutcome | None:
        """End a giveaway and (by default) immediately draw winners."""
        if giveaway.status is GiveawayStatus.ENDED:
            raise ServiceError("already_ended", "This giveaway has already ended.")
        if giveaway.locked_at is not None:
            raise ServiceError("locked", "A draw is already in progress for this giveaway.")

        if not draw:
            with self.db.transaction() as tx:
                gw_repo.set_status(
                    tx, giveaway.id, GiveawayStatus.ENDED, actor_id=actor.user_id,
                    ended_reason="cancelled_no_draw",
                )
                control.audit(
                    tx,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    action="giveaway.ended_without_draw",
                    actor_id=actor.user_id,
                    actor_name=actor.username,
                    source=actor.source,
                    before={"status": giveaway.status.value},
                    after={"status": "ended", "ended_reason": "cancelled_no_draw"},
                    metadata={"reason": reason},
                )
                control.emit(
                    tx,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    event_type="giveaway.ended",
                    payload={"drawn": False, "reason": reason},
                )
            return None

        return self._run_draw(
            actor, giveaway, trigger_reason=reason, reroll=False, mark_ended=True
        )

    def reroll(self, actor: Actor, giveaway: Giveaway, *, reason: str = "manual_reroll") -> DrawOutcome:
        # A completed draw is unlocked (the reveal releases the lock so a later
        # reroll can lock again), so "is there a draw?" means total_draws >= 1,
        # not is_locked. Checking is_locked here refused every reroll.
        if giveaway.total_draws < 1:
            raise ServiceError("no_draw", "There is no draw to reroll.")
        if giveaway.status is not GiveawayStatus.ENDED:
            raise ServiceError("not_ended", "Only ended giveaways can be rerolled.")
        # total_draws counts draws, and the first one is not a reroll, so a limit
        # of N permits N rerolls: after the initial draw total_draws == 1, and
        # `1 > N` is false for any N >= 1. With N == 0 it is true immediately, so
        # rerolling is refused outright.
        if giveaway.total_draws > self.settings.max_rerolls_per_giveaway:
            raise ServiceError(
                "reroll_limit",
                "The reroll limit for this giveaway has been reached "
                f"({self.settings.max_rerolls_per_giveaway} allowed).",
            )
        return self._run_draw(
            actor, giveaway, trigger_reason=reason, reroll=True, mark_ended=True
        )

    def _run_draw(
        self,
        actor: Actor,
        giveaway: Giveaway,
        *,
        trigger_reason: str,
        reroll: bool,
        mark_ended: bool,
        expect_locked: bool = False,
    ) -> DrawOutcome:
        """Three-phase draw: lock -> score -> reveal.

        * **lock** - transaction 1 makes the giveaway unmodifiable and freezes
          the entry set. For a reroll this also mints and publishes a *new* seed
          and commitment, so the previous randomness cannot be replayed.
          The lock is conditional: a concurrent draw loses and is told a draw
          is already running, instead of drawing a duplicate round.
        * **score** - pure computation from the seed read back out of storage.
        * **reveal** - transaction 2 writes the draw, the winners and the
          revealed seed, and releases the lock so a later reroll can lock
          again. A crash anywhere leaves ``locked_at`` set, and
          :meth:`recover_locked_draws` finishes the job on restart.
        """
        # Crash recovery: the interrupted lock already holds this round, so there
        # is nothing to acquire and the round is current, not next.
        round_number = giveaway.draw_round if expect_locked else giveaway.draw_round + 1
        reseed = reroll or giveaway.server_seed is None
        seed = generate_seed() if reseed else giveaway.server_seed or generate_seed()
        started = time.perf_counter()

        # --- phase 1: lock -------------------------------------------------
        with self.db.transaction() as tx:
            frozen_rows = entries_repo.frozen_entries(tx, giveaway.id)
            if reseed:
                gw_repo.reseal_seed(tx, giveaway.id, seed, commitment(seed))
                seed, _ = gw_repo.read_sealed_seed(tx, giveaway.id)
            else:
                seed, _ = gw_repo.read_sealed_seed(tx, giveaway.id)

            if not expect_locked and not gw_repo.mark_locked(tx, giveaway.id, round_number):
                raise ServiceError(
                    "draw_in_progress",
                    "A draw for this giveaway is already running.",
                )
            if mark_ended:
                gw_repo.set_status(
                    tx, giveaway.id, GiveawayStatus.ENDED, actor_id=actor.user_id,
                    ended_reason=trigger_reason,
                )
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.draw_locked",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                after={"round": round_number, "eligible": len(frozen_rows), "reseeded": reseed},
                metadata={"trigger": trigger_reason},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.draw_locked",
                payload={"round": round_number, "eligible_count": len(frozen_rows)},
            )

        # --- phase 2: score (pure) ---------------------------------------
        entries = [
            DrawEntry(
                user_id=str(row["user_id"]),
                entry_seq=int(row["entry_seq"]),
                entry_id=int(row["id"]),
            )
            for row in frozen_rows
        ]
        result = draw_winners(
            giveaway.id,
            entries,
            giveaway.winner_count,
            seed_hex=seed,
            round_number=round_number,
        )

        # --- phase 3: reveal ------------------------------------------------
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        with self.db.transaction() as tx:
            draw_id = draws_repo.save_draw(
                tx,
                giveaway_id=giveaway.id,
                result=result,
                trigger_reason=trigger_reason,
                triggered_by=actor.user_id,
                eligible_count=len(frozen_rows),
                duration_ms=elapsed_ms,
            )
            gw_repo.refresh_stats(tx, giveaway.id)
            gw_repo.mark_unlocked(tx, giveaway.id)
            control.audit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="giveaway.rerolled" if reroll else "giveaway.drawn",
                actor_id=actor.user_id,
                actor_name=actor.username,
                source=actor.source,
                after={
                    "round": round_number,
                    "winners": [winner.user_id for winner in result.winners],
                    "seed_commitment": result.commitment,
                    "participant_digest": result.participant_digest,
                    "participant_count": result.participant_count,
                    "shortfall": result.shortfall,
                },
                metadata={"trigger": trigger_reason, "duration_ms": elapsed_ms},
            )
            control.emit(
                tx,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                event_type="giveaway.rerolled" if reroll else "giveaway.drawn",
                payload={
                    "round": round_number,
                    "winner_count": len(result.winners),
                    "participant_count": result.participant_count,
                },
            )

        record = draws_repo.get_draw(self.db, draw_id)
        verification = None
        if record is not None:
            verification = verify_draw(
                giveaway_id=giveaway.id,
                seed_hex=record.server_seed,
                expected_commitment=record.seed_commitment,
                expected_digest=record.participant_digest,
                manifest=record.manifest_json,
            )
            control.audit(
                self.db,
                guild_id=giveaway.guild_id,
                giveaway_id=giveaway.id,
                action="draw.verified",
                actor_id="system",
                source="bot",
                after={"round": round_number, "ok": verification["ok"]},
                metadata={"checks": len(verification["checks"])},
            )

        return DrawOutcome(
            result=result,
            draw_id=draw_id,
            giveaway=self.get(giveaway.id),
            winners=[winner.as_public() for winner in result.winners],
            reroll=reroll,
            verification=verification,
        )

    def recover_locked_draws(self) -> int:
        """Finish draws interrupted by a crash. Called on every bot startup."""
        rows = self.db.query(
            """
            SELECT g.id FROM giveaways g
            LEFT JOIN giveaway_draws d ON d.giveaway_id = g.id AND d.round = g.draw_round
            WHERE g.locked_at IS NOT NULL AND d.id IS NULL
            """
        )
        if not rows:
            return 0
        system = Actor("system", "scheduler", "bot")
        recovered = 0
        for row in rows:
            giveaway = gw_repo.get_giveaway(self.db, str(row["id"]))
            if giveaway is None:  # pragma: no cover - referential integrity
                continue
            try:
                log.warning("recovering interrupted draw for giveaway %s", giveaway.id)
                self._run_draw(
                    system, giveaway, trigger_reason="crash_recovery", reroll=False, mark_ended=False,
                    expect_locked=True,
                )
                recovered += 1
            except Exception:  # noqa: BLE001 - never block startup on one giveaway
                log.exception("failed to recover draw for %s", giveaway.id)
        return recovered

    # ------------------------------------------------------------ inspection
    def verification_for(self, giveaway_id: str) -> dict[str, Any] | None:
        record = draws_repo.latest_draw(self.db, giveaway_id)
        if record is None:
            return None
        return verify_draw(
            giveaway_id=giveaway_id,
            seed_hex=record.server_seed,
            expected_commitment=record.seed_commitment,
            expected_digest=record.participant_digest,
            manifest=record.manifest_json,
        )

    def public_snapshot(self, giveaway_id: str, *, reveal_seed: bool = True) -> dict[str, Any]:
        """Everything the public giveaway page needs - no private data."""
        giveaway = self.get(giveaway_id)
        records = draws_repo.list_draws(self.db, giveaway_id)
        winners = draws_repo.list_winners(self.db, giveaway_id)
        latest = records[0] if records else None

        latest_winners = [
            {
                "rank": winner.rank,
                "user_id": winner.user_id,
                "entry_seq": winner.entry_seq,
                "score": winner.score,
                "awarded_at": winner.awarded_at,
                "round": winner.round,
            }
            for winner in winners
            if latest is None or winner.round == latest.round
        ]

        return {
            "id": giveaway.id,
            "guild_id": giveaway.guild_id,
            "channel_id": giveaway.channel_id,
            "title": giveaway.title,
            "description": giveaway.description,
            "prize": giveaway.prize,
            "prize_image_url": giveaway.prize_image_url,
            "prize_count": giveaway.prize_count,
            "winner_count": giveaway.winner_count,
            "status": giveaway.status.value,
            "ended_reason": giveaway.ended_reason,
            "starts_at": giveaway.starts_at,
            "ends_at": giveaway.ends_at,
            "created_at": giveaway.created_at,
            "participant_count": giveaway.participant_count,
            "entry_count": giveaway.entry_count,
            "rules": giveaway.rules_public(),
            "message_requirement": {
                # Public-safe: the requirement itself is a published rule.
                "enabled": giveaway.min_messages > 0,
                "min_messages": giveaway.min_messages,
                "scope": giveaway.message_count_scope,
                "channel_count": len(giveaway.message_count_channel_ids),
                "ignore_bots": giveaway.message_count_ignore_bots,
                "since": giveaway.message_count_since,
            },
            "fairness": {
                "algorithm": f"hmac-sha256-commit-reveal/{records[0].algorithm_version if records else 'v1'}",
                "seed_commitment": giveaway.seed_commitment,
                "seed": (latest.server_seed if latest and reveal_seed else None),
                "revealed": giveaway.seed_revealed_at is not None,
                "participant_digest": latest.participant_digest if latest else None,
                "total_draws": giveaway.total_draws,
                "locked": giveaway.is_locked,
            },
            "latest_draw": (
                {
                    "id": latest.id,
                    "round": latest.round,
                    "participant_count": latest.participant_count,
                    "eligible_count": latest.eligible_count,
                    "winner_count": latest.winner_count,
                    "seed": latest.server_seed,
                    "seed_commitment": latest.seed_commitment,
                    "participant_digest": latest.participant_digest,
                    "trigger_reason": latest.trigger_reason,
                    "created_at": latest.created_at,
                }
                if latest
                else None
            ),
            "winners": latest_winners,
            "history": [
                {
                    "id": record.id,
                    "round": record.round,
                    "participant_count": record.participant_count,
                    "winner_count": record.winner_count,
                    "seed_commitment": record.seed_commitment,
                    "participant_digest": record.participant_digest,
                    "trigger_reason": record.trigger_reason,
                    "created_at": record.created_at,
                }
                for record in records
            ],
        }

    def manifest_for(self, draw_id: str) -> dict[str, Any] | None:
        record = draws_repo.get_draw(self.db, draw_id)
        if record is None:
            return None
        return {
            "draw_id": record.id,
            "giveaway_id": record.giveaway_id,
            "round": record.round,
            "algorithm": record.method,
            "algorithm_version": record.algorithm_version,
            "manifest": draws_repo.manifest_payload(record),
            "verification": verify_draw(
                giveaway_id=record.giveaway_id,
                seed_hex=record.server_seed,
                expected_commitment=record.seed_commitment,
                expected_digest=record.participant_digest,
                manifest=record.manifest_json,
            ),
        }

    # ---------------------------------------------------------- housekeeping
    def housekeeping(self) -> dict[str, int]:
        """Periodic cleanup: expired OAuth states, rate limit rows, old events."""
        removed = {}
        control.prune_oauth_states(self.db)
        control.prune_rate_limits(self.db)
        removed["events"] = control.prune_events(self.db, older_than_ms=now_ms() - 7 * 86_400_000)
        # Reclaim queue rows abandoned by a cancelled or crashed worker.
        removed["requeued_commands"] = control.requeue_expired_claims(self.db)
        return removed

    def analytics(self, guild_id: str) -> dict[str, Any]:
        """Aggregated, non-identifying metrics for the dashboard."""
        totals = self.db.query_one(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)  AS running,
              SUM(CASE WHEN status = 'scheduled' THEN 1 ELSE 0 END) AS scheduled,
              SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END)    AS paused,
              SUM(CASE WHEN status = 'ended' THEN 1 ELSE 0 END)    AS ended
            FROM giveaways WHERE guild_id = ?
            """,
            (guild_id,),
        ) or {}
        participation = self.db.query_one(
            """
            SELECT COALESCE(SUM(s.entry_count), 0)      AS entries,
                   COALESCE(SUM(s.participant_count), 0) AS participants,
                   COALESCE(SUM(s.winner_count), 0)     AS winners
            FROM giveaway_stats s
            JOIN giveaways g ON g.id = s.giveaway_id
            WHERE g.guild_id = ?
            """,
            (guild_id,),
        ) or {}
        per_giveaway = self.db.query(
            """
            SELECT g.id, g.title, g.status, g.ends_at, g.winner_count,
                   COALESCE(s.entry_count, 0) AS entry_count,
                   COALESCE(s.participant_count, 0) AS participant_count,
                   COALESCE(s.winner_count, 0) AS winner_count
            FROM giveaways g
            LEFT JOIN giveaway_stats s ON s.giveaway_id = g.id
            WHERE g.guild_id = ?
            ORDER BY g.created_at DESC
            LIMIT 12
            """,
            (guild_id,),
        )
        return {
            "giveaways": {key: int(value or 0) for key, value in totals.items()},
            "participation": {key: int(value or 0) for key, value in participation.items()},
            "recent": per_giveaway,
        }


def _giveaway_audit_view(giveaway: Giveaway) -> dict[str, Any]:
    """Audit projection: rule-relevant fields only, never seeds."""
    return {
        "id": giveaway.id,
        "status": giveaway.status.value,
        "title": giveaway.title,
        "prize": giveaway.prize,
        "winner_count": giveaway.winner_count,
        "prize_count": giveaway.prize_count,
        "max_entries_per_user": giveaway.max_entries_per_user,
        "entry_limit": giveaway.entry_limit,
        "ends_at": giveaway.ends_at,
        "required_role_ids": giveaway.required_role_ids,
        "required_mode": giveaway.required_mode,
        "blacklist_role_ids": giveaway.blacklist_role_ids,
        "allowed_channel_ids": giveaway.allowed_channel_ids,
        "min_account_age_days": giveaway.min_account_age_days,
        "min_guild_join_days": giveaway.min_guild_join_days,
        "min_messages": giveaway.min_messages,
        "message_count_scope": giveaway.message_count_scope,
        "message_count_channel_ids": giveaway.message_count_channel_ids,
        "message_count_ignore_bots": giveaway.message_count_ignore_bots,
        "message_count_since": giveaway.message_count_since,
        "version": giveaway.version,
    }


__all__ = [
    "Actor",
    "DrawOutcome",
    "GiveawayService",
    "JoinOutcome",
    "ServiceError",
    "ValidationError",
    "CommandKind",
]
