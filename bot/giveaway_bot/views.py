"""Persistent Join/Leave buttons. custom_id carries the giveaway id."""

from __future__ import annotations

import discord


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str, dashboard_url: str = "") -> None:
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
        participants = discord.ui.Button(
            label="Participants",
            style=discord.ButtonStyle.primary,
            emoji="👥",
            custom_id=f"gw_participants:{giveaway_id}",
        )
        participants.callback = self._participants  # type: ignore[method-assign]
        self.add_item(join)
        self.add_item(leave)
        self.add_item(participants)
        # Link buttons must use style=link + url (no custom_id, no callback).
        # They render blue and survive restarts without any handler. Omit the
        # button entirely when no dashboard URL is configured.
        if dashboard_url:
            self.add_item(
                discord.ui.Button(
                    label="Dashboard",
                    style=discord.ButtonStyle.link,
                    emoji="💙",
                    url=dashboard_url,
                )
            )
        self._join_handler = None  # set by bot.py
        self._leave_handler = None
        self._participants_handler = None

    async def _join(self, interaction: discord.Interaction) -> None:
        if self._join_handler is not None:
            await self._join_handler(interaction, self.giveaway_id)

    async def _leave(self, interaction: discord.Interaction) -> None:
        if self._leave_handler is not None:
            await self._leave_handler(interaction, self.giveaway_id)

    async def _participants(self, interaction: discord.Interaction) -> None:
        if self._participants_handler is not None:
            await self._participants_handler(interaction, self.giveaway_id)


class ParticipantsPages(discord.ui.View):
    """Ephemeral paged entrant list: ◀ Prev | page x/y | Next ▶."""

    PAGE_SIZE = 10

    def __init__(
        self,
        *,
        render: object,
        pages: int,
    ) -> None:
        super().__init__(timeout=180)
        self._render = render  # callable(page) -> discord.Embed
        self._pages = max(1, pages)
        self.page = 0
        prev = discord.ui.Button(
            label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="gw_pages:prev"
        )
        prev.callback = self._prev  # type: ignore[method-assign]
        counter = discord.ui.Button(
            label="Page 1/1", style=discord.ButtonStyle.secondary,
            custom_id="gw_pages:counter", disabled=True,
        )
        counter.callback = self._noop  # type: ignore[method-assign]
        nxt = discord.ui.Button(
            label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="gw_pages:next"
        )
        nxt.callback = self._next  # type: ignore[method-assign]
        self.add_item(prev)
        self.add_item(counter)
        self.add_item(nxt)
        self._prev_btn = prev
        self._counter_btn = counter
        self._next_btn = nxt
        self._sync()

    def _sync(self) -> None:
        self._prev_btn.disabled = self.page <= 0
        self._next_btn.disabled = self.page >= self._pages - 1
        self._counter_btn.label = f"Page {self.page + 1}/{self._pages}"

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def _prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._sync()
        await interaction.response.edit_message(embed=self._render(self.page), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self._pages - 1, self.page + 1)
        self._sync()
        await interaction.response.edit_message(embed=self._render(self.page), view=self)
