"""Persistent Join/Leave buttons. custom_id carries the giveaway id."""

from __future__ import annotations

import discord


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str) -> None:
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        join = discord.ui.Button(
            label="Join",
            style=discord.ButtonStyle.success,
            emoji="🎟️",
            custom_id=f"gw_join:{giveaway_id}",
        )
        join.callback = self._join  # type: ignore[method-assign]
        leave = discord.ui.Button(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            emoji="🚪",
            custom_id=f"gw_leave:{giveaway_id}",
        )
        leave.callback = self._leave  # type: ignore[method-assign]
        self.add_item(join)
        self.add_item(leave)
        self._join_handler = None  # set by bot.py
        self._leave_handler = None

    async def _join(self, interaction: discord.Interaction) -> None:
        if self._join_handler is not None:
            await self._join_handler(interaction, self.giveaway_id)

    async def _leave(self, interaction: discord.Interaction) -> None:
        if self._leave_handler is not None:
            await self._leave_handler(interaction, self.giveaway_id)
