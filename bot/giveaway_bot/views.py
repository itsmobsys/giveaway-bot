"""Join / Leave / Participants buttons, plus the paged entrant list.

The giveaway id is encoded inside each button custom_id ("gw_join:gw_abc"), and
the buttons are discord.ui.DynamicItem subclasses whose template is a regular
expression. Discord sends the raw custom_id back, discord.py matches it against
every registered template and calls from_custom_id with the parsed id, so ONE
set of components serves every giveaway this process will ever post.

That matters: the previous design built a fresh discord.ui.View per giveaway and
called add_view() on it, which registered three custom ids in the discord.py
view store forever. Nothing removed them when a giveaway ended, so a
long-running bot grew without bound. Dynamic items are registered once
(GiveawayView.register) and dispatch purely off the pattern.

Handlers are injected by bot.py at startup instead of being poked onto private
instance attributes afterwards, so a view can never exist half-wired.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

import discord

#: (interaction, giveaway_id) -> None. Supplied by bot.py.
Handler = Callable[[discord.Interaction, str], Awaitable[None]]

#: Giveaway ids are "gw_" + 12 hex chars, but the pattern is kept loose enough to
#: also match ids written by older revisions (and to reject anything else).
_ID = r"(?P<id>[A-Za-z0-9_-]{1,64})"


#: prefix -> click handler, filled once by GiveawayView.register() and read on
#: every click. A plain function stored on a class becomes a bound method when
#: read through an instance, which would silently pass an extra argument; a
#: module-level mapping has no such trap.
_HANDLERS: dict[str, Handler] = {}


class _ButtonPlumbing:
    """Shared button behaviour, mixed into each DynamicItem subclass.

    A plain mixin rather than a DynamicItem: discord.py requires a regex template
    on every class that derives from DynamicItem, and only the three concrete
    classes below have a real one.
    """

    #: Filled in by each concrete subclass.
    LABEL: ClassVar[str]
    EMOJI: ClassVar[str]
    STYLE: ClassVar[discord.ButtonStyle]
    PREFIX: ClassVar[str]

    def __init__(self, giveaway_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=self.LABEL,
                emoji=self.EMOJI,
                style=self.STYLE,
                custom_id=f"{self.PREFIX}:{giveaway_id}",
            )
        )
        self.giveaway_id = giveaway_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match,
        /,
    ) -> _ButtonPlumbing:
        """Rebuild the clicked button from its custom_id."""
        return cls(match["id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = _HANDLERS.get(self.PREFIX)
        if handler is not None:
            await handler(interaction, self.giveaway_id)


class JoinButton(_ButtonPlumbing, discord.ui.DynamicItem[discord.ui.Button], template=rf"gw_join:{_ID}"):
    """Ticket button: enter the giveaway."""

    LABEL = "Join"
    EMOJI = "🎟️"
    STYLE = discord.ButtonStyle.success
    PREFIX = "gw_join"


class LeaveButton(_ButtonPlumbing, discord.ui.DynamicItem[discord.ui.Button], template=rf"gw_leave:{_ID}"):
    """Door button: withdraw from the giveaway."""

    LABEL = "Leave"
    EMOJI = "🚪"
    STYLE = discord.ButtonStyle.secondary
    PREFIX = "gw_leave"


class ParticipantsButton(
    _ButtonPlumbing,
    discord.ui.DynamicItem[discord.ui.Button],
    template=rf"gw_participants:{_ID}",
):
    """People button: open the paged entrant list."""

    LABEL = "Participants"
    EMOJI = "👥"
    STYLE = discord.ButtonStyle.primary
    PREFIX = "gw_participants"


#: Every dynamic component the bot listens for. Registered once per process.
DYNAMIC_ITEMS: tuple[type[_ButtonPlumbing], ...] = (
    JoinButton,
    LeaveButton,
    ParticipantsButton,
)


class GiveawayView(discord.ui.View):
    """The row of buttons attached to a giveaway message.

    Construction is cheap and stateless: the object is only needed to render the
    message. Clicks are dispatched by GiveawayView.register(), not by this
    instance, so nothing has to be kept alive for a giveaway to stay clickable.
    """

    def __init__(self, giveaway_id: str, dashboard_url: str = "") -> None:
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.add_item(JoinButton(giveaway_id))
        self.add_item(LeaveButton(giveaway_id))
        self.add_item(ParticipantsButton(giveaway_id))
        # Link buttons must use style=link + url (no custom_id, no callback).
        # They render blue and survive restarts without any handler. Omitted
        # entirely when no dashboard URL is configured.
        if dashboard_url:
            self.add_item(
                discord.ui.Button(
                    label="Dashboard",
                    style=discord.ButtonStyle.link,
                    emoji="💙",
                    url=dashboard_url,
                )
            )

    @classmethod
    def register(
        cls,
        client: discord.Client,
        *,
        join: Handler,
        leave: Handler,
        participants: Handler,
    ) -> None:
        """Wire handlers and make every custom_id dispatchable.

        Called once from setup_hook. Adding the dynamic items is what lets a click
        on a giveaway posted before a restart still find its handler.
        """
        _HANDLERS[JoinButton.PREFIX] = join
        _HANDLERS[LeaveButton.PREFIX] = leave
        _HANDLERS[ParticipantsButton.PREFIX] = participants
        client.add_dynamic_items(*DYNAMIC_ITEMS)


class ParticipantsPages(discord.ui.View):
    """Ephemeral paged entrant list: Prev | page x/y | Next."""

    PAGE_SIZE = 10

    #: Long enough to match the 15-minute interaction token: after that the
    #: buttons are dead anyway, and before it a click that arrives after the
    #: view timed out is dropped with no message and no log.
    VIEW_TIMEOUT = 900.0

    def __init__(self, *, render: Callable[[int], discord.Embed], pages: int) -> None:
        super().__init__(timeout=self.VIEW_TIMEOUT)
        self._render = render
        self._pages = max(1, pages)
        self.page = 0
        prev = discord.ui.Button(
            label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="gw_pages:prev"
        )
        prev.callback = self._prev  # type: ignore[method-assign]
        counter = discord.ui.Button(
            label="Page 1/1",
            style=discord.ButtonStyle.secondary,
            custom_id="gw_pages:counter",
            disabled=True,
        )
        # No callback on purpose: a disabled component is never dispatched, so
        # the counter is decoration and nothing here can respond to it.
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

    async def _show(self, interaction: discord.Interaction, page: int) -> None:
        """Render one page, recording it only once Discord accepted the edit.

        Moving self.page before the edit meant a failed edit left the buttons
        describing a page the message never showed, so the next click skipped
        one.
        """
        previous, self.page = self.page, max(0, min(page, self._pages - 1))
        self._sync()
        try:
            await interaction.response.edit_message(embed=self._render(self.page), view=self)
        except Exception:
            self.page = previous
            self._sync()
            raise

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, self.page - 1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, self.page + 1)
