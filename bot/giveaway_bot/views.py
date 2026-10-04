"""Discord UI components.

Views are thin: they own layout, styles and the ``custom_id`` scheme, while the
handlers are injected by :mod:`giveaway_bot.bot` so all business logic stays in
the service layer.

``custom_id`` scheme: ``gw:<giveaway_id>:<action>``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from .eligibility import Reason
from .embeds import format_duration
from .models import Giveaway, GiveawayStatus, snowflake_to_ms
from .roles import DEFAULT_ROLE_NAME

log = logging.getLogger("giveaway_bot.views")

ButtonHandler = Callable[[discord.Interaction, str], Awaitable[None]]


def custom_id(giveaway_id: str, action: str) -> str:
    return f"gw:{giveaway_id}:{action}"


def parse_custom_id(value: str) -> tuple[str, str] | None:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "gw":
        return None
    return parts[1], parts[2]


def member_context(member: discord.Member | discord.User, guild: discord.Guild) -> dict[str, Any]:
    """Collect the eligibility inputs for a member - data only, no logic."""
    is_member = isinstance(member, discord.Member)
    roles = [str(role.id) for role in member.roles] if is_member else []
    return {
        "user_id": str(member.id),
        "username": str(member.display_name if is_member else member.name),
        "role_ids": roles,
        "is_member": is_member,
        "account_created_at": snowflake_to_ms(getattr(member, "created_at", None)),
        "guild_joined_at": snowflake_to_ms(getattr(member, "joined_at", None)) if is_member else None,
        "guild_id": str(guild.id),
    }


def _bind(button: discord.ui.Button, handler: ButtonHandler, giveaway_id: str, action: str) -> None:
    """Attach a handler to a button before the View indexes its children."""

    async def callback(interaction: discord.Interaction) -> None:
        await handler(interaction, giveaway_id)

    button.callback = callback  # type: ignore[method-assign]
    del action


class GiveawayView(discord.ui.View):
    """Live giveaway message: Enter / Leave / Reroll / Details."""

    def __init__(
        self,
        giveaway_id: str,
        *,
        on_join: ButtonHandler,
        on_leave: ButtonHandler,
        on_reroll: ButtonHandler | None = None,
        dashboard_url: str = "",
        can_manage: bool = False,
        timeout: float | None = None,
    ) -> None:
        self.giveaway_id = giveaway_id
        self.dashboard_url = dashboard_url
        self._entered: set[str] = set()

        enter = discord.ui.Button(
            label="Enter giveaway",
            style=discord.ButtonStyle.success,
            emoji="🎟️",
            custom_id=custom_id(giveaway_id, "join"),
        )
        _bind(enter, on_join, giveaway_id, "join")
        self.add_item(enter)

        leave = discord.ui.Button(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=custom_id(giveaway_id, "leave"),
        )
        _bind(leave, on_leave, giveaway_id, "leave")
        self.add_item(leave)

        if can_manage and on_reroll is not None:
            reroll = discord.ui.Button(
                label="Reroll",
                style=discord.ButtonStyle.primary,
                emoji="🔁",
                custom_id=custom_id(giveaway_id, "reroll"),
            )
            _bind(reroll, on_reroll, giveaway_id, "reroll")
            self.add_item(reroll)

        if dashboard_url:
            self.add_item(
                discord.ui.Button(
                    label="Details & proof",
                    style=discord.ButtonStyle.link,
                    emoji="🌐",
                    url=f"{dashboard_url}/g/{giveaway_id}",
                )
            )

        super().__init__(timeout=timeout)

    def mark_entered(self, user_id: str, entered: bool) -> None:
        if entered:
            self._entered.add(str(user_id))
        else:
            self._entered.discard(str(user_id))

    def is_entered(self, user_id: str) -> bool:
        return str(user_id) in self._entered


class WinnerView(discord.ui.View):
    """Winner announcement: reroll (privileged) + winner proof link."""

    def __init__(
        self,
        giveaway_id: str,
        *,
        on_reroll: ButtonHandler | None = None,
        dashboard_url: str = "",
        can_manage: bool = False,
    ) -> None:
        self.giveaway_id = giveaway_id
        if can_manage and on_reroll is not None:
            reroll = discord.ui.Button(
                label="Reroll",
                style=discord.ButtonStyle.primary,
                emoji="🔁",
                custom_id=custom_id(giveaway_id, "reroll"),
            )
            _bind(reroll, on_reroll, giveaway_id, "reroll")
            self.add_item(reroll)
        if dashboard_url:
            self.add_item(
                discord.ui.Button(
                    label="Winner proof",
                    style=discord.ButtonStyle.link,
                    emoji="🔐",
                    url=f"{dashboard_url}/g/{giveaway_id}#verification",
                )
            )
        super().__init__(timeout=None)


class VerifyView(discord.ui.View):
    """`/giveaway reveal` output."""

    def __init__(self, giveaway_id: str, *, dashboard_url: str = "") -> None:
        if dashboard_url:
            self.add_item(
                discord.ui.Button(
                    label="Verify this draw",
                    style=discord.ButtonStyle.link,
                    emoji="🔐",
                    url=f"{dashboard_url}/g/{giveaway_id}#verification",
                )
            )
        super().__init__(timeout=None)


REASON_EMOJI = {
    Reason.INSUFFICIENT_MESSAGES: "💬",
    Reason.MISSING_REQUIRED_ROLES: "🚫",
    Reason.HAS_BLACKLISTED_ROLE: "🚫",
    Reason.ACCOUNT_TOO_NEW: "🕓",
    Reason.JOINED_TOO_RECENTLY: "🕓",
    Reason.ENTRY_LIMIT_REACHED: "🚦",
    Reason.MAX_ENTRIES_REACHED: "🎟️",
    Reason.CHANNEL_NOT_ALLOWED: "📍",
    Reason.REQUIRES_MEMBERSHIP: "🏠",
    Reason.GIVEAWAY_ENDED: "🏁",
    Reason.GIVEAWAY_NOT_RUNNING: "⏸️",
    Reason.GIVEAWAY_LOCKED: "🔒",
    Reason.INVALID_ROLE_CONFIG: "⚙️",
}


def eligibility_embed(result: Any) -> discord.Embed | None:
    """Small ephemeral embed explaining a refusal (no spam, no shame)."""
    if getattr(result, "ok", False):
        return None
    emoji = REASON_EMOJI.get(getattr(result, "reason", ""), "⚠️")
    embed = discord.Embed(description=f"{emoji} {result.message}", colour=0xF59E0B)

    # Spell out the message-activity gap with a progress bar, so the member
    # knows exactly how many messages are left rather than just "no".
    progress = getattr(result, "progress", None)
    if progress is not None:
        current, required = progress
        width = 20
        filled = 0 if required <= 0 else int(round((min(current, required) / required) * width))
        bar = "".join("▰" if index < filled else "▱" for index in range(width))
        embed.add_field(
            name="Your message activity",
            value=(
                f"`{bar}`\n"
                f"**{current} / {required}** messages\n"
                f"*{max(0, required - current)} more* message"
                f"{'s' if required - current != 1 else ''} to go - this check updates live, "
                "no need to ask anyone."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Counts update automatically as you chat. Try the button again when you qualify."
        )
        return embed

    embed.set_footer(text="Eligibility is re-checked every time you press the button.")
    return embed


def progress_footer(giveaway: Giveaway) -> str:
    """One-line requirement summary for the live embed footer."""
    if giveaway.min_messages > 0:
        scope = (
            f"{len(giveaway.message_count_channel_ids)} channel(s)"
            if giveaway.message_count_scope == "channel"
            else "the server"
        )
        return f"{giveaway.entry_count} entries · needs {giveaway.min_messages} messages in {scope}"
    return entry_count_footer(giveaway)


def joined_embed(
    giveaway: Giveaway,
    *,
    entry_seq: int,
    max_entries: int,
    role_granted: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎟️ You're in!",
        description=(
            f"Entry **{entry_seq}** of {max_entries} for **{giveaway.title}**.\n"
            f"{giveaway.participant_count} participant(s) · {giveaway.entry_count} entries · "
            f"{format_duration(giveaway.remaining_ms())} left."
        ),
        colour=0x10B981,
    )

    if giveaway.participant_role_id:
        if role_granted:
            embed.description += (
                f"\n\nYou now have the **{DEFAULT_ROLE_NAME}** role so staff can reach you. "
                "It is removed automatically when the giveaway ends."
            )
        else:
            # Be honest: the entry succeeded, the convenience role did not.
            embed.description += (
                f"\n\n⚠️ You are entered, but I could not give you the "
                f"**{DEFAULT_ROLE_NAME}** role. That only affects how staff reach "
                "entrants — your entry is valid."
            )
    if giveaway.status is GiveawayStatus.RUNNING:
        embed.add_field(
            name="Draw transparency",
            value=(
                "When this giveaway ends, winners are picked with a "
                "`HMAC-SHA256` commit–reveal draw. The seed commitment was published "
                "before you entered, so the result cannot be rigged."
            ),
            inline=False,
        )
    return embed


def left_embed(giveaway: Giveaway) -> discord.Embed:
    return discord.Embed(
        title="↩️ Entry removed",
        description=f"You left **{giveaway.title}**. You can re-enter any time before it ends.",
        colour=0x9CA3AF,
    )