"""Repository-root launcher for hosts that start an app from a Python file.

The bot itself is Rust (``rust/crates/gwbot-cli``). Some panels - Silly
Development's, for one - run ``PY_FILE`` from the repository root and cannot be
pointed at a compiled binary, so this shim bridges the two. It adds no
configuration and no behaviour of its own: it finds the built binary, forwards
its own arguments, and replaces this process with it.

Why ``os.execv`` and not ``subprocess``
---------------------------------------
Signals and the exit code both matter to a panel. Replacing the process means
SIGTERM from the host lands directly on the bot, and the bot's own status is
what the panel observes. A wrapper that spawned a child would have to catch
each signal and re-send it, and a signal arriving between ``Popen`` returning
and the wait starting would be lost, leaving an orphaned bot holding a gateway
connection.

That is why POSIX gets a real ``execv``. Windows has no such thing: CPython
emulates ``execv`` by spawning a child and exiting 0, which throws away the very
exit status this shim exists to forward, so Windows uses ``subprocess.call``
instead and returns the bot's status directly.

``os.execv`` is used rather than ``os.execve`` on purpose: the environment is
already correct in this process (the panel set it, and the bot loads ``.env``
itself), so there is nothing to pass on.

Arguments are forwarded, so this doubles as a normal CLI::

    python app.py             # starts the bot (same as `run`)
    python app.py doctor
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Repository root, derived from this file rather than the working directory, so
#: the panel can start the app from anywhere.
ROOT = Path(__file__).resolve().parent

#: Where cargo puts the binary. Release is what a deploy should run; debug is
#: accepted so a developer can `cargo build` once and then use the shim.
CANDIDATES = (
    ROOT / "rust" / "target" / "release" / "giveaway-bot",
    ROOT / "rust" / "target" / "debug" / "giveaway-bot",
)

#: Windows appends the extension; both spellings are checked so the shim works
#: on a dev machine and in a container from the same code path.
EXE_SUFFIXES = ("", ".exe")


def find_binary() -> Path | None:
    for base in CANDIDATES:
        for suffix in EXE_SUFFIXES:
            candidate = base.with_name(base.name + suffix)
            if candidate.is_file():
                return candidate
    return None


def main() -> int:
    binary = find_binary()
    if binary is None:
        searched = "\n".join(
            f"  {base.with_name(base.name + suffix)}"
            for base in CANDIDATES
            for suffix in EXE_SUFFIXES
        )
        print("error: the giveaway-bot binary has not been built", file=sys.stderr)
        print("       run: cargo build --release -p gwbot-cli", file=sys.stderr)
        print(f"       (looked for:\n{searched})", file=sys.stderr)
        return 1

    # A panel starts the app with no arguments; default to the long-running bot.
    # Anything else is forwarded so the same entrypoint covers the other
    # subcommands instead of needing a second script.
    args = sys.argv[1:] or ["run"]

    if os.name == "posix":
        # execv does not return on success. On failure (a permission problem, a
        # binary built for another platform) the exception is what the panel
        # sees, which is more useful than a silent exit 0.
        os.execv(str(binary), [str(binary), *args])
        return 1  # pragma: no cover - os.execv only returns by raising

    # Windows emulates execv by spawning a child and exiting 0, so the bot's own
    # exit status - the whole point of forwarding the call - never reaches the
    # panel. Run it as a child here and hand its status back instead. POSIX
    # keeps the real exec, where SIGTERM lands on the bot and no wrapper is in
    # the way.
    return subprocess.call([str(binary), *args])


if __name__ == "__main__":
    sys.exit(main())
