"""Optional Redis read-cache + concurrency damper for the giveaway bot.

Turso stays the single source of truth. Everything here is an optimisation
that must be safe to LOSE: with no URL, or with Redis down, evicted, rate
limited or slow, every method returns a safe default, logs at most one
warning per outage, and the caller falls back to Turso. Nothing in this
module may raise into a click handler.

Two defences protect the managed plan (30 connections, 100 ops/sec):

* a token-bucket governor that silently SKIPS operations over budget, so ONE
  process can never exceed the plan ceiling in any sliding second — a skip
  looks exactly like a miss. The budget is PER PROCESS: overlapping instances
  (a free-plan restart leaves two alive for a moment) each run their own
  bucket, so the aggregate peaks at the number of live processes times the
  per-process worst second. A single-process bucket cannot bound that;
* a circuit breaker that stops hammering a dead server for a cooldown.

Keys handed in by callers are SUFFIXES ('gw:gw_ab12', 'count:gw_ab12'); the
shared prefix is applied internally so every key this bot writes is
namespaced and versioned under one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("giveaway_bot.redis")

#: Compare-and-delete: only the current holder may release. Without the token
#: check a slow holder whose lock already expired on TTL could release the
#: lock its successor had just re-acquired.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

#: INCR + first-time TTL in ONE round trip. Two commands (INCR then EXPIRE)
#: cost two round trips, doubling the price of this path for nothing; Lua is
#: available on the managed server. The TTL is set whenever the key has none,
#: so a key left over from a half-failed write can never live forever.
_INCR_TTL_SCRIPT = """
local n = redis.call('incr', KEYS[1])
if redis.call('pttl', KEYS[1]) < 0 then
    redis.call('pexpire', KEYS[1], ARGV[1])
end
return n
"""

#: The managed plan's own ceiling, in ops/sec. The governor promises to keep
#: a SLIDING SECOND under this, which is a stricter promise than keeping the
#: sustained rate right: the worst second a token bucket can produce is
#: capacity + rate (spend the whole bucket, then earn a second of refill).
OP_CEILING_PER_SEC = 100.0

_HOST_PORT = re.compile(r"\b[A-Za-z0-9_.-]+:\d{2,5}\b")
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def _scrub(text: str) -> str:
    """Strip host:port and token-shaped runs out of anything we log.

    Exceptions from the driver like to quote the endpoint, and this module
    must never put a credential or a hostname into the log or into stats().
    """
    try:
        clean = _HOST_PORT.sub("<host:port>", str(text))
        return _LONG_SECRET.sub("<redacted>", clean)[:160]
    except Exception:
        return "<unprintable>"


class _Budget:
    """Token bucket: refills at a fixed rate, refuses (never queues) over cap.

    Refusing is the point. Queueing would just move the burst into the future
    and let one click burst spend a whole minute's plan budget; skipping lets
    the caller fall through to Turso, which is always correct.
    """

    def __init__(self, rate: float, clock: Callable[[], float]) -> None:
        self.rate = max(1.0, float(rate))
        #: The burst allowance is capped so that even the worst sliding second
        #: stays inside the plan ceiling: capacity + rate <= OP_CEILING_PER_SEC.
        #: Spending a WHOLE rate instantly (the old rule) let one process emit
        #: 2 x rate ops inside a single second -- measured 159 ops against a
        #: 100 ops/sec ceiling at the shipped 80/s, while the sustained rate
        #: was exactly right. A rate at or above the ceiling cannot be bounded
        #: by a single bucket; capacity then floors at one token and the
        #: configured rate is left saying what it says.
        self.capacity = max(1.0, min(self.rate, OP_CEILING_PER_SEC - self.rate))
        self._tokens = self.capacity
        self._clock = clock
        self._stamp = clock()

    def take(self) -> bool:
        now = self._clock()
        elapsed = now - self._stamp
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._stamp = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class RedisCache:
    """Short-TTL cache + lock helper over a managed Redis, degrading always."""

    #: Bound on every single network call, on top of the driver's own socket
    #: timeouts. A stalled Redis must never park a join handler: the call is
    #: abandoned and the caller reads Turso instead.
    _TIMEOUT_SLACK = 0.5

    def __init__(
        self,
        url: str = "",
        *,
        enabled: bool = True,
        prefix: str = "gwbot:v1:",
        socket_timeout: float = 1.5,
        socket_connect_timeout: float = 2.0,
        max_connections: int = 4,
        op_budget_per_sec: float = 80.0,
        breaker_threshold: int = 5,
        breaker_cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self._url = (url or "").strip()
        self._enabled = bool(enabled)
        self._prefix = prefix or ""
        self._socket_timeout = float(socket_timeout)
        self._connect_timeout = float(socket_connect_timeout)
        self._max_connections = max(1, int(max_connections))
        self._breaker_threshold = max(1, int(breaker_threshold))
        self._breaker_cooldown = max(0.0, float(breaker_cooldown))
        self._clock = clock
        self._connect = connect
        self._budget = _Budget(op_budget_per_sec, clock)
        self._client: Any = None
        self._closed = False
        self._fail_streak = 0
        self._open_until = 0.0
        self._warned = False
        self._counters: dict[str, int] = {
            "attempted": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "hits": 0,
            "misses": 0,
        }
        self._errors: dict[str, int] = {}

    # -- state -----------------------------------------------------------
    @property
    def configured(self) -> bool:
        """True when a URL was supplied and the switch is on."""
        return bool(self._url) and self._enabled

    @property
    def available(self) -> bool:
        """True when the next operation would actually be attempted."""
        return self.configured and not self._closed and not self._breaker_open()

    def _breaker_open(self) -> bool:
        if self._open_until and self._clock() < self._open_until:
            return True
        if self._open_until:
            # Cooldown elapsed: allow one probe through so a recovered server
            # closes the breaker without a restart.
            self._open_until = 0.0
            self._fail_streak = 0
            self._warned = False
        return False

    # -- lifecycle -------------------------------------------------------
    def _timeout(self) -> float:
        return self._connect_timeout + self._socket_timeout + self._TIMEOUT_SLACK

    def _client_or_none(self) -> Any:
        """Lazily build the shared client; never opens a socket by itself."""
        if self._closed:
            return None
        if self._client is not None:
            return self._client
        try:
            if self._connect is not None:
                self._client = self._connect()
            else:
                import redis.asyncio as aioredis  # noqa: PLC0415 - lazy on purpose

                self._client = aioredis.Redis.from_url(
                    self._url,
                    decode_responses=True,
                    socket_timeout=self._socket_timeout,
                    socket_connect_timeout=self._connect_timeout,
                    socket_keepalive=True,
                    max_connections=self._max_connections,
                    retry_on_timeout=False,
                )
        except Exception as exc:  # missing package, malformed URL, bad auth
            self._record_failure(exc)
            return None
        return self._client

    async def start(self) -> bool:
        """Connect (once) and verify with a PING. Never raises."""
        if not self.available:
            return False
        client = self._client_or_none()
        if client is None:
            return False
        return await self._run(lambda: client.ping()) is not _FAILED

    async def aclose(self) -> None:
        """Disconnect the shared client. Safe to call twice, or never started."""
        client, self._client = self._client, None
        self._closed = True
        self._open_until = 0.0
        if client is None:
            return
        for name in ("aclose", "close"):
            closer = getattr(client, name, None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:
                log.debug("redis close failed: %s", _scrub(exc))
            return

    # -- plumbing --------------------------------------------------------
    async def _run(self, make: Callable[[], Any]) -> Any:
        """One governed network call: budget, breaker, hard timeout, counters.

        Takes a zero-argument factory, not a coroutine: a skipped or
        breaker-open call must never build a coroutine it will not await
        (that leaks an un-awaited coroutine and a RuntimeWarning per call).

        Returns the raw driver value, or the sentinel _FAILED on any problem.
        """
        if not self.available:
            self._counters["skipped"] += 1
            return _FAILED
        if not self._budget.take():
            # Over the plan's ops/sec: skip without touching the network. The
            # caller sees a miss and reads Turso, which is always correct.
            self._counters["skipped"] += 1
            return _FAILED
        if self._client_or_none() is None:
            # No driver, malformed URL, or a client already closed: every
            # caller treats this exactly like a miss.
            self._counters["skipped"] += 1
            return _FAILED
        self._counters["attempted"] += 1
        try:
            value = await asyncio.wait_for(make(), timeout=self._timeout())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(exc)
            return _FAILED
        self._record_success()
        return value

    def _record_success(self) -> None:
        self._counters["ok"] += 1
        self._fail_streak = 0
        self._open_until = 0.0
        self._warned = False

    def _record_failure(self, exc: BaseException) -> None:
        self._counters["failed"] += 1
        kind = type(exc).__name__
        self._errors[kind] = self._errors.get(kind, 0) + 1
        self._fail_streak += 1
        if self._fail_streak >= self._breaker_threshold:
            self._open_until = self._clock() + self._breaker_cooldown
            if not self._warned:
                # Exactly one warning per outage episode: a dead Redis must not
                # fill the log with thousands of identical tracebacks.
                self._warned = True
                log.warning(
                    "redis unavailable (%s: %s); pausing cache for %.0fs,"
                    " falling back to the database",
                    kind, _scrub(exc), self._breaker_cooldown,
                )
        else:
            log.debug("redis op failed (%s: %s)", kind, _scrub(exc))

    def _key(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    # -- plain values ----------------------------------------------------
    async def get(self, key: str) -> str | None:
        value = await self._run(lambda: self._client_get(self._key(key)))
        if value is _FAILED or value is None:
            self._counters["misses"] += 1
            return None
        self._counters["hits"] += 1
        return str(value)

    async def _client_get(self, key: str) -> Any:
        client = self._direct_client()
        return await client.get(key) if client is not None else None

    async def set(self, key: str, value: str, ttl: float) -> bool:
        millis = max(1, int(float(ttl) * 1000))
        result = await self._run(lambda: self._client_set(self._key(key), value, millis))
        return bool(result) and result is not _FAILED

    async def _client_set(self, key: str, value: str, millis: int) -> Any:
        client = self._direct_client()
        return await client.set(key, value, px=millis) if client is not None else None

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        names = [self._key(k) for k in keys]
        result = await self._run(lambda: self._client_delete(names))
        if result is _FAILED or result is None:
            return 0
        try:
            return int(result)
        except (TypeError, ValueError):
            return 0

    async def _client_delete(self, names: list[str]) -> Any:
        client = self._direct_client()
        return await client.delete(*names) if client is not None else None


    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            # A truncated or foreign value is a miss, not a crash.
            return None

    async def set_json(self, key: str, value: Any, ttl: float) -> bool:
        try:
            payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
        except Exception as exc:
            log.debug("redis set_json could not serialise: %s", _scrub(exc))
            return False
        return await self.set(key, payload, ttl)

    # -- counter ---------------------------------------------------------
    async def incr_with_ttl(self, key: str, ttl: float) -> int | None:
        """Increment a counter and give it a TTL in one round trip.

        None means "no answer" — the caller must fall back to the database,
        never treat it as zero.
        """
        millis = max(1, int(float(ttl) * 1000))
        name = self._key(key)
        result = await self._run(lambda: self._client_incr(name, millis))
        if result is _FAILED or result is None:
            return None
        try:
            return int(result)
        except (TypeError, ValueError):
            return None

    async def _client_incr(self, key: str, millis: int) -> Any:
        client = self._direct_client()
        if client is None:
            return None
        try:
            return await client.eval(_INCR_TTL_SCRIPT, 1, key, millis)
        except Exception as exc:
            # A server without EVAL (or with scripting disabled) would lose
            # every counter, so fall back to two plain commands. Documented
            # cost: one extra round trip on that path only.
            log.debug("redis eval unavailable (%s); using incr+pexpire", _scrub(exc))
            value = await client.incr(key)
            await client.pexpire(key, millis)
            return value

    # -- lock ------------------------------------------------------------
    async def acquire(self, key: str, ttl: float) -> str | None:
        """Take a short-lived lock. Returns its token, or None when held.

        None means "somebody else holds it" AND "Redis could not answer" —
        a caller that cannot act on the difference must treat both the same
        way (skip the optional shared work), never as licence to do
        something unsafe. A caller that CAN tell "not now" from "not
        answering" uses acquire_checked, which reports which one happened.
        """
        token, _answered = await self.acquire_checked(key, ttl)
        return token

    async def acquire_checked(self, key: str, ttl: float) -> tuple[str | None, bool]:
        """Take a short-lived lock and say whether the server answered.

        Returns (token, answered):

        * (token, True) - we hold it; give the token back to release()
        * (None, True)  - somebody else holds it (a real answer): the shared
                          work is already being done, so this caller skips it
        * (None, False) - Redis was over budget, unreachable, or timed out.
                          The caller decides; for optional work the honest
                          fallback is the pre-Redis behaviour, because a
                          cache that is down must not silently disable work
                          the bot used to do without it.

        Never raises, and a lock is never reported as taken without a token:
        the three states above are the only ones a caller has to handle.
        """
        token = secrets.token_hex(16)
        millis = max(1, int(float(ttl) * 1000))
        result = await self._run(lambda: self._client_acquire(self._key(key), token, millis))
        if result is _FAILED:
            return None, False
        if not result:
            return None, True
        return token, True

    async def _client_acquire(self, key: str, token: str, millis: int) -> Any:
        client = self._direct_client()
        if client is None:
            return None
        return await client.set(key, token, nx=True, px=millis)

    async def release(self, key: str, token: str) -> bool:
        """Release a lock only if this token still owns it."""
        result = await self._run(lambda: self._client_release(self._key(key), token))
        if result is _FAILED or result is None:
            return False
        try:
            return int(result) > 0
        except (TypeError, ValueError):
            return False

    async def _client_release(self, key: str, token: str) -> Any:
        client = self._direct_client()
        if client is None:
            return None
        return await client.eval(_RELEASE_SCRIPT, 1, key, token)

    # -- diagnostics -----------------------------------------------------
    async def ping_ms(self) -> float | None:
        """Wall-clock milliseconds of one PING, or None. Never raises."""
        started = time.monotonic()
        if not await self.start():
            return None
        return (time.monotonic() - started) * 1000.0

    def describe(self) -> str:
        """One-line, log-safe summary of the configuration (no endpoint)."""
        return (
            f"max_connections={self._max_connections},"
            f" budget={self._budget.rate:.0f} ops/s (burst {self._budget.capacity:.0f}),"
            f" breaker={self._breaker_threshold}"
        )

    def stats(self) -> dict[str, Any]:
        """Counters only — never a URL, host, username or password."""
        return {
            "configured": self.configured,
            "available": self.available,
            "breaker_open": bool(self._open_until),
            "key_prefix_len": len(self._prefix),
            "counters": dict(self._counters),
            "errors": dict(self._errors),
        }

    def _direct_client(self) -> Any:
        return None if self._closed else self._client


class _Failed:
    """Sentinel: the operation did not produce a value (skip/failure)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<redis-op-failed>"


_FAILED = _Failed()
