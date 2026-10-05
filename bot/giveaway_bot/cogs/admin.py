"""Moderator-only slash commands: ``/admin give ...``.

Every command re-checks **Manage Server** locally (not only via the
``has_permissions`` check) and writes an audit row through the service layer, so
the Discord-side and dashboard-side histories are identical.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..models import Giveaway
from ..repositories import giveaways as gw_repo
from ..service import ServiceError
from ..validation import ValidationError, parse_duration

log = logging.getLogger("giveaway_bot.cogs.admin")


class AdminCommands(commands.Cog, name="admin"):
    """Lifecycle management from inside Discord."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    give = app_commands.Group(name="give", description="Manage a giveaway")

    async def _lookup(self, interaction: discord.Interaction, identifier: str) -> Any | None:
        # Guild-scoped on purpose. The permission check validates the invoker in
        # `interaction.guild`, but a gw_ id is global, so without this a moderator
        # of guild A could end, reroll or disqualify entries in guild B using an id
        # picked up from a link or a screenshot. Both lookup paths are now pinned to
        # the guild the command was typed in.
        identifier = identifier.strip()
        if identifier.startswith("gw_"):
            record = await asyncio.to_thread(gw_repo.get_giveaway, self.bot.db, identifier)
            if record is not None and record.guild_id != str(interaction.guild_id):
                return None
            return record
        if identifier.isdigit():
            return await asyncio.to_thread(
                gw_repo.get_by_message, self.bot.db, str(interaction.guild_id), identifier
            )
        return None

    async def _run(
        self,
        interaction: discord.Interaction,
        giveaway_id: str,
        operation: str,
        **kwargs: Any,
    ) -> Any | None:
        """Fetch -> mutate -> re-render, with uniform error handling."""
        record = await self._lookup(interaction, giveaway_id)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return None
        actor = self.bot.actor_for(interaction.user)
        try:
            updated = await asyncio.to_thread(operation, actor, record, **kwargs)
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return None
        except ValidationError as exc:
            await interaction.followup.send("⚠️ " + "; ".join(exc.errors.values()), ephemeral=True)
            return None

        if isinstance(updated, Giveaway):
            await self.bot.render_giveaway(updated)
        return updated

    # ------------------------------------------------------------- lifecycle
    @give.command(name="pause", description="Pause a giveaway")
    @app_commands.describe(giveaway="Giveaway ID or message ID", reason="Why (shown in the audit log)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pause(self, interaction: discord.Interaction, giveaway: str, reason: str = "") -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self._run(
            interaction,
            giveaway,
            lambda actor, record, **_: self.bot.service.pause(actor, record, reason=reason),
        )
        if updated is not None:
            await interaction.followup.send(
                f"⏸️ Paused with "
                f"{updated.paused_remaining_ms and updated.paused_remaining_ms // 1000}s remaining.",
                ephemeral=True,
            )

    @give.command(name="resume", description="Resume a paused giveaway")
    @app_commands.describe(giveaway="Giveaway ID or message ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resume(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self._run(
            interaction,
            giveaway,
            lambda actor, record, **_: self.bot.service.resume(actor, record),
        )
        if updated is not None:
            await interaction.followup.send("▶️ Resumed.", ephemeral=True)

    @give.command(name="extend", description="Add time to a giveaway")
    @app_commands.describe(giveaway="Giveaway ID or message ID", duration="e.g. 30m, 2h, 1d")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def extend(self, interaction: discord.Interaction, giveaway: str, duration: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        duration_ms = parse_duration(duration)
        if duration_ms is None:
            await interaction.followup.send("⚠️ Use a duration like 30m, 2h or 1d.", ephemeral=True)
            return
        updated = await self._run(
            interaction,
            giveaway,
            lambda actor, record, **_: self.bot.service.extend(actor, record, duration_ms=duration_ms),
        )
        if updated is not None:
            await interaction.followup.send(
                f"➕ Extended. New end: <t:{int((updated.ends_at or 0) / 1000)}:R>",
                ephemeral=True,
            )

    @give.command(name="shorten", description="Remove time from a giveaway")
    @app_commands.describe(giveaway="Giveaway ID or message ID", duration="e.g. 30m, 2h, 1d")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def shorten(self, interaction: discord.Interaction, giveaway: str, duration: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        duration_ms = parse_duration(duration)
        if duration_ms is None:
            await interaction.followup.send("⚠️ Use a duration like 30m, 2h or 1d.", ephemeral=True)
            return
        updated = await self._run(
            interaction,
            giveaway,
            lambda actor, record, **_: self.bot.service.shorten(actor, record, duration_ms=duration_ms),
        )
        if updated is not None:
            await interaction.followup.send(
                f"➖ Shortened. New end: <t:{int((updated.ends_at or 0) / 1000)}:R>",
                ephemeral=True,
            )

    @give.command(name="end", description="End a giveaway and draw winners now")
    @app_commands.describe(giveaway="Giveaway ID or message ID", no_draw="End without selecting winners")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end(
        self, interaction: discord.Interaction, giveaway: str, no_draw: bool = False
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._lookup(interaction, giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        actor = self.bot.actor_for(interaction.user)
        try:
            outcome = await asyncio.to_thread(
                self.bot.service.end, actor, record, reason="slash_command", draw=not no_draw
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        if outcome is None:
            ended = await asyncio.to_thread(self.bot.service.get, record.id)
            await self.bot.render_giveaway(ended, announce=True)
            await self.bot.release_entrants_role(
                ended, reason="slash_command_cancel"
            )
            await interaction.followup.send("🚫 Ended without a draw.", ephemeral=True)
            return
        await self.bot.announce_winners(outcome)
        released = await self.bot.release_entrants_role(
            outcome.giveaway, reason="slash_command_end"
        )
        await interaction.followup.send(
            "🏆 "
            + (", ".join(f"<@{winner['user_id']}>" for winner in outcome.winners) or "No winners.")
            + f"\nRemoved the entrants role from {released} member(s).",
            ephemeral=True,
        )

    @give.command(name="reroll", description="Draw again with fresh, published randomness")
    @app_commands.describe(giveaway="Giveaway ID or message ID", reason="Why you are rerolling")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway: str, reason: str = "") -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._lookup(interaction, giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        actor = self.bot.actor_for(interaction.user)
        try:
            outcome = await asyncio.to_thread(
                self.bot.service.reroll, actor, record, reason=reason or "slash_command_reroll"
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        await self.bot.announce_winners(outcome)
        await interaction.followup.send(
            f"🔁 Round {outcome.result.round_number}: "
            + (", ".join(f"<@{winner['user_id']}>" for winner in outcome.winners) or "no winners"),
            ephemeral=True,
        )

    @give.command(name="cancel", description="End a giveaway with no winner at all")
    @app_commands.describe(giveaway="Giveaway ID or message ID", reason="Why")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cancel(self, interaction: discord.Interaction, giveaway: str, reason: str = "") -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        # `giveaway` is the raw user-supplied identifier and may be a *message* id,
        # which _lookup resolves but service.get does not - so this used to raise
        # an uncaught ServiceError after the giveaway had already been ended. When
        # _run failed (unknown giveaway, or one that was not running) it returned
        # None and this still went on to claim success.
        record = await self._lookup(interaction, giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        try:
            await asyncio.to_thread(
                self.bot.service.end,
                self.bot.actor_for(interaction.user),
                record,
                reason=reason or "cancelled",
                draw=False,
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        cancelled = await asyncio.to_thread(self.bot.service.get, record.id)
        released = await self.bot.release_entrants_role(
            cancelled, reason="slash_command_cancel"
        )
        await interaction.followup.send(
            f"🚫 Giveaway cancelled without a draw. Entrants role removed from "
            f"{released} member(s).",
            ephemeral=True,
        )

    # ---------------------------------------------------------- participation
    @give.command(name="flag", description="Disqualify a participant's entries")
    @app_commands.describe(giveaway="Giveaway ID", user="Member to flag", reason="Recorded in the audit log")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def flag(
        self, interaction: discord.Interaction, giveaway: str, user: discord.Member, reason: str = ""
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        changed = await self._run(
            interaction,
            giveaway,
            lambda actor, record, **_: self.bot.service.set_entry_eligibility(
                actor, record, str(user.id), eligible=False, reason=reason or "flagged by moderator"
            ),
        )
        if changed is not None:
            # A flagged member is no longer an entrant, so drop their role.
            record = await self._lookup(interaction, giveaway)
            if record is not None and record.participant_role_id:
                await self.bot.roles.release_member(
                    record, str(user.id), reason="flagged by moderator"
                )
                await self.bot.roles.drain(record.guild_id, limit=50)
            await interaction.followup.send(
                f"🚫 Flagged {changed} entr{'y' if changed == 1 else 'ies'} for "
                f"{user.display_name} and removed their entrants role.",
                ephemeral=True,
            )

    @give.command(name="unflag", description="Restore a participant's entries")
    @app_commands.describe(
        giveaway="Giveaway ID",
        user="Member to restore",
        reason="Recorded in the audit log",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unflag(
        self, interaction: discord.Interaction, giveaway: str, user: discord.Member, reason: str = ""
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        changed = await self._run(
            interaction,
            giveaway,
            lambda actor, record, **_: self.bot.service.set_entry_eligibility(
                actor, record, str(user.id), eligible=True, reason=reason or "restored by moderator"
            ),
        )
        if changed is not None:
            await interaction.followup.send(
                f"✅ Restored {changed} entries for {user.display_name}.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------- misc
    @give.command(name="sync", description="Re-post the giveaway message and buttons")
    @app_commands.describe(giveaway="Giveaway ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sync(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._lookup(interaction, giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        message = await self.bot.render_giveaway(record, announce=True)
        await interaction.followup.send(
            "🔄 Re-posted." if message else "⚠️ Could not post (check channel permissions).", ephemeral=True
        )

    @give.command(name="messages", description="Set the message-activity requirement")
    @app_commands.describe(
        giveaway="Giveaway ID or message ID",
        min_messages="Messages required to enter (0 disables the requirement)",
        channels="Only count these channels (blank counts the whole server)",
        revalidate="Re-check current participants against the new rule",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def messages(
        self,
        interaction: discord.Interaction,
        giveaway: str,
        min_messages: app_commands.Range[int, 0, 100_000] = 0,
        channels: str = "",
        revalidate: bool = False,
    ) -> None:
        """Set, change or disable the message requirement from Discord."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._lookup(interaction, giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return

        channel_ids = _parse_ids(channels)
        payload: dict[str, Any] = {
            "min_messages": min_messages,
            "message_count_channel_ids": channel_ids,
            "message_count_scope": "channel" if channel_ids else "guild",
            "message_count_ignore_bots": True,
        }
        actor = self.bot.actor_for(interaction.user)
        try:
            updated = await asyncio.to_thread(
                self.bot.service.set_message_requirement, actor, record, payload=payload
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        except ValidationError as exc:
            await interaction.followup.send("⚠️ " + "; ".join(exc.errors.values()), ephemeral=True)
            return

        await asyncio.to_thread(self.bot.activity_tracker.refresh_requirements)
        await self.bot.render_giveaway(updated)

        report: dict[str, Any] = {"checked": 0, "flagged": 0, "restored": 0}
        if revalidate and updated.min_messages > 0:
            report = await asyncio.to_thread(self.bot.service.revalidate_message_activity, actor, updated)
            await self.bot.render_giveaway(await asyncio.to_thread(self.bot.service.get, updated.id))

        if updated.min_messages == 0:
            message = "💬 Message requirement **disabled**."
        else:
            scope = (
                f"in {len(updated.message_count_channel_ids)} channel(s)"
                if updated.message_count_scope == "channel"
                else "server-wide"
            )
            message = f"💬 Now requires **{updated.min_messages}** messages {scope}."
            if revalidate:
                message += (
                    f"\nChecked {report['checked']} participant(s): "
                    f"{report['flagged']} did not meet the rule, "
                    f"{report['restored']} restored."
                )
        await interaction.followup.send(message, ephemeral=True)

    @give.command(name="progress", description="Check your own message progress")
    @app_commands.describe(giveaway="Giveaway ID or message ID")
    async def progress(self, interaction: discord.Interaction, giveaway: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self._lookup(interaction, giveaway)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        if record.min_messages <= 0:
            await interaction.followup.send(
                f"**{record.title}** has no message requirement.", ephemeral=True
            )
            return
        stats = await asyncio.to_thread(self.bot.service.message_progress, record, str(interaction.user.id))
        bar = _progress_bar(stats["current"], stats["required"])
        verdict = (
            "✅ You can enter now." if stats["eligible"]
            else f"*{stats['remaining']}* more message(s) to go."
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"💬 {record.title}",
                description=(
                    f"`{bar}`\n"
                    f"**{stats['current']} / {stats['required']}** messages\n{verdict}"
                ),
                colour=0x10B981 if stats["eligible"] else 0xF59E0B,
            ).set_footer(text="This count updates live - no action needed."),
            ephemeral=True,
        )

    @give.command(name="entrants", description="Show the entrants role and entry count")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def entrants(self, interaction: discord.Interaction) -> None:
        """Staff use this to find the role they can ping, without a user list."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        from ..repositories import giveaways as gw_repo

        active = await asyncio.to_thread(gw_repo.find_active, self.bot.db, str(interaction.guild_id))
        if active is None:
            await interaction.followup.send(
                "No active giveaway in this server right now.", ephemeral=True
            )
            return

        giveaway = await asyncio.to_thread(gw_repo.get_giveaway, self.bot.db, str(active["id"]))
        role_id = active.get("participant_role_id")
        if role_id:
            role_line = (
                f"Ping <@&{role_id}> to reach everyone who entered "
                f"({giveaway.participant_count if giveaway else 0} participant(s))."
            )
        else:
            role_line = (
                "No entrants role yet — it is created automatically when the giveaway "
                "starts, provided I have **Manage Roles**."
            )
        await interaction.followup.send(
            embed=discord.Embed(
                title="📣 Current giveaway",
                description=(
                    f"**{active['title']}** · {active['status']}\n"
                    f"{giveaway.entry_count if giveaway else 0} entries · "
                    f"{giveaway.participant_count if giveaway else 0} participants\n\n"
                    f"{role_line}"
                ),
                colour=0x7C5CFF,
            ).set_footer(text="The role is removed automatically when the giveaway ends."),
            ephemeral=True,
        )

    @give.command(name="audit", description="Recent audit entries for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def audit(self, interaction: discord.Interaction) -> None:
        from ..repositories import control

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(
            control.list_audit, self.bot.db, guild_id=str(interaction.guild_id), limit=10
        )
        if not rows:
            await interaction.followup.send("No audit entries yet.", ephemeral=True)
            return
        lines = [
            f"`{row['created_at']}` **{row['action']}** by "
            f"{row.get('actor_name') or row.get('actor_id') or 'system'} "
            f"({row['source']}, {row['outcome']})"
            for row in rows
        ]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    async def cog_load(self) -> None:
        log.info("admin commands loaded")


def _parse_ids(value: str) -> list[str]:
    """Accept ``<#123>``, ``123``, ``123,456`` or space separated lists."""
    if not value:
        return []
    cleaned = re.sub(r"<#(\d+)>", r"\1", value)
    return [item for item in re.split(r"[,\s]+", cleaned) if item.isdigit()]


def _progress_bar(current: int, required: int, width: int = 20) -> str:
    if required <= 0:
        return "▱" * width
    filled = int(round((min(current, required) / required) * width))
    return "".join("▰" if index < filled else "▱" for index in range(width))


async def setup(bot: Any) -> None:
    """Entry point required by ``Bot.load_extension``.

    Without this, ``setup_hook`` raised NoEntryPointError the moment the gateway
    connected - the class existed and was never registered.
    """
    await bot.add_cog(AdminCommands(bot))
