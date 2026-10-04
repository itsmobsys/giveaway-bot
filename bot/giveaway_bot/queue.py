"""Execution of dashboard-originated commands.

The dashboard never calls Discord directly.  It appends a row to
``command_queue``; the bot claims it, re-validates **everything** (including
Discord-side reality such as "does that channel/role still exist?"), executes
the operation, re-renders the message and records the outcome.

This module is the second half of the trust boundary: even a fully compromised
dashboard token cannot execute anything outside this whitelist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from .models import CommandKind, GiveawayStatus
from .repositories import control
from .repositories import draws as draws_repo
from .repositories import giveaways as gw_repo
from .service import Actor, ServiceError
from .validation import ValidationError, validate_mutation_action

log = logging.getLogger("giveaway_bot.queue")


async def execute_command(bot: Any, command: Any) -> dict[str, Any]:
    """Run one command and return the JSON result stored on the queue row."""
    handler = _HANDLERS.get(command.kind)
    if handler is None:
        raise ServiceError("unknown_command", f"Unsupported command: {command.kind}")

    actor = Actor(command.requested_by, command.requested_by_name, command.source)
    try:
        return await handler(bot, command, actor)
    except ValidationError as exc:
        raise ServiceError("invalid_payload", "; ".join(exc.errors.values()), details=exc.errors) from exc
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("command %s (%s) crashed", command.kind, command.id)
        control.audit(
            bot.db,
            guild_id=command.guild_id,
            giveaway_id=command.giveaway_id,
            action=f"command.{command.kind}",
            actor_id=actor.user_id,
            actor_name=actor.username,
            source=command.source,
            outcome="error",
            metadata={"command_id": command.id, "error": str(exc)[:300]},
        )
        raise ServiceError("internal_error", "The bot failed to execute this command.") from exc


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _guild_or_fail(bot: Any, guild_id: str) -> Any:
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise ServiceError("bot_not_in_guild", "The bot is not in this server any more.")
    return guild


def _validate_roles_exist(bot: Any, guild: Any, role_ids: list[str]) -> None:
    missing = [
        role_id
        for role_id in role_ids
        if role_id not in {str(role.id) for role in guild.roles}
    ]
    if missing:
        raise ServiceError(
            "unknown_roles",
            "These roles no longer exist in this server: " + ", ".join(missing[:5]),
            details={"missing": missing},
        )


def _resolve_giveaway_channel(bot: Any, guild: Any) -> Any:
    """The one channel giveaways are posted in, resolved by the bot.

    The channel is deployment configuration (`DISCORD_GIVEAWAY_CHANNEL_ID` or the
    guild's dedicated giveaway channel), never something a dashboard payload can
    choose. A payload-supplied ``channel_id`` is ignored on purpose: otherwise a
    compromised dashboard could post giveaways anywhere it liked.
    """
    channel = None
    configured = getattr(bot.settings, "giveaway_channel_id", "")
    if configured:
        channel = bot.get_channel(int(configured))
        if channel is None:
            channel = guild.get_channel(int(configured))

    if channel is None:
        raise ServiceError(
            "channel_not_configured",
            "No giveaway channel is configured for this bot. Set "
            "**DISCORD_GIVEAWAY_CHANNEL_ID** to the ID of the channel giveaways "
            "should be posted in (right-click the channel → Copy ID).",
            details={"channel_id": configured},
        )

    if getattr(channel, "guild_id", None) not in (None, guild.id):
        raise ServiceError(
            "wrong_guild_channel",
            "The configured giveaway channel belongs to a different server.",
        )

    # Duck-typed rather than isinstance-checked: a DM channel has no
    # `permissions_for`, so this still rejects non-server channels while keeping
    # the rule unit-testable without constructing real Discord objects.
    me = guild.me
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        raise ServiceError(
            "wrong_channel_type",
            "DISCORD_GIVEAWAY_CHANNEL_ID must be a server text channel, not a DM.",
        )
    if permissions_for(me).send_messages is False:
        raise ServiceError(
            "missing_channel_permission",
            "I cannot post in that channel. Give me **View Channel** and "
            "**Send Messages** there.",
        )
    return channel


def _validate_channel(bot: Any, guild: Any, channel_id: str) -> Any:
    """Check a channel referenced by a *rule* (e.g. an entry channel filter)."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise ServiceError("unknown_channel", "That channel no longer exists.")
    return channel


async def _refresh(bot: Any, giveaway_id: str) -> None:
    """Re-render a giveaway message after a mutation."""
    giveaway = bot.service.get(giveaway_id)
    if not giveaway.message_id:
        return
    await bot.render_giveaway(giveaway, announce=giveaway.status is GiveawayStatus.ENDED)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def _handle_create(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    guild = _guild_or_fail(bot, command.guild_id)
    payload = dict(command.payload)

    # Single-giveaway-per-guild policy: this keeps the entrants role meaningful
    # and means "who is in the giveaway?" has exactly one answer.
    # No payload-supplied bypass. `force` was read straight off the dashboard
    # payload, so a compromised or buggy dashboard could start a second
    # concurrent giveaway in a guild - exactly what this invariant exists to
    # prevent, and what the temporary entrants role depends on. It was also never
    # audited.
    existing = gw_repo.find_active(bot.db, str(guild.id))
    if existing is not None:
        raise ServiceError(
            "giveaway_already_running",
            f"This server already has an active giveaway: **{existing['title']}** "
            f"({existing['status']}). End it before starting another.",
            details={"giveaway_id": existing["id"], "status": existing["status"]},
        )

    # The channel is bot configuration, never client input.
    if payload.pop("channel_id", None) is not None:
        log.info("ignoring client-supplied channel_id for giveaway.create")
    channel = _resolve_giveaway_channel(bot, guild)
    _validate_roles_exist(bot, guild, list(payload.get("required_role_ids") or []))
    _validate_roles_exist(bot, guild, list(payload.get("blacklist_role_ids") or []))

    giveaway = await asyncio.to_thread(
        bot.service.create, actor, guild_id=str(guild.id), channel_id=str(channel.id), payload=payload
    )
    # Attach the temporary entrants role before posting, so anyone who enters
    # from the very first second gets it.
    giveaway = await bot.attach_entrants_role(giveaway)
    await bot.render_giveaway(giveaway)
    fresh = bot.service.get(giveaway.id)
    return {
        "ok": True,
        "giveaway_id": fresh.id,
        "status": fresh.status.value,
        "message_id": fresh.message_id,
        "seed_commitment": fresh.seed_commitment,
        "participant_role_id": fresh.participant_role_id,
    }


async def _handle_update(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    guild = _guild_or_fail(bot, command.guild_id)
    giveaway = bot.service.get(str(command.giveaway_id))
    _validate_roles_exist(bot, guild, list(command.payload.get("required_role_ids") or []))
    _validate_roles_exist(bot, guild, list(command.payload.get("blacklist_role_ids") or []))

    # A giveaway cannot be moved to another channel; the channel is fixed by the
    # bot's configuration. Strip it so an update cannot change where it lives.
    payload = dict(command.payload)
    if payload.pop("channel_id", None) is not None:
        log.info("ignoring client-supplied channel_id for giveaway.update")

    updated = await asyncio.to_thread(bot.service.update, actor, giveaway, payload)
    await _refresh(bot, updated.id)
    return {"ok": True, "giveaway_id": updated.id, "version": updated.version}


async def _handle_pause(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    giveaway = bot.service.get(str(command.giveaway_id))
    updated = await asyncio.to_thread(
        bot.service.pause, actor, giveaway, reason=command.payload.get("reason")
    )
    await _refresh(bot, updated.id)
    return {"ok": True, "status": updated.status.value, "remaining_ms": updated.paused_remaining_ms}


async def _handle_resume(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    giveaway = bot.service.get(str(command.giveaway_id))
    updated = await asyncio.to_thread(bot.service.resume, actor, giveaway)
    await _refresh(bot, updated.id)
    return {"ok": True, "status": updated.status.value, "ends_at": updated.ends_at}


async def _handle_extend(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    args = validate_mutation_action(command.kind, command.payload)
    giveaway = bot.service.get(str(command.giveaway_id))
    updated = await asyncio.to_thread(
        bot.service.extend, actor, giveaway, duration_ms=args["duration_ms"]
    )
    await _refresh(bot, updated.id)
    return {"ok": True, "ends_at": updated.ends_at}


async def _handle_shorten(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    args = validate_mutation_action(command.kind, command.payload)
    giveaway = bot.service.get(str(command.giveaway_id))
    updated = await asyncio.to_thread(
        bot.service.shorten, actor, giveaway, duration_ms=args["duration_ms"]
    )
    await _refresh(bot, updated.id)
    return {"ok": True, "ends_at": updated.ends_at}


async def _handle_end(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    giveaway = bot.service.get(str(command.giveaway_id))
    reason = command.payload.get("reason") or "ended_from_dashboard"

    # If the giveaway carries a message-activity rule, re-check it immediately
    # before freezing entries so the draw reflects current, audited eligibility.
    activity_report: dict[str, Any] | None = None
    if giveaway.min_messages > 0 and command.payload.get("revalidate_activity", True):
        activity_report = await asyncio.to_thread(
            bot.service.revalidate_message_activity, actor, giveaway
        )
        giveaway = bot.service.get(giveaway.id)

    outcome = await asyncio.to_thread(bot.service.end, actor, giveaway, reason=reason, draw=True)
    if outcome is not None:
        await bot.announce_winners(outcome, activity_report=activity_report)
        # The giveaway is over: everyone loses the temporary entrants role.
        outcome.role_released = await bot.release_entrants_role(
            outcome.giveaway, reason="giveaway_ended"
        )
        return {
            "ok": True,
            "round": outcome.result.round_number,
            "winners": outcome.winners,
            "seed_commitment": outcome.result.commitment,
            "verification_ok": (outcome.verification or {}).get("ok"),
        }
    return {"ok": True, "drawn": False}


async def _handle_cancel(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    """End a giveaway *without* selecting any winner."""
    giveaway = bot.service.get(str(command.giveaway_id))
    reason = command.payload.get("reason") or "cancelled_from_dashboard"
    outcome = await asyncio.to_thread(bot.service.end, actor, giveaway, reason=reason, draw=False)
    if outcome is None:
        await bot.render_giveaway(bot.service.get(giveaway.id), announce=True)
    # A cancelled giveaway still releases the entrants role.
    released = await bot.release_entrants_role(
        bot.service.get(giveaway.id), reason="cancelled"
    )
    return {"ok": True, "drawn": False, "reason": reason, "role_released": released}


async def _handle_reroll(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    giveaway = bot.service.get(str(command.giveaway_id))
    reason = command.payload.get("reason") or "reroll_from_dashboard"
    outcome = await asyncio.to_thread(bot.service.reroll, actor, giveaway, reason=reason)
    await bot.announce_winners(outcome)
    return {
        "ok": True,
        "round": outcome.result.round_number,
        "winners": outcome.winners,
        "seed_commitment": outcome.result.commitment,
        "verification_ok": (outcome.verification or {}).get("ok"),
    }


async def _handle_reveal(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    """Post the revealed seed + verification into the Discord channel."""
    giveaway = bot.service.get(str(command.giveaway_id))
    if giveaway.seed_revealed_at is None:
        raise ServiceError("not_revealed", "This giveaway has not been drawn yet.")
    verification = await asyncio.to_thread(bot.service.verification_for, giveaway.id)
    await bot.post_verify(giveaway, verification)
    return {"ok": True, "seed": giveaway.server_seed, "verified": bool((verification or {}).get("ok"))}


async def _handle_announce(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    """Repost the last winner announcement (used after manual DMs etc.)."""
    giveaway = bot.service.get(str(command.giveaway_id))
    record = await asyncio.to_thread(draws_repo.latest_draw, bot.db, giveaway.id)
    if record is None:
        raise ServiceError("no_draw", "There is no draw to announce.")
    channel = bot.get_channel(int(giveaway.channel_id))
    if channel is None:
        raise ServiceError("unknown_channel", "The announcement channel is unavailable.")
    winners = [
        str(row["user_id"])
        for row in bot.db.query(
            "SELECT user_id FROM giveaway_winners WHERE draw_id = ? ORDER BY rank",
            (record.id,),
        )
    ]
    await channel.send(
        content=" ".join(f"<@{user_id}>" for user_id in winners) or "No winners this round.",
        allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(id=int(uid)) for uid in winners]
        ),
    )
    return {"ok": True, "mentions": len(winners)}


async def _handle_message_requirement(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    """Set or clear the per-giveaway message-activity requirement."""
    guild = _guild_or_fail(bot, command.guild_id)
    giveaway = bot.service.get(str(command.giveaway_id))

    channels = list(command.payload.get("message_count_channel_ids") or [])
    if channels:
        _validate_channel_ids_exist(bot, guild, channels)

    updated = await asyncio.to_thread(
        bot.service.set_message_requirement, actor, giveaway, payload=command.payload
    )
    # Recompute which guilds/channels need counting right away.
    bot.activity_tracker.refresh_requirements()
    await _refresh(bot, updated.id)
    return {
        "ok": True,
        "giveaway_id": updated.id,
        "enabled": updated.min_messages > 0,
        "min_messages": updated.min_messages,
        "scope": updated.message_count_scope,
        "channel_count": len(updated.message_count_channel_ids),
    }


async def _handle_activity_revalidate(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    """Re-check every participant against the current message requirement."""
    giveaway = bot.service.get(str(command.giveaway_id))
    report = await asyncio.to_thread(bot.service.revalidate_message_activity, actor, giveaway)
    await _refresh(bot, giveaway.id)
    return {"ok": True, **report}


def _validate_channel_ids_exist(bot: Any, guild: Any, channel_ids: list[str]) -> None:
    missing = [cid for cid in channel_ids if bot.get_channel(int(cid)) is None
               and guild.get_channel(int(cid)) is None]
    if missing:
        raise ServiceError(
            "unknown_channels",
            "These channels no longer exist: " + ", ".join(missing[:5]),
            details={"missing": missing},
        )


async def _handle_disqualify(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    giveaway = bot.service.get(str(command.giveaway_id))
    user_id = str(command.payload.get("user_id") or "")
    reason = str(command.payload.get("reason") or "flagged by a server administrator")
    if not user_id.isdigit():
        raise ServiceError("invalid_user", "Provide a valid Discord user ID.")
    changed = await asyncio.to_thread(
        bot.service.set_entry_eligibility, actor, giveaway, user_id, eligible=False, reason=reason
    )
    # A disqualified member no longer belongs with the entrants.
    if giveaway.participant_role_id and changed:
        try:
            await bot.roles.release_member(giveaway, user_id, reason="disqualified")
            await bot.roles.drain(giveaway.guild_id, limit=50)
        except Exception:  # noqa: BLE001 - never fail the command over a role
            log.exception("failed to release the entrants role for %s", user_id)
    await _refresh(bot, giveaway.id)
    return {"ok": True, "entries_changed": changed, "user_id": user_id}


async def _handle_restore(bot: Any, command: Any, actor: Actor) -> dict[str, Any]:
    giveaway = bot.service.get(str(command.giveaway_id))
    user_id = str(command.payload.get("user_id") or "")
    reason = str(command.payload.get("reason") or "restored by a server administrator")
    if not user_id.isdigit():
        raise ServiceError("invalid_user", "Provide a valid Discord user ID.")
    changed = await asyncio.to_thread(
        bot.service.set_entry_eligibility, actor, giveaway, user_id, eligible=True, reason=reason
    )
    await _refresh(bot, giveaway.id)
    return {"ok": True, "entries_changed": changed, "user_id": user_id}


_HANDLERS = {
    CommandKind.CREATE.value: _handle_create,
    CommandKind.UPDATE.value: _handle_update,
    CommandKind.PAUSE.value: _handle_pause,
    CommandKind.RESUME.value: _handle_resume,
    CommandKind.EXTEND.value: _handle_extend,
    CommandKind.SHORTEN.value: _handle_shorten,
    CommandKind.END.value: _handle_end,
    CommandKind.CANCEL.value: _handle_cancel,
    CommandKind.REROLL.value: _handle_reroll,
    CommandKind.REVEAL.value: _handle_reveal,
    CommandKind.ANNOUNCE.value: _handle_announce,
    CommandKind.DISQUALIFY.value: _handle_disqualify,
    CommandKind.RESTORE.value: _handle_restore,
    CommandKind.MESSAGE_REQUIREMENT.value: _handle_message_requirement,
    CommandKind.ACTIVITY_REVALIDATE.value: _handle_activity_revalidate,
}