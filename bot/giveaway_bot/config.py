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
    #: Port for the built-in health server. Render sets PORT itself; this lets
    #: the bot run as a Web Service (free tier has no background workers).
    port: int = field(default_factory=lambda: _int("PORT", 10000))
    embed_color: int = 0x7C5CFF

    @property
    def uses_turso(self) -> bool:
        return bool(self.turso_url)


def get_settings() -> Settings:
    raw = (os.getenv("EMBED_COLOR", "") or "").strip().lstrip("#")
    color = 0x7C5CFF
    if raw:
        try:
            color = int(raw, 0)
        except ValueError:
            pass
    settings = Settings()
    settings.embed_color = color
    if settings.tick_seconds < 5:
        settings.tick_seconds = 5
    return settings
