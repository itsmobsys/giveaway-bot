"""Embed construction - all Discord presentation lives here.

Design goals: readable at a glance, animated feel through a live progress bar
and countdown, and every giveaway message carries the seed commitment so the
fairness claim is visible in Discord itself (not only on the dashboard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from .eligibility import summarise_rules
from .models import Giveaway, GiveawayStatus

#: Discord's own hard limits. Exceeding any of them makes channel.send() raise
#: HTTPException 400, which render_giveaway catches and logs as a warning - so a
#: giveaway that validated cleanly could end up running with no message and
#: message_id IS NULL, which nobody can enter, while /giveaway create reported
#: success. The validators cap title at 256 and description at 4000, which are the
#: same numbers as Discord's title and *near* its 4096 description, and the
#: renderer then prepends an emoji to the title and appends the prize and status
#: line to the description. So the composed values had to be clamped here.
DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_FIELD_VALUE_LIMIT = 1024


def _clip(value: str, limit: int) -> str:
    """Clamp to Discord's limit, marking the cut so nothing looks complete."""
    value = value or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "\u2026"

#: Gradient stops for the progress bar (green -> yellow -> red).
BAR_STOPS = ("🟩", "🟨", "🟥")
BAR_EMPTY = "⬜"

STATUS_COLORS = {
    GiveawayStatus.SCHEDULED: 0x6B7280,
    GiveawayStatus.RUNNING: 0x7C5CFF,
    GiveawayStatus.PAUSED: 0xF59E0B,
    GiveawayStatus.ENDED: 0x10B981,
}

STATUS_EMOJI = {
    GiveawayStatus.SCHEDULED: "⏳",
    GiveawayStatus.RUNNING: "🎉",
    GiveawayStatus.PAUSED: "⏸️",
    GiveawayStatus.ENDED: "🏆",
}


def format_duration(ms: int) -> str:
    """``93600000`` -> ``1d 02h 00m 00s`` (largest non-zero units first)."""
    ms = max(0, int(ms))
    seconds = ms // 1000
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours:02d}h")
    if minutes or hours or days:
        parts.append(f"{minutes:02d}m")
    parts.append(f"{seconds:02d}s")
    return " ".join(parts)


def format_timestamp(ms: int | None, *, style: str = "f") -> str:
    if not ms:
        return "not set"
    moment = datetime.fromtimestamp(ms / 1000, tz=UTC)
    if style == "R":
        return discord.utils.format_dt(moment, "R")
    return discord.utils.format_dt(moment, "f")


def progress_bar(remaining_ms: int, total_ms: int, *, width: int = 24) -> str:
    """Emoji progress bar that empties left-to-right as time runs out."""
    if total_ms <= 0:
        return BAR_EMPTY * width
    fraction = max(0.0, min(1.0, remaining_ms / total_ms))
    filled = int(round(fraction * width))
    bar: list[str] = []
    for index in range(width):
        if index < filled:
            position = index / max(1, width - 1)
            if position < 0.5:
                bar.append(BAR_STOPS[0])
            elif position < 0.8:
                bar.append(BAR_STOPS[1])
            else:
                bar.append(BAR_STOPS[2])
        else:
            bar.append(BAR_EMPTY)
    return "".join(bar)


def status_line(giveaway: Giveaway, *, now: int | None = None) -> str:
    current = now if now is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
    emoji = STATUS_EMOJI[giveaway.status]
    if giveaway.status is GiveawayStatus.RUNNING and giveaway.ends_at:
        remaining = max(0, giveaway.ends_at - current)
        return (
            f"{emoji} **Ends** {format_timestamp(giveaway.ends_at, style='R')} · "
            f"`{format_duration(remaining)}` left"
        )
    if giveaway.status is GiveawayStatus.PAUSED:
        return (
            f"{emoji} **Paused** with `{format_duration(giveaway.paused_remaining_ms or 0)}` remaining"
        )
    if giveaway.status is GiveawayStatus.ENDED:
        return f"{emoji} **Ended** {format_timestamp(giveaway.updated_at, style='R')}"
    return f"{emoji} **Starts** {format_timestamp(giveaway.starts_at, style='R')}"


def build_giveaway_embed(
    giveaway: Giveaway,
    *,
    role_names: dict[str, str] | None = None,
    dashboard_url: str = "",
    now: int | None = None,
) -> discord.Embed:
    """The live giveaway embed (running / paused / scheduled)."""
    current = now if now is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
    color = STATUS_COLORS[giveaway.status]
    rules = summarise_rules(giveaway, role_names=role_names)

    embed = discord.Embed(
        title=_clip(f"{STATUS_EMOJI[giveaway.status]} {giveaway.title}", DISCORD_TITLE_LIMIT),
        description=_clip(
            f"{giveaway.description}\n\n"
            f"**Prize:** {giveaway.prize or 'To be announced'}\n"
            f"{status_line(giveaway, now=current)}",
            DISCORD_DESCRIPTION_LIMIT,
        ),
        colour=color,
    )

    if giveaway.min_messages > 0:
        scope = (
            f"in {len(giveaway.message_count_channel_ids)} specific channel(s)"
            if giveaway.message_count_scope == "channel" and giveaway.message_count_channel_ids
            else "in this server"
        )
        embed.add_field(
            name="💬 Activity requirement",
            value=_clip(
                f"Send at least **{giveaway.min_messages}** messages {scope} to be eligible.\n"
                "Your count is tracked live - press the button again once you qualify."
            , DISCORD_FIELD_VALUE_LIMIT),
            inline=False,
        )

    if giveaway.prize_image_url:
        embed.set_image(url=giveaway.prize_image_url)

    total = (giveaway.original_ends_at or giveaway.ends_at or 0) - (
        giveaway.starts_at or giveaway.created_at or 0
    )
    remaining = giveaway.remaining_ms(now=current)
    if giveaway.status is GiveawayStatus.RUNNING and total > 0:
        embed.add_field(
            name="Time left",
            value=_clip(
                f"{progress_bar(remaining, total)}\n`{format_duration(remaining)}` remaining",
                DISCORD_FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )

    if rules:
        embed.add_field(
            name="Entry rules",
            value=_clip("\n".join(f"• {rule}" for rule in rules[:6]), DISCORD_FIELD_VALUE_LIMIT),
            inline=False,
        )

    embed.add_field(
        name="Entries",
        value=_clip(
            f"👥 **{giveaway.participant_count}** participant(s)\n"
            f"🎟️ **{giveaway.entry_count}** total entr"
            f"{'y' if giveaway.entry_count == 1 else 'ies'}\n"
            f"🏆 **{giveaway.winner_count}** winner(s)"
        , DISCORD_FIELD_VALUE_LIMIT),
        inline=True,
    )

    if giveaway.participant_role_id:
        # Shows staff that they can ping one role instead of a long user list.
        embed.add_field(
            name="📣 Entrants role",
            value=_clip(
                f"Entrants receive <@&{giveaway.participant_role_id}> so staff can "
                "ping everyone at once.\nThe role is removed automatically when this "
                "giveaway ends."
            , DISCORD_FIELD_VALUE_LIMIT),
            inline=False,
        )

    host = giveaway.created_by
    embed.set_footer(
        text=f"Giveaway {giveaway.id} · host {host} · provably fair draw"
    )
    embed.timestamp = datetime.fromtimestamp((giveaway.updated_at or current) / 1000, tz=UTC)

    if giveaway.seed_commitment:
        embed.add_field(
            name="🔐 Seed commitment (published before entries opened)",
            value=_clip(
                f"`{giveaway.seed_commitment[:32]}…`\nFull value and verification on the dashboard.",
                DISCORD_FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )

    if dashboard_url:
        embed.url = f"{dashboard_url}/giveaways/{giveaway.id}"
    return embed


def build_winner_embed(
    giveaway: Giveaway,
    winners: list[tuple[str, str]],
    *,
    round_number: int = 1,
    reroll: bool = False,
    mention_winners: bool = True,
    previous_winner_ids: list[str] | None = None,
    activity_report: dict[str, Any] | None = None,
) -> discord.Embed:
    """Winner announcement with the revealed seed and verification pointers."""
    embed = discord.Embed(
        title=_clip(
            ("🔁 Reroll complete" if reroll else "🎉 Giveaway winners")
            + (f" · round {round_number}" if round_number > 1 else ""),
            DISCORD_TITLE_LIMIT,
        ),
        description=_clip("", DISCORD_DESCRIPTION_LIMIT),
        colour=0x10B981,
    )

    if winners:
        lines = []
        for rank, (user_id, display_name) in enumerate(winners, start=1):
            mention = f"<@{user_id}>" if mention_winners else display_name
            suffix = " ✨ *(previous winner)*" if user_id in (previous_winner_ids or []) else ""
            lines.append(f"**#{rank}** {mention} — `{display_name}`{suffix}")
        embed.description = "\n".join(lines)
    else:
        embed.description = (
            "**No winners.** Not enough eligible participants entered this giveaway."
        )

    if giveaway.prize_image_url:
        embed.set_image(url=giveaway.prize_image_url)

    embed.add_field(
        name="Prize",
        value=_clip(f"{giveaway.prize or '—'} (x{giveaway.prize_count})", DISCORD_FIELD_VALUE_LIMIT),
        inline=True,
    )
    embed.add_field(
        name="Entries",
        value=_clip(
            f"{giveaway.entry_count} entries · {giveaway.participant_count} participants",
            DISCORD_FIELD_VALUE_LIMIT,
        ),
        inline=True,
    )

    embed.add_field(
        name="🔐 Provably fair",
        value=_clip(
            f"Algorithm `hmac-sha256-commit-reveal/v1`\n"
            f"Round: `{round_number}` · participants: `{giveaway.entry_count}`\n"
            f"Anyone can recompute every score from the revealed seed."
        , DISCORD_FIELD_VALUE_LIMIT),
        inline=False,
    )

    # Publish how the activity rule was applied, so the draw is auditable on the
    # message itself and not only in the dashboard.
    if activity_report:
        checked = int(activity_report.get("checked") or 0)
        flagged = int(activity_report.get("flagged") or 0)
        if checked:
            embed.add_field(
                name="💬 Activity check",
                value=_clip(
                    f"Required **{giveaway.min_messages}** messages · "
                    f"{checked} participant(s) checked · {flagged} did not meet it "
                    "and were excluded before the draw."
                , DISCORD_FIELD_VALUE_LIMIT),
                inline=False,
            )

    embed.set_footer(text=f"Giveaway {giveaway.id} · seed revealed · draws are fully reproducible")
    return embed


def build_paused_embed(giveaway: Giveaway, *, reason: str | None = None) -> discord.Embed:
    embed = build_giveaway_embed(giveaway)
    embed.title = f"⏸️ Paused · {giveaway.title}"
    embed.colour = STATUS_COLORS[GiveawayStatus.PAUSED]
    embed.description = (
        f"{giveaway.description}\n\n"
        f"**Paused** with `{format_duration(giveaway.paused_remaining_ms or 0)}` remaining.\n"
        f"{f'_Reason: {reason}_' if reason else ''}"
    )
    return embed


def build_cancelled_embed(giveaway: Giveaway, *, reason: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=_clip(f"🚫 Cancelled · {giveaway.title}", DISCORD_TITLE_LIMIT),
        description=_clip(
            f"This giveaway was ended **without a draw**.\n"
            f"{giveaway.entry_count} entries from {giveaway.participant_count} participants "
            f"were not eligible for any prize.\n"
            f"{f'_Reason: {reason}_' if reason else ''}",
            DISCORD_DESCRIPTION_LIMIT,
        ),
        colour=0xEF4444,
    )
    embed.set_footer(text=f"Giveaway {giveaway.id} · no winners were selected")
    return embed


def build_verify_embed(giveaway: Giveaway, verification: dict[str, Any] | None) -> discord.Embed:
    """`/giveaway reveal` output - seed + commitment + local check result."""
    ok = bool(verification and verification.get("ok"))
    embed = discord.Embed(
        title=_clip(
        f"{'✅' if ok else '⚠️'} Draw verification · {giveaway.title}", DISCORD_TITLE_LIMIT
    ),
        colour=0x10B981 if ok else 0xF59E0B,
    )
    seed = giveaway.server_seed or "(sealed - not yet revealed)"
    embed.add_field(name="Server seed", value=f"`{seed}`", inline=False)
    embed.add_field(
        name="Commitment (published before entries)",
        value=_clip(f"`{giveaway.seed_commitment or 'n/a'}`", DISCORD_FIELD_VALUE_LIMIT),
        inline=False,
    )
    embed.add_field(
        name="Participant digest",
        value=_clip(
            f"`{(verification or {}).get('recomputed', {}).get('participant_digest', 'n/a')}`",
            DISCORD_FIELD_VALUE_LIMIT,
        ),
        inline=False,
    )
    if verification and not verification.get("ok"):
        embed.add_field(
            name="Local re-verification",
            # Clamped: a verification error can carry a whole manifest dump, and
            # five of those is far past Discord's 1024-char field-value limit, so
            # the send would fail outright.
            value=_clip(
                "\n".join(f"• {error}" for error in verification["errors"][:5]),
                DISCORD_FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Local re-verification",
            value=_clip(
            "All recomputed scores and the winner ordering match the stored draw.",
            DISCORD_FIELD_VALUE_LIMIT,
        ),
            inline=False,
        )
    embed.set_footer(text="Reproduce it yourself: the algorithm is published in the source repository")
    return embed


def member_mention(user_id: str) -> str:
    return f"<@{user_id}>"


def relative_time(ms: int | None) -> str:
    if not ms:
        return "unknown"
    delta = timedelta(milliseconds=ms - int(datetime.now(tz=UTC).timestamp() * 1000))
    return discord.utils.format_dt(datetime.now(tz=UTC) + delta, "R")