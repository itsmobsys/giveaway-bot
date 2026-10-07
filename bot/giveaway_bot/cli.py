"""CLI: `python -m giveaway_bot run|doctor`."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .bot import run_forever
from .config import get_settings
from .db import Database

#: A bot token is three dot-separated parts. Only used to warn, never to block:
#: the format has changed before and a false alarm must not stop a working bot.
TOKEN_PARTS = 3


def cmd_run(_: argparse.Namespace) -> int:
    run_forever(get_settings())
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = get_settings()
    problems: list[str] = []
    notes: list[str] = []

    if not settings.bot_token:
        problems.append("DISCORD_BOT_TOKEN is not set.")
    elif settings.bot_token.count(".") != TOKEN_PARTS - 1:
        notes.append(
            "DISCORD_BOT_TOKEN does not look like a bot token (expected three"
            " dot-separated parts). The bot would start and then fail to log in."
        )

    if not settings.turso_url:
        problems.append(
            "TURSO_DATABASE_URL is not set. This bot is Turso-only so restarts"
            " never lose data — set it (plus TURSO_AUTH_TOKEN) and restart."
        )
    else:
        if settings.turso_url.startswith("file:"):
            problems.append(
                "TURSO_DATABASE_URL is a file: URL, which Render wipes on every"
                " redeploy. Point it at the remote Turso database instead."
            )
        elif not settings.turso_url.startswith(("libsql://", "https://", "http://")):
            notes.append(
                "TURSO_DATABASE_URL does not start with libsql:// or https:// —"
                " check for a stray space or a copy-paste slip."
            )
        if not settings.turso_token:
            notes.append(
                "TURSO_AUTH_TOKEN is empty; a remote Turso URL needs one."
            )
        try:
            db = Database(settings)
        except Exception as exc:
            problems.append(f"database driver unavailable: {exc}")
        else:
            try:
                db.init_schema()
                row = db.query_one("SELECT COUNT(*) AS n FROM simple_giveaways")
                live = db.query_one(
                    "SELECT COUNT(*) AS n FROM simple_giveaways WHERE status = 'active'"
                )
                print(f"database backend: {db.backend}")
                print(f"giveaways on record: {(row or {}).get('n', 0)} ({(live or {}).get('n', 0)} active)")
            except Exception as exc:
                problems.append(f"database unreachable: {exc}")
            finally:
                db.close_all()

    print(f"tick: every {settings.tick_seconds}s")
    print(f"giveaway channel: {settings.giveaway_channel_id or 'the channel the command runs in'}")
    print(f"dashboard button: {settings.dashboard_url or 'disabled'}")

    for note in notes:
        print(f"  [!] {note}")
    if problems:
        print("Problems:")
        for problem in problems:
            print(f"  [x] {problem}")
        return 1
    print("[ok] configuration looks good")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="giveaway-bot")
    parser.add_argument("--version", action="version", version=f"giveaway-bot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the Discord bot").set_defaults(func=cmd_run)
    sub.add_parser("doctor", help="validate configuration").set_defaults(func=cmd_doctor)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
