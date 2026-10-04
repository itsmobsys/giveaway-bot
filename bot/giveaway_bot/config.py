"""Configuration loading for the giveaway bot.

All runtime configuration comes from environment variables (a local `.env` is
loaded automatically).  Values are validated once, at startup, by pydantic so a
misconfigured deployment fails loudly instead of silently misbehaving.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    #:
    #: The alias is load-bearing. This Settings sets no `env_prefix`, so
    #: pydantic-settings matches environment variables against the uppercased
    #: *field* name: this field would read `GIVEAWAY_CHANNEL_ID`. But
    #: .env.example, docs/DEPLOYMENT.md and the error raised from
    #: repositories/control -> queue.py all told operators to set
    #: `DISCORD_GIVEAWAY_CHANNEL_ID`, which was therefore silently ignored: the
    #: field came back empty and every create failed with "No giveaway channel is
    #: configured" however the variable was set. Both spellings are accepted so a
    #: deployment that discovered the working one keeps working.
    giveaway_channel_id: str = Field(
        default="",
        validation_alias=AliasChoices("DISCORD_GIVEAWAY_CHANNEL_ID", "GIVEAWAY_CHANNEL_ID"),
    )
    discord_client_id: str = ""
    discord_client_secret: str = Field(default="", repr=False)
    discord_redirect_uri: str = "http://localhost:3000/api/auth/callback"
    # `NoDecode` is load-bearing, not decoration. pydantic-settings otherwise
    # JSON-decodes any list-typed field straight out of the environment, so
    # `DISCORD_GUILD_ALLOWLIST=1,2` would fail to parse, and an operator who
    # followed .env.example and left it empty (`=`) would crash the bot at
    # startup - the decode happens before any validator can run. Suppressing it
    # hands the raw string to _split_csv, which is what the CSV syntax needs.
    guild_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    guild_blocklist: Annotated[list[str], NoDecode] = Field(default_factory=list)

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
    #: Rerolls allowed per giveaway. The count is of *rerolls*, so 1 permits one
    #: redraw. This used to be 0 with the check written as
    #: `if max_rerolls and total_draws >= max_rerolls`, which made 0 falsy and so
    #: unlimited - the shipped default and the documented value both meant "no
    #: limit", letting an owner reroll until a chosen entrant won, because every
    #: reroll mints a fresh seed. 0 now genuinely means no rerolls.
    max_rerolls_per_giveaway: int = Field(default=1, ge=0)
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
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Derived -----------------------------------------------------------
    migrations_dir: Path = Path(__file__).resolve().parents[2] / "shared" / "migrations"

    @field_validator("embed_color", mode="before")
    @classmethod
    def _parse_color(cls, value: object) -> object:
        """Accept a colour written the way it is written everywhere else.

        The default and .env.example both use ``0x7C5CFF``, which is how Discord
        colours are quoted, but pydantic's int type only accepts base-10. Without
        this an operator who copies .env.example - the documented first step -
        gets a bot that refuses to start.
        """
        if isinstance(value, str):
            text = value.strip().lstrip("#")
            try:
                return int(text, 0)  # base 0 -> honours 0x, 0o, 0b and decimal
            except ValueError:
                raise ValueError(
                    f"EMBED_COLOR must be an integer, optionally hex like 0x7C5CFF "
                    f"(got {value!r})"
                ) from None
        return value

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
        return not self.guild_allowlist or guild_id in self.guild_allowlist


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()