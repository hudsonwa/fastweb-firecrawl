"""Fail fast on dead hosts, XML/JS bodies, no 500s, fallback SSRF re-check, ports in logs; byte budget before
download, process-model pins, lane wait, windows-1252 default."""
import asyncio
import concurrent.futures
import io
import os
import ssl
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import fakeredis
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from .conftest import ARTICLE, EXTRA, _free_port, serve
from .test_api import scrape


def build(furl, **kw):
    """(server, url, app) with fakeredis and no impersonation."""
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    st = Settings(**{"upstream": furl, "searxng_url": furl, "max_bytes": 2 * 1024 * 1024, "total_timeout": 2.0,
                     "redis_url": "", "dead_reprobe_rate": 0.0, "allow_hosts": frozenset({"127.0.0.1"}), **kw})

    async def imp(url, deadline):
        return 403, url, {"content-type": "text/html"}, b"<html><body>Access denied</body></html>"
    app = create_app(st, redis=fakeredis.FakeAsyncRedis(), impersonator=imp)
    srv, aurl = serve(app)
    return srv, aurl, app


@pytest.fixture
def app12(ctx):
    made = []

    def mk(**kw):
        srv, aurl, app = build(ctx[1], **kw)
        made.append(srv)
        return aurl, app
    yield mk
    for srv in made:
        srv.should_exit = True


def fb(d):
    return d["data"]["metadata"]["fastweb"]


# ---------- (1) dead hosts fail fast; TLS / protocol errors still go to Firecrawl ----------
_real_handle = httpx.AsyncHTTPTransport.handle_async_request
RAISE = {}  # port -> exception factory


@pytest.fixture
def transport_errors(monkeypatch):
    async def handle(self, request):
        if request.url.port in RAISE:
            raise RAISE[request.url.port]()
        return await _real_handle(self, request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    yield RAISE
    RAISE.clear()


def test_refused_port_fails_fast_without_firecrawl(ctx):
    up, _, aurl = ctx
    port = _free_port()  # nothing listens
    t = time.monotonic()
    r = scrape(aurl, f"http://127.0.0.1:{port}/x", timeout=60000)
    assert time.monotonic() - t < 20  # not the 60 s budget
    assert r.status_code == 422 and "refused" in r.json()["error"] and f":{port}" in r.json()["error"]
    assert up.scrapes == []


def test_connect_timeout_fails_fast_504(ctx, transport_errors):
    up, _, aurl = ctx
    port = _free_port()
    transport_errors[port] = lambda: httpx.ConnectTimeout("connect timed out")
    r = scrape(aurl, f"http://127.0.0.1:{port}/x", timeout=60000)
    assert r.status_code == 504 and "connect" in r.json()["error"].lower()
    assert up.scrapes == []


def test_unreachable_fails_fast(ctx, transport_errors):
    up, _, aurl = ctx
    port = _free_port()

    def unreachable():
        e = httpx.ConnectError("[Errno 65] No route to host")
        e.__cause__ = OSError(65, "No route to host")
        return e
    transport_errors[port] = unreachable
    r = scrape(aurl, f"http://127.0.0.1:{port}/x", timeout=60000)
    assert r.status_code == 422 and "unreachable" in r.json()["error"]
    assert up.scrapes == []


def test_tls_handshake_error_still_rescued_by_firecrawl(ctx, transport_errors):
    up, _, aurl = ctx
    port = _free_port()

    def tls():
        e = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired")
        e.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
        return e
    transport_errors[port] = tls
    r = scrape(aurl, f"http://127.0.0.1:{port}/x", timeout=10000)
    assert r.status_code == 200 and fb(r.json())["engine"] == "firecrawl"


@pytest.mark.parametrize("exc", [lambda: httpx.RemoteProtocolError("peer closed"), lambda: httpx.ReadTimeout("slow")])
def test_protocol_and_read_errors_still_rescued_by_firecrawl(ctx, transport_errors, exc):
    up, _, aurl = ctx
    port = _free_port()
    transport_errors[port] = exc
    r = scrape(aurl, f"http://127.0.0.1:{port}/x", timeout=10000)
    assert r.status_code == 200 and fb(r.json())["engine"] == "firecrawl"


def test_safe_url_keeps_port_drops_query():
    from fastweb_firecrawl.heuristics import safe_url
    assert safe_url("http://example.com:81/a?token=x") == "http://example.com:81/a"
    assert safe_url("https://example.com/a?token=x") == "https://example.com/a"
    assert safe_url("https://example.com/a;jsessionid=XYZ/b;v=1") == "https://example.com/a/b"


def test_drop_added_query_strips_challenge_tokens_only():
    from fastweb_firecrawl.heuristics import drop_added_query as d
    page = "https://www.example.com/r/x/1/"
    assert d(page, page + "?js_challenge=1&jsc_token=t&solution=s") == page
    assert d("https://e.com/a?q=1", "https://e.com/a?q=1&t=2") == "https://e.com/a?q=1"
    assert d("https://e.com/a", "https://e.com/b?t=2") == "https://e.com/b?t=2"  # moved: the final URL as is


# ---------- (2) XML / RSS / JavaScript bodies come back as text ----------
RSS = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>BBC News</title>'
       b'<item><title>Harbour vote passes</title><link>https://example.com/a</link></item></channel></rss>')


@pytest.mark.parametrize("ctype,body,want", [
    ("text/xml", RSS, "Harbour vote passes"),
    ("application/xml", b'<?xml version="1.0"?><urlset><url><loc>https://example.com/page</loc></url></urlset>',
     "https://example.com/page"),
    ("application/rss+xml", RSS, "Harbour vote passes"),
    ("application/javascript", b"var harbour = 'tides';", "harbour"),
    ("text/javascript; charset=utf-8", b"var harbour = 'tides';", "harbour"),
])
def test_xml_and_js_bodies_are_text(ctx, ctype, body, want):
    up, furl, aurl = ctx
    EXTRA["xmlish"] = (body, ctype, 200)
    r = scrape(aurl, furl + "/p/xmlish")
    assert r.status_code == 200, r.text
    assert want in r.json()["data"]["markdown"] and up.scrapes == []


def test_xhtml_still_html(ctx):
    up, furl, aurl = ctx
    EXTRA["xhtml"] = (ARTICLE.encode(), "application/xhtml+xml", 200)
    r = scrape(aurl, furl + "/p/xhtml")
    assert r.status_code == 200 and "harbour plan" in r.json()["data"]["markdown"]
    assert not r.json()["data"]["markdown"].lstrip().startswith("<")


# ---------- (3) test gaps ----------
def test_hostname_resolving_to_private_ip_is_refused(ctx, monkeypatch):
    up, _, aurl = ctx
    real = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def fake(self, host, *a, **k):
        if host == "intranet.example":
            import socket
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))]
        return await real(self, host, *a, **k)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake)
    r = scrape(aurl, "http://intranet.example/admin")
    assert r.status_code == 422 and "private" in r.json()["error"]
    assert up.scrapes == []


def test_fallback_success_with_empty_markdown_is_422(ctx):
    up, furl, aurl = ctx
    up.mode = "empty_md"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 422 and "no content" in r.json()["error"]


def test_empty_text_plain_hits_final_guard(ctx):
    up, furl, aurl = ctx
    EXTRA["emptytxt"] = (b"", "text/plain", 200)
    r = scrape(aurl, furl + "/p/emptytxt")
    assert r.status_code == 422 and "no extractable content" in r.json()["error"]


def test_textless_pdf_hits_final_guard(ctx):
    from pypdf import PdfWriter
    up, furl, aurl = ctx
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    EXTRA["blankpdf"] = (buf.getvalue(), "application/pdf", 200)
    r = scrape(aurl, furl + "/p/blankpdf")
    assert r.status_code == 422 and "no extractable content" in r.json()["error"]


def test_redirect_loop_is_422(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/loop")
    assert r.status_code == 422 and "redirects" in r.json()["error"]
    assert up.hits.count("loop") == 11


# ---------- (4) no 500s ----------
@pytest.mark.parametrize("url", ["http://[::1", "http://" + "a" * 64 + ".example.com/", "http://exa mple.com/"])
def test_bad_urls_are_4xx_not_500(ctx, url):
    _, _, aurl = ctx
    r = scrape(aurl, url)
    assert 400 <= r.status_code < 500 and r.json()["success"] is False


@pytest.mark.parametrize("t", ["Infinity", "-Infinity", "NaN", "1e400"])
def test_non_finite_timeout_is_400(ctx, t):
    _, furl, aurl = ctx
    r = httpx.post(aurl + "/v2/scrape", content='{"url": "%s/p/article", "timeout": %s}' % (furl, t),
                   headers={"content-type": "application/json"}, timeout=30)
    assert r.status_code == 400 and "timeout" in r.json()["error"]


def test_fallback_with_null_metadata_is_ok(ctx):
    up, furl, aurl = ctx
    up.mode = "null_meta"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 200 and fb(r.json())["engine"] == "firecrawl"


def test_unexpected_error_is_422_not_500(app12, ctx, monkeypatch):
    from fastweb_firecrawl import pipeline
    aurl, _ = app12()

    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(pipeline, "is_thin", boom)
    r = scrape(aurl, ctx[1] + "/p/article")
    assert r.status_code == 422 and r.json()["success"] is False


# ---------- (5) Firecrawl's final URL gets the same SSRF check ----------
def test_fallback_final_url_private_is_refused(ctx):
    up, furl, aurl = ctx
    up.mode = "private_final"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 422 and "private" in r.json()["error"]
    assert "secret" not in r.text


# ---------- the byte budget is taken before download and counts decoded width ----------
def test_decoded_size_estimate():
    """An upper bound of the decoded str's width (CPython: 1/2/4 bytes per char), exact for ASCII and emoji."""
    from fastweb_firecrawl.heuristics import decoded_size
    assert decoded_size(b"plain ascii") == 11
    assert decoded_size("wave \U0001f30a".encode()) == 4 * 9
    for text, enc in [("caf\u00e9", "utf-8"), ("\u201ccurly\u201d", "utf-8"), ("\u0100x", "utf-8"),
                      ("caf\u00e9", "cp1252"), ("\u201cq\u201d", "cp1252"), ("\u20ac5", "cp1252")]:
        kind = 4 if max(text) > "\uffff" else 2 if max(text) > "\xff" else 1
        assert decoded_size(text.encode(enc)) >= kind * len(text), (text, enc)


def test_budget_counts_decoded_emoji_width_and_is_released(app12, ctx):
    up, furl, _ = ctx
    page = ("<html><body><article>" + "<p>Harbour tides and the council vote, a long paragraph.</p>" * 400
            + "<p>\U0001f30a</p></article></body></html>").encode()
    EXTRA["emoji"] = (page, "text/html; charset=utf-8", 200)
    aurl, app = app12()
    r = scrape(aurl, furl + "/p/emoji", timeout=10000)
    b = app.state.budget
    assert r.status_code == 200
    assert b.peak >= 4 * len(page)  # decoded str is 4 bytes/char with an astral char
    assert b.used == 0


def test_budget_reserved_before_body_is_read(app12, ctx):
    """While a Content-Length body streams in, its bytes are already reserved."""
    _, furl, _ = ctx
    n = 300_000
    seen = {}

    async def slow(request):
        async def body():
            yield b"<html><body>" + b"x" * 1000
            await asyncio.sleep(0.8)
            yield b"y" * (n - 1012) + b"</body>"
        return StreamingResponse(body(), headers={"content-length": str(n - 5), "content-type": "text/html"})
    fsrv, surl = serve(Starlette(routes=[Route("/s", slow)]))
    aurl, app = app12(total_timeout=5.0)
    try:
        t = threading.Thread(target=lambda: seen.setdefault("r", scrape(aurl, surl + "/s", timeout=10000)))
        t.start()
        time.sleep(0.5)
        seen["used"] = app.state.budget.used
        t.join()
    finally:
        fsrv.should_exit = True
    assert seen["used"] >= n - 5


def test_budget_full_before_download_goes_to_firecrawl(app12, ctx):
    up, furl, _ = ctx
    aurl, app = app12(fallback_timeout=1.0)
    from fastweb_firecrawl.worker import Hold
    Hold(app.state.budget).force(app.state.budget.cap)  # every byte held by an older request
    try:
        r = scrape(aurl, furl + "/p/article", timeout=3000)
    finally:
        app.state.budget.holders.clear()
        app.state.budget.used = 0
    assert r.status_code == 200 and fb(r.json())["engine"] == "firecrawl"
    assert "busy" in fb(r.json())["fallback_reason"]


# ---------- process-model claims ----------
class TimerPool:
    """Executor stand-in: each job completes after `delay` s (never, if None) on a timer; no thread to wait for."""
    names = []

    def __init__(self, name, delay=0.0, exc=None):
        self.name, self.delay, self.exc = name, delay, exc
        self.active = 0
        self.max_active = 0
        TimerPool.names.append(name)

    def submit(self, fn, *args):
        f = concurrent.futures.Future()
        self.active += 1
        self.max_active = max(self.max_active, self.active)

        def done():
            self.active -= 1
            if not f.set_running_or_notify_cancel():  # the caller gave up on it (timed out)
                return
            if self.exc:
                f.set_exception(self.exc)
            else:
                f.set_result(fn(*args))
        if self.delay is not None:
            threading.Timer(self.delay, done).start()
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def test_lanes_table():
    from fastweb_firecrawl.worker import LANES
    assert LANES == {"small": 25, "big": 1, "pdf": 1, "pf": 1}  # pf: a fresh worker per page


def test_lane_runs_one_page_at_a_time():
    from fastweb_firecrawl.worker import Lane
    pool = TimerPool("t", delay=0.2)
    lane = Lane("t", 1, lambda: pool)

    async def go():
        await asyncio.gather(lane.run(5, 5, len, "a"), lane.run(5, 5, len, "b"))
    asyncio.new_event_loop().run_until_complete(go())
    assert pool.max_active == 1


def _pid_then_sleep(path, s):
    Path(path).write_text(str(os.getpid()))
    time.sleep(s)


def test_timed_out_worker_process_is_gone(tmp_path):
    from fastweb_firecrawl.worker import Lane, WorkerFailed
    lane = Lane("t", tasks_per_child=10)
    pidfile = tmp_path / "pid"

    async def go():
        with pytest.raises(WorkerFailed):
            await lane.run(5, 3.0, _pid_then_sleep, str(pidfile), 60)  # 3 s: room for a slow worker spawn
    try:
        asyncio.new_event_loop().run_until_complete(go())
        pid = int(pidfile.read_text())
        gone, deadline = False, time.monotonic() + 15  # polled, not a tight window: a loaded host reaps slowly
        while not gone and time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
            else:
                time.sleep(0.05)
        assert gone, "timed-out worker still running"
    finally:
        lane.close()


def test_worker_failure_goes_to_firecrawl(app12, ctx):
    up, furl, _ = ctx
    aurl, _ = app12(lane_factory=lambda name: TimerPool(name, exc=BrokenProcessPool("dead")))
    r = scrape(aurl, furl + "/p/article", timeout=10000)
    assert r.status_code == 200 and fb(r.json())["engine"] == "firecrawl"
    assert "worker died" in fb(r.json())["fallback_reason"]


def test_extract_hard_timeout_is_capped(app12, ctx):
    up, furl, _ = ctx
    aurl, _ = app12(lane_factory=lambda name: TimerPool(name, delay=None), extract_timeout=0.5)
    t = time.monotonic()
    r = scrape(aurl, furl + "/p/article", timeout=30000)
    assert time.monotonic() - t < 15  # 0.5 s hard timeout, not the 30 s budget
    assert r.status_code == 200 and "timed out" in fb(r.json())["fallback_reason"]


def test_defaults_pinned(monkeypatch):
    from fastweb_firecrawl.settings import Settings
    for k in ("FASTWEB_EXTRACT_TIMEOUT", "FASTWEB_DEAD_REPROBE_RATE", "FASTWEB_INFLIGHT_BYTES"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.extract_timeout == 20 and s.dead_reprobe_rate == 0.05 and s.inflight_bytes == 48 * 1024 * 1024


def test_pdf_goes_to_pdf_lane(app12, ctx):
    up, furl, _ = ctx
    TimerPool.names.clear()
    aurl, _ = app12(lane_factory=lambda name: TimerPool(name))
    r = scrape(aurl, furl + "/p/pdf")
    assert r.status_code == 200 and fb(r.json())["lane"] == "pdf" and TimerPool.names == ["pdf"]


@pytest.mark.slow
def test_health_answers_while_page_extracts(app12, ctx):
    up, furl, _ = ctx
    aurl, _ = app12(lane_factory=lambda name: TimerPool(name, delay=8.0))
    th = threading.Thread(target=scrape, args=(aurl, furl + "/p/article"), kwargs={"timeout": 10000})
    th.start()
    time.sleep(0.3)
    t = time.monotonic()
    assert httpx.get(aurl + "/health", timeout=10).status_code == 200
    assert time.monotonic() - t < 5  # not after the 8 s extraction
    th.join()


@pytest.mark.slow
def test_concurrent_bodies_over_budget_go_to_firecrawl(app12, ctx):
    up, furl, _ = ctx
    aurl, app = app12(lane_factory=lambda name: TimerPool(name, delay=1.5), inflight_bytes=10, fallback_timeout=1.0)
    out = []
    ths = [threading.Thread(target=lambda i=i: out.append(scrape(aurl, furl + f"/p/article?c={i}", timeout=4000).json()))
           for i in range(3)]  # distinct URLs: identical ones would join one single-flight fetch
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    engines = sorted(fb(d)["engine"] for d in out)
    assert engines.count("plain") >= 1
    assert any("busy" in (fb(d).get("fallback_reason") or "") for d in out)
    assert app.state.budget.used == 0


# ---------- lane wait keeps room for Firecrawl; never past the request's timeout ----------
@pytest.mark.slow
def test_lane_queue_busy_goes_to_firecrawl_within_timeout(app12, ctx):
    up, furl, _ = ctx
    aurl, _ = app12(lane_factory=lambda name: TimerPool(name, delay=6.0), fallback_timeout=1.5)
    out = []

    def one(ms):
        t = time.monotonic()
        r = scrape(aurl, furl + f"/p/article?q={ms}", timeout=ms)  # distinct: no single-flight join
        out.append((time.monotonic() - t, r))
    # the second has less time: it gives up on the lane (~1.5 s) before the first is killed (~2.5 s)
    ths = [threading.Thread(target=one, args=(ms,)) for ms in (5000, 4000)]
    for t in ths:
        t.start()
        time.sleep(0.1)
    for t in ths:
        t.join()
    for took, r in out:
        assert took < 5.0 and r.status_code == 200 and fb(r.json())["engine"] == "firecrawl"
    reasons = [fb(r.json())["fallback_reason"] for _, r in out]
    assert any("queue busy" in x for x in reasons) and any("timed out" in x for x in reasons)


# ---------- undeclared legacy pages decode as windows-1252 ----------
LEGACY = ["Le prix est de 12 \u00a3 pour les \u00e9l\u00e8ves, pr\u00e8s de la gare.",
          "Les \u00e9l\u00e8ves sont pr\u00e8s du lyc\u00e9e. C'est na\u00efve. Le billet co\u00fbte \u00a312.",
          "Informa\u00e7\u00e3o: n\u00e3o h\u00e1 aulas. Pre\u00e7o \u00a35."]


@pytest.mark.parametrize("n", [1, 3, 30])
@pytest.mark.parametrize("text", LEGACY)
def test_undeclared_western_legacy_is_cp1252(text, n):
    """French/Portuguese + pound sign were sniffed as cp1250/cp932 mojibake."""
    from fastweb_firecrawl.extract import decode_html
    html = ("<html><body><p>" + text + "</p></body></html>") * n
    assert decode_html(html.encode("cp1252"), "text/html") == html


def test_undeclared_cyrillic_still_sniffed():
    from fastweb_firecrawl.extract import decode_html
    html = "<html><body><p>" + "\u041f\u0440\u0438\u0432\u0435\u0442, \u043c\u0438\u0440! \u042d\u0442\u043e \u0442\u0435\u0441\u0442 \u043a\u043e\u0434\u0438\u0440\u043e\u0432\u043a\u0438. " * 30 + "</p></body></html>"
    assert decode_html(html.encode("cp1251"), "text/html") == html


# ---------- worker start-up and reaping ----------
def test_compose_fastweb_reaps_zombies():
    import yaml
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())
    assert compose["services"]["fastweb"].get("init") is True


def test_lifespan_warms_small_lane(ctx):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    app = create_app(Settings(upstream=ctx[1], redis_url="", allow_hosts=frozenset({"127.0.0.1"})),
                     redis=fakeredis.FakeAsyncRedis())
    srv, _ = serve(app)
    try:
        for _ in range(100):
            if app.state.lanes["small"].pool is not None:
                break
            time.sleep(0.05)
        assert app.state.lanes["small"].pool is not None
    finally:
        srv.should_exit = True


def test_budget_for_decoded_page_taken_before_decode(app12, ctx):
    """The body fits the budget but its decoded copy does not: Firecrawl renders it (no decode here)."""
    up, furl, _ = ctx
    aurl, app = app12(fallback_timeout=1.0)
    b = app.state.budget
    from fastweb_firecrawl.worker import Hold
    Hold(b).force(b.cap - len(ARTICLE) - 10)  # an older request leaves room for the download only
    try:
        r = scrape(aurl, furl + "/p/article", timeout=3000)
    finally:
        b.holders.clear()
        b.used = 0
    assert r.status_code == 200 and "busy" in fb(r.json())["fallback_reason"]


def test_budget_oldest_holder_always_grows():
    """Holders that each fit their download but not their decoded copy must not jam: the oldest grows."""
    from fastweb_firecrawl.worker import ByteBudget, Hold
    b = ByteBudget(100)

    async def go():
        a, c = Hold(b), Hold(b)
        assert await a.to(50, 0) and await c.to(50, 0)
        assert not await c.to(200, 0.05)   # not the oldest: waits, gives up
        assert await a.to(200, 0.05)       # the oldest always fits
        a.release()
        assert await c.to(200, 0.05)       # now c is the oldest
        c.release()
        return b.used
    assert asyncio.new_event_loop().run_until_complete(go()) == 0
