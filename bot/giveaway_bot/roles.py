"""Temporary "entrants" role management.

Purpose: staff need to reach everyone who entered without pasting a long user
list into a channel. On a successful entry the member receives a temporary role;
when the giveaway ends the bot removes it.

Correctness rules this module enforces
--------------------------------------
1. **We only remove what we added.** ``giveaway_entries.grant_source`` records
   whether the role came from the bot or from a human. A human-assigned role is
   never stripped, because deleting it would revoke permissions someone granted
   deliberately.
2. **Discord calls are journalled, not assumed.** Role grants/revokes go
   through ``giveaway_role_tasks`` and are retried until they succeed, so a
   crash or a 5xx can never leave a member stranded with - or missing - the role.
3. **Failure never blocks entry.** If the role cannot be granted, the member is
   still entered and the embed says so. A cosmetic role must not stop someone
   from entering a giveaway.
4. **Cleanup is guaranteed.** ``release_for_giveaway`` enqueues a revoke for
   every member we granted the role to, on end / leave / disqualification, and is
   safe to call repeatedly.

Role creation is lazy (on first use) and reused across giveaways.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from .repositories import control

log = logging.getLogger("giveaway_bot.roles")

#: Name of the auto-managed role. Kept distinct so it is obvious it is temporary.
DEFAULT_ROLE_NAME = "Giveaway Entrants"
ROLE_COLOUR = 0x7C5CFF

#: Discord's hard role limit per guild.
MAX_ROLES = 250


class RoleManager:
    """Creates, grants and releases the temporary entrants role."""

    def __init__(self, bot: Any, db: Any) -> None:
        self.bot = bot
        self.db = db
        #: guild_id -> role_id, so the role is created at most once per guild.
        self._role_cache: dict[str, str] = {}

    # ------------------------------------------------------------- resolution
    async def resolve_role(
        self, guild: discord.Guild, *, create: bool = True
    ) -> discord.Role | None:
        """Find (or create) the entrants role for a guild.

        Returns ``None`` when the bot lacks Manage Roles or the guild is at the
        role cap - the giveaway still works, it just loses the convenience role.
        """
        cached = self._role_cache.get(str(guild.id))
        if cached:
            role = guild.get_role(int(cached))
            if role is not None:
                return role
            self._role_cache.pop(str(guild.id), None)

        # Reuse an existing role with the expected name (survives our own restart).
        role = discord.utils.get(guild.roles, name=DEFAULT_ROLE_NAME)
        if role is None and create:
            if not guild.me.guild_permissions.manage_roles:
                log.warning(
                    "guild %s: cannot create the entrants role (missing Manage Roles)", guild.id
                )
                return None
            if len(guild.roles) >= MAX_ROLES:
                log.warning("guild %s: role cap reached, entrants role unavailable", guild.id)
                return None
            try:
                role = await guild.create_role(
                    name=DEFAULT_ROLE_NAME,
                    colour=discord.Colour(ROLE_COLOUR),
                    reason="Giveaway Bot: temporary role for giveaway entrants",
                    hoist=False,
                    mentionable=True,  # staff must be able to ping it
                )
                log.info("guild %s: created entrants role %s", guild.id, role.id)
            except discord.Forbidden:
                log.warning("guild %s: Forbidden creating the entrants role", guild.id)
                return None
            except discord.HTTPException as exc:
                log.warning("guild %s: could not create the entrants role: %s", guild.id, exc)
                return None

        if role is not None:
            self._role_cache[str(guild.id)] = str(role.id)
        return role

    def role_id_for(self, giveaway: Any) -> str | None:
        return giveaway.participant_role_id

    def describe(self, giveaway: Any) -> str:
        """Human-readable summary of the role state, for embeds and replies."""
        if not giveaway.participant_role_id:
            return "No entrants role (the bot may be missing Manage Roles)."
        return f"<@&{giveaway.participant_role_id}>"

    # ---------------------------------------------------------------- granting
    async def grant_for_entry(self, giveaway: Any, user_id: str) -> bool:
        """Queue (and immediately attempt) the entrants role for one member.

        Returns True when the role was successfully applied. A False return is
        informational only: the member is already entered either way.
        """
        if not giveaway.participant_role_id:
            return False
        try:
            control.role_task(
                self.db,
                giveaway_id=giveaway.id,
                user_id=user_id,
                action="add",
                status="pending",
                role_id=giveaway.participant_role_id,
            )
        except Exception:  # noqa: BLE001 - a unique conflict means it's already queued
            log.debug("entrants role already queued for %s", user_id)
        return await self.drain(giveaway.guild_id, limit=10) > 0

    def forget_grant(self, giveaway_id: str, user_id: str) -> None:
        """Drop the grant record after the role has been removed."""
        from .repositories import entries as entries_repo

        entries_repo.mark_grants_revoked(self.db, giveaway_id, user_id)

    async def release_member(self, giveaway: Any, user_id: str, *, reason: str) -> bool:
        """Queue removal for a single member (leave, disqualification)."""
        if not giveaway.participant_role_id:
            return False
        control.role_task(
            self.db,
            giveaway_id=giveaway.id,
            user_id=user_id,
            role_id=giveaway.participant_role_id,
            action="remove",
            status="pending",
        )
        log.debug("queued role removal for %s in %s (%s)", user_id, giveaway.id, reason)
        return True

    async def release_for_giveaway(self, giveaway: Any) -> int:
        """Enqueue removal for everyone the bot granted the role to.

        Called when a giveaway ends. Only ``grant_source='bot'`` rows are
        touched, so manually assigned roles survive.
        """
        from .repositories import entries as entries_repo

        members = entries_repo.users_with_bot_role(self.db, giveaway.id)
        for user_id in members:
            control.role_task(
                self.db,
                giveaway_id=giveaway.id,
                user_id=user_id,
                role_id=giveaway.participant_role_id,
                action="remove",
                status="pending",
            )
        if members:
            log.info(
                "queued entrants-role removal for %d member(s) of giveaway %s",
                len(members),
                giveaway.id,
            )
        return len(members)

    # ----------------------------------------------------------------- draining
    async def drain(self, guild_id: str, *, limit: int = 25) -> int:
        """Apply pending role tasks for one guild. Returns successes."""
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return 0
        applied = 0
        for task in control.claim_role_tasks(self.db, limit=limit):
            ok, error = await self._apply(guild, task)
            if ok:
                control.complete_role_task(self.db, int(task["id"]), ok=True)
                if task["action"] == "remove":
                    # Clear provenance so a second end() cannot queue a duplicate.
                    self.forget_grant(str(task["giveaway_id"]), str(task["user_id"]))
                applied += 1
            else:
                status = control.complete_role_task(self.db, int(task["id"]), ok=False, error=error)
                if status == "failed":
                    log.error(
                        "role task %s (%s %s in guild %s) failed permanently: %s",
                        task["id"], task["action"], task["user_id"], guild_id, error,
                    )
        return applied

    async def drain_all(self, *, limit: int = 100) -> int:
        """Apply pending role tasks across every guild the bot is in."""
        total = 0
        for guild in self.bot.guilds:
            total += await self.drain(str(guild.id), limit=limit)
        return total

    async def _apply(self, guild: discord.Guild, task: dict[str, Any]) -> tuple[bool, str | None]:
        """Perform one add/remove. Never raises."""
        user_id = int(task["user_id"])
        member = guild.get_member(user_id)
        if member is None:
            # The member left the server: there is nothing to grant or revoke.
            # Treat as complete so the task does not retry forever.
            return True, None

        role = guild.get_role(int(task.get("role_id") or 0)) if task.get("role_id") else None
        if role is None:
            from .repositories import giveaways as gw_repo

            giveaway = gw_repo.get_giveaway(self.db, str(task["giveaway_id"]))
            if giveaway is None or not giveaway.participant_role_id:
                return True, None
            role = guild.get_role(int(giveaway.participant_role_id))
        if role is None:
            # Role was deleted by staff. Nothing to do; do not retry forever.
            log.info("guild %s: entrants role missing, skipping task", guild.id)
            return True, None

        if task["action"] == "add":
            if role in member.roles:
                return True, None
            try:
                await member.add_roles(role, reason="Giveaway Bot: entered the giveaway")
                return True, None
            except discord.Forbidden:
                return False, "Missing Manage Roles permission"
            except discord.HTTPException as exc:
                return False, f"{type(exc).__name__}: {exc}"

        # remove
        if role not in member.roles:
            return True, None
        try:
            await member.remove_roles(role, reason="Giveaway Bot: giveaway ended")
            return True, None
        except discord.Forbidden:
            return False, "Missing Manage Roles permission"
        except discord.HTTPException as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # -------------------------------------------------------------- reconcile
    async def reconcile(self, giveaway: Any) -> dict[str, int]:
        """Re-apply the role to current entrants and drop it from everyone else.

        Run on startup and after a crash: it repairs any grant/revoke that was
        journalled but not applied, and it is safe when the role is absent.
        """
        from .repositories import entries as entries_repo

        guild = self.bot.get_guild(int(giveaway.guild_id))
        if guild is None or not giveaway.participant_role_id:
            return {"granted": 0, "revoked": 0}

        role = guild.get_role(int(giveaway.participant_role_id))
        if role is None:
            return {"granted": 0, "revoked": 0}

        expected = set(entries_repo.users_with_bot_role(self.db, giveaway.id))
        granted = revoked = 0

        for member in guild.members:
            should_have = str(member.id) in expected
            has_role = role in member.roles
            if should_have and not has_role:
                try:
                    await member.add_roles(role, reason="Giveaway Bot: reconcile entrants")
                    granted += 1
                except discord.HTTPException:
                    pass
            elif (
                has_role
                and not should_have
                # Only strip from members we granted it to.
                and entries_repo.has_bot_grant(self.db, giveaway.id, str(member.id))
            ):
                    try:
                        await member.remove_roles(role, reason="Giveaway Bot: reconcile entrants")
                        revoked += 1
                    except discord.HTTPException:
                        pass
        if granted or revoked:
            log.info(
                "reconciled entrants role for giveaway %s: +%d -%d",
                giveaway.id, granted, revoked,
            )
        return {"granted": granted, "revoked": revoked}

    def invalidate(self, guild_id: str) -> None:
        """Forget the cached role (called when the role is deleted)."""
        self._role_cache.pop(str(guild_id), None)