"""Configuration loading for the giveaway bot.

All runtime configuration comes from environment variables (a local `.env` is
loaded automatically).  Values are validated once, at startup, by pydantic so a
misconfigured deployment fails loudly instead of silently misbehaving.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Discord permission bitfields we care about.
PERM_ADMINISTRATOR = 0x00000008
PERM_MANAGE_GUILD = 0x00000020
PERM_MANAGE_ROLES = 0x00000010
PERM_MANAGE_CHANNELS = 0x00000004
PERM_VIEW_CHANNEL = 0x00000400

#: Any of these bits makes a user a dashboard administrator for a guild.
ADMIN_PERMISSION_MASK = PERM_ADMINISTRATOR | PERM_MANAGE_GUILD

_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,25}$")


def is_admin_permissions(permissions: int) -> bool:
    """True when a raw Discord permission bitfield grants guild administration."""
    return bool(permissions & ADMIN_PERMISSION_MASK)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Discord -----------------------------------------------------------
    discord_bot_token: str = Field(default="", repr=False)
    #: Single channel every giveaway is posted in. The bot owns this channel and
    #: admins never pick one - see `DISCORD_GIVEAWAY_CHANNEL_ID` in .env.example.
    giveaway_channel_id: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = Field(default="", repr=False)
    discord_redirect_uri: str = "http://localhost:3000/api/auth/callback"
    guild_allowlist: list[str] = Field(default_factory=list)
    guild_blocklist: list[str] = Field(default_factory=list)

    # --- Database ----------------------------------------------------------
    turso_database_url: str = ""
    turso_auth_token: str = Field(default="", repr=False)
    sqlite_path: str = "./data/giveaways.db"

    # --- Bot behaviour -----------------------------------------------------
    command_prefix: str = "!"
    tick_interval_seconds: int = Field(default=30, ge=5, le=3600)
    max_embed_refresh_per_tick: int = Field(default=8, ge=0, le=50)
    queue_workers: int = Field(default=4, ge=1, le=32)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_pretty: bool = True
    log_json: bool = False

    # --- Fairness ----------------------------------------------------------
    fairness_freeze_entries_on_draw: bool = True
    max_rerolls_per_giveaway: int = Field(default=0, ge=0)
    audit_log_redact_contact_details: bool = True

    # --- Control API -------------------------------------------------------
    enable_control_api: bool = False
    control_api_host: str = "127.0.0.1"
    control_api_port: int = Field(default=8787, ge=1, le=65535)
    control_api_secret: str = Field(default="", repr=False)

    # --- Rate limiting -----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_max: int = Field(default=30, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    # --- Misc --------------------------------------------------------------
    dashboard_url: str = "http://localhost:3000"
    embed_color: int = 0x7C5CFF
    trusted_proxies: list[str] = Field(default_factory=list)

    # --- Derived -----------------------------------------------------------
    migrations_dir: Path = Path(__file__).resolve().parents[2] / "shared" / "migrations"

    @field_validator("guild_allowlist", "guild_blocklist", "trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("guild_allowlist", "guild_blocklist")
    @classmethod
    def _validate_snowflakes(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _SNOWFLAKE_RE.match(item):
                raise ValueError(f"{item!r} is not a valid Discord snowflake")
        return value

    @field_validator("giveaway_channel_id")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if not _SNOWFLAKE_RE.match(value):
            raise ValueError(
                "DISCORD_GIVEAWAY_CHANNEL_ID is not a valid Discord snowflake"
            )
        return value

    @field_validator("dashboard_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        if self.enable_control_api and len(self.control_api_secret) < 32:
            raise ValueError(
                "CONTROL_API_SECRET must be at least 32 characters when ENABLE_CONTROL_API=true"
            )
        return self

    @property
    def uses_turso(self) -> bool:
        return bool(self.turso_database_url)

    def guild_allowed(self, guild_id: str) -> bool:
        if guild_id in self.guild_blocklist:
            return False
        if self.guild_allowlist and guild_id not in self.guild_allowlist:
            return False
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()