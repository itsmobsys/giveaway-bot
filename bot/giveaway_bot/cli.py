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
import json
import logging
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

    async def _main() -> None:
        bot = GiveawayBot(service, db, settings)
        settings_ = settings
        if settings_.enable_control_api:
            from .api import start_control_api

            await start_control_api(settings_, bot)
        async with bot:
            await bot.start(settings.discord_bot_token)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        log.info("shutting down")
    return 0


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
