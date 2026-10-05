"""CLI: `python -m giveaway_bot run|doctor`."""

from __future__ import annotations

import argparse
import sys

from .bot import run_forever
from .config import get_settings
from .db import Database


def cmd_run(_: argparse.Namespace) -> int:
    run_forever(get_settings())
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = get_settings()
    problems: list[str] = []
    if not settings.bot_token:
        problems.append("DISCORD_BOT_TOKEN is not set.")
    if not settings.turso_url:
        problems.append(
            "TURSO_DATABASE_URL is not set. This bot is Turso-only so restarts"
            " never lose data — set it (plus TURSO_AUTH_TOKEN) and restart."
        )
    try:
        db = Database(settings)
        db.init_schema()
        print(f"database backend: {db.backend}")
        db.close()
    except Exception as exc:
        problems.append(f"database unreachable: {exc}")
    if problems:
        print("Problems:")
        for problem in problems:
            print(f"  [x] {problem}")
        return 1
    print("[ok] configuration looks good")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="giveaway-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the Discord bot").set_defaults(func=cmd_run)
    sub.add_parser("doctor", help="validate configuration").set_defaults(func=cmd_doctor)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
