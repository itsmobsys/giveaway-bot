"""Giveaway rules: create / join / leave / end / reroll. Nothing else."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database

DAY = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ServiceError(Exception):
    message: str


@dataclass
class Giveaway:
    id: str
    guild_id: str
    channel_id: str
    message_id: str | None
    prize: str
    winner_count: int
    ends_at: int
    status: str
    required_role_id: str | None
    required_role_ids: list[str]
    blocked_role_id: str | None
    min_account_age_days: int
    min_messages: int
    image_url: str | None
    entrants_role_id: str | None
    host_id: str | None
    host_name: str | None
    winners: list[str]

    @property
    def active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_row(cls, row: dict) -> Giveaway:
        try:
            winners = json.loads(row.get("winners_json") or "[]")
        except (ValueError, TypeError):
            winners = []
        required: list[str] = []
        raw_ids = row.get("required_role_ids")
        if raw_ids:
            try:
                parsed = json.loads(raw_ids)
                if isinstance(parsed, list):
                    required = [str(r) for r in parsed if str(r).strip()]
            except (ValueError, TypeError):
                pass
        legacy = str(row.get("required_role_id") or "").strip()
        if legacy and legacy not in required:
            required.append(legacy)
        return cls(
            id=str(row["id"]),
            guild_id=str(row["guild_id"]),
            channel_id=str(row["channel_id"]),
            message_id=str(row["message_id"]) if row.get("message_id") else None,
            prize=str(row["prize"]),
            winner_count=int(row["winner_count"]),
            ends_at=int(row["ends_at"]),
            status=str(row["status"]),
            required_role_id=str(row["required_role_id"]) if row.get("required_role_id") else None,
            required_role_ids=required,
            blocked_role_id=str(row["blocked_role_id"]) if row.get("blocked_role_id") else None,
            min_account_age_days=int(row.get("min_account_age_days") or 0),
            min_messages=int(row.get("min_messages") or 0),
            image_url=str(row["image_url"]) if row.get("image_url") else None,
            entrants_role_id=str(row["entrants_role_id"]) if row.get("entrants_role_id") else None,
            host_id=str(row["host_id"]) if row.get("host_id") else None,
            host_name=str(row["host_name"]) if row.get("host_name") else None,
            winners=[str(w) for w in winners] if isinstance(winners, list) else [],
        )


class GiveawayService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._rand = secrets.SystemRandom()

    # -- create ---------------------------------------------------------
    def create(
        self,
        *,
        guild_id: str,
        channel_id: str,
        prize: str,
        winner_count: int,
        duration_seconds: int,
        created_by: str,
        required_role_id: str | None = None,
        required_role_ids: list[str] | None = None,
        blocked_role_id: str | None = None,
        min_account_age_days: int = 0,
        min_messages: int = 0,
        image_url: str | None = None,
        host_id: str | None = None,
        host_name: str | None = None,
    ) -> Giveaway:
        prize = prize.strip()
        if not prize or len(prize) > 256:
            raise ServiceError("Prize must be 1-256 characters.")
        if winner_count < 1 or winner_count > 25:
            raise ServiceError("Winner count must be 1-25.")
        if duration_seconds < 30 or duration_seconds > 60 * 86400:
            raise ServiceError("Duration must be 30 seconds to 60 days.")
        if min_messages < 0 or min_messages > 100000:
            raise ServiceError("Minimum messages must be 0-100000.")
        if min_account_age_days < 0 or min_account_age_days > 3650:
            raise ServiceError("Minimum account age must be 0-3650 days.")
        image_url = (image_url or "").strip() or None
        if image_url and (len(image_url) > 512 or not image_url.startswith(("http://", "https://"))):
            raise ServiceError("Image must be an http(s) URL.")
        role_ids = [str(r).strip() for r in (required_role_ids or []) if str(r).strip()]
        if required_role_id and str(required_role_id).strip() not in role_ids:
            role_ids.append(str(required_role_id).strip())
        role_ids = role_ids[:5]
        gid = "gw_" + uuid.uuid4().hex[:12]
        created = now_ms()
        self.db.execute(
            "INSERT INTO simple_giveaways (id, guild_id, channel_id, prize, winner_count, ends_at,"
            " status, required_role_id, required_role_ids, blocked_role_id, min_account_age_days,"
            " min_messages, image_url, created_by, created_at, host_id, host_name)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gid, guild_id, channel_id, prize, winner_count, created + duration_seconds * 1000,
                role_ids[0] if role_ids else None, json.dumps(role_ids),
                blocked_role_id or None, max(0, min_account_age_days),
                max(0, min_messages), image_url, created_by, created,
                host_id or created_by, (host_name or "")[:64] or None,
            ),
        )
        return self.get(gid)

    # -- guild settings (one-time notify-role setup) ----------------------
    def get_notify_role(self, guild_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT notify_role_id FROM simple_guild_settings WHERE guild_id = ?", (guild_id,)
        )
        return str(row["notify_role_id"]) if row and row.get("notify_role_id") else None

    def set_notify_role(self, guild_id: str, role_id: str | None) -> None:
        self.db.execute(
            "INSERT INTO simple_guild_settings (guild_id, notify_role_id) VALUES (?, ?)"
            " ON CONFLICT (guild_id) DO UPDATE SET notify_role_id = excluded.notify_role_id",
            (guild_id, role_id),
        )

    # -- read -----------------------------------------------------------
    def get(self, giveaway_id: str) -> Giveaway:
        row = self.db.query_one("SELECT * FROM simple_giveaways WHERE id = ?", (giveaway_id,))
        if row is None:
            raise ServiceError("Giveaway not found.")
        return Giveaway.from_row(row)

    def get_by_message(self, message_id: str) -> Giveaway | None:
        row = self.db.query_one("SELECT * FROM simple_giveaways WHERE message_id = ?", (message_id,))
        return Giveaway.from_row(row) if row else None

    def list_active(self, guild_id: str) -> list[Giveaway]:
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE guild_id = ? AND status = 'active' ORDER BY ends_at ASC",
            (guild_id,),
        )
        return [Giveaway.from_row(r) for r in rows]

    def list_all_active(self, limit: int = 25) -> list[Giveaway]:
        """Every active giveaway, soonest deadline first.

        Feeds both the embed refresher and the autocomplete cache, so the cap is
        per tick rather than per guild: 200 rows is one small query and keeps
        suggestions available in every guild the bot is in.
        """
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active'"
            " ORDER BY ends_at ASC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        return [Giveaway.from_row(r) for r in rows]

    def resolve(self, guild_id: str, raw_id: str) -> Giveaway:
        """Find a giveaway by id, falling back to the live one.

        Operators keep mistyping ids, so when the id does not match and
        exactly one giveaway is running in this server, that one is used
        instead of failing. With zero or several running, a valid id is
        required (the slash commands suggest them as you type).
        """
        raw_id = (raw_id or "").strip()
        if raw_id:
            try:
                gw = self.get(raw_id)
            except ServiceError:
                pass
            else:
                # An id can be typed by hand (or copied from another server's
                # message), and every command here is guild-scoped: refuse
                # rather than end someone else's giveaway.
                if str(gw.guild_id) != str(guild_id):
                    raise ServiceError("That giveaway belongs to another server.")
                return gw
        active = self.list_active(guild_id)
        if len(active) == 1:
            return active[0]
        if not active:
            raise ServiceError("No active giveaway in this server.")
        raise ServiceError(
            "That ID did not match. Pick one from the suggestions as you type,"
            " or copy the ID from /giveaway_list."
        )

    def entry_count(self, giveaway_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?", (giveaway_id,)
        )
        return int(row["n"]) if row else 0

    def entries(self, giveaway_id: str) -> list[dict]:
        return self.db.query(
            "SELECT user_id, username, entered_at FROM simple_entries"
            " WHERE giveaway_id = ? ORDER BY entered_at ASC",
            (giveaway_id,),
        )

    # -- join / leave ---------------------------------------------------
    def check_eligible(
        self,
        gw: Giveaway,
        *,
        member_roles: list[str],
        account_created_ts: float | None,
        user_id: str = "",
        pending_messages: int = 0,
    ) -> None:
        if not gw.active:
            raise ServiceError("This giveaway has ended.")
        if user_id and self.is_blacklisted(gw.guild_id, user_id):
            raise ServiceError("🚫 You are blocked from giveaways in this server.")
        if gw.ends_at <= now_ms():
            raise ServiceError("This giveaway has ended.")
        roles = set(member_roles)
        if gw.required_role_ids and not (set(gw.required_role_ids) & roles):
            raise ServiceError("You need one of the required roles to enter.")
        if gw.blocked_role_id and gw.blocked_role_id in roles:
            raise ServiceError("Your role is not allowed to enter.")
        if gw.min_account_age_days > 0:
            if account_created_ts is None:
                # The age could not be read, so the requirement cannot be
                # checked. Refusing is the safe side of that trade: waving it
                # through would silently disable the alt-account filter.
                raise ServiceError("Could not check your account age — try again in a moment.")
            age_days = (time.time() - account_created_ts) / 86400
            if age_days < gw.min_account_age_days:
                raise ServiceError(f"Account must be {gw.min_account_age_days}+ days old.")
        if gw.min_messages > 0:
            # pending_messages adds the rows still sitting in the bot's in-memory
            # buffer, so nobody is told they sent fewer messages than they did
            # since the last flush.
            sent = self.message_count(gw.guild_id, user_id) + max(0, pending_messages)
            if sent < gw.min_messages:
                raise ServiceError(
                    f"You need {gw.min_messages}+ messages in this server ({sent} counted)."
                )

    def message_count(self, guild_id: str, user_id: str) -> int:
        row = self.db.query_one(
            "SELECT count FROM simple_message_counts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return int(row["count"]) if row else 0

    def record_message(self, guild_id: str, user_id: str) -> None:
        self.db.execute(
            "INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES (?, ?, 1)"
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
            (guild_id, user_id),
        )

    #: Rows per INSERT when flushing buffered counts. SQLite's default parameter
    #: limit is 999 and each row takes 3, so 300 leaves plenty of headroom.
    MESSAGE_FLUSH_CHUNK = 300

    def add_message_counts(self, rows: list[tuple[str, str, int]]) -> None:
        """Apply many (guild_id, user_id, delta) counts in a few statements.

        One statement per flush instead of one per message is the entire point:
        N messages in the server cost ceil(N / MESSAGE_FLUSH_CHUNK) round-trips
        per flush interval rather than N immediate ones.
        """
        for start in range(0, len(rows), self.MESSAGE_FLUSH_CHUNK):
            chunk = rows[start : start + self.MESSAGE_FLUSH_CHUNK]
            values = ", ".join("(?, ?, ?)" for _ in chunk)
            params: list[Any] = []
            for guild_id, user_id, count in chunk:
                params.extend((guild_id, user_id, count))
            # Interpolated text is the placeholder list only: every value still
            # travels as a bound parameter, so there is nothing to inject.
            statement = f"INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES {values}"  # noqa: S608
            self.db.execute(
                statement
                + " ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
                params,
            )

    def join(
        self,
        gw: Giveaway,
        *,
        user_id: str,
        username: str,
        member_roles: list[str],
        account_created_ts: float | None,
        pending_messages: int = 0,
    ) -> int:
        self.check_eligible(
            gw, member_roles=member_roles, account_created_ts=account_created_ts,
            user_id=user_id, pending_messages=pending_messages,
        )
        # The giveaway is re-checked inside the statement itself. check_eligible
        # above reads a row that was fetched moments earlier, so an end() landing
        # in between would otherwise let the entry through; this makes that
        # impossible without a second round-trip.
        try:
            cur = self.db.execute(
                "INSERT INTO simple_entries (giveaway_id, user_id, username, entered_at)"
                " SELECT ?, ?, ?, ? WHERE EXISTS ("
                "   SELECT 1 FROM simple_giveaways WHERE id = ? AND status = 'active'"
                " )",
                (gw.id, user_id, username[:64], now_ms(), gw.id),
            )
        except Exception as exc:
            msg = str(exc).upper()
            if "UNIQUE" in msg or "PRIMARY" in msg or "CONSTRAINT" in msg:
                raise ServiceError("You are already entered.") from None
            raise
        try:
            inserted = int(cur.rowcount or 0)
        except (TypeError, ValueError):
            inserted = 1
        if inserted == 0:
            raise ServiceError("This giveaway has ended.")
        return self.entry_count(gw.id)

    def leave(self, giveaway_id: str, user_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM simple_entries WHERE giveaway_id = ? AND user_id = ?", (giveaway_id, user_id)
        )
        try:
            return (cur.rowcount or 0) > 0
        except Exception:
            return True

    # -- end / reroll / cancel ------------------------------------------
    def _pick(self, giveaway_id: str, n: int, exclude: list[str] | None = None) -> list[str]:
        # The exclusion set is built once per draw: rebuilding it for every row
        # made picking from a large giveaway quadratic for no reason.
        skipped = set(exclude or ())
        pool = [
            str(row["user_id"])
            for row in self.entries(giveaway_id)
            if str(row["user_id"]) not in skipped
        ]
        if not pool or n < 1:
            return []
        return self._rand.sample(pool, min(n, len(pool)))

    def end(self, giveaway_id: str) -> tuple[Giveaway, list[str]]:
        gw = self.get(giveaway_id)
        if not gw.active:
            return gw, list(gw.winners)
        winners = self._pick(gw.id, gw.winner_count)
        self.db.execute(
            "UPDATE simple_giveaways SET status = 'ended', ended_at = ?, winners_json = ? WHERE id = ?",
            (now_ms(), json.dumps(winners), gw.id),
        )
        # Fresh grind for the next giveaway: everybody's message count goes
        # back to zero, so the next min-messages requirement measures activity
        # *after* this giveaway, not lifetime activity.
        self.db.execute("DELETE FROM simple_message_counts")
        return self.get(gw.id), winners

    def reroll(self, giveaway_id: str, count: int = 1) -> tuple[Giveaway, list[str]]:
        gw = self.get(giveaway_id)
        if gw.active:
            raise ServiceError("End the giveaway before rerolling.")
        prev = list(gw.winners)
        fresh = self._pick(gw.id, count or gw.winner_count, exclude=prev)
        if not fresh:  # not enough fresh entrants left; draw from everyone
            fresh = self._pick(gw.id, count or gw.winner_count)
        combined = prev + [w for w in fresh if w not in prev]
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?", (json.dumps(combined), gw.id)
        )
        return self.get(gw.id), fresh

    def discard(self, giveaway_id: str) -> None:
        """Drop a giveaway that never reached Discord.

        Used when the announcement message could not be posted: the row would
        otherwise stay 'active' forever, auto-end on schedule, and try to
        announce winners into a channel that already refused the bot.
        """
        self.db.execute("DELETE FROM simple_entries WHERE giveaway_id = ?", (giveaway_id,))
        self.db.execute("DELETE FROM simple_giveaways WHERE id = ?", (giveaway_id,))

    def cancel(self, giveaway_id: str) -> Giveaway:
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("Only an active giveaway can be cancelled.")
        self.db.execute(
            "UPDATE simple_giveaways SET status = 'cancelled', ended_at = ? WHERE id = ?",
            (now_ms(), gw.id),
        )
        return self.get(gw.id)

    def set_message(self, giveaway_id: str, message_id: str) -> None:
        self.db.execute(
            "UPDATE simple_giveaways SET message_id = ? WHERE id = ?", (message_id, giveaway_id)
        )

    def extend(self, giveaway_id: str, extra_seconds: int) -> Giveaway:
        """Push the deadline back. Only on a running giveaway."""
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("Only a running giveaway can be extended.")
        if extra_seconds < 60 or extra_seconds > 60 * 86400:
            raise ServiceError("Extend by 1 minute to 60 days at a time.")
        self.db.execute(
            "UPDATE simple_giveaways SET ends_at = ends_at + ? WHERE id = ?",
            (extra_seconds * 1000, gw.id),
        )
        return self.get(gw.id)

    def set_entrants_role(self, giveaway_id: str, role_id: str | None) -> None:
        self.db.execute(
            "UPDATE simple_giveaways SET entrants_role_id = ? WHERE id = ?",
            (role_id, giveaway_id),
        )

    def ended_with_roles(self, limit: int = 200) -> list[Giveaway]:
        """Finished giveaways whose entrants role was never deleted.

        Restart recovery for the delayed role cleanup: anything listed here
        is a leftover whose 5-minute delete never ran.
        """
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status != 'active'"
            " AND entrants_role_id IS NOT NULL AND entrants_role_id != ''"
            " ORDER BY ended_at DESC LIMIT ?",
            (limit,),
        )
        return [Giveaway.from_row(r) for r in rows]

    #: How long join data (who entered) survives after a giveaway finishes.
    #: The giveaway record itself (prize, winners, status) is kept; only the
    #: per-user entry rows are wiped.
    ENTRY_RETENTION_MS = 5 * 3600 * 1000

    def wipe_stale_entries(self, now: int | None = None) -> int:
        """Delete entry rows for giveaways finished over 5h ago. Returns count."""
        cutoff = (now if now is not None else now_ms()) - self.ENTRY_RETENTION_MS
        cur = self.db.execute(
            "DELETE FROM simple_entries WHERE giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE status != 'active'"
            " AND ended_at IS NOT NULL AND ended_at <= ?)",
            (cutoff,),
        )
        try:
            return int(cur.rowcount or 0)
        except (TypeError, ValueError):
            return 0

    # -- blacklist ------------------------------------------------------
    def is_blacklisted(self, guild_id: str, user_id: str) -> bool:
        if not guild_id or not user_id:
            return False
        return (
            self.db.query_one(
                "SELECT user_id FROM simple_blacklist WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            is not None
        )

    def blacklist_add(self, guild_id: str, user_id: str) -> None:
        """Block a user, and purge their entries from running giveaways."""
        self.db.execute(
            "INSERT OR IGNORE INTO simple_blacklist (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        self.db.execute(
            "DELETE FROM simple_entries WHERE user_id = ? AND giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE guild_id = ? AND status = 'active')",
            (user_id, guild_id),
        )

    def blacklist_remove(self, guild_id: str, user_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM simple_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        try:
            return (cur.rowcount or 0) > 0
        except Exception:
            return True

    def blacklist_list(self, guild_id: str, limit: int = 100) -> list[str]:
        rows = self.db.query(
            "SELECT user_id FROM simple_blacklist WHERE guild_id = ?"
            " ORDER BY user_id ASC LIMIT ?",
            (guild_id, limit),
        )
        return [str(r["user_id"]) for r in rows]

    def due(self, now: int | None = None) -> list[Giveaway]:
        ts = now if now is not None else now_ms()
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active' AND ends_at <= ?"
            " ORDER BY ends_at ASC LIMIT 25",
            (ts,),
        )
        return [Giveaway.from_row(r) for r in rows]
