"""Browser tier: a warm Chrome headless-shell driven over raw CDP, for pages the cheap tiers found thin or blocked.

One persistent websocket to the browser (opened lazily, reconnected with backoff), a fresh browser context per page, at
most `tabs` pages at once (a page that cannot get a tab within `wait` seconds is BrowserBusy: Firecrawl takes it, or,
when there is no Firecrawl, the caller gets a retryable 502 with Retry-After).
Every request the page makes is paused (Fetch domain) and checked by fastweb's target guard: documents, redirects,
subresources and iframes to non-public addresses are failed, as are images/fonts/media and tracker hosts. Settle:
DOMContentLoaded, then innerText.length polled every 50 ms until non-zero and stable for 200 ms with no XHR/fetch in
flight, capped at `settle` seconds; the whole page at `timeout` seconds, after which the HTML so far is taken.

Network boundary: the browser sits on an internal docker network (no gateway) and reaches the web only
through the browser-egress proxy (egress.py), which resolves, checks and pins every host. That covers what this CDP
guard cannot see (workers, out-of-process iframes, popups, prefetch, beacons) and DNS rebinding. The guard here stays as
defence in depth and gives the honest PRIVATE_ERROR on the main document; child targets (workers, OOPIFs) are
auto-attached paused and get the same Fetch guard before they run. Popups: Chrome runs with --block-new-web-contents.
A CDP call that times out on a live socket means a wedged browser: the connection is dropped (BrowserDown, Firecrawl
takes the page, reconnect with backoff); the container's supervisor restarts a Chrome that stops answering.
"""
import asyncio
import collections
import contextlib
import hmac
import ipaddress
import json
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .settings import DEFAULT_UA as UA  # one Chrome version string, in step with the pinned browser image

BLOCK_TYPES = {"Image", "Font", "Media"}  # never Stylesheet: module apps await their CSS and never render
TRACKERS = ("googletagmanager.com", "google-analytics.com", "doubleclick.net", "facebook.net", "hotjar.com",
            "scorecardresearch.com", "googlesyndication.com", "adservice.google.com")
QUIET_TYPES = {"XHR", "Fetch"}  # scripts and documents never go quiet on ad-heavy pages
TEXT_LEN = "document.body ? document.body.innerText.length : 0"
# No WebSocket / WebRTC / service workers from rendered pages: the Fetch domain does not see them.
HARDEN = ("(() => { try { Object.defineProperty(window, 'WebSocket', {value: undefined}); "
          "Object.defineProperty(window, 'RTCPeerConnection', {value: undefined}); "
          "if (navigator.serviceWorker) navigator.serviceWorker.register = () => Promise.reject(new Error('off')); "
          "} catch (e) {} })()")
STABLE_S = 0.2
THIN_RENDER = 1000  # body text (chars) below which a render still fetching at the settle cap gets one more settle
SETUP_S = 1.0    # createBrowserContext / createTarget / attach: a hang beyond it recycles the browser
DISPOSE_S = 1.0  # close/dispose run after the page returned; a hang beyond it recycles the browser
POLL_S = 0.05
WAITERS_PER_TAB = 4  # renders queued for a tab beyond tabs * this fail fast (busy) instead of waiting their budget
AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
# Chrome's errors when browser-egress itself is down or refused the tunnel: the tier failed, not the target.
# A refused https target lands here too; Firecrawl then applies its own guard.
PROXY_ERRORS = ("net::ERR_PROXY_CONNECTION_FAILED", "net::ERR_TUNNEL_CONNECTION_FAILED")


class BrowserError(Exception):
    """The browser tier could not run (busy, down or crashed): says nothing about the page."""


class BrowserBusy(BrowserError):
    """No tab freed within the wait: Firecrawl renders the page instead."""


class BrowserDown(BrowserError):
    """Unreachable, crashed, hung or timed out: Firecrawl renders the page instead."""


class NavFailed(BrowserError):
    """Chrome reached no page (net::ERR_*): an honest failure, not a browser fault."""


class PrivateTarget(BrowserError):
    """The page's main document (or a redirect hop) targets a non-public address."""


class Rendered:
    """A finished render: outerHTML, final URL, HTTP status, render time, and too_large when the HTML was over the
    cap."""

    __slots__ = ("html", "ms", "status", "too_large", "url")

    def __init__(self, html: str, url: str, status: int, ms: float, too_large: int = 0):
        self.html, self.url, self.status, self.ms, self.too_large = html, url, status, ms, too_large


def p50(xs: collections.abc.Iterable[float]) -> float | None:
    s = sorted(xs)
    return round(s[len(s) // 2], 1) if s else None


def is_tracker(url: str) -> bool:
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:  # noqa: BLE001 - any unparsable URL is simply not a tracker
        return False
    return any(host == t or host.endswith("." + t) for t in TRACKERS)


class Conn:
    """One CDP websocket: request/response by id, events routed to per-session handlers."""

    def __init__(self, ws: Any):
        self.ws, self.n = ws, 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.handlers: dict[str | None, Callable[[dict[str, Any]], None]] = {}
        self.dead: str | None = None
        self.task = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        why = "connection closed"
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                if "id" in m:
                    f = self.pending.pop(m["id"], None)
                    if f and not f.done():
                        f.set_result(m)
                else:
                    h = self.handlers.get(m.get("sessionId"))
                    if h:
                        h(m)
        except Exception as e:  # noqa: BLE001 - any reader failure ends the connection; the reason is kept
            why = f"connection lost ({type(e).__name__})"
        finally:
            self.dead = why
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(BrowserDown(f"browser {why}"))
            self.pending.clear()
            for h in list(self.handlers.values()):
                h({"method": "fastweb.closed"})

    async def send(self, method: str, params: dict[str, Any] | None = None, session: str | None = None,
                   timeout: float = 5.0) -> dict[str, Any]:
        if self.dead:
            raise BrowserDown(f"browser {self.dead}")
        self.n += 1
        i = self.n
        msg: dict[str, Any] = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        f: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[i] = f
        try:
            await self.ws.send(json.dumps(msg))
            m = await asyncio.wait_for(f, max(0.05, timeout))
        except (TimeoutError, BrowserDown, asyncio.CancelledError):
            raise
        except Exception as e:  # websockets.ConnectionClosed and friends
            raise BrowserDown(f"browser connection lost ({type(e).__name__})") from e
        finally:
            self.pending.pop(i, None)
        if "error" in m:
            raise BrowserError(f"{method}: {m['error'].get('message')}")
        result: dict[str, Any] = m.get("result") or {}
        return result

    async def close(self) -> None:
        with contextlib.suppress(Exception):  # teardown: the socket may already be gone
            await asyncio.wait_for(self.ws.close(), 2)
        self.task.cancel()


class Page:
    """Per-page CDP state: navigation events, in-flight requests, the main response."""

    def __init__(self) -> None:
        self.dcl = asyncio.Event()
        self.inflight: set[str | None] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        self.status: int | None = None
        self.url: str | None = None
        self.doc_private, self.crashed = False, False
        # X-Fastweb-Egress on the main document: the proxy answered itself ("busy" or "refused", then its token)
        self.egress: str | None = None
        self.children: set[str] = set()  # auto-attached worker / OOPIF sessions
        self.ctx: str | None = None  # browser context, target and session ids, once created
        self.tid: str | None = None
        self.sid: str | None = None


class Browser:
    """Client for one warm Chrome over a persistent CDP websocket: a fresh context per page, a bounded number of
    tabs, request interception through `guard`, and recycling every `recycle` pages."""

    def __init__(self, endpoint: str, guard: Callable[[str], Awaitable[Any]], tabs: int = 2, settle: float = 3.0,
                 timeout: float = 4.0, recycle: int = 200, wait: float = 0.5, backoff: float = 1.0,
                 connect_timeout: float = 1.5, max_html: int = 6_000_000, egress: str | None = None,
                 user_agent: str = UA):
        """guard: async (url) -> bool, True when the URL may be fetched (fastweb's check_target). egress: the
        browser-egress base URL, from which the marker token is read (None: no marker is trusted)."""
        self.egress: str | None = egress
        self.egress_token: str | None = None
        self.user_agent = user_agent
        self.endpoint, self.guard = endpoint, guard
        self.tabs, self.settle, self.timeout, self.recycle, self.wait = tabs, settle, timeout, recycle, wait
        self.backoff, self.connect_timeout, self.max_html = backoff, connect_timeout, max_html
        self.sem = asyncio.Semaphore(tabs)
        self.waiting = 0  # renders queued for a tab
        self.conn: Conn | None = None
        self.lock = asyncio.Lock()
        self.fails, self.retry_at, self.pages, self.active = 0, 0.0, 0, 0
        self.bg: set[asyncio.Future[None]] = set()  # context disposals running after their page returned
        # "scheme://host:port" -> (ok, expires)
        self.guard_cache: collections.OrderedDict[str, tuple[bool, float]] = collections.OrderedDict()
        self.counters = {"rendered": 0, "busy": 0, "errors": 0, "nav_failed": 0, "private": 0, "blocked_private": 0,
                  "blocked_type": 0, "blocked_tracker": 0, "paused": 0, "recycles": 0, "connects": 0, "hangs": 0,
                  "wedged": 0, "children": 0,
                  # how each settle ended (stable text with no XHR in flight, the settle cap, the page not
                  # answering), and thin renders given one more settle because XHRs were still in flight
                  "settle_stable": 0, "settle_cap": 0, "settle_page_busy": 0, "settle_extended": 0}
        self.render_ms: collections.deque[float] = collections.deque(maxlen=500)
        self.intercept_ms: collections.deque[float] = collections.deque(maxlen=2000)

    def stats(self) -> dict[str, Any]:
        return {**self.counters, "p50_render_ms": p50(self.render_ms), "p50_intercept_ms": p50(self.intercept_ms),
                "active": self.active, "connected": bool(self.conn and not self.conn.dead)}

    # ---------------------------------------------------------------- connection
    async def _open(self) -> Conn:
        import websockets
        u = httpx.URL(self.endpoint)
        if u.scheme in ("ws", "wss"):
            ws_url = self.endpoint
        else:
            # Chrome answers /json only to a Host header that is an IP or localhost: connect by address.
            infos = await asyncio.get_running_loop().getaddrinfo(u.host, u.port or 9222, type=socket.SOCK_STREAM)
            ip = ipaddress.ip_address(str(infos[0][4][0]).split("%")[0])
            hostport = f"[{ip}]:{u.port or 9222}" if ip.version == 6 else f"{ip}:{u.port or 9222}"
            async with httpx.AsyncClient(timeout=self.connect_timeout, trust_env=False) as c:
                j = (await c.get(f"http://{hostport}/json/version")).json()
            ws_url = f"ws://{hostport}{httpx.URL(j['webSocketDebuggerUrl']).raw_path.decode()}"
        ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024, ping_interval=None,
                                      open_timeout=self.connect_timeout, proxy=None)
        return Conn(ws)

    async def _connect(self) -> Conn:
        conn = self.conn
        if conn and not conn.dead:
            return conn
        async with self.lock:
            if self.conn and not self.conn.dead:
                return self.conn
            if time.monotonic() < self.retry_at:
                raise BrowserDown("browser unavailable (reconnect backoff)")
            try:
                self.conn = await asyncio.wait_for(self._open(), self.connect_timeout + 0.5)
            except Exception as e:
                self.conn = None
                self.fails += 1
                self.retry_at = time.monotonic() + min(self.backoff * 2 ** (self.fails - 1), 30.0)
                raise BrowserDown(f"browser unreachable ({type(e).__name__})") from e
            self.fails, self.pages = 0, 0
            self.counters["connects"] += 1
            return self.conn

    async def _recycle(self, conn: Conn) -> None:
        """Browser.close: the container exits and docker restarts it with a fresh profile and heap."""
        self.counters["recycles"] += 1
        if self.conn is conn:
            self.conn = None
        with contextlib.suppress(Exception):
            await conn.send("Browser.close", timeout=2)
        await conn.close()

    def _hung(self, conn: Conn, what: str) -> None:
        """A context create/dispose hung (some sites hang context disposal for seconds): recycle in the background, so
        the hung browser stalls no further page."""
        if self.conn is conn and not conn.dead:
            self.counters["hangs"] += 1
            t = asyncio.ensure_future(self._recycle(conn))
            self.bg.add(t)
            t.add_done_callback(self.bg.discard)

    async def _unresponsive(self, conn: Conn) -> bool:
        """A page call timed out: is the browser process answering at all? If not, drop it (_wedged)."""
        try:
            await conn.send("Browser.getVersion", timeout=0.5)
            return False
        except TimeoutError:
            self._wedged(conn)
            return True
        except BrowserError:
            return True

    def _wedged(self, conn: Conn) -> None:
        """A CDP call timed out on a live socket: drop the connection now (every waiter gets BrowserDown, Firecrawl
        takes their pages) and reconnect with backoff; the container's supervisor restarts the wedged Chrome."""
        if self.conn is conn:
            self.conn = None
            self.counters["wedged"] += 1
            self.fails += 1
            self.retry_at = time.monotonic() + min(self.backoff * 2 ** (self.fails - 1), 30.0)
        t = asyncio.ensure_future(conn.close())
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)

    async def close(self) -> None:
        if self.bg:  # let pending disposals finish (each is bounded by DISPOSE_S)
            await asyncio.wait(list(self.bg), timeout=DISPOSE_S + 0.5)
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ---------------------------------------------------------------- one page
    async def render(self, url: str, timeout: float | None = None, wait: float | None = None,
                     deadline: float | None = None) -> Rendered:
        """Render url in a fresh context. wait: how long to queue for a tab (default self.wait); the page gets
        `timeout` seconds from when it has a tab, but never past `deadline` (time.monotonic())."""
        if self.sem.locked() and self.waiting >= self.tabs * WAITERS_PER_TAB:
            self.counters["busy"] += 1
            raise BrowserBusy("browser busy (too many renders queued for a tab)")
        self.waiting += 1
        try:
            await asyncio.wait_for(self.sem.acquire(), self.wait if wait is None else wait)
        except TimeoutError:
            self.counters["busy"] += 1
            raise BrowserBusy("browser busy (all tabs in use)") from None
        finally:
            self.waiting -= 1
        t0 = time.monotonic()
        end = t0 + min(timeout or self.timeout, self.timeout)
        if deadline is not None:
            end = min(end, deadline)
        if end - t0 < 1.0:
            self.sem.release()
            self.counters["busy"] += 1
            raise BrowserBusy("browser busy (a tab freed too late to render)")
        try:
            try:
                conn = await self._connect()
            except BrowserDown:
                self.counters["errors"] += 1
                raise
            self.active += 1
            try:
                r = await self._page(conn, url, t0, end)
            except BrowserDown:
                self.counters["errors"] += 1
                raise
            except NavFailed:
                self.counters["nav_failed"] += 1
                raise
            except PrivateTarget:
                self.counters["private"] += 1
                raise
            finally:
                self.active -= 1
                self.pages += 1
                if self.pages >= self.recycle and self.active == 0 and self.conn is conn:
                    await asyncio.shield(self._recycle(conn))
            self.counters["rendered"] += 1
            self.render_ms.append(r.ms)
            return r
        finally:
            self.sem.release()

    async def _page(self, conn: Conn, url: str, t0: float, deadline: float) -> Rendered:
        """Load url in a fresh browser context and return its rendered HTML.

        Steps: open a context, target and session and navigate (_navigate); turn a failed navigation into the
        matching error (_check_nav); wait for DOMContentLoaded, reject the egress proxy's own answers, settle
        (_load); read the HTML, capped at max_html (_read_html). Invariants: nothing waits past `deadline`; a CDP
        timeout or protocol error is BrowserDown (the browser, not the page); the context is always disposed, in the
        background, even after a cancellation."""
        pg = Page()
        try:
            nav = await self._navigate(conn, pg, url, deadline)
            self._check_nav(pg, nav)
            await self._load(conn, pg, url, deadline)
            html, big = await self._read_html(conn, pg)
            return Rendered(html, pg.url or url, pg.status or 200, round((time.monotonic() - t0) * 1000, 1),
                            too_large=big)
        finally:
            self._release(conn, pg)

    async def _navigate(self, conn: Conn, pg: Page, url: str, deadline: float) -> dict[str, Any]:
        """Create the page (a hang recycles the browser), enable interception and the hardening, navigate."""
        try:
            try:
                pg.ctx = (await conn.send("Target.createBrowserContext", timeout=SETUP_S))["browserContextId"]
                pg.tid = (await conn.send("Target.createTarget", {"url": "about:blank", "browserContextId": pg.ctx},
                                          timeout=SETUP_S))["targetId"]
                pg.sid = sid = (await conn.send("Target.attachToTarget", {"targetId": pg.tid, "flatten": True},
                                                timeout=SETUP_S))["sessionId"]
            except TimeoutError:
                self._hung(conn, "create")
                raise BrowserDown("browser hung creating a page (recycling it)") from None
            tid = pg.tid
            conn.handlers[sid] = lambda m: self._event(conn, sid, tid, pg, m)
            await asyncio.gather(
                conn.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, sid),
                conn.send("Network.enable", {}, sid),
                conn.send("Page.enable", {}, sid),
                conn.send("Network.setUserAgentOverride",
                          {"userAgent": self.user_agent, "acceptLanguage": "en-AU,en;q=0.9"}, sid),
                conn.send("Network.setBypassServiceWorker", {"bypass": True}, sid),
                conn.send("Network.setBlockedURLs", {"urls": ["ws://*", "wss://*"]}, sid),
                conn.send("Page.addScriptToEvaluateOnNewDocument", {"source": HARDEN}, sid),
                conn.send("Target.setAutoAttach", AUTO_ATTACH, sid))
            return await conn.send("Page.navigate", {"url": url}, sid, timeout=deadline - time.monotonic())
        except TimeoutError:
            raise BrowserDown("browser timed out before the page answered") from None
        except BrowserError as e:
            if isinstance(e, BrowserDown):
                raise
            raise BrowserDown(f"browser protocol error: {e}") from e

    @staticmethod
    def _check_nav(pg: Page, nav: dict[str, Any]) -> None:
        """A navigation error: a private redirect, the egress proxy failing (the browser's fault), or the page's."""
        err = nav.get("errorText")
        if not err:
            return
        if pg.doc_private:
            raise PrivateTarget("redirected to a private address")
        if err in PROXY_ERRORS:
            raise BrowserDown(f"browser egress proxy failed ({err})")
        raise NavFailed(err)

    async def _load(self, conn: Conn, pg: Page, url: str, deadline: float) -> None:
        """Wait for DOMContentLoaded (bounded by the deadline), then settle; without it, stop loading and read what
        is there. The egress proxy's own busy/refused answer is not the page."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pg.dcl.wait(), max(0.0, deadline - time.monotonic()))
        mark = await self._proxy_mark(pg, url)
        if mark == "busy":
            raise BrowserBusy("browser egress proxy busy")
        if mark:
            raise BrowserDown(f"browser egress proxy answered itself ({mark}, HTTP {pg.status})")
        if pg.crashed:
            raise BrowserDown("browser page crashed")
        if pg.dcl.is_set():
            why, n = await self._settle(conn, str(pg.sid), pg, min(time.monotonic() + self.settle, deadline))
            if why == "cap" and (n or 0) < THIN_RENDER and pg.inflight and deadline - time.monotonic() > POLL_S:
                # still thin with XHRs in flight at the cap (a JS-filled listing): one more settle, within the budget
                self.counters["settle_extended"] += 1
                why, n = await self._settle(conn, str(pg.sid), pg, min(time.monotonic() + self.settle, deadline))
            self.counters[f"settle_{why}"] += 1
        else:
            with contextlib.suppress(TimeoutError, BrowserError):  # best effort: the HTML is read either way
                await conn.send("Page.stopLoading", {}, pg.sid, timeout=0.5)

    async def _read_html(self, conn: Conn, pg: Page) -> tuple[str, int]:
        """(html, 0), or ("", length) when the document is over max_html (never transferred)."""
        expr = f"(h => h.length > {int(self.max_html)} ? {{big: h.length}} : h)(document.documentElement.outerHTML)"
        try:
            r = await conn.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, pg.sid, timeout=2.0)
        except TimeoutError:
            await self._unresponsive(conn)
            raise BrowserDown("browser hung reading the page") from None
        except BrowserError as e:
            if isinstance(e, BrowserDown):
                raise
            raise BrowserDown(f"browser protocol error: {e}") from e
        if pg.crashed:
            raise BrowserDown("browser page crashed")
        html = (r.get("result") or {}).get("value") or ""
        big = html.get("big", 1) if isinstance(html, dict) else 0
        return ("" if big else html), big

    def _release(self, conn: Conn, pg: Page) -> None:
        """Drop the page's handlers and tasks; dispose of its context in the background, off the critical path."""
        conn.handlers.pop(pg.sid, None)
        for c in pg.children:
            conn.handlers.pop(c, None)
        for t in pg.tasks:
            t.cancel()
        if pg.ctx:
            task = asyncio.ensure_future(self._dispose(conn, pg.tid, pg.ctx))
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)

    async def _proxy_mark(self, pg: Page, url: str) -> str | None:
        """The proxy's own answer, `busy`/`refused`, only when it carries the proxy's token: a target can send the
        header too. Not unforgeable: page JS can learn the token, so a hostile page can fake it for its own load, at
        worst sending itself to Firecrawl. An https document is never the proxy's (it never answers inside a tunnel; a
        refused CONNECT is ERR_TUNNEL_CONNECTION_FAILED)."""
        if not pg.egress or not self.egress or not (pg.url or url).startswith("http://"):
            return None
        mark, _, tok_s = pg.egress.strip().partition(" ")
        tok = tok_s.strip().encode()
        if not tok:
            return None
        for fresh in (False, True):  # a restarted proxy has a new token: read it again once on a mismatch
            if fresh or not self.egress_token:
                try:
                    async with httpx.AsyncClient(timeout=1.0, trust_env=False) as c:
                        r = await c.get(self.egress.rstrip("/") + "/token")
                    if r.status_code == 200 and r.text.strip():
                        self.egress_token = r.text.strip()
                except httpx.HTTPError:
                    pass
            if self.egress_token and hmac.compare_digest(tok, self.egress_token.encode()):
                return mark
        return None

    async def _dispose(self, conn: Conn, tid: str | None, ctx: str) -> None:
        for method, params in (("Target.closeTarget", {"targetId": tid}),
                               ("Target.disposeBrowserContext", {"browserContextId": ctx})):
            if method == "Target.closeTarget" and not tid:
                continue
            try:
                await conn.send(method, params, timeout=DISPOSE_S)
            except TimeoutError:
                self._hung(conn, "dispose")
                return
            except Exception:  # noqa: BLE001 - disposal is best effort; a hang is handled above
                pass

    async def _settle(self, conn: Conn, sid: str, pg: Page, end: float) -> tuple[str, int | None]:
        """Poll the body text until it is stable and no XHR/fetch is in flight, or until end. Returns how it ended
        ("stable", "cap" or "page_busy": the page did not answer in time) and the last text length."""
        last: Any = None
        since = time.monotonic()
        while True:
            try:
                r = await conn.send("Runtime.evaluate", {"expression": TEXT_LEN, "returnByValue": True}, sid,
                                    timeout=min(0.5, max(0.05, end - time.monotonic())))
                n = (r.get("result") or {}).get("value")
            except TimeoutError:
                if await self._unresponsive(conn):  # the browser itself, not just a busy page
                    raise BrowserDown("browser stopped answering") from None
                return "page_busy", last
            except BrowserError as e:
                if isinstance(e, BrowserDown):
                    raise
                n = None  # a navigation destroyed the context: not stable yet
            now = time.monotonic()
            if n != last:
                last, since = n, now
            elif n and now - since >= STABLE_S and not pg.inflight:  # an empty body is still loading
                return "stable", n
            if now >= end or pg.crashed:
                return "cap", last
            await asyncio.sleep(min(POLL_S, max(0.0, end - now)))

    # ---------------------------------------------------------------- events
    def _event(self, conn: Conn, sid: str, tid: str, pg: Page, m: dict[str, Any], child: bool = False) -> None:
        meth, p = m.get("method"), m.get("params") or {}
        if meth == "Target.attachedToTarget":
            c = p.get("sessionId")
            if c:
                pg.children.add(c)
                conn.handlers[c] = lambda m2: self._event(conn, c, tid, pg, m2, child=True)
                t = asyncio.get_running_loop().create_task(self._child(conn, c))
                pg.tasks.add(t)
                t.add_done_callback(pg.tasks.discard)
        elif meth == "Target.detachedFromTarget":
            if p.get("sessionId") in pg.children:
                conn.handlers.pop(p["sessionId"], None)
        elif child and meth != "Fetch.requestPaused":
            pass  # a worker or frame ending is not the page crashing
        elif meth == "Fetch.requestPaused":
            t = asyncio.get_running_loop().create_task(self._decide(conn, sid, tid, pg, p))
            pg.tasks.add(t)
            t.add_done_callback(pg.tasks.discard)
        elif meth == "Network.requestWillBeSent":
            if p.get("type") in QUIET_TYPES:
                pg.inflight.add(p.get("requestId"))
        elif meth in ("Network.loadingFinished", "Network.loadingFailed"):
            pg.inflight.discard(p.get("requestId"))
        elif meth == "Network.responseReceived":
            if p.get("type") == "Document" and p.get("frameId") == tid:
                resp = p.get("response") or {}
                pg.status, pg.url = resp.get("status"), resp.get("url")
                pg.egress = next((v for k, v in (resp.get("headers") or {}).items()
                                  if k.lower() == "x-fastweb-egress"), None)
        elif meth == "Page.domContentEventFired":
            pg.dcl.set()
        elif meth in ("Inspector.targetCrashed", "Inspector.detached", "fastweb.closed"):
            pg.crashed = True
            pg.dcl.set()

    async def _child(self, conn: Conn, c: str) -> None:
        """A worker / out-of-process frame, paused at start: same guard and blocks, then let it run."""
        self.counters["children"] += 1
        await asyncio.gather(
            conn.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, c, timeout=1),
            conn.send("Network.setBlockedURLs", {"urls": ["ws://*", "wss://*"]}, c, timeout=1),
            conn.send("Target.setAutoAttach", AUTO_ATTACH, c, timeout=1), return_exceptions=True)
        with contextlib.suppress(Exception):
            await conn.send("Runtime.runIfWaitingForDebugger", {}, c, timeout=1)

    async def _allowed(self, url: str) -> bool:
        try:
            u = httpx.URL(url)
            key = f"{u.scheme}://{u.host}:{u.port}"
        except Exception:  # noqa: BLE001 - an unparsable URL is refused
            return False
        hit = self.guard_cache.get(key)
        now = time.monotonic()
        if hit and hit[1] > now:
            return hit[0]
        try:
            ok = bool(await self.guard(url))
        except Exception:  # noqa: BLE001 - a guard failure refuses the request (fail closed)
            ok = False
        self.guard_cache[key] = (ok, now + 30.0)
        self.guard_cache.move_to_end(key)
        while len(self.guard_cache) > 1024:
            self.guard_cache.popitem(last=False)
        return ok

    async def _decide(self, conn: Conn, sid: str, tid: str, pg: Page, p: dict[str, Any]) -> None:
        t = time.perf_counter()
        self.counters["paused"] += 1
        url, rtype = (p.get("request") or {}).get("url", ""), p.get("resourceType")
        fail = None
        if rtype in BLOCK_TYPES:
            fail = "blocked_type"
        elif is_tracker(url):
            fail = "blocked_tracker"
        elif not await self._allowed(url):
            fail = "blocked_private"
            if rtype == "Document" and p.get("frameId") == tid:
                pg.doc_private = True
        if fail:
            self.counters[fail] += 1
        self.intercept_ms.append((time.perf_counter() - t) * 1000)
        try:
            if fail:
                await conn.send("Fetch.failRequest",
                                {"requestId": p.get("requestId"), "errorReason": "BlockedByClient"}, sid, timeout=2)
            else:
                await conn.send("Fetch.continueRequest", {"requestId": p.get("requestId")}, sid, timeout=2)
        except Exception:  # noqa: BLE001 - the page or connection went away; nothing left to answer
            pass
