"""The scrape tier ladder.

    scrape cache -> 1. plain GET + extraction -> 2. Chrome-impersonating GET (on 401/403)
                 -> 3. warm headless Chrome (thin, walled, blocked, TLS-stalled or suspect-SPA pages)
                 -> 4. optional Firecrawl (only when the browser is off, busy, unreachable or crashed)

`ScrapeService.run(job)` walks it for one request and returns a Firecrawl-shaped document or raises `Fail`. Per-host
memory in Redis shortcuts the ladder: a host remembered as NEEDS_RENDER skips the plain GET (1 in 10 re-probed); a
host remembered as DEAD (it keeps refusing every tier) fails fast. The same code runs prefetch jobs (PREFETCHING set):
cheap tiers only, with stricter caps, yielding the CPU to on-demand requests. docs/design.md explains the thresholds.
"""
import asyncio
import collections
import contextlib
import json
import math
import random
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any

import httpx

from .browser import Browser, BrowserBusy, BrowserDown, BrowserError, NavFailed, PrivateTarget, Rendered
from .cache import DEAD, NEEDS_RENDER, Store
from .errors import BUSY_STATUS, Busy, Fail
from .extract import (
    LARGE_HTML,
    MAX_MD_CHARS,
    TooLarge,
    absolutize_links,
    big_text_job,
    decode_html,
    extract_job,
    junk_share,
    lighten,
    pdf_job,
)
from .fetchers import Fetcher, Impersonator, left
from .guard import PRIVATE_ERROR, TargetGuard
from .heuristics import (
    MIN_HTML_TEXT,
    TEXT_TYPES,
    binary_message,
    choose_lane,
    decoded_size,
    drop_added_query,
    is_binary,
    is_blocked,
    is_refused,
    is_thin,
    is_tls_error,
    safe_url,
    suspect_spa,
    text_markdown,
)
from .prefetch import PF_KEY, PREFETCHING, GaveUp, SingleFlight, mem_usage
from .settings import Settings
from .upstream import UPSTREAM_MARGIN, FirecrawlFallback
from .worker import LANES, ByteBudget, Hold, Lane, QueueTimeout, WorkerFailed

PF_MIN_EXTRACT = 3.0  # seconds a prefetch that yielded needs left to extract; less: it gives up
PF_GRACE = 2.0        # a started pf extraction may run this long past the prefetch deadline
# Formats fastweb's own tiers always build: the whole page is cached once and each request gets its projection.
WHOLE = ["markdown", "html"]
BUSY_MIN_RENDER = 3.0  # seconds of render time a page queued for a browser tab must keep (no Firecrawl configured)
BUSY_RETRY_AFTER = 2   # Retry-After (s) on the busy answer for a browser with every tab in use
BUSY_REASON = "fastweb busy (in-flight byte budget)"


def pf_lane_min(n_chars: int) -> float:
    """Seconds a prefetch holding the pf lane needs left to extract n_chars of markup (~1 s per MB); less: it gives
    up."""
    return max(1.5, 0.75 + n_chars / (1024 * 1024))  # 0.5 s + 1 s/MB killed 2 of 250


def parse_scrape_body(body: dict[str, Any], max_timeout: float = math.inf) -> tuple[str, list[str], float]:
    """(url, formats, timeout in seconds) from a scrape request body, or Fail(400). The timeout is clamped to
    max_timeout."""
    url = body["url"]
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        raise Fail(400, "Invalid URL") from None
    try:
        _ = u.host  # decodes the IDNA host name
    except ValueError:  # idna.IDNAError: e.g. a symbol (U+2603) that IDNA 2008 does not allow in a host name
        raise Fail(400, "Invalid host name (not a valid IDNA 2008 name)") from None
    fmts = body.get("formats") or ["markdown"]
    if not isinstance(fmts, list):
        raise Fail(400, "'formats' must be a list")
    formats = []
    for f in fmts:
        f = f.get("type") if isinstance(f, dict) else f
        if not isinstance(f, str):
            raise Fail(400, "'formats' entries must be strings or objects with a 'type'")
        formats.append(f)
    t = body.get("timeout")
    if t is None:
        t = 30000
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0:
        raise Fail(400, "'timeout' must be a positive number of milliseconds")
    return url, formats, min(float(t) / 1000, max_timeout)


def document(formats: list[str], md: str, html: str, metadata: dict[str, Any], engine_meta: dict[str, Any],
             max_chars: int) -> dict[str, Any]:
    """A Firecrawl `{"success": true, "data": ...}` answer. markdown is included when asked for (or when no other
    content format was); html and rawHtml are the page markup as fetched or rendered."""
    fw = dict(engine_meta)
    if metadata.pop("_text_only", False):
        fw["text_only_large"] = True
    if len(md) >= max_chars:  # the extractor cut it at the cap
        fw["truncated"] = True
    metadata["fastweb"] = fw
    out: dict[str, Any] = {}
    if "markdown" in formats or not any(f in formats for f in ("html", "rawHtml")):
        # Every markdown answer (HTML, text, PDF and big-lane pages, cache hits) gets the link-scheme pass;
        # extract_job has already applied it with the page base, so this is a no-op there.
        out["markdown"] = absolutize_links(md, "")
    out["metadata"] = metadata
    if "html" in formats:
        out["html"] = html
    if "rawHtml" in formats:
        out["rawHtml"] = html
    return {"success": True, "data": out}


@dataclass
class Job:
    """One scrape walking the ladder (on demand or prefetch)."""
    body: dict[str, Any]
    url: str
    formats: list[str]
    deadline: float  # time.monotonic() by which the caller wants an answer
    t0: float
    hold: Hold       # this job's share of the in-flight byte budget
    main_only: bool = True
    host: str = ""

    @classmethod
    def create(cls, body: dict[str, Any], url: str, formats: list[str], deadline: float, t0: float,
               hold: Hold) -> "Job":
        return cls(body, url, formats, deadline, t0, hold, main_only=body.get("onlyMainContent", True),
                   host=(httpx.URL(url).host or "").lower())


@dataclass
class Fetched:
    """What the cheap download tiers (plain, impersonation) produced, and what it says about the next step."""
    memory: str | None = None    # host memory before this request (NEEDS_RENDER, DEAD or None)
    dead_probe: bool = False     # a DEAD host's occasional re-probe of the fallback
    status: int = 200
    final_url: str = ""
    headers: Any = field(default_factory=dict)
    data: bytes = b""
    engine: str = "plain"
    plain_err: Fail | None = None  # the cheap tiers' own failure, reported if nothing later does better
    refused: bool = False        # plain *and* a Chrome fingerprint were refused with 401/403
    blocked: bool = False        # the target blocks the cheap tiers (remembered on a later success)
    fixable: bool = False        # a real browser may fix it (thin, walled, TLS stall, ...)
    need_fallback: bool = False
    reason: str = ""
    fetch_ms: int = 0

    def fall_back(self, reason: str, *, fixable: bool = False, blocked: bool = False) -> None:
        self.need_fallback, self.reason = True, reason
        self.fixable = self.fixable or fixable
        self.blocked = self.blocked or blocked


@dataclass
class Extracted:
    """The cheap tiers' extraction result (markdown, title, page markup and extra metadata)."""

    md: str = ""
    title: str = ""
    html: str = ""
    ctype: str = ""
    lane: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    extract_ms: int = 0


class ScrapeService:
    """Holds what the tiers share (settings, clients, semaphores, lanes, byte budget, cache, browser) and walks the
    ladder for each job."""

    def __init__(self, settings: Settings, store: Store, impersonator: Impersonator | None = None):
        s = self.settings = settings
        self.store = store
        self.guard = TargetGuard(s.allow_hosts)
        self.fetcher = Fetcher(s, self.guard)
        self.firecrawl = FirecrawlFallback(s, self.guard)
        self.budget = ByteBudget(s.inflight_bytes)
        self.lanes = {name: Lane(name, n, self._lane_executor(name)) for name, n in LANES.items()}
        self.flights = SingleFlight()
        self.imp: Impersonator | None = impersonator or (self.fetcher.impersonated if s.impersonate else None)
        self._imp_builtin = impersonator is None
        self.chrome = (Browser(s.browser_url, self.guard.allows, tabs=s.browser_tabs, settle=s.browser_settle,
                               timeout=s.browser_timeout, recycle=s.browser_recycle, wait=s.browser_wait,
                               max_html=s.max_bytes, egress=s.browser_egress or None, user_agent=s.user_agent)
                       if s.browser and s.browser_url else None)
        self.cheap = 0     # on-demand requests in their cheap tiers (fetch, decode, extraction lane)
        self.ondemand = 0  # on-demand requests anywhere in the ladder
        self.browser_counts = {"ok": 0, "thin": 0, "blocked": 0, "http_error": 0, "fallback": 0, "suspect_tried": 0,
                               "suspect_won": 0, "suspect_skipped_known_loss": 0}
        self.impersonate_errors: collections.Counter[str] = collections.Counter()  # by exception class, in stats

    def _lane_executor(self, name: str) -> Callable[[], Executor] | None:
        """Executor factory for a lane: Settings.lane_factory (tests use threads), else recycled worker processes."""
        factory = self.settings.lane_factory
        if factory is None:
            return None
        return lambda: factory(name)

    # ---- lifecycle ----
    async def start(self) -> None:
        await self.fetcher.start()
        await self.firecrawl.start()

    async def close(self) -> None:
        if self.chrome:
            await self.chrome.close()
        await self.fetcher.close()
        await self.firecrawl.close()
        for lane in self.lanes.values():
            lane.close()

    def browser_stats(self) -> dict[str, Any]:
        return {"enabled": bool(self.chrome), **self.browser_counts,
                "impersonate_errors": dict(self.impersonate_errors),
                **(self.chrome.stats() if self.chrome else {})}

    # ---- budgets ----
    def room(self, deadline: float) -> float:
        """Time to spend here while keeping Firecrawl a usable budget: its cap, or half of what is left."""
        if PREFETCHING.get():  # no Firecrawl after a prefetch
            return max(0.0, left(deadline))
        rest = left(deadline) - UPSTREAM_MARGIN
        return max(0.0, rest - min(self.settings.fallback_timeout, max(rest, 0.0) / 2))

    def budget_wait(self, deadline: float) -> float:
        """How long to wait for the byte budget; prefetch never waits for it, it skips."""
        return 0.0 if PREFETCHING.get() else self.room(deadline)

    def mem_high(self) -> bool:
        cur, limit = (self.settings.mem_usage or (lambda: mem_usage(fallback_limit=self.settings.prefetch_mem_limit)))()
        return limit > 0 and cur > self.settings.prefetch_mem_frac * limit

    def prefetch_busy(self) -> bool:
        """Prefetch holds back while on-demand work is in its cheap tiers or the fetch slots are nearly full."""
        s = self.settings
        return self.cheap >= 1 or self.ondemand >= max(1, s.plain_concurrency - s.prefetch_concurrency)

    # ---- entry points ----
    async def run(self, job: Job) -> dict[str, Any]:
        """The tier chain. An on-demand request counts as `cheap` (prefetch holds back its CPU work) from its start
        until it hands the page to the browser or Firecrawl, or ends."""
        if PREFETCHING.get():
            return await self._ladder(job, lambda: None)
        held = [1]
        self.cheap += 1

        def cheap_done() -> None:
            if held:
                self.cheap -= held.pop()
        try:
            return await self._ladder(job, cheap_done)
        finally:
            cheap_done()

    async def prefetch(self, body: dict[str, Any], key: str) -> tuple[bytes, bool]:
        """One prefetch job: cheap tiers under the prefetch timeout; only a success is cached (and marked)."""
        t0 = time.monotonic()
        url, formats, _ = parse_scrape_body(body)
        hold = Hold(self.budget)
        try:
            resp = await self.run(Job.create(body, url, formats, t0 + self.settings.prefetch_timeout, t0, hold))
            # Serialised (and compressed, in put_scrape) off the event loop: tens of ms per big page. No yield
            # here: the page is done, and a joiner may be waiting for it.
            payload = await asyncio.to_thread(
                lambda: json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode())
        finally:
            hold.release()
        stored = await self.store.put_scrape(key, payload)
        if stored:
            await self.store.mark_prefetched(key)
        return payload, stored

    # ---- the ladder ----
    async def _ladder(self, job: Job, cheap_done: Callable[[], None]) -> dict[str, Any]:
        f = await self._cheap_fetch(job)
        await self._prefetch_checkpoint(job)
        ext_t = time.monotonic()
        ext = await self._extract_cheap(job, f)
        ext.extract_ms = int((time.monotonic() - ext_t) * 1000)
        if f.need_fallback:
            cheap_done()  # the browser / Firecrawl render elsewhere: prefetch may use this CPU
            return await self._fallback(job, f, ext)
        if (self.chrome and self.settings.browser_suspect and not PREFETCHING.get() and f.status == 200
                and ext.lane in ("small", "big") and suspect_spa(ext.html, ext.md)):
            cheap_done()
            rendered = await self._try_suspect(job, f, ext)
            if rendered is not None:
                return rendered
        return await self._plain_success(job, f, ext)

    async def _cheap_fetch(self, job: Job) -> Fetched:
        """Tiers 1 and 2: plain GET, then the Chrome-impersonating GET when plain was refused (or the host is known
        to need more than plain). Raises Fail for answers no later tier can improve (a dead port, a private
        target)."""
        fetch_t = time.monotonic()
        memory = await self.store.host(job.host)
        f = Fetched(memory=memory, final_url=job.url,
                    dead_probe=memory == DEAD and random.random() < self.settings.dead_reprobe_rate)
        skip_plain = memory == NEEDS_RENDER and random.random() >= self.settings.reprobe_rate
        total = min(self.settings.dead_budget if memory == DEAD else self.settings.total_timeout, left(job.deadline))
        fetch_deadline = fetch_t + total
        plain_refused = False
        if skip_plain:
            await self.guard.check(job.url)
        else:
            await self._plain(job, f, fetch_deadline, total)
            plain_refused = not f.plain_err and f.status in (401, 403)
        if (skip_plain or plain_refused) and self.imp and left(fetch_deadline) > 0.2:
            await self._impersonate(job, f, fetch_deadline, plain_refused)
        if skip_plain and f.engine != "impersonate":
            f.fall_back("host known to need Firecrawl", fixable=True, blocked=True)
        f.fetch_ms = int((time.monotonic() - fetch_t) * 1000)
        return f

    async def _plain(self, job: Job, f: Fetched, fetch_deadline: float, total: float) -> None:
        """Tier 1. Network failures become f.plain_err (a later tier may still succeed) or a final Fail."""
        url, stage = job.url, dict[str, bool]()
        try:
            r, f.data = await asyncio.wait_for(
                self.fetcher.plain(url, fetch_deadline, job.hold, min(self.budget_wait(job.deadline), total - 0.25),
                                   stage), total)
            f.status, f.final_url, f.headers = r.status_code, str(r.url), r.headers
        except Busy:
            f.plain_err = Fail(504, BUSY_REASON)
        except httpx.ConnectTimeout:  # dead host: Firecrawl would only burn the caller's budget
            if not stage.get("tls"):
                raise Fail(504, f"Connecting to {safe_url(url)} timed out (connect timeout)") from None
            # TCP connected, the handshake stalled: a fingerprint filter a real browser may pass
            f.plain_err, f.fixable = Fail(504, f"Fetching {safe_url(url)} timed out (TLS handshake)"), True
        except httpx.ConnectError as e:
            if not (is_tls_error(e) or stage.get("tls")):
                raise Fail(422, f"Connecting to {safe_url(url)} failed: connection "
                                f"{'refused' if is_refused(e) else 'unreachable'}") from e
            f.plain_err = Fail(422, f"Fetching {safe_url(url)} failed: TLS handshake ({type(e).__name__})")
            f.fixable = True
        except (TimeoutError, httpx.TimeoutException):
            f.plain_err = Fail(504, f"Fetching {safe_url(url)} timed out")
        except httpx.HTTPError as e:
            f.plain_err = Fail(422, f"Fetching {safe_url(url)} failed: {type(e).__name__}")
        except Fail as e:
            if e.status != 413 or PREFETCHING.get():  # prefetch has no Firecrawl to hand a big page to
                raise
            f.plain_err = e  # over the download cap: Firecrawl may still render it

    async def _impersonate(self, job: Job, f: Fetched, fetch_deadline: float, plain_refused: bool) -> None:
        """Tier 2. Shares the fetch budget (no fresh one); replaces the plain result only when it got past the
        refusal."""
        if self.imp is None:
            raise RuntimeError("self.imp is not set")
        try:
            kw: dict[str, Any] = {}
            if self._imp_builtin:
                kw = {"hold": job.hold, "wait": min(self.budget_wait(job.deadline), left(fetch_deadline))}
            status, url, headers, data = await asyncio.wait_for(self.imp(job.url, fetch_deadline, **kw),
                                                                left(fetch_deadline))
            if status < 400:
                f.status, f.final_url, f.headers, f.data, f.engine = status, url, headers, data, "impersonate"
                await job.hold.to(len(data), 0)
                job.hold.force(len(data))
            else:  # evidence only when plain *and* a Chrome fingerprint were refused
                f.refused = plain_refused and status in (401, 403)
        except Fail as e:
            if PREFETCHING.get() or (e.status == 422 and e.error == PRIVATE_ERROR):
                raise  # prefetch: its caps and type checks are final, no Firecrawl after
        except Exception as e:  # noqa: BLE001 - any curl_cffi failure only means the next tier gets the page
            self.impersonate_errors[type(e).__name__] += 1

    async def _prefetch_checkpoint(self, job: Job) -> None:
        """Prefetch only: yield the CPU to on-demand work, and give up rather than extract too late or under memory
        pressure. A prefetch an on-demand request has joined (promoted) goes on."""
        if not PREFETCHING.get():
            return
        yielded = await self._yield_cpu(job)
        if not self.flights.joined(PF_KEY.get()):
            if self.cheap:  # never extract beside an on-demand page: CPU and memory are its
                raise GaveUp("prefetch: on-demand scrapes kept the CPU until the prefetch deadline")
            if yielded and left(job.deadline) < PF_MIN_EXTRACT:  # a fresh nice-10 worker would be killed
                raise GaveUp("prefetch: too little time left to extract after yielding")
        if self.mem_high():  # the job-start check is seconds old by now
            raise GaveUp("prefetch: memory high")

    async def _yield_cpu(self, job: Job) -> bool:
        """Wait while an on-demand request is in its cheap tiers. On one CPU the decode and lighten threads of a
        prefetch (started while the on-demand page is still downloading) slow it as much as the pf worker would.
        True if it waited. While waiting it holds no budget bytes (no priority inversion); afterwards it takes them
        back without waiting or gives up. Its downloaded body (<= the prefetch cap) stays in memory meanwhile,
        outside the budget: bounded by the prefetch concurrency."""
        hold = job.hold
        n, waited = hold.n, False
        while self.cheap and not self.flights.joined(PF_KEY.get()) and left(job.deadline) > 1.0:
            if not waited:
                hold.release()
                waited = True
            await asyncio.sleep(0.05)
        if waited and n and not await hold.to(n, 0):
            raise GaveUp("prefetch: byte budget taken while it yielded")
        return waited

    async def _extract_cheap(self, job: Job, f: Fetched) -> Extracted:
        """Decode and extract the cheap tiers' page, or mark it for the fallback (busy, too large, thin, walled).
        Raises Fail for honest final answers (404, unsupported or binary content)."""
        ext = Extracted()
        try:
            if f.plain_err:
                f.need_fallback, f.reason = True, f.plain_err.error
            elif not f.need_fallback:
                await self._classify_and_extract(job, f, ext)
        except Busy:
            f.fall_back(BUSY_REASON)
        except TooLarge:
            f.fall_back("page too large to extract here")
        except WorkerFailed as w:
            f.fall_back(w.reason)
        if f.need_fallback:
            ext.html, f.data = "", b""
            job.hold.release()
        return ext

    async def _decode(self, job: Job, f: Fetched, ext: Extracted) -> None:
        """Decode the body into ext.html. Budget for the decoded str (1/2/4 bytes per char) and its lightened copy
        before decoding; the raw bytes are dropped once decoded. Full: Busy, a later tier renders it instead."""
        data, hold = f.data, job.hold
        est = decoded_size(data)
        if not await hold.to(len(data) + 2 * est, self.budget_wait(job.deadline)):
            raise Busy
        if PREFETCHING.get() and len(data) > 200_000:  # off the loop: on-demand requests share it
            ext.html = await asyncio.to_thread(decode_html, data, ext.ctype)
        else:
            ext.html = decode_html(data, ext.ctype)
        f.data = b""
        await hold.to(2 * est, 0)

    async def _classify_and_extract(self, job: Job, f: Fetched, ext: Extracted) -> None:
        """Classify the fetched body by status and type, then extract it: text types to markdown directly, HTML and
        PDF through the extraction lanes. Fails (422) on error statuses, binary bodies and unsupported types; a
        blocked status falls back to the next tier."""
        data = f.data
        ext.ctype = f.headers.get("content-type", "").lower()
        mime = ext.ctype.split(";")[0].strip()
        is_pdf = "pdf" in mime or data[:5] == b"%PDF-"
        head = data[:16]
        if not is_pdf:
            await self._decode(job, f, ext)
            data = b""
        if mime in ("application/xml", "text/xml") and re.search(r"<html[\s>]", ext.html[:2048], re.I):
            mime = "application/xhtml+xml"  # XHTML served as XML: a web page, not a feed
        status = f.status
        if status in (404, 410):
            raise Fail(422, f"Target page returned HTTP {status}")
        if is_blocked(status, ext.html):
            f.fall_back(f"Target page returned HTTP {status}", fixable=True, blocked=True)
        elif status >= 400:
            raise Fail(422, f"Target page returned HTTP {status}")
        elif not is_pdf and (mime in TEXT_TYPES or mime == "application/json"
                             or (mime.endswith(("+json", "+xml")) and mime != "application/xhtml+xml")):
            if is_binary(head, ext.html):  # honest 422: a browser renders it no better
                raise Fail(422, binary_message(ext.html, mime))
            ext.md = text_markdown(ext.html, mime, self.settings.max_text_chars)
            ext.lane = "text"  # converted here, no extraction lane
        elif not is_pdf and mime and "html" not in mime:
            raise Fail(422, f"Unsupported content type {mime}")
        else:
            if not is_pdf and is_binary(head, ext.html):  # byte soup as text/html: the big path would keep it
                raise Fail(422, binary_message(ext.html, mime or "html"))
            md, title, desc, n, ext.lane = await self.extract_here(is_pdf, data, ext.html, f.final_url,
                                                                   job.main_only, job.deadline, ext.extra)
            f.data = b""
            ext.md, ext.title = md, title
            if desc:
                ext.extra["description"] = desc
            if n is not None and junk_share(md) > 0.02:  # binary the byte checks missed
                raise Fail(422, binary_message(md, mime or "html"))
            if n is not None and (is_thin(ext.html, md) or n < MIN_HTML_TEXT):
                f.fall_back("thin plain result", fixable=True)

    async def _fallback(self, job: Job, f: Fetched, ext: Extracted) -> dict[str, Any]:
        """Tiers 3 and 4 for a page the cheap tiers could not serve, with host-memory bookkeeping."""
        if PREFETCHING.get():  # cheap tiers only: no browser, no Firecrawl, no host memory from a prefetch
            raise Fail(422, f"prefetch: {f.reason}")
        fu = httpx.URL(f.final_url)
        fhost, fpath = (fu.host or job.host).lower(), fu.path or "/"
        memory, reason = f.memory, f.reason
        if memory == DEAD and not f.dead_probe:
            raise Fail(422, f"Host {job.host} is blocked: it refused every tier repeatedly; not retrying Firecrawl")
        if self.chrome and f.fixable:
            try:
                return await self.browser_tier(job, reason, f.fetch_ms, memory, f.blocked,
                                               plain_ok=f.plain_err is None and f.status < 400)
            except BrowserBusy as e:
                if not self.settings.upstream:  # nobody else can render it: a capacity answer the caller may retry
                    raise Fail(BUSY_STATUS, f"{reason}; {e}, retry shortly", transient=True,
                               retry_after=BUSY_RETRY_AFTER) from e
                self.browser_counts["fallback"] += 1  # Firecrawl renders it instead
                reason = f"{reason}; {e}"
            except BrowserDown as e:  # Firecrawl renders it instead
                self.browser_counts["fallback"] += 1
                reason = f"{reason}; {e}"
            except Fail as e:  # refused in the browser too: counts toward DEAD like Firecrawl's refusals
                if f.refused and e.target_side:
                    await self.store.observe_block(fhost, fpath)
                raise
        try:
            j = await self.firecrawl.scrape(job.body, job.formats, job.deadline, reason, f.plain_err)
        except Fail as e:
            if memory == NEEDS_RENDER:
                await self.store.forget_host(job.host)  # the remembered route broke: start over, never escalate
            elif f.refused and e.target_side:
                await self.store.observe_block(fhost, fpath)
            raise
        if memory == DEAD:
            await self.store.forget_host(job.host)  # the re-probe worked: the host is not dead
        elif f.blocked and memory != NEEDS_RENDER:
            await self.store.set_needs_render(fhost)
        j["data"]["metadata"]["fastweb"] = {"engine": "firecrawl", "fetch_ms": f.fetch_ms,
                                            "extract_ms": ext.extract_ms, "fallback_reason": reason}
        return j

    async def _try_suspect(self, job: Job, f: Fetched, ext: Extracted) -> dict[str, Any] | None:
        """A small app-shell page plain served as "ok": render it, keep the render only when much bigger. Any
        failure returns None (the plain result stands, never a 422). Losses are remembered for an hour per host and
        path class so the next request does not pay ~4 s again."""
        # Keyed on the URL the browser renders, not the plain tier's final URL: a host that redirects httpx
        # elsewhere must not write a loss onto another host's key. Root pages share class "".
        ru = httpx.URL(job.url)
        rhost = (ru.host or "").lower().rstrip(".")
        pcls = "/".join((ru.path or "/").split("/")[1:-1][:2])  # path class: up to two parent directories
        full = left(job.deadline) - UPSTREAM_MARGIN >= self.settings.browser_timeout  # else a truncated render
        if await self.store.suspect_lost(rhost, pcls):
            self.browser_counts["suspect_skipped_known_loss"] += 1
            return None
        self.browser_counts["suspect_tried"] += 1
        try:
            j = await self.browser_tier(job, "suspect SPA (small text on an app shell)", f.fetch_ms, None, False,
                                        beat=len(ext.md.strip()), plain_ok=True)
        except BrowserError:  # busy, down, crashed: says nothing about the page, not remembered
            return None
        except Fail as e:  # rendered and not better (or refused, thin, an error page): skip it for an hour
            if full and not e.transient:
                await self.store.set_suspect_loss(rhost, pcls)
            return None
        self.browser_counts["suspect_won"] += 1
        await self.store.clear_suspect_loss(rhost, pcls)
        return j

    async def _plain_success(self, job: Job, f: Fetched, ext: Extracted) -> dict[str, Any]:
        if not ext.md.strip():
            raise Fail(422, "Target page has no extractable content")
        # Any success clears the host's memory and pending observations.
        fhost = (httpx.URL(f.final_url).host or job.host).lower()
        await self.store.forget_host(fhost)
        if f.memory and job.host != fhost:
            await self.store.forget_host(job.host)
        meta = {"title": ext.title, "sourceURL": job.url, "url": f.final_url, "statusCode": f.status,
                "contentType": ext.ctype, **ext.extra}
        return document(WHOLE, ext.md, ext.html, meta,
                        {"engine": f.engine, "fetch_ms": f.fetch_ms, "extract_ms": ext.extract_ms, "lane": ext.lane},
                        self._md_cap())

    def _md_cap(self) -> int:
        return min(self.settings.max_text_chars, MAX_MD_CHARS)

    # ---- tier 3: the browser ----
    async def _vet_render(self, r: Rendered, plain_ok: bool = False) -> None:
        """A render is usable only under the size cap, on a public final URL, and with a non-error status.
        plain_ok: the cheap tiers got a page (status < 400), so an error status in the browser is a bot block."""
        if r.too_large:
            raise Fail(422, f"browser render too large ({r.too_large} chars of HTML, cap {self.settings.max_bytes})")
        try:
            await self.guard.check(r.url)
        except Fail as e:
            if e.error != PRIVATE_ERROR:
                raise
            raise Fail(422, f"{PRIVATE_ERROR} (the browser was redirected there)") from e
        if r.status in (404, 410) or (r.status >= 400 and not is_blocked(r.status, r.html)):
            self.browser_counts["http_error"] += 1
            if plain_ok:  # the same URL answered a plain request: the browser was refused, the page is not gone
                raise Fail(422, f"Target page returned HTTP {r.status} to the browser but a page to a plain request: "
                                "blocked (bot defence)")
            raise Fail(422, f"Target page returned HTTP {r.status} (browser)")
        if is_blocked(r.status, r.html):
            self.browser_counts["blocked"] += 1
            raise Fail(422, f"Target page returned HTTP {r.status} in the browser too", target_side=True)

    async def browser_tier(self, job: Job, reason: str, fetch_ms: int, memory: str | None, blocked: bool,
                           beat: int | None = None, plain_ok: bool = False) -> dict[str, Any]:
        """Render in the warm browser, then the normal extraction lanes and honesty checks. A page it rendered that is
        still thin, walled or an error is an honest 422: in measurements Firecrawl almost never found content Chrome
        missed, at up to 9 s per failure. beat: (suspect SPA) the plain text length the render must clearly beat.
        plain_ok: the cheap tiers got the page itself (status < 400), only too thin or walled to serve."""
        if self.chrome is None:
            raise RuntimeError("self.chrome is not set")
        b = min(self.settings.browser_timeout, left(job.deadline) - UPSTREAM_MARGIN)
        if b < 1.0:
            raise BrowserDown("browser skipped: too little time left")
        # With Firecrawl behind it a busy browser hands over quickly; without, the page queues for a tab for the
        # rest of the request's budget, keeping BUSY_MIN_RENDER seconds to render.
        wait = (self.settings.browser_wait if self.settings.upstream
                else max(self.settings.browser_wait, left(job.deadline) - UPSTREAM_MARGIN - BUSY_MIN_RENDER))
        try:
            r = await self.chrome.render(job.url, timeout=b, wait=wait, deadline=job.deadline - UPSTREAM_MARGIN)
        except PrivateTarget:
            raise Fail(422, f"{PRIVATE_ERROR} (the browser was redirected there)") from None
        except NavFailed as e:
            raise Fail(422, f"{reason}; the browser could not load it either ({e})", transient=True) from e
        await self._vet_render(r, plain_ok)
        html = r.html
        job.hold.force(2 * len(html.encode("utf-8", "surrogatepass")))  # already in memory: accounted, never refused
        if is_binary(html[:16].encode("utf-8", "replace"), html):
            raise Fail(422, "Unsupported content: binary body in the browser")
        ext_t, extra = time.monotonic(), dict[str, Any]()
        try:
            md, title, desc, n, lane = await self.extract_here(False, b"", html, r.url, job.main_only, job.deadline,
                                                               extra)
        except (TooLarge, WorkerFailed) as e:
            why = getattr(e, "reason", "too large")
            raise Fail(422, f"browser rendered the page but extraction failed: {why}") from e
        if beat is not None:  # suspect SPA: the render must be substantially bigger than the plain text
            got = len(md.strip())
            if got < 2 * beat or got < beat + 1000:
                raise Fail(422, "browser render not substantially bigger than the plain page")
        if not md.strip() or (n is not None and (is_thin(html, md) or n < MIN_HTML_TEXT)):
            self.browser_counts["thin"] += 1
            raise Fail(422, f"{reason}; browser render still thin (JS shell, wall or challenge)")
        self.browser_counts["ok"] += 1
        fhost = (httpx.URL(r.url).host or job.host).lower()
        if memory == DEAD:
            await self.store.forget_host(job.host)
        elif blocked and memory != NEEDS_RENDER:
            await self.store.set_needs_render(fhost)  # cheap tiers are refused here: next time straight to the browser
        if desc:
            extra["description"] = desc
        meta = {"title": title, "sourceURL": job.url, "url": drop_added_query(job.url, r.url), "statusCode": r.status,
                "contentType": "text/html", **extra}
        return document(WHOLE, md, html, meta,
                        {"engine": "browser", "fetch_ms": fetch_ms, "render_ms": r.ms,
                         "extract_ms": int((time.monotonic() - ext_t) * 1000), "lane": lane,
                         "fallback_reason": reason},
                        self._md_cap())

    # ---- extraction ----
    async def extract_here(self, is_pdf: bool, data: bytes, html: str, final_url: str, main_only: bool,
                           deadline: float, extra: dict[str, Any]) -> tuple[str, str, str | None, int | None, str]:
        """(markdown, title, description, text length or None for PDFs, lane) from a worker process."""
        try:
            if is_pdf:
                md, title, pages = await self.in_lane("pdf", deadline, pdf_job, data, self.settings.max_text_chars,
                                                      need=PF_MIN_EXTRACT)  # PDF bytes say little about its time
                extra["numPages"] = pages
                return md, title, None, None, "pdf"
            if PREFETCHING.get() and len(html) > LARGE_HTML:
                light = await asyncio.to_thread(lighten, html)
            else:
                light = lighten(html) if len(html) > LARGE_HTML else html  # linear, cheap
            # a big page waits for an on-demand ask
            if PREFETCHING.get() and len(light) > self.settings.prefetch_max_html:
                raise Fail(413, f"prefetch: {len(light)} chars of markup over the prefetch cap")
            # a good 200 body, just big: lexbor-only text, not Firecrawl
            if len(light) > self.settings.extract_max_html:
                extra["_text_only"] = True
                md, title, desc, n = await self.in_lane("big", deadline, big_text_job, light, final_url)
                return md, title, desc, n, "big"
            lane = choose_lane(False, len(light))
            md, title, desc, n = await self.in_lane(lane, deadline, extract_job, light, final_url, main_only,
                                                    self.settings.extract_max_html, size=len(light))
            return md, title, desc, n, "pf" if PREFETCHING.get() else lane
        except (Fail, TooLarge, WorkerFailed, GaveUp):
            raise
        except Exception as e:
            raise Fail(422, f"Could not extract content: {type(e).__name__}") from e

    async def in_lane(self, name: str, deadline: float, fn: Callable[..., Any], *args: Any, size: int = 0,
                      need: float | None = None) -> Any:
        """Run fn(*args) in an extraction lane's worker. Prefetch always uses its own (niced) "pf" lane."""
        name = "pf" if PREFETCHING.get() else name
        queued_t = time.monotonic()

        def hard_s() -> float:  # evaluated once the lane is held; a prefetch that queued for it and now has too
            # little time left (need, else estimated from its markup size) gives up instead of starting an extraction
            if name != "pf":
                return min(self.room(deadline), self.settings.extract_timeout)
            if (time.monotonic() - queued_t > 0.05 and not self.flights.joined(PF_KEY.get())
                    and left(deadline) < (pf_lane_min(size) if need is None else need)):
                raise GaveUp("prefetch: too little time left to extract after the pf lane queue")
            # Once started it is not killed at its own soft deadline: a nice-10 worker beside an on-demand
            # extraction gets ~10 % of the CPU; only a real hang is killed
            return min(self.settings.extract_timeout, max(0.0, left(deadline)) + PF_GRACE)
        try:
            return await self.lanes[name].run(self.room(deadline), hard_s, fn, *args)
        except QueueTimeout:
            raise WorkerFailed("extraction queue busy") from None


@contextlib.contextmanager
def counting(service: ScrapeService) -> Iterator[None]:
    """Counts an on-demand request in service.ondemand while it runs."""
    service.ondemand += 1
    try:
        yield
    finally:
        service.ondemand -= 1
