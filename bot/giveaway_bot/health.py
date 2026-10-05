"""Tiny HTTP server so the bot can run as a Render Web Service.

Render's free tier has no background workers, and a Web Service deploy times
out ("no open ports detected") unless the process binds a port. This binds
``$PORT`` (Render sets it; defaults to 10000 locally) and answers ``/`` and
``/health`` with 200 — nothing else. It runs in a daemon thread on plain
stdlib so it needs no dependencies and can never block the Discord loop.

Free-tier services also sleep after ~15 min without traffic: point a free
UptimeRobot monitor at ``https://<your-service>.onrender.com/health`` every
5 minutes and Render keeps the bot awake.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("giveaway_bot.health")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path in ("/", "/health"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass  # stay quiet; Render logs the requests itself


def start_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104 - Render requires all interfaces
    thread = threading.Thread(
        target=server.serve_forever, name="health-server", daemon=True
    )
    thread.start()
    log.info("health server listening on port %d", port)
    return server
