"""Command line interface.

    python -m giveaway_bot migrate        # apply pending SQL migrations
    python -m giveaway_bot selftest       # run the fairness + eligibility suite
    python -m giveaway_bot genvectors     # regenerate shared/test_vectors.json
    python -m giveaway_bot run            # start the Discord bot
    python -m giveaway_bot draw <id>      # (re)draw from the CLI - audited
    python -m giveaway_bot doctor         # validate configuration
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from pathlib import Path

from .config import get_settings
from .db import Database, get_database
from .logging_setup import configure_logging

log = logging.getLogger("giveaway_bot.cli")

ROOT = Path(__file__).resolve().parents[2]
VECTOR_PATH = ROOT / "shared" / "test_vectors.json"


def _safe_print(text: str) -> None:
    """Print ASCII-safe text.

    Windows consoles frequently default to cp1252, where the box/emoji
    characters used elsewhere in the UI raise UnicodeEncodeError and mask the
    real error message. Only the CLI needs this; the Discord renderer can use
    full Unicode because discord.py encodes explicitly.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def cmd_migrate(_: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    db = Database(settings)
    applied = db.migrate()
    _safe_print(f"backend: {db.backend}")
    if applied:
        _safe_print(f"applied {len(applied)} migration(s):")
        for name in applied:
            _safe_print(f"  + {name}")
    else:
        _safe_print("database is already up to date")
    return 0


def _describe_intents() -> str:
    """Which privileged intents this build asks for, and where to enable them.

    Read from the same helper the bot uses, so this cannot drift from what is
    actually requested at connect time. The portal setting is invisible from here,
    so it is surfaced as a warning rather than reported as a pass or a failure.
    """

    from .bot import build_intents

    intents = build_intents()
    needed = [name for name, on in (("Server Members Intent", intents.members),) if on]
    summary = ", ".join(needed) if needed else "none"
    message = f"{summary} (privileged)"
    if needed:
        message += (
            " - must be enabled at https://discord.com/developers/applications"
            " -> your app -> Bot -> Privileged Gateway Intents, or the connection"
            " is refused"
        )
    return message


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    problems: list[str] = []

    if not settings.discord_bot_token:
        problems.append("DISCORD_BOT_TOKEN is not set - the bot cannot log in.")
    if not settings.migrations_dir.is_dir():
        problems.append(f"migrations directory missing: {settings.migrations_dir}")
    if settings.uses_turso and not settings.turso_auth_token:
        problems.append("TURSO_DATABASE_URL is set but TURSO_AUTH_TOKEN is empty.")

    db = Database(settings)
    try:
        pending = [name for name, _ in db.pending_migrations()]
        applied = {row["filename"] for row in db.query("SELECT filename FROM schema_migrations")}
        missing = [name for name in pending if name not in applied]
        if missing:
            _safe_print(f"pending migrations: {', '.join(missing)}")
        else:
            _safe_print("migrations: up to date")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"database unreachable: {exc}")

    _safe_print(f"database backend: {db.backend}")
    _safe_print(f"dashboard url:    {settings.dashboard_url}")
    _safe_print(f"fairness freeze:  {settings.fairness_freeze_entries_on_draw}")
    _safe_print(f"gateway intents:  {_describe_intents()}")

    if problems:
        _safe_print("\nProblems:")
        for problem in problems:
            # ASCII markers: Windows consoles still default to cp1252, where
            # box-drawing/emoji characters raise UnicodeEncodeError.
            _safe_print(f"  [x] {problem}")
        return 1
    _safe_print("\n[ok] configuration looks good")
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    from .selftest import main as selftest_main

    return selftest_main()


def cmd_genvectors(_: argparse.Namespace) -> int:
    from .selftest import write_vectors

    path = write_vectors(VECTOR_PATH)
    _safe_print(f"wrote {path}")
    return 0


def cmd_draw(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    from .service import Actor, GiveawayService

    db = get_database()
    service = GiveawayService(db, settings)
    actor = Actor(args.actor, "cli", "cli")
    giveaway = service.get(args.giveaway_id)
    if args.reroll:
        outcome = service.reroll(actor, giveaway, reason=args.reason)
    else:
        outcome = service.end(actor, giveaway, reason=args.reason)
    if outcome is None:
        _safe_print("ended without a draw")
        return 0
    _safe_print(json.dumps(outcome.result.manifest(), indent=2)[:4000])
    if outcome.verification:
        _safe_print(f"verification ok: {outcome.verification['ok']}")
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)

    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env first.")
        return 1

    from .bot import GiveawayBot
    from .service import GiveawayService

    db = get_database()
    applied = db.migrate()
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))

    service = GiveawayService(db, settings)

    def _install_signal_handlers() -> None:
        """Make SIGTERM unwind the bot rather than kill the process.

        ``asyncio.run`` only arranges for SIGINT. Every container platform,
        orchestrator and panel sends SIGTERM, and with no handler for it the
        process died instantly on deploy: the gateway session was never closed,
        ``GiveawayBot.close()`` never ran, so buffered message activity was
        dropped and database connections were left to the garbage collector. It
        also left the in-flight queue command stuck in ``claimed``.

        Cancelling the tasks makes ``bot.start()`` raise CancelledError, which
        propagates through ``async with bot`` and runs ``close()``.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - always inside a loop here
            return

        def _cancel() -> None:
            log.info("signal received, shutting down")
            for task in asyncio.all_tasks(loop):
                task.cancel()

        # Windows has neither SIGTERM nor add_signal_handler; the in-process shim
        # still reaches KeyboardInterrupt there.
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, _cancel)

    async def _main() -> None:
        bot = GiveawayBot(service, db, settings)
        settings_ = settings
        if settings_.enable_control_api:
            # There is no giveaway_bot.api module in this build, and the settings
            # validate happily, so following the documentation produced a
            # ModuleNotFoundError traceback and a crash loop on every restart.
            # Fail with something actionable instead.
            raise RuntimeError(
                "ENABLE_CONTROL_API is set, but the signed HTTP control API is not "
                "included in this build. Unset ENABLE_CONTROL_API, or add "
                "bot/giveaway_bot/api.py. (CONTROL_API_* and the bot-side rate "
                "limit settings depend on it.)"
            )
        _install_signal_handlers()
        async with bot:
            await bot.start(settings.discord_bot_token)

    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):  # SIGINT or SIGTERM
        # CancelledError is a BaseException, so it escapes `except Exception`
        # below and has to be named here or the process exits with a traceback
        # on every ordinary deploy.
        log.info("shutting down")
    except Exception as exc:
        # Startup failures that are caused by configuration rather than by a bug
        # get an actionable message instead of a traceback. These two are by far
        # the most common first-run failures and neither is diagnosable from the
        # exception alone.
        hint = _explain_startup_failure(exc)
        if hint is None:
            raise
        _safe_print("")
        _safe_print(f"error: {hint}")
        return 1
    return 0


def _explain_startup_failure(exc: BaseException) -> str | None:
    """Turn a known Discord startup error into a fix, or None if unexplained.

    Kept separate from cmd_run so the self-test can assert the wording without
    opening a gateway connection.
    """
    import discord

    if isinstance(exc, discord.errors.PrivilegedIntentsRequired):
        return (
            "Discord rejected the connection: a privileged intent this bot needs is "
            "not enabled for the application.\n"
            "\n"
            "  Fix (about a minute, and only once per application):\n"
            "    1. Open https://discord.com/developers/applications\n"
            "    2. Pick this application -> Bot -> Privileged Gateway Intents\n"
            "    3. Turn ON 'Server Members Intent' and press Save\n"
            "    4. Restart this server\n"
            "\n"
            "  Server Members Intent is required because giveaway eligibility reads\n"
            "  a member's roles and their server join date. Without it those two rules\n"
            "  cannot be evaluated.\n"
            "\n"
            "  Message Content Intent is NOT required - this bot counts messages from\n"
            "  gateway events and never reads message text, so the 'privileged message\n"
            "  content intent is missing' warning above is expected and harmless."
        )

    if isinstance(exc, discord.errors.LoginFailure):
        return (
            "Discord rejected the bot token. Check DISCORD_BOT_TOKEN: it must be the\n"
            "bot token from the Bot tab (a 'Bot' prefix and two dots), not the client\n"
            "secret or an application ID, and it must not have been reset since."
        )

    if isinstance(exc, discord.errors.HTTPException):
        return (
            f"Discord returned an HTTP error while connecting: {exc}. This is usually\n"
            "network or DNS trouble on the host rather than a configuration problem."
        )

    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="giveaway-bot", description="Open-source giveaway bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("doctor", help="validate configuration and connectivity").set_defaults(func=cmd_doctor)
    sub.add_parser("selftest", help="run the fairness and eligibility test suite").set_defaults(
        func=cmd_selftest
    )
    sub.add_parser("genvectors", help="regenerate shared/test_vectors.json").set_defaults(
        func=cmd_genvectors
    )
    sub.add_parser("run", help="start the Discord bot").set_defaults(func=cmd_run)

    draw = sub.add_parser("draw", help="draw a giveaway from the CLI (audited)")
    draw.add_argument("giveaway_id")
    draw.add_argument("--reroll", action="store_true", help="reroll an already drawn giveaway")
    draw.add_argument("--reason", default="cli_draw")
    draw.add_argument("--actor", default="system", help="discord user id recorded in the audit log")
    draw.set_defaults(func=cmd_draw)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
