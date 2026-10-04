"""Periodic liveness ping to the dashboard.

The bot is a long-lived worker and the dashboard is a web service, so the bot is
the only process here that is guaranteed to be running. Pinging the dashboard's
``/api/health`` every 30 seconds does two useful things at once: it keeps a
free-tier instance from being idled out, and it tells us when the dashboard has
gone away.

Deliberately tiny. No schema, no queue, no shared state beyond a failure counter.
If the ping fails it logs the transition once and then stays quiet until the
dashboard answers again, so an outage does not become two log lines a minute.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("giveaway_bot.healthcheck")

#: How often to ping. Comfortably inside the ~15 minute idle window a free-tier
#: web service uses before sleeping.
POLL_SECONDS = 30.0

#: Per-request timeout. Shorter than the interval so a hung dashboard cannot make
#: requests overlap.
TIMEOUT_SECONDS = 10.0


class DashboardHealth:
    """Tracks whether the dashboard is answering."""

    def __init__(self, base_url: str) -> None:
        self.url = base_url.rstrip("/") + "/api/health"
        self.consecutive_failures = 0

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0

    async def poll(self) -> bool:
        """Ping once. Returns True when the dashboard answered."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(self.url)
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - any failure means unreachable
            self.consecutive_failures += 1
            if self.consecutive_failures == 1:
                log.warning("dashboard health check failed: %s", exc)
            return False

        if self.consecutive_failures:
            log.info(
                "dashboard health check recovered after %d failure(s)",
                self.consecutive_failures,
            )
        self.consecutive_failures = 0
        return True

    def status(self) -> dict[str, Any]:
        """Small dict for logs or a status command."""
        return {
            "url": self.url,
            "healthy": self.healthy,
            "consecutive_failures": self.consecutive_failures,
        }