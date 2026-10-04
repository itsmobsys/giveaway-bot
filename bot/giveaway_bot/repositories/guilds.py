"""Guild and admin-cache repositories."""

from __future__ import annotations

from typing import Any

from ..config import is_admin_permissions
from ..db import Database, now_ms
from ..models import Guild

__all__ = [
    "get_admin_permissions",
    "get_guild",
    "is_admin_permissions",
    "list_admin_guilds",
    "list_guilds",
    "record_admin",
    "touch_guild",
    "upsert_guild",
]


def upsert_guild(
    db: Database,
    guild_id: str,
    *,
    name: str,
    icon_url: str | None = None,
    owner_id: str | None = None,
    member_count: int = 0,
    bot_present: bool = True,
) -> None:
    timestamp = now_ms()
    db.execute(
        """
        INSERT INTO guilds (id, name, icon_url, owner_id, member_count, bot_present,
                            synced_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          icon_url = excluded.icon_url,
          owner_id = excluded.owner_id,
          member_count = excluded.member_count,
          bot_present = excluded.bot_present,
          synced_at = excluded.synced_at,
          updated_at = excluded.updated_at
        """,
        (
            guild_id,
            name,
            icon_url,
            owner_id,
            member_count,
            int(bot_present),
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def touch_guild(db: Database, guild_id: str, *, bot_present: bool | None = None) -> None:
    if bot_present is None:
        db.execute("UPDATE guilds SET synced_at = ?, updated_at = ? WHERE id = ?",
                   (now_ms(), now_ms(), guild_id))
        return
    db.execute(
        "UPDATE guilds SET synced_at = ?, updated_at = ?, bot_present = ? WHERE id = ?",
        (now_ms(), now_ms(), int(bot_present), guild_id),
    )


def get_guild(db: Database, guild_id: str) -> Guild | None:
    row = db.query_one("SELECT * FROM guilds WHERE id = ?", (guild_id,))
    return Guild.from_row(row) if row else None


def list_guilds(db: Database, *, bot_present_only: bool = False) -> list[Guild]:
    sql = "SELECT * FROM guilds"
    if bot_present_only:
        sql += " WHERE bot_present = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return [Guild.from_row(row) for row in db.query(sql)]


def record_admin(
    db: Database,
    guild_id: str,
    user_id: str,
    permissions: int,
    *,
    username: str | None = None,
    source: str = "rest",
) -> None:
    """Cache a guild administrator's permission bitfield."""
    db.execute(
        """
        INSERT INTO guild_admins (guild_id, user_id, username, permissions, source, synced_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
          username = excluded.username,
          permissions = excluded.permissions,
          source = excluded.source,
          synced_at = excluded.synced_at
        """,
        (guild_id, user_id, username, permissions, source, now_ms()),
    )


def get_admin_permissions(db: Database, guild_id: str, user_id: str) -> int | None:
    row = db.query_one(
        "SELECT permissions, synced_at FROM guild_admins WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    if not row:
        return None
    return int(row["permissions"])


def list_admin_guilds(db: Database, user_id: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT g.id, g.name, g.icon_url, g.member_count, a.permissions, a.synced_at
        FROM guild_admins a
        JOIN guilds g ON g.id = a.guild_id
        WHERE a.user_id = ? AND a.permissions != 0 AND g.bot_present = 1
        ORDER BY g.name COLLATE NOCASE
        """,
        (user_id,),
    )