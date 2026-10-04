"""Structured logging setup (pretty for humans, JSON for production)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .config import Settings


class JsonFormatter(logging.Formatter):
    """One JSON object per line - friendly to Vercel/Datadog/Loki."""

    def __init__(self, service: str = "giveaway-bot") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for key in ("guild_id", "giveaway_id", "user_id", "command", "duration_ms", "error"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    COLOURS = {
        "DEBUG": "\033[90m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def __init__(self, *, colour: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)-28s %(message)s", "%H:%M:%S")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.colour and sys.stderr.isatty():
            prefix = self.COLOURS.get(record.levelname, "")
            return f"{prefix}{text}{self.RESET}"
        return text


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    if settings.log_json or not settings.log_pretty:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter(colour=sys.stdout.isatty()))

    root.addHandler(handler)

    # discord.py is extremely chatty at INFO.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("webview").setLevel(logging.WARNING)