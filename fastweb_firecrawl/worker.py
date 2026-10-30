"""Extraction lanes in recycled worker processes, and the in-flight byte budget.

Each lane is one worker process (ProcessPoolExecutor, max_workers=1) forked from a forkserver that has the extraction
libraries preloaded, so a fresh worker starts in milliseconds. A worker is replaced after `tasks_per_child` pages:
its memory goes back to the OS with the process instead of ratcheting inside the server. A page that outruns its hard
timeout, or kills its worker (OOM), costs only that worker: it is killed, the pool recreated, and the caller gets
WorkerFailed. The event loop never runs extraction code, so no page can freeze /health or /search.
"""
import asyncio
import contextlib
import multiprocessing
import os
import time
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

# lane -> pages per worker process before it is replaced. Small pages hold little; big pages and PDFs get a fresh
# process each. Each lane adds ~40 MB of idle RSS once it has been
# used (one idle worker per used lane).
LANES = {
    "small": 25,
    "big": 1,
    "pdf": 1,
    "pf": 1,  # prefetch: its own worker; a fresh one per page, so an idle one holds no big heap
}
# lane -> niceness of its worker: on one CPU the prefetch worker takes only what on-demand workers leave.
NICE = {"pf": 10}
PRELOAD = ["fastweb_firecrawl.extract", "pypdf", "trafilatura", "markdownify", "selectolax.lexbor"]
_ctx = None


def _lower_priority(n: int) -> None:
    os.nice(n)


def context() -> Any:
    global _ctx  # noqa: PLW0603 - one lazily created context per process
    if _ctx is None:
        _ctx = multiprocessing.get_context("forkserver")
        _ctx.set_forkserver_preload(PRELOAD)
    return _ctx


class WorkerFailed(Exception):
    """An extraction did not finish (timed out, crashed or the lane queue stayed busy); `reason` says which."""

    def __init__(self, reason: str):
        self.reason = reason


class QueueTimeout(Exception):
    """No worker of the lane became free within the wait."""


class Lane:
    """One page at a time; the slot is held until the worker has finished or been killed."""

    def __init__(self, name: str, tasks_per_child: int, factory: Callable[[], Executor] | None = None):
        self.name, self.tasks_per_child, self.factory = name, tasks_per_child, factory
        self.sem = asyncio.Semaphore(1)
        self.pool: Executor | None = None

    def _new(self) -> Executor:
        if self.factory:
            return self.factory()
        nice = NICE.get(self.name, 0)
        if not nice:
            return ProcessPoolExecutor(1, mp_context=context(), max_tasks_per_child=self.tasks_per_child)
        return ProcessPoolExecutor(1, mp_context=context(), max_tasks_per_child=self.tasks_per_child,
                                   initializer=_lower_priority, initargs=(nice,))

    def _kill(self) -> Executor | None:
        """Kill the worker; returns the pool when it has no process to kill (threads: wait for it instead)."""
        pool, self.pool = self.pool, None
        if pool is None:
            return None
        procs = list((getattr(pool, "_processes", None) or {}).values())
        for p in procs:
            p.kill()
        for p in procs:
            p.join(1)  # reap it: a killed worker must not linger as a zombie
        pool.shutdown(wait=False, cancel_futures=True)
        return None if procs else pool

    async def _end(self) -> None:
        pool = self._kill()
        if pool is not None:  # a thread cannot be killed: keep the slot until it really ends
            await asyncio.to_thread(pool.shutdown, True)

    async def run(self, wait_s: float, hard_s: float | Callable[[], float], fn: Callable[..., Any], *args: Any) -> Any:
        """wait_s: longest wait for the lane; hard_s: longest run before the worker is killed (a callable is
        evaluated once the lane is held, so time spent waiting is not granted twice)."""
        if self.sem.locked():
            try:
                await asyncio.wait_for(self.sem.acquire(), max(0.0, wait_s))
            except TimeoutError:
                raise QueueTimeout from None
        else:
            await self.sem.acquire()
        try:
            hard = hard_s() if callable(hard_s) else hard_s
            if self.pool is None:
                self.pool = self._new()
            try:
                fut = asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)
                return await asyncio.wait_for(fut, max(0.05, hard))
            except TimeoutError:
                await self._end()
                raise WorkerFailed("extraction timed out; worker killed") from None
            except BrokenProcessPool:
                await self._end()
                raise WorkerFailed("extraction worker died (out of memory?)") from None
        finally:
            self.sem.release()

    async def warm(self) -> None:
        """Start the forkserver and this lane's worker now, not on the first page."""
        with contextlib.suppress(Exception):
            await self.run(30, 30, len, "")

    def close(self) -> None:
        self._kill()


class ByteBudget:
    """Bytes of page bodies held for extraction across requests. One body alone always fits."""

    def __init__(self, cap: int):
        self.cap, self.used, self.peak = cap, 0, 0
        self.cond: asyncio.Condition | None = None
        self.holders: dict[Hold, None] = {}  # Hold -> None, in admission order
        self.wakers: set[asyncio.Task[None]] = set()  # pending wake tasks, referenced so none is GC'd before it runs

    async def acquire(self, n: int, timeout: float) -> bool:
        return await self.grow(0, n, timeout)

    async def grow(self, have: int, n: int, timeout: float, holder: "Hold | None" = None) -> bool:
        """Add n to a holder that already has `have`. A holder alone always fits, and so does the oldest holder:
        requests that each hold a downloaded body and wait to add its decoded copy would otherwise jam until their
        deadlines (16 x 5.7 MB emoji pages). The overshoot is bounded by that one page."""
        if self.cond is None:
            self.cond = asyncio.Condition()
        end = time.monotonic() + max(0.0, timeout)
        async with self.cond:
            while not (self.used == have or self.used + n <= self.cap
                       or (holder is not None and next(iter(self.holders), None) is holder)):
                try:
                    await asyncio.wait_for(self.cond.wait(), end - time.monotonic())
                except TimeoutError:
                    return False
            self.adjust(n)
            return True

    def adjust(self, n: int) -> None:
        """Add without waiting: memory that is already allocated (a body longer than its Content-Length)."""
        self.used += n
        self.peak = max(self.peak, self.used)

    def release(self, n: int) -> None:
        self.used -= n
        cond = self.cond
        if cond is not None:
            async def wake() -> None:
                async with cond:
                    cond.notify_all()
            t = asyncio.get_running_loop().create_task(wake())
            self.wakers.add(t)
            t.add_done_callback(self.wakers.discard)


class Hold:
    """One request's share of the ByteBudget, resized as its body is downloaded, decoded and answered."""

    def __init__(self, budget: "ByteBudget"):
        self.budget, self.n = budget, 0

    async def to(self, n: int, wait: float) -> bool:
        """Resize to n bytes; growing waits up to `wait` s, shrinking never waits. False: no room in time."""
        if n <= self.n:
            self.release(self.n - n)
            return True
        if not self.n:
            self.budget.holders[self] = None
        if not await self.budget.grow(self.n, n - self.n, wait, self):
            if not self.n:
                self.budget.holders.pop(self, None)
            return False
        self.n = n
        return True

    def force(self, n: int) -> None:
        if n > self.n:
            self.budget.holders.setdefault(self, None)
            self.budget.adjust(n - self.n)
            self.n = n

    def release(self, n: int | None = None) -> None:
        n = self.n if n is None else n
        if n:
            self.n -= n
            self.budget.release(n)
        if not self.n:
            self.budget.holders.pop(self, None)
