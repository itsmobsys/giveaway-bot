"""Discord embeds for the simple bot."""

from __future__ import annotations

import time

import discord

from .service import Giveaway

#: Winners named individually in an announcement. Past this the list is
#: summarised, because the mention text alone would overflow the embed.
WINNERS_NAMED = 50


def countdown(ends_at_ms: int) -> str:
    """Human 'ends in' text, re-rendered every tick so it visibly ticks down."""
    secs = max(0, int(ends_at_ms / 1000 - time.time()))
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    minutes, secs = divmod(secs, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def _req_lines(gw: Giveaway) -> str:
    lines: list[str] = []
    if gw.required_role_ids:
        mentions = " ".join(f"<@&{r}>" for r in gw.required_role_ids)
        lines.append(f"• Requires one of: {mentions}")
    if gw.blocked_role_id:
        lines.append(f"• <@&{gw.blocked_role_id}> cannot enter")
    if gw.min_account_age_days > 0:
        lines.append(f"• Account {gw.min_account_age_days}+ days old")
    if gw.min_messages > 0:
        lines.append(f"• Send {gw.min_messages}+ messages in this server")
    return "\n".join(lines)


def giveaway_embed(gw: Giveaway, entries: int, color: int) -> discord.Embed:
    ends = int(gw.ends_at / 1000)
    desc = (
        f"🏆 **{gw.prize}**\n\n"
        f"⏳ Ends in **{countdown(gw.ends_at)}** (<t:{ends}:R>)\n"
        f"Winners: **{gw.winner_count}**  •  Entries: **{entries}**\n"
    )
    reqs = _req_lines(gw)
    if reqs:
        desc += f"\n**Requirements**\n{reqs}\n"
    desc += "\nClick **Join** to enter, **Leave** to withdraw."
    if gw.host_id:
        host_label = gw.host_name or "host"
        desc += f"\n\n🎤 Hosted by <@{gw.host_id}> ({host_label})"
    embed = discord.Embed(title="🎉 Giveaway", description=desc, colour=color)
    if gw.image_url:
        embed.set_image(url=gw.image_url)
    embed.set_footer(text=f"ID: {gw.id}")
    return embed


def participants_embed(
    *,
    prize: str,
    rows: list[dict],
    page: int,
    pages: int,
    total: int,
    mine: int,
    winner_count: int,
    color: int,
    shown: int | None = None,
) -> discord.Embed:
    lines = [f"<@{row['user_id']}> (1 entry)" for row in rows]
    # Clamped like every other odds readout in the bot: a giveaway created with
    # 25 winners and 3 entrants would otherwise advertise 833%.
    chance = min(100.0, winner_count / total * 100) if mine and total else 0.0
    window = f"\nShowing the first {shown} of {total}." if shown is not None and shown < total else ""
    desc = (
        f"These are the members that have participated in the giveaway of {prize}:{window}\n\n"
        + "\n".join(lines)
        + f"\n\nTotal Participants: {total}\nTotal Entries: {total}"
        + f"\n\nYour Entries: {mine}\nYour Chance of Winning: {chance:g}%"
    )
    return discord.Embed(
        title=f"👥 Participants — page {page + 1}/{pages}", description=desc, colour=color
    )


def timeout_bans_embed(
    *,
    rows: list[dict],
    page: int,
    pages: int,
    total: int,
    color: int,
    hidden: int = 0,
) -> discord.Embed:
    """One page of members sitting out a penalty for joining while timed out.

    Rows arrive from GiveawayService.list_timeout_bans(); each one carries the
    "in_guild" flag the caller resolved from its own member cache, so a member
    who left the server is shown by id instead of a mention that would never
    resolve.
    """
    lines: list[str] = []
    for row in rows:
        user_id = str(row["user_id"])
        who = f"<@{user_id}>" if row.get("in_guild") else f"`{user_id}` (left the server)"
        line = f"{who} — **{int(row['giveaways_remaining'])}** giveaway(s) left"
        since = int(row.get("created_at") or 0) // 1000
        if since:
            line += f" · since <t:{since}:R>"
        lines.append(line)
    desc = "\n".join(lines) + f"\n\nTotal: **{total}** member(s) banned from giveaways"
    if hidden > 0:
        # The listing is capped (list_timeout_bans), so say so rather than
        # letting the page count pass for the whole picture.
        desc += f"\n{hidden} more not shown."
    return discord.Embed(
        title=f"⏳ Timed-out giveaway bans — page {page + 1}/{pages}",
        description=desc,
        colour=color,
    )


def winner_embed(gw: Giveaway, winners: list[str], entries: int, color: int) -> discord.Embed:
    if winners:
        # Discord caps a description at 4096 characters, so an old reroll row
        # holding hundreds of winners would lose the whole announcement: name
        # the first fifty and count the rest.
        named = winners[:WINNERS_NAMED]
        mentions = ", ".join(f"<@{w}>" for w in named)
        if len(winners) > len(named):
            mentions += f" …and {len(winners) - len(named)} more"
        desc = (
            f"## 🎊 {gw.prize} 🎊\n\n"
            f"Congratulations {mentions} — you won!\n\n"
            f"👥 Entries: **{entries}**  •  🏆 Winners: **{len(winners)}**"
        )
    else:
        desc = f"## 🎊 {gw.prize} 🎊\n\nNo valid entries — no winners this time."
    if gw.host_id:
        host_label = gw.host_name or "host"
        desc += f"\n🎤 Hosted by <@{gw.host_id}> ({host_label})"
    embed = discord.Embed(title="Giveaway Ended", description=desc, colour=color)
    if gw.image_url:
        embed.set_image(url=gw.image_url)
    embed.set_footer(text=f"ID: {gw.id}")
    return embed
