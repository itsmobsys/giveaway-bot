"""Client for the Rust test harness.

The bot is Rust; the tests are Python. ``gwbot-harness`` is a small binary that
speaks one JSON object per line on stdin and answers one per line on stdout, so
the Python suite exercises the *real* service, storage layer and SQL rather than
a re-implementation of them.

Two rules keep the suite honest:

* every test starts a fresh harness against a fresh database, so no test can be
  made green by another test's leftovers;
* the harness clock is faked by default, so an "expires in 5 minutes" assertion
  never races a slow CI box.

Nothing here needs a Discord token, a network connection or a Turso account.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

#: Repository root, derived from this file so the suite runs from anywhere.
ROOT = Path(__file__).resolve().parent.parent

#: Where cargo puts the harness. Release first: that is what CI builds.
_CANDIDATES = (
    ROOT / "rust" / "target" / "release" / "gwbot-harness",
    ROOT / "rust" / "target" / "debug" / "gwbot-harness",
)


class HarnessError(RuntimeError):
    """The harness could not be started, or broke protocol."""


class ServiceError(RuntimeError):
    """A user-facing refusal, mirroring the Python ``ServiceError``.

    ``kind`` carries the same marker the bot branches on; ``timeout_ban`` is the
    only one today.
    """

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


class PartialFlush(RuntimeError):
    """A batched write applied part of its rows before failing."""

    def __init__(self, message: str, applied: int = 0) -> None:
        super().__init__(message)
        self.applied = applied


def find_binary() -> Path:
    """Locate the harness binary, or explain how to build it."""
    override = os.environ.get("GWBOT_HARNESS_BIN")
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise HarnessError(f"GWBOT_HARNESS_BIN points at a missing file: {path}")
    for base in _CANDIDATES:
        for suffix in ("", ".exe"):
            candidate = base.with_name(base.name + suffix)
            if candidate.is_file():
                return candidate
    searched = "\n  ".join(
        str(base.with_name(base.name + suffix))
        for base in _CANDIDATES
        for suffix in ("", ".exe")
    )
    raise HarnessError(
        "the Rust harness has not been built; run "
        "`cargo build --release -p gwbot-harness` in rust/\n  looked for:\n  "
        + searched
    )


class Harness:
    """One harness process = one bot process.

    Use as a context manager so the child is always reaped::

        with Harness() as h:
            gw = h.call("create", guild_id="g", ...)
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        binary: Path | None = None,
        env: dict[str, str] | None = None,
        fake_clock: bool = True,
        start_ms: int = 1_700_000_000_000,
    ) -> None:
        self._binary = binary or find_binary()
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        if db_path is None:
            # A file (not :memory:) so a test can open a second harness against
            # the same database, and so a crash leaves evidence behind.
            self._tmpdir = tempfile.TemporaryDirectory(prefix="gwbot-harness-")
            db_path = Path(self._tmpdir.name) / "test.db"
        self.db_path = str(db_path)

        child_env = dict(os.environ)
        child_env["GWBOT_HARNESS_DB"] = self.db_path
        child_env["GWBOT_HARNESS_FAKE_CLOCK"] = "1" if fake_clock else "0"
        child_env["GWBOT_HARNESS_START_MS"] = str(start_ms)
        # Never let a developer's real .env or Redis leak into a test run.
        for name in ("REDIS_URL", "DISCORD_BOT_TOKEN", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
            child_env.pop(name, None)
        if env:
            child_env.update(env)

        creationflags = 0
        if sys.platform == "win32":
            # No console window per test on a dev machine.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._proc = subprocess.Popen(  # noqa: S603 - fixed binary, no shell
            [str(self._binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=child_env,
            creationflags=creationflags,
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        self._next_id = 1
        self._responses: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._err_reader.start()

    # -- plumbing --------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._responses.put(json.loads(line))
            except json.JSONDecodeError:
                self._responses.put({"ok": False, "error": f"bad protocol line: {line[:200]}"})
        self._responses.put(None)

    def _read_stderr(self) -> None:
        assert self._proc.stderr
        for line in self._proc.stderr:
            # Keep a bounded tail: a panicking harness is the interesting case,
            # and an unbounded list would be a slow leak in a long run.
            self._stderr_lines.append(line.rstrip())
            del self._stderr_lines[:-200]

    def stderr_tail(self, limit: int = 40) -> str:
        return "\n".join(self._stderr_lines[-limit:])

    def call(self, op: str, /, **args: Any) -> Any:
        """Run one operation and return its result, or raise its error."""
        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"id": request_id, "op": op, "args": args}, ensure_ascii=False)
        try:
            assert self._proc.stdin
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise HarnessError(f"harness died before {op}: {exc}\n{self.stderr_tail()}") from exc

        try:
            response = self._responses.get(timeout=60)
        except queue.Empty as exc:
            raise HarnessError(f"harness timed out on {op}\n{self.stderr_tail()}") from exc
        if response is None:
            code = self._proc.poll()
            raise HarnessError(
                f"harness exited (code {code}) during {op}\n{self.stderr_tail()}"
            )
        if response.get("id") not in (None, request_id):
            raise HarnessError(f"out-of-order response: {response}")
        if response.get("ok"):
            return response.get("result")

        kind = response.get("kind", "")
        message = response.get("error", "unknown error")
        if kind == "service_error":
            raise ServiceError(message, response.get("detail", ""))
        if kind == "partial_flush":
            raise PartialFlush(message, int(response.get("detail") or 0))
        raise HarnessError(f"{op} failed: {message}")

    def sql(self, statement: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a statement straight against the test database.

        Used to assert storage state (row counts, guarded writes) without
        trusting the service to report on itself.
        """
        return self.call("sql", sql=statement, params=params or [])

    def one(self, statement: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        rows = self.sql(statement, params)
        return rows[0] if rows else None

    def scalar(self, statement: str, params: list[Any] | None = None) -> Any:
        row = self.one(statement, params)
        if row is None:
            return None
        return next(iter(row.values()))

    # -- clock -----------------------------------------------------------

    def now(self) -> int:
        return int(self.call("clock.now"))

    def advance(self, milliseconds: int) -> int:
        """Move the fake clock forward and return the new 'now'."""
        return int(self.call("clock.advance", ms=milliseconds))

    def set_time(self, milliseconds: int) -> int:
        return int(self.call("clock.set", ms=milliseconds))

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self.call("shutdown")
            except (HarnessError, OSError):
                pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
        # Release the pipes too: the reader threads have hit EOF by now, and a
        # long run would otherwise leak two file handles per harness.
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# -- convenience builders ------------------------------------------------


def create(h: Harness, **overrides: Any) -> dict[str, Any]:
    """Create a giveaway with sane defaults, overridable per test."""
    args: dict[str, Any] = {
        "guild_id": "100000000000000001",
        "channel_id": "100000000000000002",
        "prize": "Nitro",
        "winner_count": 1,
        "duration_seconds": 3600,
        "created_by": "100000000000000003",
    }
    args.update(overrides)
    return h.call("create", **args)


def join(h: Harness, gw: dict[str, Any], user_id: str, **overrides: Any) -> Any:
    """Join a giveaway with a member that passes every requirement."""
    args: dict[str, Any] = {
        "gw": gw,
        "user_id": user_id,
        "username": f"user{user_id[-3:]}",
        "member_roles": [],
        "account_created_ts": None,
    }
    args.update(overrides)
    return h.call("join", **args)


# -- shared fixture ------------------------------------------------------


class HarnessTestCase(unittest.TestCase):
    """One fresh database and one fresh harness per test method.

    The base owns the temporary directory instead of letting ``Harness`` own it:
    ``Harness.close()`` deletes a directory it created, and recovery tests have
    to close a process and reopen the *same* file. Passing an explicit
    ``db_path`` keeps the file alive across a simulated restart.

    Helpers here only encode protocol details (which op, which argument names).
    They must never re-implement a rule, otherwise the suite would be testing
    this file instead of the Rust engine.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="gwbot-test-")
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self._harnesses: list[Harness] = []
        # Fixed ids so a test never has to care about Discord's snowflake shape.
        self.guild = "111111111111111111"
        self.other_guild = "222222222222222222"
        self.channel = "333333333333333333"
        self.h = self.make()

    def tearDown(self) -> None:
        for harness in reversed(self._harnesses):
            harness.close()
        self._harnesses.clear()
        self._tmpdir.cleanup()

    # -- processes -------------------------------------------------------

    def make(self) -> Harness:
        """Start another harness on this test's database."""
        harness = Harness(db_path=self.db_path)
        self._harnesses.append(harness)
        return harness

    def sibling(self) -> Harness:
        """A second bot process on the same file, warm and ready.

        The ``ping`` round-trip proves the child is serving (and that
        ``init_schema`` finished) before a test starts racing it.
        """
        harness = self.make()
        self.assertEqual("pong", harness.call("ping"))
        return harness

    def reopen(self) -> Harness:
        """Simulate a restart: close this process, reopen the same file."""
        self.h.close()
        self._harnesses.remove(self.h)
        self.h = self.make()
        return self.h

    # -- giveaways -------------------------------------------------------

    def gw(self, **overrides: Any) -> dict[str, Any]:
        overrides.setdefault("guild_id", self.guild)
        overrides.setdefault("channel_id", self.channel)
        return create(self.h, **overrides)

    def enter(self, gw: dict[str, Any] | str, user_id: str = "100000000000000010", **overrides: Any) -> Any:
        return join(self.h, gw, user_id, **overrides)  # type: ignore[arg-type]

    def join_error(
        self,
        gw: dict[str, Any] | str,
        user_id: str = "100000000000000010",
        **overrides: Any,
    ) -> ServiceError:
        """Join and return the refusal, failing the test if it succeeded."""
        with self.assertRaises(ServiceError) as caught:
            join(self.h, gw, user_id, **overrides)  # type: ignore[arg-type]
        return caught.exception

    def timed_join_error(self, gw: dict[str, Any] | str, user_id: str) -> ServiceError:
        return self.join_error(gw, user_id, timed_out=True)

    def end(self, gw: dict[str, Any] | str, *, now: int | None = None, require_due: bool = False,
            known: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
        giveaway_id = gw["id"] if isinstance(gw, dict) else gw
        args: dict[str, Any] = {"giveaway_id": giveaway_id, "require_due": require_due}
        if now is not None:
            args["now"] = now
        if known is not None:
            args["known"] = known
        return self.h.call("end", **args)

    def draw(self, entrants: int = 5, **overrides: Any) -> tuple[dict[str, Any], list[str]]:
        """Create a giveaway, enter ``entrants`` members, and end it."""
        gw = self.gw(**overrides)
        for index in range(entrants):
            self.enter(gw, f"20000000000000{index:04d}")
        return self.end(gw)

    def account_age(self, days: float) -> float:
        """A Discord account creation timestamp that is ``days`` old right now.

        Rust's ``now_secs()`` reads the wall clock, not the harness fake clock,
        so this cannot be derived from ``h.now()``.
        """
        return time.time() - days * 86400.0

    # -- storage ---------------------------------------------------------

    def execute(self, statement: str, params: list[Any] | None = None) -> int:
        return int(self.h.call("exec", sql=statement, params=params or []))

    def trigger(self, statement: str) -> None:
        self.execute(statement)

    def drop_trigger(self, name: str) -> None:
        self.execute(f"DROP TRIGGER IF EXISTS {name}")

    def entry_rows(self, giveaway_id: str) -> int:
        return int(self.h.scalar("SELECT COUNT(*) FROM simple_entries WHERE giveaway_id = ?", [giveaway_id]))

    # -- concurrency -----------------------------------------------------

    def run_concurrently(
        self,
        jobs: list[tuple[Harness, Callable[[Harness], Any]]],
    ) -> list[tuple[str, Any]]:
        """Run every job at the same instant on its own harness.

        A barrier makes the race real rather than sequential: without it the
        first writer would finish long before the second started, and the test
        would pass even if the guard it targets did not exist. Outcomes are
        ``("ok", value)``, ``("refused", ServiceError)`` or ``("error", exc)``.
        """
        barrier = threading.Barrier(len(jobs))
        outcomes: list[tuple[str, Any] | None] = [None] * len(jobs)

        def run(index: int, harness: Harness, job: Callable[[Harness], Any]) -> None:
            barrier.wait()
            try:
                outcomes[index] = ("ok", job(harness))
            except ServiceError as exc:
                outcomes[index] = ("refused", exc)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test
                outcomes[index] = ("error", exc)

        threads = [
            threading.Thread(target=run, args=(index, harness, job), daemon=True)
            for index, (harness, job) in enumerate(jobs)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads), "a concurrent job never finished")
        self.assertNotIn(None, outcomes, "a concurrent job produced no outcome")
        return outcomes  # type: ignore[return-value]
