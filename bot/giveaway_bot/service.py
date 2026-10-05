"""Giveaway rules: create / join / leave / end / reroll. Nothing else."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

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
    blocked_role_id: str | None
    min_account_age_days: int
    min_messages: int
    image_url: str | None
    entrants_role_id: str | None
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
            blocked_role_id=str(row["blocked_role_id"]) if row.get("blocked_role_id") else None,
            min_account_age_days=int(row.get("min_account_age_days") or 0),
            min_messages=int(row.get("min_messages") or 0),
            image_url=str(row["image_url"]) if row.get("image_url") else None,
            entrants_role_id=str(row["entrants_role_id"]) if row.get("entrants_role_id") else None,
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
        blocked_role_id: str | None = None,
        min_account_age_days: int = 0,
        min_messages: int = 0,
        image_url: str | None = None,
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
        image_url = (image_url or "").strip() or None
        if image_url and (len(image_url) > 512 or not image_url.startswith(("http://", "https://"))):
            raise ServiceError("Image must be an http(s) URL.")
        gid = "gw_" + uuid.uuid4().hex[:12]
        created = now_ms()
        self.db.execute(
            "INSERT INTO simple_giveaways (id, guild_id, channel_id, prize, winner_count, ends_at,"
            " status, required_role_id, blocked_role_id, min_account_age_days, min_messages,"
            " image_url, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (
                gid, guild_id, channel_id, prize, winner_count, created + duration_seconds * 1000,
                required_role_id or None, blocked_role_id or None, max(0, min_account_age_days),
                max(0, min_messages), image_url, created_by, created,
            ),
        )
        return self.get(gid)

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
    ) -> None:
        if not gw.active:
            raise ServiceError("This giveaway has ended.")
        if gw.ends_at <= now_ms():
            raise ServiceError("This giveaway has ended.")
        roles = set(member_roles)
        if gw.required_role_id and gw.required_role_id not in roles:
            raise ServiceError("You need the required role to enter.")
        if gw.blocked_role_id and gw.blocked_role_id in roles:
            raise ServiceError("Your role is not allowed to enter.")
        if gw.min_account_age_days > 0 and account_created_ts:
            age_days = (datetime.now(UTC).timestamp() - account_created_ts) / 86400
            if age_days < gw.min_account_age_days:
                raise ServiceError(f"Account must be {gw.min_account_age_days}+ days old.")
        if gw.min_messages > 0:
            sent = self.message_count(gw.guild_id, user_id)
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
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + 1",
            (guild_id, user_id),
        )

    def join(
        self,
        gw: Giveaway,
        *,
        user_id: str,
        username: str,
        member_roles: list[str],
        account_created_ts: float | None,
    ) -> int:
        self.check_eligible(
            gw, member_roles=member_roles, account_created_ts=account_created_ts,
            user_id=user_id,
        )
        try:
            self.db.execute(
                "INSERT INTO simple_entries (giveaway_id, user_id, username, entered_at)"
                " VALUES (?, ?, ?, ?)",
                (gw.id, user_id, username[:64], now_ms()),
            )
        except Exception as exc:
            msg = str(exc).upper()
            if "UNIQUE" in msg or "PRIMARY" in msg or "CONSTRAINT" in msg:
                raise ServiceError("You are already entered.") from None
            raise
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
        rows = self.entries(giveaway_id)
        pool = [r["user_id"] for r in rows if not exclude or r["user_id"] not in set(exclude)]
        if not pool:
            return []
        n = min(n, len(pool))
        return self._rand.sample(pool, n)

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

    def set_entrants_role(self, giveaway_id: str, role_id: str | None) -> None:
        self.db.execute(
            "UPDATE simple_giveaways SET entrants_role_id = ? WHERE id = ?",
            (role_id, giveaway_id),
        )

    def due(self, now: int | None = None) -> list[Giveaway]:
        ts = now if now is not None else now_ms()
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active' AND ends_at <= ?"
            " ORDER BY ends_at ASC LIMIT 25",
            (ts,),
        )
        return [Giveaway.from_row(r) for r in rows]
