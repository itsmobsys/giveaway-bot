"""Background scheduling.

A tiny, dependency-free job runner: each job gets its own interval, runs with
error isolation, and never blocks another job.  Jobs are registered by the bot
at startup; the scheduler itself knows nothing about Discord, which keeps it
unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger("giveaway_bot.scheduler")

JobFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class Job:
    name: str
    fn: JobFn
    interval: float
    #: Run once immediately on start instead of waiting a full interval.
    run_immediately: bool = True
    last_run: float = 0.0
    failures: int = 0


@dataclass(slots=True)
class Scheduler:
    """Runs registered jobs on independent intervals."""

    jobs: list[Job] = field(default_factory=list)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stopping: bool = False

    def add(
        self, name: str, fn: JobFn, *, interval: float, run_immediately: bool = True
    ) -> None:
        self.jobs.append(
            Job(name=name, fn=fn, interval=max(1.0, interval), run_immediately=run_immediately)
        )

    async def start(self) -> None:
        self._stopping = False
        for job in self.jobs:
            self._tasks.append(asyncio.create_task(self._loop(job), name=f"job:{job.name}"))
        log.info("scheduler started with %d job(s)", len(self.jobs))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("scheduler stopped")

    async def run_job_now(self, name: str) -> None:
        """Trigger a job immediately (used after admin actions)."""
        for job in self.jobs:
            if job.name == name:
                await self._safe_run(job)
                return

    async def _loop(self, job: Job) -> None:
        # Small jitter so many bots in one guild do not stampede the API.
        await asyncio.sleep(random.uniform(0, min(5.0, job.interval)))  # noqa: S311 - not security
        while not self._stopping:
            started = time.monotonic()
            await self._safe_run(job)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, job.interval - elapsed))

    async def _safe_run(self, job: Job) -> None:
        try:
            await job.fn()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001 - a failing job must never kill the loop
            job.failures += 1
            log.exception("job %s failed (%d consecutive failures)", job.name, job.failures)
        else:
            if job.failures:
                log.info("job %s recovered", job.name)
            job.failures = 0
        finally:
            job.last_run = time.time()