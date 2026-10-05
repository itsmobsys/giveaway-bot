"""Repository-root entrypoint for hosts that start a Python app from a file.

Silly Development's panel runs ``PY_FILE`` from the repository root, which points
here. The bot package lives one level down in ``bot/`` and is normally started as
``python -m giveaway_bot run``, so this shim bridges the two. It adds no
configuration and no behaviour of its own - it only puts ``bot/`` on the import
path and calls the same entrypoint everything else uses.

Why the bot runs in this process
--------------------------------
Both requirements are met by there being only one process, rather than by
forwarding anything:

* **Signals.** SIGTERM and SIGINT are delivered by the kernel straight to the bot,
  because the bot is this process. A wrapper that spawned a child would have to
  catch each signal and re-send it, and a signal arriving before the handler was
  installed - or between ``Popen`` returning and the wait starting - would be
  lost, leaving an orphaned bot holding a gateway connection.
* **Exit code.** ``cli.main`` returns the status, which this script passes to
  ``sys.exit``, so the panel sees the bot's own code.

The obvious alternative, ``os.execve``, is *not* portable. A real ``exec`` is the
better mechanism on Linux, but CPython emulates ``os.exec*`` on Windows and does
not propagate the child's status, so the shim would exit 0 on every crash. Since
a dev machine is a normal place to run ``python app.py``, that failure mode is
worse than having no shim at all.

Arguments are forwarded, so this doubles as a normal CLI::

    python app.py             # starts the bot (same as `run`)
    python app.py doctor
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Repository root, derived from this file rather than the working directory, so
#: the panel can start the app from anywhere.
ROOT = Path(__file__).resolve().parent

#: Where the bot package lives. Added to sys.path rather than changing directory,
#: because a relative SQLITE_PATH would otherwise resolve somewhere new and
#: quietly point at a different database.
BOT_DIR = ROOT / "bot"

#: Existence check target, so a missing checkout fails with a readable message
#: instead of a bare ImportError.
BOT_CLI = BOT_DIR / "giveaway_bot" / "cli.py"


def main() -> int:
    if not BOT_CLI.exists():
        print(f"error: cannot find the bot package at {BOT_CLI}", file=sys.stderr)
        print("       expected giveaway_bot/ inside bot/", file=sys.stderr)
        return 1

    # Insert, not append, so this checkout wins over any giveaway_bot that happens
    # to be installed in site-packages - a stale copy would silently run instead.
    sys.path.insert(0, str(BOT_DIR))

    from giveaway_bot.cli import main as bot_main

    # A panel starts the app with no arguments; default to the long-running bot.
    # Anything else is forwarded so the same entrypoint covers the other
    # subcommands instead of needing a second script. The list is passed
    # explicitly, so nothing from this script's own argv leaks into the CLI.
    return int(bot_main(sys.argv[1:] or ["run"]))


if __name__ == "__main__":
    sys.exit(main())