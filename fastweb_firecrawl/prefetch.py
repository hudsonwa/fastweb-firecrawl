"""Prefetch-on-search: fetch the top-N search results into the scrape cache in the background.

Agents almost always scrape some of the top results a few seconds after a search; prefetching makes that scrape a
cache hit. Prefetch is fire-and-forget (submit() is synchronous and never awaits), bounded (own
workers, bounded queue dropping the oldest job, per-host cap, at most N per search), deduped (scrape cache, queued and
in-flight keys) and cheap (plain + impersonation only, short timeout, no Firecrawl, no host-memory writes).
SingleFlight lets an on-demand scrape join an in-flight fetch of the same cache key instead of fetching twice.
"""
import asyncio
import collections
import contextlib
import contextvars
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any

import httpx

from .cache import DEAD, NEEDS_RENDER, scrape_key_for_request

if TYPE_CHECKING:
    from .cache import Store

# True inside a prefetch job: fetch() applies the prefetch byte cap, extraction uses the prefetch lane, do_scrape stops
# before Firecrawl.
PREFETCHING = contextvars.ContextVar("fastweb_prefetching", default=False)
# The cache key of the running prefetch job: its flight, which an on-demand request may join (promotion).
PF_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar("fastweb_prefetch_key", default=None)
# The body firecrawl-py 4.17 sends for scrape(url, formats=["markdown", "html"]) (tests/data/sdk_scrape_body_4.17.json),
# url swapped in, so a prefetched page lands on the same cache key as the client's own request.
SDK_BODY = {"blockAds": True, "fastMode": False, "formats": ["markdown", "html"], "maxAge": 14400000,
            "mobile": False, "onlyMainContent": True, "origin": "python-sdk@4.17.0", "removeBase64Images": True,
            "skipTlsVerification": True, "storeInCache": True, "timeout": 60000}
SKIP_TYPES = re.compile(r"\.(png|jpe?g|gif|webp|avif|svg|ico|bmp|tiff?|heic|mp4|m4v|mov|avi|mkv|webm|flv|wmv|mp3|"
                        r"m4a|wav|ogg|flac|aac|zip|gz|tgz|bz2|xz|7z|rar|tar|zst|exe|msi|dmg|pkg|deb|rpm|apk|iso|img|"
                        r"bin|jar|woff2?|ttf|otf)$", re.I)
X_HOSTS = ("x.com", "twitter.com")  # handled by x_search
SEEN_MAX = 4096                     # remembered prefetched keys (miss attribution)


PAGE_TYPES = ("html", "xml", "pdf", "text/", "json")  # anything else is not downloaded by prefetch


def prefetch_type_ok(ctype: str | None) -> bool:
    ctype = (ctype or "").lower()
    return not ctype or any(t in ctype for t in PAGE_TYPES)


def _rss() -> int:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0  # no /proc (macOS dev runs): no current-RSS reading, so no guard


def mem_usage(cgroup: str = "/sys/fs/cgroup", fallback_limit: int = 0) -> tuple[int, int]:
    """(current, limit) bytes: the container's cgroup v2 usage and limit; without a limit, RSS vs fallback_limit."""
    try:
        with open(os.path.join(cgroup, "memory.max")) as f:
            limit = f.read().strip()
        with open(os.path.join(cgroup, "memory.current")) as f:
            cur = int(f.read().strip())
        if limit != "max":
            return cur, int(limit)
    except (OSError, ValueError):
        pass
    return _rss(), fallback_limit


def prefetch_body(url: str) -> dict[str, Any]:
    return {**SDK_BODY, "url": url}


def skip_reason(url: str) -> str:
    """Reason to never prefetch this URL, or ''."""
    try:
        u = httpx.URL(url)
    except Exception:  # noqa: BLE001 - an unparsable URL is skipped
        return "scheme"
    if u.scheme not in ("http", "https") or not u.host:
        return "scheme"
    host = u.host.lower().rstrip(".")
    if any(host == h or host.endswith("." + h) for h in X_HOSTS):
        return "x"
    if SKIP_TYPES.search(u.path):
        return "type"
    return ""


class GaveUp(Exception):
    """A prefetch stepped aside for on-demand work before extracting: counted as gave_up, not failed."""

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


# What a single-flight leader shares: the page (bytes), a failure for its joiners, or None (joiners run themselves).
Shared = bytes | Exception | None
PfJob = tuple[str, str, str]  # (url, cache key, host)


class SingleFlight:
    """One fetch per cache key: later callers wait for the leader's result. The leader sets the future to its payload
    (bytes), to a failure its joiners should answer with too (an exception instance), or leaves it to lead() to set
    None (joiners then run the ladder themselves)."""

    def __init__(self) -> None:
        self.flights: dict[str, tuple[asyncio.Future[Shared], bool]] = {}  # key -> (future, by_prefetch)
        # flights someone waits on: a prefetching leader is promoted
        self.joined_futs: set[asyncio.Future[Shared]] = set()

    def busy(self, key: str | None) -> bool:
        return key in self.flights

    def joined(self, key: str | None) -> bool:
        f = self.flights.get(key) if key is not None else None
        return f is not None and f[0] in self.joined_futs

    @contextlib.contextmanager
    def lead(self, key: str, by_prefetch: bool = False) -> Iterator[asyncio.Future[Shared]]:
        fut: asyncio.Future[Shared] = asyncio.get_running_loop().create_future()
        self.flights[key] = (fut, by_prefetch)
        try:
            yield fut
        finally:
            if not fut.done():
                fut.set_result(None)
            self.joined_futs.discard(fut)
            if self.flights.get(key, (None,))[0] is fut:
                del self.flights[key]

    async def join(self, key: str, timeout: float) -> tuple[bytes | Exception, bool] | None:
        """(payload or shared failure, by_prefetch) of an in-flight fetch of key, or None (no flight, no shareable
        result, or timeout)."""
        f = self.flights.get(key)
        if not f or timeout <= 0:
            return None
        fut, by_prefetch = f
        self.joined_futs.add(fut)
        try:
            payload = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            return None
        return (payload, by_prefetch) if payload else None


class Prefetcher:
    """run: async (body, key) -> payload bytes, raising on failure (the app's cheap-tier scrape + cache write).
    busy: () -> True while on-demand load is high enough that prefetch should wait.
    mem_high: () -> True while memory is too high to prefetch at all. max_age: queued jobs older than this are
    dropped."""

    def __init__(self, store: "Store", flights: SingleFlight,
                 run: Callable[[dict[str, Any], str], Awaitable[tuple[bytes, bool]]], log: Callable[..., None],
                 n: int = 5, concurrency: int = 4, queue_max: int = 64, per_host: int = 2,
                 busy: Callable[[], bool] = lambda: False, max_age: float = 12.0,
                 mem_high: Callable[[], bool] = lambda: False):
        self.store, self.flights, self.run, self.log = store, flights, run, log
        self.n, self.concurrency, self.per_host, self.busy = n, concurrency, per_host, busy
        self.max_age, self.mem_high = max_age, mem_high
        self.tasks: set[asyncio.Task[None]] = set()  # strong refs to notify tasks (asyncio keeps only weak ones)
        self.queue: collections.deque[tuple[str, str, str, float]] = collections.deque()  # (url, key, host, queued at)
        self.queue_max = queue_max
        self.queued: set[str] = set()
        self.host_inflight: collections.Counter[str] = collections.Counter()
        self.seen: collections.OrderedDict[str, bool] = collections.OrderedDict()  # keys a prefetch was attempted for
        self.cond: asyncio.Condition | None = None
        self.workers: list[asyncio.Task[None]] = []
        self.stats: dict[str, Any] = {
            "scheduled": 0, "done": 0, "failed": 0, "dropped": 0, "skipped": collections.Counter(), "skipped_late": 0,
            "failed_by": collections.Counter(), "stale": 0, "uncached": 0, "gave_up": 0}

    # ---- request path (synchronous, never awaits) ----
    def submit(self, urls: list[Any]) -> tuple[int, int]:
        """Queue the top-N urls; returns (scheduled, skipped). Never raises."""
        sched = skipped = 0
        if self.n <= 0 or self.concurrency <= 0:
            return 0, 0
        try:
            self._start()
            for url in urls[:self.n]:
                why = skip_reason(url) if isinstance(url, str) else "scheme"
                key = scrape_key_for_request(prefetch_body(url)) if not why else None
                if not why and (key in self.queued or self.flights.busy(key)):
                    why = "dup"
                if why:
                    self.stats["skipped"][why] += 1
                    skipped += 1
                    continue
                if key is None:
                    raise RuntimeError("key is not set")
                if len(self.queue) >= self.queue_max:
                    _, old, _, _ = self.queue.popleft()
                    self.queued.discard(old)
                    self.stats["dropped"] += 1
                self.queue.append((url, key, (httpx.URL(url).host or "").lower(), time.monotonic()))
                self.queued.add(key)
                self.stats["scheduled"] += 1
                sched += 1
            self._notify()
        except Exception as e:  # noqa: BLE001 - never let prefetch touch the search response
            self.log(path="prefetch", outcome="error", error=type(e).__name__)
        return sched, skipped

    def was_prefetched(self, key: str) -> bool:
        return key in self.seen

    def snapshot(self) -> dict[str, Any]:
        st = dict(self.stats)
        st["skipped"] = dict(st["skipped"])
        st["failed_by"] = {str(k): v for k, v in st["failed_by"].items()}
        st["queued"], st["inflight"] = len(self.queue), sum(self.host_inflight.values())
        return st

    # ---- workers ----
    def _start(self) -> None:
        if self.workers:
            return
        self.cond = asyncio.Condition()
        ctx = contextvars.copy_context()
        ctx.run(PREFETCHING.set, True)
        loop = asyncio.get_running_loop()
        # one Context per worker: PF_KEY is per job, a shared Context would hold the last job's key
        self.workers = [loop.create_task(self._worker(), context=ctx.copy()) for _ in range(self.concurrency)]

    def _notify(self) -> None:
        cond = self.cond
        if cond is None:
            raise RuntimeError("cond is not set")

        async def go() -> None:
            async with cond:
                cond.notify_all()
        task = asyncio.get_running_loop().create_task(go())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _take(self) -> PfJob | None:
        now = time.monotonic()
        while self.queue and now - self.queue[0][3] > self.max_age:  # oldest first: the follow-up has come and gone
            _, old, _, _ = self.queue.popleft()
            self.queued.discard(old)
            self.stats["stale"] += 1
        for i, job in enumerate(self.queue):
            if self.host_inflight[job[2]] < self.per_host:
                del self.queue[i]
                self.queued.discard(job[1])
                return job[:3]
        return None

    async def stop(self) -> None:
        for w in self.workers:
            w.cancel()
        for w in self.workers:
            with contextlib.suppress(BaseException):
                await w
        self.workers = []

    async def _worker(self) -> None:
        cond = self.cond
        if cond is None:
            raise RuntimeError("cond is not set")
        while True:
            async with cond:
                job = self._take()
                while job is None:
                    await cond.wait()
                    job = self._take()
                self.host_inflight[job[2]] += 1
            try:
                await self._one(*job)
            except Exception as e:  # noqa: BLE001 - a prefetch failure is logged and never stops the worker
                self.log(path="prefetch", outcome="error", error=type(e).__name__)
            finally:
                self.host_inflight[job[2]] -= 1
                if self.host_inflight[job[2]] <= 0:  # forget idle hosts: one key per host ever seen otherwise
                    del self.host_inflight[job[2]]
                async with cond:
                    cond.notify_all()

    async def _one(self, url: str, key: str, host: str) -> None:
        t0 = time.monotonic()
        waited = 0.0
        while self.busy() and waited < 2.0:  # on-demand scrapes win
            await asyncio.sleep(0.05)
            waited += 0.05
        why = ""
        if self.busy():  # on-demand pages kept the CPU (and memory) all along: they win
            why = "busy"
        elif self.mem_high():
            why = "memory"
        elif self.flights.busy(key):
            why = "dup"
        elif await self.store.has_scrape(key):  # EXISTS: no GET + decompress of a multi-MB entry
            why = "cached"
        else:
            memory = await self.store.host(host)  # the raw host, as do_scrape and host memory use it
            if memory in (DEAD, NEEDS_RENDER):
                why = f"host-{memory}"
        if not why and self.flights.busy(key):  # an on-demand request started leading during the awaits above
            why = "dup"
        if why:
            self.stats["skipped"][why] += 1
            self.stats["skipped_late"] += 1
            self.log(path="prefetch", host=host, outcome=f"skipped-{why}", ms=int((time.monotonic() - t0) * 1000))
            return
        self.seen[key] = True
        while len(self.seen) > SEEN_MAX:
            self.seen.popitem(last=False)
        PF_KEY.set(key)
        with self.flights.lead(key, by_prefetch=True) as fut:
            try:
                payload, stored = await self.run(prefetch_body(url), key)
            except GaveUp as e:
                self.stats["gave_up"] += 1
                self.log(path="prefetch", host=host, outcome="gave-up", error=e.error[:120],
                         ms=int((time.monotonic() - t0) * 1000))
                return
            except Exception as e:  # noqa: BLE001 - counted in stats by class or status
                status = getattr(e, "status", None) or type(e).__name__
                self.stats["failed"] += 1
                self.stats["failed_by"][status] += 1
                self.log(path="prefetch", host=host, outcome="failed", status=status,
                         error=getattr(e, "error", "")[:120], ms=int((time.monotonic() - t0) * 1000))
                return
            fut.set_result(payload)
        self.stats["done" if stored else "uncached"] += 1
        self.log(path="prefetch", host=host, outcome="cached" if stored else "uncached",
                 ms=int((time.monotonic() - t0) * 1000), bytes=len(payload))
