"""Tiny HTTP server so the bot can run as a Render Web Service.

Render free tier has no background workers, and a Web Service deploy times out
("no open ports detected") unless the process binds a port. This binds
``$PORT`` (Render sets it; defaults to 10000 locally) and answers ``/`` and
``/health`` with 200 — nothing else. It runs in a daemon thread on plain stdlib
so it needs no dependencies and can never block the Discord loop.

Free-tier services also sleep after ~15 min without traffic: point a free
UptimeRobot monitor at ``https://<your-service>.onrender.com/health`` every
5 minutes and Render keeps the bot awake.
"""

from __future__ import annotations

import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("giveaway_bot.health")

#: Paths answered with 200. Everything else 404s. A query string is ignored,
#: so /health?cachebust=1 still works for monitors that add one.
OK_PATHS = ("/", "/health")


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keeps the connection alive between polls and, with an explicit
    # Content-Length on every response (including 404 and HEAD), cannot hang a
    # client waiting for a body that never comes.
    protocol_version = "HTTP/1.1"
    #: A thread is spawned per connection, and keep-alive means a client that
    #: connects and then says nothing would park that thread until the process
    #: exits. http.server turns a socket timeout into a closed connection.
    timeout = 10

    def _respond(self, status: int, body: bytes, *, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _known_path(self) -> bool:
        return self.path.split("?", 1)[0] in OK_PATHS

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self._known_path():
            self._respond(200, b"ok")
        else:
            self._respond(404, b"not found")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        """Uptime monitors and load balancers often probe with HEAD first."""
        self._respond(200 if self._known_path() else 404, b"", head_only=True)

    def log_message(self, *args: object) -> None:
        pass  # stay quiet; Render logs the requests itself


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        """Do not dump a traceback when a client goes away mid-response.

        Uptime monitors hang up the moment they have their 200, so a reset here
        is routine; only a genuine fault is worth a stack trace.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            log.debug("health connection dropped: %s", exc)
            return
        log.exception("health server error")


def start_health_server(port: int) -> ThreadingHTTPServer:
    server = _Server(("0.0.0.0", port), _Handler)  # noqa: S104 - Render requires all interfaces
    thread = threading.Thread(
        target=server.serve_forever, name="health-server", daemon=True
    )
    thread.start()
    log.info("health server listening on port %d", port)
    return server
