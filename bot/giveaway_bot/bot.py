"""The Discord client.

Responsibilities kept deliberately narrow:

* own the Discord.py connection and the gateway event handlers,
* translate Discord objects into the plain data the service layer expects,
* render messages/embeds/buttons from service results,
* execute dashboard-originated commands from the shared queue.

No giveaway rules live here - if you find yourself adding an ``if`` about roles
or entry counts to this file, it belongs in :mod:`giveaway_bot.eligibility` or
:mod:`giveaway_bot.service`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import discord
from discord.ext import commands

from . import embeds, views
from .activity import MessageActivityTracker
from .config import Settings, is_admin_permissions
from .db import Database
from .healthcheck import POLL_SECONDS, DashboardHealth
from .models import Giveaway, GiveawayStatus
from .repositories import activity as activity_repo
from .repositories import control
from .repositories import entries as entries_repo
from .repositories import giveaways as gw_repo
from .repositories import guilds as guilds_repo
from .roles import RoleManager
from .scheduler import Scheduler
from .service import Actor, DrawOutcome, GiveawayService, ServiceError
from .views import GiveawayView, VerifyView, WinnerView

log = logging.getLogger("giveaway_bot.bot")

#: Command queue polling cadence. Small enough to feel instant on the dashboard.
QUEUE_POLL_SECONDS = 2.0


def build_intents() -> discord.Intents:
    """The gateway intents this bot requests.

    A single definition, used by the client and reported by ``doctor``, so what an
    operator is told cannot drift from what is actually requested.

    * ``members`` is privileged and must be enabled in the Developer Portal under
      Bot -> Privileged Gateway Intents. Eligibility reads a member's roles and
      their server join date, and Discord omits the member object from interaction
      payloads unless this intent is on, so those two rules cannot be evaluated
      without it. A connection that requests it while the portal has it off is
      refused outright with PrivilegedIntentsRequired.
    * ``message_content`` is deliberately left off. Message counting uses gateway
      events and never reads message text, so there is no reason to ask Discord for
      the content of every message in the server. discord.py logs "privileged
      message content intent is missing" regardless; that warning is expected and
      harmless for a slash-command-only bot.
    """
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = False
    return intents


class GiveawayBot(commands.Bot):
    """discord.py client with giveaway services attached."""

    def __init__(self, service: GiveawayService, db: Database, settings: Settings) -> None:
        super().__init__(
            command_prefix=settings.command_prefix,
            intents=build_intents(),
            help_command=None,
        )

        self.service = service
        self.db = db
        self.settings = settings
        self.scheduler = Scheduler()
        #: Pinged every 30s so a free-tier dashboard is not idled out, and so
        #: we notice when it goes away.
        self.dashboard_health = DashboardHealth(settings.dashboard_url)
        self._views: dict[str, GiveawayView | WinnerView] = {}
        self._ready = asyncio.Event()
        #: Message-activity counter (buffered + batch flushed).
        self.activity_tracker = MessageActivityTracker(db, service)
        #: Temporary "entrants" role manager.
        self.roles = RoleManager(self, db)

    # ------------------------------------------------------------------ setup
    async def setup_hook(self) -> None:
        await self.load_extension("giveaway_bot.cogs.giveaways")
        await self.load_extension("giveaway_bot.cogs.admin")
        await self.tree.sync()
        log.info("application commands synced (%d)", len(self.tree.get_commands()))

        self.scheduler.add("end_due", self._job_end_due, interval=5.0)
        self.scheduler.add("queue", self._job_process_queue, interval=QUEUE_POLL_SECONDS)
        self.scheduler.add("refresh", self._job_refresh_embeds, interval=self.settings.tick_interval_seconds)
        self.scheduler.add("maintenance", self._job_maintenance, interval=300.0, run_immediately=False)
        # Flush buffered message counts even when traffic is quiet.
        self.scheduler.add(
            "activity_flush", self._job_flush_activity, interval=5.0, run_immediately=False
        )
        self.scheduler.add(
            "activity_backfill", self._job_activity_backfill, interval=60.0, run_immediately=False
        )
        # Entrants-role grants/revokes are journalled; this retries them.
        self.scheduler.add("role_tasks", self._job_role_tasks, interval=15.0, run_immediately=False)
        self.scheduler.add(
            "dashboard_health",
            self._job_dashboard_health,
            interval=POLL_SECONDS,
            run_immediately=False,
        )
        await self.scheduler.start()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%d guilds)", self.user, len(self.guilds))
        for guild in self.guilds:
            self._sync_guild(guild)
        # Finish any draw that a crash interrupted.
        recovered = await asyncio.to_thread(self.service.recover_locked_draws)
        if recovered:
            log.warning("recovered %d interrupted draw(s)", recovered)
        # Re-attach buttons to live messages after a restart.
        await self.restore_views()
        # Repair any entrants-role grant/revoke interrupted by the restart.
        await self._reconcile_roles()
        await self.roles.drain_all(limit=200)
        self._ready.set()

    async def close(self) -> None:
        # Flush buffered message activity first. Up to MAX_PENDING_EVENTS per guild
        # was being dropped on every clean shutdown, and nothing marks those
        # messages for backfill - `activity_backfill_pending` is only set on a
        # gateway *resume* - so the counts stayed permanently low and members were
        # told they had not sent enough messages.
        try:
            await asyncio.to_thread(self.activity_tracker.flush_all)
        except Exception:  # noqa: BLE001 - shutdown must continue regardless
            log.exception("failed to flush message activity during shutdown")
        await self.scheduler.stop()
        await super().close()
        with contextlib.suppress(Exception):
            await self.dashboard_health.aclose()
        with contextlib.suppress(Exception):
            self.activity_tracker.shutdown()
        with contextlib.suppress(Exception):
            # SQLite on Windows holds the file until every connection is released.
            self.db.close_all()

    # ---------------------------------------------------------------- gateway
    async def on_raw_guild_role_delete(self, payload: discord.RawGuildRoleDeletePayload) -> None:
        """Forget the cached entrants role if staff delete it.

        Without this, a stale cache entry would make us keep referencing a role
        that no longer exists until the next restart.
        """
        self.roles.invalidate(str(payload.guild_id))
        log.info("guild %s: a role was deleted, cleared entrants-role cache", payload.guild_id)

    async def on_message(self, message: discord.Message) -> None:
        """Count message activity for the message-activity requirement.

        Cost control (the "thousands of users" concern):

        * ``on_message`` is not a bottleneck when counting is unnecessary, so we
          bail out in one dict lookup unless a *running* giveaway in this guild
          has a requirement enabled.
        * Bots, DMs, webhooks and empty messages never count.
        * Counters live in memory and are flushed in batches, so a busy channel
          does not produce one write per message.
        """
        self.activity_tracker.record(message)
        await self.process_commands(message)

    async def on_disconnect(self) -> None:
        log.warning("gateway disconnected - message counting will backfill on resume")

    async def on_resumed(self) -> None:
        # A resume can skip messages; flag the guild so the scheduler repairs the
        # gaps from the REST API instead of leaving counts quietly low.
        resumed_guilds = sorted(guild.id for guild in self.guilds)
        log.warning("gateway resumed for %d guild(s); scheduling activity backfill", len(resumed_guilds))
        control.set_state(self.db, "activity_backfill_pending", ",".join(map(str, resumed_guilds)))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not self.settings.guild_allowed(str(guild.id)):
            log.warning("ignoring guild %s (not in allowlist)", guild.id)
            return
        self._sync_guild(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        guilds_repo.touch_guild(self.db, str(guild.id), bot_present=False)
        log.info("left guild %s", guild.id)

    def _sync_guild(self, guild: discord.Guild) -> None:
        if not self.settings.guild_allowed(str(guild.id)):
            log.warning("guild %s is blocked by configuration", guild.id)
            return
        icon = None
        if guild.icon:
            icon = str(guild.icon.url)
        guilds_repo.upsert_guild(
            self.db,
            str(guild.id),
            name=guild.name,
            icon_url=icon,
            owner_id=str(guild.owner_id) if guild.owner_id else None,
            member_count=guild.member_count or 0,
            bot_present=True,
        )

    # ------------------------------------------------------------ permissions
    def can_manage(self, actor: discord.abc.GuildUser | None) -> bool:
        """Manage Server (or Administrator) - the only admin gate in the bot."""
        if actor is None:
            return False
        permissions = getattr(actor, "guild_permissions", None)
        if permissions is None:
            return False
        return is_admin_permissions(int(permissions.value))

    async def _assert_manage(self, interaction: discord.Interaction) -> bool:
        if self.can_manage(interaction.user):
            return True
        embed = discord.Embed(
            title="🚫 Missing permissions",
            description=(
                "You need **Manage Server** (or **Administrator**) to manage giveaways, "
                "in this server *and* on the dashboard."
            ),
            colour=0xEF4444,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    def actor_for(self, user: discord.abc.GuildUser, *, source: str = "discord") -> Actor:
        return Actor(user_id=str(user.id), username=str(user.display_name), source=source)

    # --------------------------------------------------------------- rendering
    def _text_channel(self, channel_id: str) -> Any | None:
        """Resolve a channel ID to something we can post in, or None.

        IDs are validated as snowflakes at input, but rows outlive validation:
        a corrupt row must not crash a scheduler tick, and get_channel can
        return a voice/stage/forum channel with no send/fetch_message.
        """
        try:
            channel = self.get_channel(int(channel_id))
        except (TypeError, ValueError):
            return None
        if channel is None:
            return None
        if not hasattr(channel, "send") or not hasattr(channel, "fetch_message"):
            return None
        return channel

    async def render_giveaway(
        self, giveaway: Giveaway, *, announce: bool = False
    ) -> discord.Message | None:
        """Create the live giveaway message, or refresh the existing one."""
        channel = self._text_channel(giveaway.channel_id)
        if channel is None:
            log.warning(
                "cannot render giveaway %s: channel %s unavailable",
                giveaway.id,
                giveaway.channel_id,
            )
            return None

        embed = self._embed_for(giveaway)
        view = self._giveaway_view(giveaway)

        if giveaway.message_id and not announce:
            try:
                message = await channel.fetch_message(int(giveaway.message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.info("giveaway %s: message fetch failed (%s), reposting", giveaway.id, exc)
                message = None
            if message is not None:
                try:
                    return await message.edit(embed=embed, view=view)
                except discord.HTTPException as exc:  # pragma: no cover - transient
                    log.warning("giveaway %s: edit failed (%s)", giveaway.id, exc)
                    return None

        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning("missing permissions to post in channel %s", giveaway.channel_id)
            return None
        except discord.HTTPException as exc:  # pragma: no cover - transient
            log.warning("failed to post giveaway %s: %s", giveaway.id, exc)
            return None

        gw_repo.set_message_id(self.db, giveaway.id, str(message.id))
        log.info("posted giveaway %s as message %s", giveaway.id, message.id)
        return message

    def _embed_for(self, giveaway: Giveaway) -> discord.Embed:
        return embeds.build_giveaway_embed(
            giveaway,
            role_names=self._role_names(giveaway),
            dashboard_url=self.settings.dashboard_url,
        )

    def _role_names(self, giveaway: Giveaway) -> dict[str, str]:
        names: dict[str, str] = {}
        try:
            guild = self.get_guild(int(giveaway.guild_id))
        except (TypeError, ValueError):
            return names
        if guild is None:
            return names
        role_ids = giveaway.required_role_ids + giveaway.blacklist_role_ids
        for role_id in role_ids:
            try:
                role = guild.get_role(int(role_id))
            except (TypeError, ValueError):
                continue
            if role is not None:
                names[role_id] = role.name
        return names

    def _giveaway_view(self, giveaway: Giveaway) -> GiveawayView:
        cached = self._views.get(giveaway.id)
        if isinstance(cached, GiveawayView):
            return cached
        view = GiveawayView(
            giveaway.id,
            on_join=self._on_join,
            on_leave=self._on_leave,
            on_reroll=self._on_reroll,
            dashboard_url=self.settings.dashboard_url,
            can_manage=True,  # the handler re-checks the clicker's permission
        )
        self._views[giveaway.id] = view
        self.add_view(view)
        return view

    async def announce_winners(
        self, outcome: DrawOutcome, *, activity_report: dict[str, Any] | None = None
    ) -> None:
        giveaway = outcome.giveaway
        channel = self._text_channel(giveaway.channel_id)
        if channel is None:
            log.warning("cannot announce winners: channel %s missing", giveaway.channel_id)
            return

        winners: list[tuple[str, str]] = []
        for winner in outcome.result.winners:
            member = channel.guild.get_member(int(winner.user_id))
            display = member.display_name if member else f"User {winner.user_id}"
            winners.append((winner.user_id, str(display)))

        previous = [record["user_id"] for record in self.db.query(
            "SELECT DISTINCT user_id FROM giveaway_winners WHERE giveaway_id = ? AND round < ?",
            (giveaway.id, outcome.result.round_number),
        )]
        embed = embeds.build_winner_embed(
            giveaway,
            winners,
            round_number=outcome.result.round_number,
            reroll=outcome.rerolled,
            previous_winner_ids=previous,
            activity_report=activity_report,
        )
        view = WinnerView(
            giveaway.id,
            on_reroll=self._on_reroll,
            dashboard_url=self.settings.dashboard_url,
            can_manage=True,
        )
        # Register it. A view only routes interactions once discord.py has it, and
        # the giveaway's own view is not registered for an *ended* giveaway, so
        # after any restart the Reroll button on the winner message - the only
        # place it appears - answered "This interaction failed".
        self._views[giveaway.id] = view
        self.add_view(view)
        try:
            if giveaway.message_id:
                try:
                    message = await channel.fetch_message(int(giveaway.message_id))
                except discord.HTTPException:
                    message = None
                if message is not None:
                    await message.edit(embed=embed, view=view)
                    if winners:
                        await message.reply(
                            content=" ".join(f"<@{user_id}>" for user_id, _ in winners),
                            allowed_mentions=discord.AllowedMentions(
                                users=[discord.Object(id=int(uid)) for uid, _ in winners]
                            ),
                        )
                    return
            await channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:  # pragma: no cover - network
            log.warning("failed to announce winners for %s: %s", giveaway.id, exc)

    async def restore_views(self) -> None:
        """Re-register button handlers for every live giveaway after a restart."""
        for giveaway in gw_repo.list_live(self.db, limit=400):
            self._giveaway_view(giveaway)
        log.info("restored %d live giveaway view(s)", len(self._views))

    # -------------------------------------------------------- button handlers
    async def _on_join(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        await self._handle_entry(interaction, giveaway_id, join=True)

    async def _on_leave(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        await self._handle_entry(interaction, giveaway_id, join=False)

    async def _handle_entry(self, interaction: discord.Interaction, giveaway_id: str, *, join: bool) -> None:
        user = interaction.user
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This button only works inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        giveaway = gw_repo.get_giveaway(self.db, giveaway_id)
        if giveaway is None:
            await interaction.followup.send("This giveaway no longer exists.", ephemeral=True)
            return

        channel = interaction.channel
        member = guild.get_member(user.id) or user
        context = views.member_context(member, guild)
        context["channel_id"] = str(channel.id) if channel else giveaway.channel_id

        if join:
            outcome = self.service.join(giveaway, context)
            if not outcome.joined:
                embed = views.eligibility_embed(outcome.eligibility)
                if outcome.duplicate:
                    embed = discord.Embed(
                        description="🎟️ You are already entered in this giveaway.",
                        colour=0xF59E0B,
                    )
                if embed is not None:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                await self._sync_live_embed(giveaway_id)
                return

            fresh = self.service.get(giveaway_id)
            view = self._giveaway_view(fresh)
            view.mark_entered(str(user.id), True)

            # Temporary entrants role. A failure here never blocks the entry.
            role_applied = False
            if fresh.participant_role_id:
                try:
                    role_applied = await self.roles.grant_for_entry(fresh, str(user.id))
                except Exception:  # noqa: BLE001
                    log.exception("failed to grant the entrants role to %s", user.id)

            await interaction.followup.send(
                embed=views.joined_embed(
                    fresh,
                    entry_seq=outcome.entry_seq or 1,
                    max_entries=fresh.max_entries_per_user,
                    role_granted=role_applied,
                ),
                ephemeral=True,
            )
        else:
            try:
                removed = self.service.leave(giveaway, str(user.id))
            except ServiceError as exc:
                await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
                return
            if not removed:
                await interaction.followup.send("You were not entered in this giveaway.", ephemeral=True)
                return
            fresh = self.service.get(giveaway_id)
            view = self._giveaway_view(fresh)
            view.mark_entered(str(user.id), False)
            # Leaving also releases the temporary role for this member.
            if fresh.participant_role_id:
                try:
                    await self.roles.release_member(fresh, str(user.id), reason="left")
                    await self.roles.drain(fresh.guild_id, limit=50)
                except Exception:  # noqa: BLE001
                    log.exception("failed to release the entrants role for %s", user.id)
            await interaction.followup.send(embed=views.left_embed(fresh), ephemeral=True)

        await self._sync_live_embed(giveaway_id)

    async def _on_reroll(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        """Reroll button - visible to everyone, executed only for Manage Server."""
        if not await self._assert_manage(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # service.reroll() takes a Giveaway and reads giveaway.is_locked. It used
        # to be handed the id string instead, so every click raised AttributeError
        # - which `except ServiceError` does not catch - leaving the interaction
        # deferred forever. The button never worked. The suite missed it because
        # test_lifecycle calls service.reroll directly, never through the button.
        record = await asyncio.to_thread(gw_repo.get_giveaway, self.db, giveaway_id)
        if record is None:
            await interaction.followup.send("⚠️ Giveaway not found.", ephemeral=True)
            return
        try:
            outcome = await asyncio.to_thread(
                self.service.reroll, self.actor_for(interaction.user), record
            )
        except ServiceError as exc:
            await interaction.followup.send(f"⚠️ {exc.message}", ephemeral=True)
            return
        await interaction.followup.send(
            f"🔁 Reroll complete — round {outcome.result.round_number}. "
            f"Winner(s): {', '.join('<@' + winner['user_id'] + '>' for winner in outcome.winners) or 'none'}",
            ephemeral=True,
        )
        await self.announce_winners(outcome)

    async def _sync_live_embed(self, giveaway_id: str) -> None:
        giveaway = gw_repo.get_giveaway(self.db, giveaway_id)
        if giveaway is None or not giveaway.message_id:
            return
        if giveaway.status is GiveawayStatus.ENDED:
            return
        await self.render_giveaway(giveaway)

    # --------------------------------------------------------------- scheduled
    async def _job_end_due(self) -> None:
        due = await asyncio.to_thread(gw_repo.list_due, self.db)
        for giveaway in due:
            try:
                actor = Actor("system", "scheduler", "scheduler")
                outcome = await asyncio.to_thread(
                    self.service.end, actor, giveaway, reason="timer", draw=True
                )
            except ServiceError as exc:
                log.warning("could not end giveaway %s: %s", giveaway.id, exc.message)
                control.audit(
                    self.db,
                    guild_id=giveaway.guild_id,
                    giveaway_id=giveaway.id,
                    action="giveaway.end_failed",
                    source="scheduler",
                    outcome="error",
                    metadata={"error": exc.code},
                )
                continue
            except Exception:  # noqa: BLE001
                log.exception("failed to end giveaway %s", giveaway.id)
                continue
            if outcome is not None:
                log.info("giveaway %s ended with %d winner(s)", giveaway.id, len(outcome.winners))
                # Announce first, then release the role: the announcement needs
                # the channel to still hold the entrants for context, and the
                # winner mention is explicit anyway.
                await self.announce_winners(outcome)
                await self.release_entrants_role(outcome.giveaway, reason="giveaway_ended")

    async def _job_refresh_embeds(self) -> None:
        live = await asyncio.to_thread(gw_repo.list_live, self.db)
        budget = self.settings.max_embed_refresh_per_tick
        if budget <= 0:
            return
        # Prioritise the giveaways closest to ending.
        live.sort(key=lambda item: item.ends_at or 2**62)
        for giveaway in live[:budget]:
            try:
                await self.render_giveaway(giveaway)
            except Exception:  # noqa: BLE001
                log.exception("failed to refresh embed for %s", giveaway.id)

    async def _job_process_queue(self) -> None:
        commands = await asyncio.to_thread(control.claim_batch, self.db, limit=self.settings.queue_workers)
        for command in commands:
            try:
                result = await self._execute_command(command)
                await asyncio.to_thread(control.complete_command, self.db, command.id, result)
            except ServiceError as exc:
                log.warning("command %s rejected: %s", command.kind, exc.message)
                await asyncio.to_thread(
                    control.fail_command, self.db, command.id, f"{exc.code}: {exc.message}", retryable=False
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("command %s failed", command.kind)
                await asyncio.to_thread(
                    control.fail_command, self.db, command.id, str(exc), retryable=True
                )

    async def _job_dashboard_health(self) -> None:
        await self.dashboard_health.poll()

    async def _job_maintenance(self) -> None:
        await asyncio.to_thread(self.service.housekeeping)
        self.activity_tracker.refresh_requirements()

    async def _job_flush_activity(self) -> None:
        if self.activity_tracker.pending():
            await asyncio.to_thread(self.activity_tracker.flush_all)

    # ------------------------------------------------------- entrants role
    async def attach_entrants_role(self, giveaway: Giveaway) -> Giveaway:
        """Ensure this giveaway has an entrants role and record it."""
        try:
            guild = self.get_guild(int(giveaway.guild_id))
        except (TypeError, ValueError):
            return giveaway
        if guild is None:
            return giveaway
        role = await self.roles.resolve_role(guild)
        if role is None:
            log.info(
                "guild %s: no entrants role available (missing Manage Roles?); "
                "giveaway %s will run without it",
                guild.id,
                giveaway.id,
            )
            return giveaway
        gw_repo.set_participant_role(self.db, giveaway.id, str(role.id))
        control.audit(
            self.db,
            guild_id=giveaway.guild_id,
            giveaway_id=giveaway.id,
            action="giveaway.entrants_role_attached",
            actor_id="system",
            source="bot",
            after={"participant_role_id": str(role.id), "role_name": role.name},
        )
        return self.service.get(giveaway.id)

    async def release_entrants_role(self, giveaway: Giveaway, *, reason: str) -> int:
        """Remove the temporary entrants role from everyone it was granted to.

        Idempotent and journalled, so calling it twice (or after a crash) is safe.
        """
        if not giveaway.participant_role_id:
            return 0
        queued = await self.roles.release_for_giveaway(giveaway)
        await self.roles.drain(giveaway.guild_id, limit=200)
        control.audit(
            self.db,
            guild_id=giveaway.guild_id,
            giveaway_id=giveaway.id,
            action="giveaway.entrants_role_released",
            actor_id="system",
            source="bot",
            after={"members_released": queued, "reason": reason},
        )
        log.info(
            "released the entrants role for %d member(s) after giveaway %s (%s)",
            queued,
            giveaway.id,
            reason,
        )
        return queued

    async def _job_role_tasks(self) -> None:
        """Retry any entrants-role grant/revoke that has not landed yet."""
        pending = self.db.scalar(
            "SELECT COUNT(*) FROM giveaway_role_tasks WHERE status = 'pending'"
        )
        if not pending:
            return
        applied = await self.roles.drain_all(limit=50)
        if applied:
            log.info("applied %d entrants-role task(s)", applied)

    async def _reconcile_roles(self) -> None:
        """Repair role state after a restart or a missed grant/revoke."""
        for giveaway in gw_repo.list_public(self.db, limit=200):
            if giveaway.status is GiveawayStatus.ENDED:
                continue
            try:
                await self.roles.reconcile(giveaway)
            except Exception:  # noqa: BLE001 - one guild must not block startup
                log.exception("role reconciliation failed for giveaway %s", giveaway.id)

    async def _job_activity_backfill(self) -> None:
        """Repair counter gaps after a gateway resume."""
        # These are synchronous database calls and this is an async method, so they
        # were blocking the gateway thread.
        pending = await asyncio.to_thread(control.get_state, self.db, "activity_backfill_pending")
        if not pending:
            return
        guild_ids = [part for part in pending.split(",") if part]
        for guild_id in guild_ids:
            for channel_id in activity_repo.watched_channels(self.db, guild_id)[:10]:
                try:
                    await self.activity_tracker.backfill_channel(self, guild_id, channel_id)
                except Exception:  # noqa: BLE001 - backfill must never crash the bot
                    log.exception("activity backfill failed for %s/%s", guild_id, channel_id)
        # Cleared only once the work is done. It used to be cleared *before* the
        # loop, so a crash part-way through lost the marker and the remaining
        # channels were never repaired - silently and permanently.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(control.set_state, self.db, "activity_backfill_pending", "")

    # ------------------------------------------------------- command execution
    async def _execute_command(self, command: Any) -> dict[str, Any]:
        """Execute one dashboard-originated command."""
        from .queue import execute_command  # local import: avoids an import cycle

        return await execute_command(self, command)

    # ------------------------------------------------------------- utilities
    async def post_verify(self, giveaway: Giveaway, verification: dict[str, Any] | None) -> None:
        channel = self._text_channel(giveaway.channel_id)
        if channel is None:
            return
        embed = embeds.build_verify_embed(giveaway, verification)
        await channel.send(
            embed=embed,
            view=VerifyView(giveaway.id, dashboard_url=self.settings.dashboard_url),
        )

    # ASYNC109: this is an internal await helper whose whole job is to bound
    # the wait, not a public request API.
    async def wait_ready(self, timeout: float = 60.0) -> None:  # noqa: ASYNC109
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:  # pragma: no cover - slow network
            log.warning("bot did not report ready within %.0fs", timeout)

    def participant_counts(self, giveaway_id: str, user_ids: list[str]) -> dict[str, int]:
        """Used by slash-command autocompletes and the participant summary."""
        return entries_repo.list_user_entry_status(self.db, giveaway_id, user_ids)


__all__ = ["GiveawayBot"]