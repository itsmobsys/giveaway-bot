"""Giveaway rules: create / join / leave / end / reroll. Nothing else."""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database, _is_ambiguous_loss

log = logging.getLogger("giveaway_bot.service")

DAY = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def _changed(cur: Any) -> bool:
    """True when a statement that must match a row actually matched one.

    rowcount is not part of every driver's contract, and a missing count reads
    as "did not match": for a status flip that means refusing to draw rather
    than drawing a second time.
    """
    try:
        return int(cur.rowcount or 0) > 0
    except (TypeError, ValueError):
        return False


#: Tag carried by the refusals a caller has to treat differently from a plain
#: "no": the member was stopped by the timed-out rule and their entry was
#: dropped, so anything granted on joining has to go with it.
TIMEOUT_BAN_KIND = "timeout_ban"


@dataclass
class PartialFlush(Exception):
    """A batched write stopped part way, after applied rows had landed.

    A batch is written in chunks that commit individually, so the rows before
    the failure are already stored. Retrying the whole batch would add them a
    second time; the caller hands back only the tail.
    """

    applied: int
    error: Exception


@dataclass
class ServiceError(Exception):
    message: str
    #: Optional machine-readable tag. Empty for an ordinary refusal.
    kind: str = ""


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
    #: Optional winner-claim window in seconds. 0 = disabled (legacy default).
    claim_timeout_seconds: int = 0

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def claim_enabled(self) -> bool:
        return self.claim_timeout_seconds > 0

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
            claim_timeout_seconds=int(row.get("claim_timeout_seconds") or 0),
        )


class GiveawayService:
    #: Hard bound on winners per giveaway. create() and reroll() both enforce it:
    #: the winner announcement has to fit in one Discord message.
    MAX_WINNERS = 25

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
        claim_timeout_seconds: int = 0,
    ) -> Giveaway:
        prize = prize.strip()
        if not prize or len(prize) > 256:
            raise ServiceError("Prize must be 1-256 characters.")
        claim_timeout_seconds = int(claim_timeout_seconds or 0)
        if claim_timeout_seconds < 0 or claim_timeout_seconds > 60 * 86400:
            raise ServiceError("Claim window must be 0 (off) or up to 60 days in seconds.")
        if winner_count < 1 or winner_count > self.MAX_WINNERS:
            raise ServiceError(f"Winner count must be 1-{self.MAX_WINNERS}.")
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
            " min_messages, image_url, created_by, created_at, host_id, host_name,"
            " claim_timeout_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gid, guild_id, channel_id, prize, winner_count, created + duration_seconds * 1000,
                role_ids[0] if role_ids else None, json.dumps(role_ids),
                blocked_role_id or None, max(0, min_account_age_days),
                max(0, min_messages), image_url, created_by, created,
                host_id or created_by, (host_name or "")[:64] or None,
                claim_timeout_seconds,
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

    def entries(self, giveaway_id: str, limit: int | None = None) -> list[dict]:
        """Entrants in entry order. Pass limit for a display path.

        The draw needs the whole pool, but a listing shows the first page only:
        a giveaway with tens of thousands of entries should not ship all of them
        over the wire (and allocate them) to print fifty mentions.
        """
        statement = (
            "SELECT user_id, username, entered_at FROM simple_entries"
            " WHERE giveaway_id = ? ORDER BY entered_at ASC"
        )
        if limit is None:
            return self.db.query(statement, (giveaway_id,))
        return self.db.query(statement + " LIMIT ?", (giveaway_id, max(1, int(limit))))

    def has_entry(self, giveaway_id: str, user_id: str) -> bool:
        """Whether this member is entered: one lookup, no rows loaded.

        The Participants panel shows a bounded window, so "am I in it" cannot be
        answered by scanning the rows it happens to hold.
        """
        if not user_id:
            return False
        return (
            self.db.query_one(
                "SELECT 1 AS one FROM simple_entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, user_id),
            )
            is not None
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
        timed_out: bool = False,
    ) -> None:
        if not gw.active:
            raise ServiceError("This giveaway has ended.")
        if user_id and self.is_blacklisted(gw.guild_id, user_id):
            raise ServiceError("🚫 You are blocked from giveaways in this server.")
        if gw.ends_at <= now_ms():
            raise ServiceError("This giveaway has ended.")
        # Discord's native timeout (/mute), and anyone still sitting out the
        # penalty that started from it. Deliberately placed after the "is this
        # giveaway still joinable" checks, so a stale button on an ended one
        # cannot hand out a penalty. This is the only check here that writes:
        # it also drops an entry the blocked member already had.
        self.check_timeout_ban(gw, user_id=user_id, timed_out=timed_out)
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

    def add_message_counts(self, rows: list[tuple[str, str, int]]) -> int:
        """Apply many (guild_id, user_id, delta) counts. Returns rows applied.

        One statement per flush instead of one per message is the entire point:
        N messages in the server cost ceil(N / MESSAGE_FLUSH_CHUNK) round-trips
        per flush interval rather than N immediate ones.

        Each chunk commits on its own, so a failure part way leaves the earlier
        chunks stored. The failure is raised as PartialFlush, which says how many
        rows landed: the caller hands back only the tail, because restoring the
        whole batch would write the committed head a second time.
        """
        applied = 0
        for start in range(0, len(rows), self.MESSAGE_FLUSH_CHUNK):
            chunk = rows[start : start + self.MESSAGE_FLUSH_CHUNK]
            # Snapshot first: if this chunk later fails ambiguously (a mid-write
            # "connection reset" db.execute will not replay), the snapshot
            # lets the retry requeue only what is still missing instead of the
            # whole chunk. One extra read per chunk, not per key.
            before = self._read_counts(chunk)
            values = ", ".join("(?, ?, ?)" for _ in chunk)
            params: list[Any] = []
            for guild_id, user_id, count in chunk:
                params.extend((guild_id, user_id, count))
            # Interpolated text is the placeholder list only: every value still
            # travels as a bound parameter, so there is nothing to inject.
            statement = f"INSERT INTO simple_message_counts (guild_id, user_id, count) VALUES {values}"  # noqa: S608
            try:
                self.db.execute(
                    statement
                    + " ON CONFLICT (guild_id, user_id) DO UPDATE SET count = count + excluded.count",
                    params,
                )
            except Exception as exc:
                if _is_ambiguous_loss(str(exc).lower()):
                    # The write may have landed before the response was lost.
                    # applied counts whole chunks and the caller requeues
                    # rows[applied:], so the reconciled shortfall replaces the
                    # chunk in place (padded with zero-deltas to keep the list
                    # length stable) and the retry adds each key once.
                    short = self._reconcile_chunk(chunk, before)
                    filler = [(g, u, 0) for g, u, _ in chunk[len(short):]]
                    rows[start : start + len(chunk)] = short + filler
                raise PartialFlush(applied, exc) from exc
            applied += len(chunk)
        return applied

    def _read_counts(
        self, chunk: list[tuple[str, str, int]]
    ) -> dict[tuple[str, str], int]:
        """Stored count per key before a flush chunk runs.

        Static per-key reads (no interpolated SQL): this runs once per chunk,
        and only its result matters when the chunk later fails ambiguously.
        Keys that cannot be read are left out — _reconcile_chunk requeues
        those whole rather than risking silent loss.
        """
        out: dict[tuple[str, str], int] = {}
        for guild_id, user_id, _ in chunk:
            try:
                row = self.db.query_one(
                    "SELECT count AS n FROM simple_message_counts"
                    " WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            except Exception:
                log.warning("flush snapshot read failed for %s/%s", guild_id, user_id)
                continue
            out[(guild_id, user_id)] = int((row or {}).get("n") or 0)
        return out

    def _reconcile_chunk(
        self, chunk: list[tuple[str, str, int]], before: dict[tuple[str, str], int]
    ) -> list[tuple[str, str, int]]:
        """Shortfall per key after an ambiguous chunk failure.

        `before` holds each key's count read just before the chunk ran. Whatever
        is already reflected in the table is not requeued, so retrying the flush
        cannot double-add the failed chunk. Keys that cannot be re-read are
        requeued whole: over-counting beats silently losing messages.
        """
        shortfall: list[tuple[str, str, int]] = []
        for guild_id, user_id, delta in chunk:
            try:
                row = self.db.query_one(
                    "SELECT count AS n FROM simple_message_counts"
                    " WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            except Exception:
                shortfall.append((guild_id, user_id, delta))
                continue
            stored = int((row or {}).get("n") or 0)
            if (guild_id, user_id) not in before:
                # No snapshot for this key (the pre-read failed): the stored
                # value cannot be compared, so requeue the whole delta rather
                # than risk silently losing messages.
                shortfall.append((guild_id, user_id, delta))
                continue
            missing = before[(guild_id, user_id)] + delta - stored
            if missing > 0:
                shortfall.append((guild_id, user_id, missing))
        return shortfall

    def join(
        self,
        gw: Giveaway,
        *,
        user_id: str,
        username: str,
        member_roles: list[str],
        account_created_ts: float | None,
        pending_messages: int = 0,
        timed_out: bool = False,
    ) -> int:
        self.check_eligible(
            gw, member_roles=member_roles, account_created_ts=account_created_ts,
            user_id=user_id, pending_messages=pending_messages, timed_out=timed_out,
        )
        # The giveaway and the blacklist are re-checked inside the statement
        # itself. check_eligible above reads rows fetched moments earlier, so an
        # end() or a blacklist_add landing in between would otherwise let the
        # entry through; this makes both impossible without a second round-trip.
        try:
            cur = self.db.execute(
                "INSERT INTO simple_entries (giveaway_id, user_id, username, entered_at)"
                " SELECT ?, ?, ?, ? WHERE EXISTS ("
                "   SELECT 1 FROM simple_giveaways WHERE id = ? AND status = 'active'"
                " ) AND NOT EXISTS ("
                "   SELECT 1 FROM simple_blacklist WHERE guild_id = ? AND user_id = ?"
                " )",
                (gw.id, user_id, username[:64], now_ms(), gw.id, gw.guild_id, user_id),
            )
        except Exception as exc:
            msg = str(exc).upper()
            # Only the duplicate-entry constraint means "already entered". Any
            # other constraint failure (NOT NULL, CHECK, ...) is a real bug and
            # must surface instead of being reported as a harmless duplicate.
            if "UNIQUE" in msg or "PRIMARY" in msg:
                raise ServiceError("You are already entered.") from None
            raise
        try:
            inserted = int(cur.rowcount or 0)
        except (TypeError, ValueError):
            inserted = 1
        if inserted == 0:
            # The guard can refuse for two reasons now, so say which one bit.
            if self.is_blacklisted(gw.guild_id, user_id):
                raise ServiceError("🚫 You are blocked from giveaways in this server.")
            raise ServiceError("This giveaway has ended.")
        return self.entry_count(gw.id)

    def leave(self, giveaway_id: str, user_id: str) -> bool:
        """Drop this member's entry. Only while the giveaway is running.

        Leaving an ended giveaway would rewrite who took part after the draw (and
        a cancelled one keeps its entries on purpose), so it reads as "not
        entered" there: the caller already reports False that way.
        """
        cur = self.db.execute(
            "DELETE FROM simple_entries WHERE giveaway_id = ? AND user_id = ? AND EXISTS ("
            " SELECT 1 FROM simple_giveaways WHERE id = ? AND status = 'active')",
            (giveaway_id, user_id, giveaway_id),
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
        """End a running giveaway and draw its winners.

        The draw happens first, then one UPDATE writes status, ended_at, the
        winners and the entrant count together, with "status = 'active'" in the
        WHERE clause as the claim: the auto-draw tick and /giveaway_end can fire
        in the same second, and only one of them may store (and announce) its
        draw. Whoever loses the claim is told the giveaway is already over. One
        statement also means a failure can no longer strand a giveaway as
        'ended' with no winners and no announcement.
        """
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("This giveaway has already ended.")
        pool = [str(row["user_id"]) for row in self.entries(gw.id)]
        winners = self._rand.sample(pool, min(gw.winner_count, len(pool))) if pool else []
        cur = self.db.execute(
            "UPDATE simple_giveaways SET status = 'ended', ended_at = ?, winners_json = ?,"
            " entrant_count = ? WHERE id = ? AND status = 'active'",
            (now_ms(), json.dumps(winners), len(pool), gw.id),
        )
        if not _changed(cur):
            raise ServiceError("This giveaway has already ended.")
        # Fresh grind for the next giveaway *in this server*: message counts go
        # back to zero here, so the next min-messages requirement measures
        # activity after this giveaway rather than lifetime activity. Scoped by
        # guild — wiping the whole table would silently zero every other
        # server's counts — and skipped while another running giveaway here
        # still has a min-messages requirement, which would otherwise be reset
        # under the members already grinding for it.
        self.db.execute(
            "DELETE FROM simple_message_counts WHERE guild_id = ? AND NOT EXISTS ("
            " SELECT 1 FROM simple_giveaways WHERE guild_id = ? AND status = 'active'"
            " AND min_messages > 0)",
            (gw.guild_id, gw.guild_id),
        )
        ended = self.get(gw.id)
        # Claim windows open here so the deadline survives restarts: rows are
        # durable, and the tick picks them up even if the process dies first.
        self.start_claims(ended, winners)
        return ended, winners

    def reroll(
        self, giveaway_id: str, count: int = 1, replace_user_id: str | None = None
    ) -> tuple[Giveaway, list[str]]:
        gw = self.get(giveaway_id)
        if gw.active:
            raise ServiceError("End the giveaway before rerolling.")
        if gw.status != "ended":
            # A cancelled giveaway keeps its entries, so without this a reroll
            # would hand out prizes for a giveaway the server called off.
            raise ServiceError("A cancelled giveaway cannot be rerolled.")
        if replace_user_id is not None:
            return self._reroll_replace(gw, str(replace_user_id))
        # The whole list has to stay inside MAX_WINNERS. create() bounds the first
        # draw, but a reroll only ever adds, so repeated rerolls used to grow the
        # announcement past what Discord will accept — and the post then failed.
        prev = list(gw.winners)
        room = self.MAX_WINNERS - len(prev)
        if room < 1:
            raise ServiceError(
                f"This giveaway already names {len(prev)} winners, the most one"
                " announcement can hold."
            )
        wanted = max(1, min(int(count or gw.winner_count), room))
        # Claim-aware exclusion: winners_json holds every manual draw,
        # simple_claims holds every auto draw (expired/claimed/skipped). Union
        # both so a manual /reroll can never re-pick someone the claim timer
        # already drew, and a later expiry can never re-pick this reroll.
        drawn: set[str] = set(prev)
        for r in self.db.query(
            "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (gw.id,)
        ):
            drawn.add(str(r.get("user_id")))
        fresh = self._pick(gw.id, wanted, exclude=sorted(drawn))
        if not fresh:
            # Redrawing from everyone would announce earlier winners as new
            # ones (or nobody at all once the entries were wiped): say so.
            raise ServiceError("No other entrants left to reroll")
        combined = prev + [w for w in fresh if w not in prev]
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?", (json.dumps(combined), gw.id)
        )
        ended = self.get(gw.id)
        # Open a claim window for the fresh winners. No-op when the giveaway
        # has no claim timer; INSERT OR IGNORE keeps a retried announce safe.
        # The old winner's pending row is left alone: a manual reroll is
        # additive, and its own deadline still expires/skips independently.
        self.start_claims(ended, fresh)
        return ended, fresh

    def _reroll_replace(self, gw: Giveaway, replace_user_id: str) -> tuple[Giveaway, list[str]]:
        """Swap one named winner for a fresh entrant. Returns (giveaway, [new]).

        Targeted /reroll: the admin picks which winner loses their slot (e.g.
        C never claimed while A and B did). The old winner is swapped out in
        place so the winners list never grows, and their pending claim (if any)
        is closed as `replaced` so the tick can never expire it into a duplicate
        slot. The replacement gets its own claim window. Never re-picks anyone
        ever drawn (winners_json + every simple_claims row, any status).
        """
        prev = list(gw.winners)
        if replace_user_id not in prev:
            raise ServiceError("That member is not a winner of this giveaway.")
        drawn: set[str] = set(prev)
        for r in self.db.query(
            "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (gw.id,)
        ):
            drawn.add(str(r.get("user_id")))
        fresh = self._pick(gw.id, 1, exclude=sorted(drawn))
        if not fresh:
            raise ServiceError("No other entrants left to reroll")
        new_user = fresh[0]
        # Swap in place: first matching slot only, order preserved.
        combined = [new_user if w == replace_user_id else w for w in prev]
        # Defensive: if the winners list somehow held the old id twice, only
        # the first slot swaps and the rest stay (never duplicate the new id).
        seen_new = False
        fixed: list[str] = []
        for w in combined:
            if w == new_user:
                if seen_new:
                    fixed.append(replace_user_id)
                    continue
                seen_new = True
            fixed.append(w)
        combined = fixed
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?", (json.dumps(combined), gw.id)
        )
        # Close the old slot's pending claim so a later tick cannot expire it
        # into a second replacement for the same slot. Any status counts as
        # "ever drawn", so the exclusion holds even after the swap.
        self.db.execute(
            "UPDATE simple_claims SET status = ?"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
            (self.CLAIM_REPLACED, gw.id, replace_user_id),
        )
        row = self.db.query_one(
            "SELECT user_id FROM simple_claims WHERE giveaway_id = ? AND user_id = ? LIMIT 1",
            (gw.id, replace_user_id),
        )
        if row is None:
            # No claim row at all (e.g. claims disabled): leave a `replaced`
            # marker so this user stays excluded from every future draw.
            self.db.execute(
                "INSERT OR IGNORE INTO simple_claims"
                " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
                " VALUES (?, ?, 0, ?, 0, ?)",
                (gw.id, replace_user_id, self.CLAIM_REPLACED, now_ms()),
            )
        ended = self.get(gw.id)
        # Fresh winner's own claim window. No-op when claims are disabled.
        self.start_claims(ended, [new_user])
        return ended, [new_user]

    # -- winner claims (optional claim timer) -----------------------------
    #: Claim row statuses. `pending` rows hold a deadline; the rest are final.
    CLAIM_PENDING = "pending"
    CLAIM_CLAIMED = "claimed"
    CLAIM_EXPIRED = "expired"
    CLAIM_SKIPPED = "skipped"
    #: A pending claim closed by a targeted /reroll (replaced, not expired).
    CLAIM_REPLACED = "replaced"

    def start_claims(self, gw: Giveaway, winners: list[str], now: int | None = None) -> None:
        """Open a claim window for freshly drawn winners. Idempotent.

        Called once per draw (end, and each claim-timeout replacement). Rows
        are INSERT OR IGNORE so a retried announce never doubles a deadline.
        No-op when the giveaway has no claim window configured.
        """
        if not gw.claim_enabled or not winners:
            return
        ts = now if now is not None else now_ms()
        deadline = ts + gw.claim_timeout_seconds * 1000
        for user_id in winners:
            self.db.execute(
                "INSERT OR IGNORE INTO simple_claims"
                " (giveaway_id, user_id, round, status, deadline_ms, created_at)"
                " VALUES (?, ?, 0, 'pending', ?, ?)",
                (gw.id, str(user_id), deadline, ts),
            )

    def pending_claims(self, giveaway_id: str) -> list[dict]:
        """Pending claim rows for a giveaway, oldest deadline first."""
        return self.db.query(
            "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at,"
            " skipped_by, skipped_at, created_at FROM simple_claims"
            " WHERE giveaway_id = ? AND status = 'pending' ORDER BY deadline_ms ASC",
            (giveaway_id,),
        )

    def claim_status(self, giveaway_id: str) -> list[dict]:
        """Every claim row for a giveaway (for the dashboard/admin view)."""
        return self.db.query(
            "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at,"
            " skipped_by, skipped_at, created_at FROM simple_claims"
            " WHERE giveaway_id = ? ORDER BY created_at ASC",
            (giveaway_id,),
        )

    def due_claims(self, now: int | None = None, limit: int = 25) -> list[dict]:
        """Pending claims past their deadline. Tick feeds on this."""
        ts = now if now is not None else now_ms()
        return self.db.query(
            "SELECT giveaway_id, user_id, round, status, deadline_ms, claimed_at,"
            " skipped_by, skipped_at, created_at FROM simple_claims"
            " WHERE status = 'pending' AND deadline_ms <= ?"
            " ORDER BY deadline_ms ASC LIMIT ?",
            (ts, max(1, min(int(limit), 100))),
        )

    def claim(self, giveaway_id: str, user_id: str) -> dict:
        """Claim a prize as its drawn winner. Returns the claim row.

        Guarded UPDATE: only a pending row for this exact (giveaway, winner)
        flips, so double-clicks and two processes racing both collapse into one
        success and one "already claimed" refusal.
        """
        cur = self.db.execute(
            "UPDATE simple_claims SET status = 'claimed', claimed_at = ?"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
            (now_ms(), giveaway_id, str(user_id)),
        )
        if not _changed(cur):
            row = self.db.query_one(
                "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
                " ORDER BY round DESC LIMIT 1",
                (giveaway_id, str(user_id)),
            )
            if row is None:
                raise ServiceError("Only the drawn winner can claim this prize.")
            status = str(row.get("status") or "")
            if status == self.CLAIM_CLAIMED:
                raise ServiceError("This prize was already claimed.")
            if status == self.CLAIM_SKIPPED:
                raise ServiceError("This prize was already verified by staff.")
            if status == self.CLAIM_REPLACED:
                raise ServiceError("This winner was replaced by a reroll.")
            raise ServiceError("This claim window has closed.")
        row = self.db.query_one(
            "SELECT * FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
            " AND status = 'claimed' ORDER BY round DESC LIMIT 1",
            (giveaway_id, str(user_id)),
        )
        return dict(row or {})

    def skip_claim(self, giveaway_id: str, user_id: str, staff_id: str) -> dict:
        """Mark a pending claim manually verified (ticket path). No replacement.

        Guarded like claim(): only a pending row flips, so a second /skipclaim
        (or a claim racing it) gets a clear refusal instead of a double write.
        """
        if not staff_id:
            raise ServiceError("A staff member must verify the claim.")
        cur = self.db.execute(
            "UPDATE simple_claims SET status = 'skipped', skipped_by = ?, skipped_at = ?"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending'",
            (str(staff_id), now_ms(), giveaway_id, str(user_id)),
        )
        if not _changed(cur):
            row = self.db.query_one(
                "SELECT status FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
                " ORDER BY round DESC LIMIT 1",
                (giveaway_id, str(user_id)),
            )
            if row is None:
                raise ServiceError("No pending claim for that member in this giveaway.")
            status = str(row.get("status") or "")
            if status == self.CLAIM_CLAIMED:
                raise ServiceError("That prize was already claimed.")
            if status == self.CLAIM_SKIPPED:
                raise ServiceError("That claim was already verified by staff.")
            if status == self.CLAIM_REPLACED:
                raise ServiceError("That winner was replaced by a reroll.")
            raise ServiceError("That claim window has already closed.")
        row = self.db.query_one(
            "SELECT * FROM simple_claims WHERE giveaway_id = ? AND user_id = ?"
            " AND status = 'skipped' ORDER BY round DESC LIMIT 1",
            (giveaway_id, str(user_id)),
        )
        return dict(row or {})

    def expire_claim(
        self, giveaway_id: str, user_id: str, now: int | None = None
    ) -> tuple[Giveaway, list[str]]:
        """Expire one pending claim and draw its replacement. Atomic-ish.

        The guarded UPDATE is the concurrency gate: whoever flips pending ->
        expired owns the replacement draw, and a racing tick/claim/skip sees a
        non-pending row and stops. The replacement reuses _pick with every
        ever-drawn user excluded, so nobody is ever selected twice. Returns the
        giveaway and the (possibly empty) replacement list.
        """
        gw = self.get(giveaway_id)
        if gw.active:
            raise ServiceError("This giveaway is still running.")
        ts = now if now is not None else now_ms()
        cur = self.db.execute(
            "UPDATE simple_claims SET status = 'expired'"
            " WHERE giveaway_id = ? AND user_id = ? AND status = 'pending' AND deadline_ms <= ?",
            (giveaway_id, str(user_id), ts),
        )
        if not _changed(cur):
            raise ServiceError("That claim is no longer pending.")
        drawn = [
            str(r.get("user_id"))
            for r in self.db.query(
                "SELECT user_id FROM simple_claims WHERE giveaway_id = ?", (giveaway_id,)
            )
        ]
        fresh = self._pick(gw.id, 1, exclude=drawn)
        if not fresh:
            return self.get(gw.id), []
        combined = list(gw.winners) + [w for w in fresh if w not in gw.winners]
        self.db.execute(
            "UPDATE simple_giveaways SET winners_json = ? WHERE id = ?",
            (json.dumps(combined), gw.id),
        )
        ended = self.get(gw.id)
        self.start_claims(ended, fresh, now=ts)
        return ended, fresh

    def discard(self, giveaway_id: str) -> None:
        """Drop a giveaway that never reached Discord.

        Used when the announcement message could not be posted: the row would
        otherwise stay 'active' forever, auto-end on schedule, and try to
        announce winners into a channel that already refused the bot.
        """
        self.db.execute("DELETE FROM simple_claims WHERE giveaway_id = ?", (giveaway_id,))
        self.db.execute("DELETE FROM simple_entries WHERE giveaway_id = ?", (giveaway_id,))
        self.db.execute("DELETE FROM simple_giveaways WHERE id = ?", (giveaway_id,))

    def cancel(self, giveaway_id: str) -> Giveaway:
        gw = self.get(giveaway_id)
        if not gw.active:
            raise ServiceError("Only an active giveaway can be cancelled.")
        # Guarded like end(): an end() or a second cancel landing between the
        # read above and this write must not be overwritten.
        cur = self.db.execute(
            "UPDATE simple_giveaways SET status = 'cancelled', ended_at = ?,"
            " entrant_count = (SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?)"
            " WHERE id = ? AND status = 'active'",
            (now_ms(), gw.id, gw.id),
        )
        if not _changed(cur):
            raise ServiceError("Only an active giveaway can be cancelled.")
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
        cur = self.db.execute(
            "UPDATE simple_giveaways SET ends_at = ends_at + ? WHERE id = ? AND status = 'active'",
            (extra_seconds * 1000, gw.id),
        )
        if not _changed(cur):
            raise ServiceError("Only a running giveaway can be extended.")
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
            # ended_at was added to a table that already held data, so giveaways
            # that finished before that migration carry NULL forever; created_at
            # is the fallback that still lets their entry rows expire.
            "DELETE FROM simple_entries WHERE giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE status != 'active'"
            " AND COALESCE(ended_at, created_at) <= ?"
            # A pending claim still needs its entry pool for the replacement
            # draw, so its giveaway is exempt until the claim settles.
            " AND NOT EXISTS (SELECT 1 FROM simple_claims"
            " WHERE simple_claims.giveaway_id = simple_giveaways.id"
            " AND simple_claims.status = 'pending'))",
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

    def count_blacklisted(self, guild_id: str) -> int:
        """How many members of this guild are blocked from giveaways.

        The listing is capped, so this is what keeps a caller from reporting the
        size of the page set as if it were the whole blacklist.
        """
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_blacklist WHERE guild_id = ?", (guild_id,)
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    def blacklist_list(self, guild_id: str, limit: int = 100) -> list[str]:
        rows = self.db.query(
            "SELECT user_id FROM simple_blacklist WHERE guild_id = ?"
            " ORDER BY user_id ASC LIMIT ?",
            (guild_id, limit),
        )
        return [str(r["user_id"]) for r in rows]

    # -- timed-out penalty (Discord's native /mute) ----------------------
    #: Giveaways a member sits out after being caught timed out while joining.
    #: Small on purpose: this is a cooldown, not a blacklist.
    TIMEOUT_PENALTY = 3

    def timeout_ban_remaining(self, guild_id: str, user_id: str) -> int:
        """Giveaways this member still has to sit out. 0 means they may enter."""
        if not guild_id or not user_id:
            return 0
        row = self.db.query_one(
            "SELECT giveaways_remaining FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is None:
            return 0
        try:
            return max(0, int(row["giveaways_remaining"]))
        except (TypeError, ValueError):
            return 0

    def apply_timeout_penalty(
        self,
        guild_id: str,
        user_id: str,
        *,
        giveaways: int = TIMEOUT_PENALTY,
        giveaway_id: str | None = None,
    ) -> int:
        """Start the penalty for a member caught timed out. Returns what is left.

        MAX() rather than a plain overwrite is the "never stack" rule made
        durable: somebody already sitting out three giveaways stays at three no
        matter how often they are caught timed out in the meantime. The
        giveaway they were caught in is remembered as the one already counted,
        so hammering the same Join button cannot burn the penalty down.
        """
        count = max(1, int(giveaways))
        ts = now_ms()
        # Hits from giveaways that are over can never refuse anyone again, so
        # each new penalty starts by dropping this member's stale ones.
        self.db.execute(
            "DELETE FROM simple_giveaway_ban_hits WHERE guild_id = ? AND user_id = ?"
            " AND giveaway_id NOT IN (SELECT id FROM simple_giveaways WHERE status = 'active')",
            (guild_id, user_id),
        )
        if giveaway_id:
            # The trigger giveaway is recorded but never spends a unit, which is
            # what keeps hammering its Join button from burning the penalty.
            self._record_ban_hit(guild_id, user_id, giveaway_id)
        self.db.execute(
            "INSERT INTO simple_giveaway_bans"
            " (guild_id, user_id, giveaways_remaining, last_blocked_giveaway_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET"
            " giveaways_remaining = MAX(giveaways_remaining, excluded.giveaways_remaining),"
            " last_blocked_giveaway_id = excluded.last_blocked_giveaway_id,"
            " updated_at = excluded.updated_at",
            (guild_id, user_id, count, giveaway_id, ts, ts),
        )
        return self.timeout_ban_remaining(guild_id, user_id)

    def _record_ban_hit(self, guild_id: str, user_id: str, giveaway_id: str) -> bool:
        """Remember a giveaway this member was blocked from. True when new.

        The primary key makes it idempotent, and rowcount says whether this
        call was the one that inserted it: that is the "count once" test, safe
        against two clicks racing each other.
        """
        cur = self.db.execute(
            "INSERT OR IGNORE INTO simple_giveaway_ban_hits (guild_id, user_id, giveaway_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (guild_id, user_id, giveaway_id, now_ms()),
        )
        return _changed(cur)

    def _was_blocked_from(self, guild_id: str, user_id: str, giveaway_id: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 AS one FROM simple_giveaway_ban_hits"
                " WHERE guild_id = ? AND user_id = ? AND giveaway_id = ?",
                (guild_id, user_id, giveaway_id),
            )
            is not None
        )

    def consume_timeout_ban(self, guild_id: str, user_id: str, giveaway_id: str) -> int:
        """Spend one giveaway of the penalty. Returns what is left (0 = lifted).

        Every giveaway already held against this member is remembered (not just
        the last one), so only a giveaway never seen before spends a unit:
        alternating A, B, A cannot burn the penalty down. The penalty row is
        deleted as soon as the counter reaches zero; the remembered giveaways
        stay, so the ones they sat out keep refusing them while still running.
        """
        if self._record_ban_hit(guild_id, user_id, giveaway_id):
            # last_blocked_giveaway_id is still honoured for rows written before
            # the hits table existed, whose trigger giveaway has no hit row.
            self.db.execute(
                "UPDATE simple_giveaway_bans SET giveaways_remaining = giveaways_remaining - 1,"
                " last_blocked_giveaway_id = ?, updated_at = ?"
                " WHERE guild_id = ? AND user_id = ? AND giveaways_remaining > 0"
                " AND (last_blocked_giveaway_id IS NULL OR last_blocked_giveaway_id != ?)",
                (giveaway_id, now_ms(), guild_id, user_id, giveaway_id),
            )
        left = self.timeout_ban_remaining(guild_id, user_id)
        if left > 0:
            return left
        self.db.execute(
            "DELETE FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND user_id = ? AND giveaways_remaining <= 0",
            (guild_id, user_id),
        )
        return 0

    def count_timeout_bans(self, guild_id: str) -> int:
        """How many members of this guild are sitting out a penalty right now.

        The listing is capped, so the count is what lets a caller say how many
        it did not show instead of quietly reporting a short total.
        """
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND giveaways_remaining > 0",
            (guild_id,),
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    def list_timeout_bans(self, guild_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Members of this guild still sitting out a penalty, longest first.

        Only this table and only this guild: the dashboard, the entry list and
        the winner draw never read it. A zero row cannot normally exist (it is
        deleted on the way to zero) but is filtered out anyway, so a row written
        by hand cannot show a member who is actually free to enter. The limit is
        a ceiling on one response, so pair it with count_timeout_bans() when the
        caller has to say whether anything was left out.
        """
        rows = self.db.query(
            "SELECT user_id, giveaways_remaining, created_at FROM simple_giveaway_bans"
            " WHERE guild_id = ? AND giveaways_remaining > 0"
            " ORDER BY giveaways_remaining DESC, user_id ASC LIMIT ?",
            (guild_id, max(1, min(int(limit), 500))),
        )
        banned: list[dict[str, Any]] = []
        for row in rows:
            try:
                remaining = max(0, int(row["giveaways_remaining"]))
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                continue
            banned.append(
                {
                    "user_id": str(row["user_id"]),
                    "giveaways_remaining": remaining,
                    "created_at": int(row.get("created_at") or 0),
                }
            )
        return banned

    def check_timeout_ban(self, gw: Giveaway, *, user_id: str, timed_out: bool = False) -> None:
        """Refuse a timed-out member — or one still serving that penalty.

        Called by check_eligible() only once the giveaway is known to still be
        joinable, so a stale button never hands out a penalty.

        A member who is already sitting out the penalty spends one giveaway
        here: the giveaway they are blocked from *is* one of the ones they have
        to sit out. The timed-out test is skipped while a penalty is running,
        which is what stops a second penalty stacking on the first. Either way
        an entry they already had is dropped rather than left to win.
        """
        if not user_id:
            return
        left = self.timeout_ban_remaining(gw.guild_id, user_id)
        if left <= 0 and not timed_out:
            # The penalty is over, but a giveaway it was served on is still one
            # they sat out: rejoining it (say, the one that spent the last unit)
            # would hand the penalty straight back.
            if self._was_blocked_from(gw.guild_id, user_id, gw.id):
                raise ServiceError(
                    "You sat out this giveaway as part of your timed-out penalty.",
                    kind=TIMEOUT_BAN_KIND,
                )
            return
        # Blocked, so they must not be in a giveaway at all: entries made before
        # Discord timed them out (or before the penalty started) are dropped from
        # every running giveaway in the server, exactly as blacklist_add does.
        # Dropping only the one they clicked would let them win the giveaway
        # they joined yesterday while sitting out this one.
        self.db.execute(
            "DELETE FROM simple_entries WHERE user_id = ? AND giveaway_id IN"
            " (SELECT id FROM simple_giveaways WHERE guild_id = ? AND status = 'active')",
            (user_id, gw.guild_id),
        )
        if left > 0:
            left = self.consume_timeout_ban(gw.guild_id, user_id, gw.id)
            if left > 0:
                raise ServiceError(
                    "You are sitting out a penalty for joining while timed out."
                    f" {left} giveaway(s) left.",
                    kind=TIMEOUT_BAN_KIND,
                )
            raise ServiceError(
                "That was the last giveaway of your timed-out penalty —"
                " you can enter the next one.",
                kind=TIMEOUT_BAN_KIND,
            )
        left = self.apply_timeout_penalty(gw.guild_id, user_id, giveaway_id=gw.id)
        raise ServiceError(
            "You are currently timed out in this server, so you cannot enter giveaways."
            f" Timeout penalty: sit out the next {left} giveaway(s).",
            kind=TIMEOUT_BAN_KIND,
        )

    def due(self, now: int | None = None) -> list[Giveaway]:
        ts = now if now is not None else now_ms()
        rows = self.db.query(
            "SELECT * FROM simple_giveaways WHERE status = 'active' AND ends_at <= ?"
            " ORDER BY ends_at ASC LIMIT 25",
            (ts,),
        )
        return [Giveaway.from_row(r) for r in rows]
