"""Simple configuration from environment variables. No dashboard, no extras."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or str(default))
    except ValueError:
        return default


def _color(name: str, default: int) -> int:
    """A colour setting as a 24-bit int, in any spelling people actually write.

    Accepts 0x7C5CFF (the documented form), #7C5CFF, 7C5CFF and plain decimal.
    Discord's colour field is 24 bits wide, so anything wider — an 8-digit ARGB
    value pasted in — is masked down instead of being sent for the API to
    reject. Anything unparseable keeps the default, as _int does.
    """
    raw = (os.getenv(name, "") or "").strip().lstrip("#")
    if not raw:
        return default
    for base in (0, 16):
        try:
            return int(raw, base) & 0xFFFFFF
        except ValueError:
            continue
    return default


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", "").strip())
    giveaway_channel_id: str = field(
        default_factory=lambda: (
            os.getenv("DISCORD_GIVEAWAY_CHANNEL_ID", "") or os.getenv("GIVEAWAY_CHANNEL_ID", "")
        ).strip()
    )
    turso_url: str = field(default_factory=lambda: os.getenv("TURSO_DATABASE_URL", "").strip())
    turso_token: str = field(default_factory=lambda: os.getenv("TURSO_AUTH_TOKEN", "").strip())
    tick_seconds: int = field(default_factory=lambda: _int("TICK_SECONDS", 30))
    #: Public dashboard URL (the Vercel frontend). Every giveaway message
    #: gets a blue "Dashboard" link button pointing at it; empty = no button.
    dashboard_url: str = field(
        default_factory=lambda: (
            os.getenv("DASHBOARD_URL", "") or "https://giveaway-bot-duggal.vercel.app/"
        ).strip()
    )
    #: Port for the built-in health server. Render sets PORT itself; this lets
    #: the bot run as a Web Service (free tier has no background workers).
    port: int = field(default_factory=lambda: _int("PORT", 10000))
    embed_color: int = 0x7C5CFF

    @property
    def uses_turso(self) -> bool:
        return bool(self.turso_url)


def get_settings() -> Settings:
    settings = Settings()
    settings.embed_color = _color("EMBED_COLOR", settings.embed_color)
    if settings.tick_seconds < 5:
        settings.tick_seconds = 5
    return settings
