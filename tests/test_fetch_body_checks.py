"""Binary bodies under text types, TLS-stage resets/stalls keep the fallback, bodies without Content-Length don't
reserve the whole cap inside plain_sem, and edge cases of those checks."""
import asyncio
import gzip
import random
import socket
import struct
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .conftest import ARTICLE, EXTRA, serve
from .test_api import scrape
from .test_pipeline_fail_fast import TimerPool, app12, build, fb

NEW_TEXT_TYPES = ["application/xml", "text/xml", "application/javascript", "text/javascript",
                  "application/x-javascript", "application/ecmascript"]
RANDOM = random.Random(12).randbytes(30_000)
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + random.Random(1).randbytes(4000)
SITEMAP = ('<?xml version="1.0"?><urlset>'
           + "".join(f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(200)) + "</urlset>").encode()
GZ_SITEMAP = gzip.compress(SITEMAP)


# ---------- binary garbage under a text type is an honest 422, not success ----------
@pytest.mark.parametrize("ctype", NEW_TEXT_TYPES + ["text/plain"])
@pytest.mark.parametrize("kind,body", [("random", RANDOM), ("png", PNG), ("gzip", GZ_SITEMAP)])
def test_binary_body_under_text_type_is_422(ctx, ctype, kind, body):
    up, furl, aurl = ctx
    EXTRA["bin"] = (body, ctype, 200)
    r = scrape(aurl, furl + "/p/bin")
    assert r.status_code == 422, (kind, ctype, r.text[:200])
    assert "binary" in r.json()["error"]
    assert up.scrapes == []


def test_utf16_xml_without_bom_is_422(ctx):
    up, furl, aurl = ctx
    EXTRA["u16"] = (SITEMAP.decode().encode("utf-16-le"), "text/xml", 200)
    r = scrape(aurl, furl + "/p/u16")
    assert r.status_code == 422 and "binary" in r.json()["error"]


def test_real_text_with_a_few_odd_chars_is_still_text(ctx):
    up, furl, aurl = ctx
    EXTRA["oddxml"] = (SITEMAP.replace(b"p1<", b"p1\xff<"), "application/xml", 200)
    r = scrape(aurl, furl + "/p/oddxml")
    assert r.status_code == 200 and "https://example.com/p199" in r.json()["data"]["markdown"]


# ---------- a reset or stall during the TLS handshake keeps the fallback (real sockets) ----------
def _tls_server(after_hello):
    """Listens on 127.0.0.1; on each connection reads the ClientHello, then calls after_hello(conn)."""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(8)
    stop = threading.Event()

    def run():
        ls.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = ls.accept()
            except OSError:
                continue
            c.settimeout(5)
            try:
                c.recv(4096)
                after_hello(c, stop)
            except OSError:
                pass
            finally:
                c.close()
        ls.close()
    threading.Thread(target=run, daemon=True).start()
    return ls.getsockname()[1], stop


def _rst(c, stop):
    c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def _stall(c, stop):
    stop.wait(6)


@pytest.mark.slow
@pytest.mark.parametrize("shape", [_rst, _stall])
def test_tls_stage_reset_or_stall_goes_to_firecrawl(app12, ctx, shape):
    up, _, _ = ctx
    port, stop = _tls_server(shape)
    aurl, _ = app12(total_timeout=5.0)
    try:
        r = scrape(aurl, f"https://127.0.0.1:{port}/x", timeout=20000)
    finally:
        stop.set()
    assert r.status_code == 200, r.text
    assert fb(r.json())["engine"] == "firecrawl" and len(up.scrapes) == 1


def test_tcp_refused_on_https_still_fails_fast(ctx):
    from .conftest import _free_port
    up, _, aurl = ctx
    port = _free_port()
    r = scrape(aurl, f"https://127.0.0.1:{port}/x", timeout=60000)
    assert r.status_code == 422 and "refused" in r.json()["error"] and up.scrapes == []


# ---------- a no-CL body doesn't wait for the whole cap while the budget is mostly held ----------
def _nocl_app(chunks, gap=0.0, ctype="text/html"):
    async def page(request):
        async def body():
            for i, c in enumerate(chunks):
                if i and gap:
                    await asyncio.sleep(gap)
                yield c
        return StreamingResponse(body(), headers={"content-type": ctype})  # chunked: no Content-Length
    return serve(Starlette(routes=[Route("/n", page)]))


def test_nocl_small_page_answers_plain_while_budget_mostly_held(app12, ctx):
    up, _, _ = ctx
    fsrv, surl = _nocl_app([ARTICLE.encode()])
    aurl, app = app12(total_timeout=20.0, fallback_timeout=1.0)
    b = app.state.budget
    from fastweb_firecrawl.worker import Hold
    Hold(b).force(b.cap - 300_000)  # a big page in flight holds most of the budget
    try:
        t = time.monotonic()
        r = scrape(aurl, surl + "/n", timeout=10000)
        dt = time.monotonic() - t
    finally:
        b.holders.clear()
        b.used = 0
        fsrv.should_exit = True
    assert r.status_code == 200 and fb(r.json())["engine"] == "plain", r.text[:300]
    assert dt < 8.0  # not held for the budget until the deadline


def _sample_used_midstream(app, aurl, url, first, rest, gap=0.8, **kw):
    seen = {}
    t = threading.Thread(target=lambda: seen.setdefault("r", scrape(aurl, url, timeout=10000)))
    t.start()
    time.sleep(gap / 2)
    seen["used"] = app.state.budget.used
    t.join()
    return seen


def test_nocl_body_is_counted_as_it_arrives(app12, ctx):
    """Plain fetch, no Content-Length: the bytes already received are in the budget mid-stream."""
    first = b"<html><body>" + b"x" * 600_000
    fsrv, surl = _nocl_app([first, b"</body></html>"], gap=0.8)
    aurl, app = app12(total_timeout=5.0)
    try:
        seen = _sample_used_midstream(app, aurl, surl + "/n", first, None)
    finally:
        fsrv.should_exit = True
    assert seen["used"] >= len(first)


# ---------- curl_cffi budget path (real impersonated_fetch, no fake) ----------
def _imp_app(chunks, with_cl):
    hits = []

    async def page(request):
        hits.append(1)
        if len(hits) == 1:
            return Response(b"<html><body>Access denied</body></html>", status_code=403,
                            headers={"content-type": "text/html"})

        async def body():
            for i, c in enumerate(chunks):
                if i:
                    await asyncio.sleep(0.8)
                yield c
        h = {"content-type": "text/html"}
        if with_cl:
            h["content-length"] = str(sum(map(len, chunks)))
        return StreamingResponse(body(), headers=h)
    return serve(Starlette(routes=[Route("/i", page)]))


@pytest.mark.parametrize("with_cl", [True, False])
def test_impersonated_body_is_in_the_budget(ctx, with_cl):
    pytest.importorskip("curl_cffi")
    import fakeredis

    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    _, furl, _ = ctx
    first = b"<html><body>" + b"x" * 600_000
    fsrv, surl = _imp_app([first, b"</body></html>"], with_cl)
    st = Settings(upstream=furl, searxng_url=furl, max_bytes=2 * 1024 * 1024, total_timeout=5.0, redis_url="",
                  dead_reprobe_rate=0.0, allow_hosts=frozenset({"127.0.0.1"}), impersonate=True)
    app = create_app(st, redis=fakeredis.FakeAsyncRedis())
    asrv, aurl = serve(app)
    try:
        seen = _sample_used_midstream(app, aurl, surl + "/i", first, None, gap=1.6)
    finally:
        fsrv.should_exit = asrv.should_exit = True
    assert seen["used"] >= len(first)
    assert app.state.budget.used == 0


# ---------- other survivors ----------
def test_budget_oldest_holder_with_three_holders():
    """A released holder leaves the admission order, so the next one becomes the oldest (holder-nopop)."""
    from fastweb_firecrawl.worker import ByteBudget, Hold
    b = ByteBudget(100)

    async def go():
        a, c, d = Hold(b), Hold(b), Hold(b)
        assert await a.to(30, 0) and await c.to(30, 0) and await d.to(30, 0)
        a.release()
        assert await c.to(200, 0.05)       # c is now the oldest
        assert not await d.to(200, 0.05)   # d is not
        c.release()
        d.release()
        return b.used, len(b.holders)
    assert asyncio.new_event_loop().run_until_complete(go()) == (0, 0)


def test_holder_alone_always_fits():
    from fastweb_firecrawl.worker import ByteBudget, Hold
    b = ByteBudget(100)

    async def go():
        a = Hold(b)
        ok = await a.to(500, 0)
        a.release()
        return ok
    assert asyncio.new_event_loop().run_until_complete(go())


def test_decode_waits_for_budget_to_free(app12, ctx):
    """The decoded copy doesn't fit now but does 0.4 s later: the page is answered plain (decode-wait0)."""
    up, furl, _ = ctx
    aurl, app = app12(total_timeout=5.0, fallback_timeout=1.0)
    b = app.state.budget
    from fastweb_firecrawl.worker import Hold
    other = Hold(b)
    other.force(b.cap - len(ARTICLE) - 10)

    def free_later():
        time.sleep(0.4)
        b.cond._loop.call_soon_threadsafe(other.release)
    threading.Thread(target=free_later, daemon=True).start()
    r = scrape(aurl, furl + "/p/article", timeout=10000)
    assert r.status_code == 200 and fb(r.json())["engine"] == "plain", r.text[:300]


def test_raw_body_dropped_from_budget_during_extraction(app12, ctx, monkeypatch):
    """While the page extracts, only the decoded copies are held, not the raw bytes too (decode-no-shrink)."""
    from fastweb_firecrawl import pipeline
    from fastweb_firecrawl.heuristics import decoded_size
    _, furl, _ = ctx
    seen = {}
    aurl, app = app12(lane_factory=lambda name: TimerPool(name))
    real = pipeline.extract_job

    def spy(*a):
        seen["used"] = app.state.budget.used
        return real(*a)
    monkeypatch.setattr(pipeline, "extract_job", spy)
    r = scrape(aurl, furl + "/p/article", timeout=10000)
    assert r.status_code == 200
    assert seen["used"] == 2 * decoded_size(ARTICLE.encode())


@pytest.mark.slow
def test_budget_released_before_firecrawl_call(app12, ctx):
    """A page sent to Firecrawl holds no budget while Firecrawl works (fallback-no-release)."""
    up, furl, _ = ctx
    aurl, app = app12()
    seen = {}
    t = threading.Thread(target=lambda: seen.setdefault("r", scrape(aurl, furl + "/p/consent", timeout=10000)))
    up.mode = "slow"
    t.start()
    deadline = time.monotonic() + 5
    while not up.scrapes and time.monotonic() < deadline:
        time.sleep(0.02)
    seen["used"] = app.state.budget.used
    up.mode = ""
    t.join()
    assert up.scrapes and seen["used"] == 0


def test_xhtml_served_as_text_xml_is_extracted(ctx):
    up, furl, aurl = ctx
    xhtml = ARTICLE.replace("<html>", '<html xmlns="http://www.w3.org/1999/xhtml">', 1)
    EXTRA["xh"] = (('<?xml version="1.0"?>' + xhtml).encode(), "text/xml", 200)
    r = scrape(aurl, furl + "/p/xh")
    assert r.status_code == 200
    md = r.json()["data"]["markdown"]
    assert "harbour plan" in md and "<p>" not in md


def test_killed_worker_is_joined():
    from fastweb_firecrawl.worker import Lane
    calls = []

    class P:
        def kill(self):
            calls.append("kill")

        def join(self, t=None):
            calls.append("join")

    class Pool:
        _processes = {1: P()}

        def shutdown(self, wait=True, cancel_futures=False):
            pass
    lane = Lane("x", 1)
    lane.pool = Pool()
    lane.close()
    assert calls == ["kill", "join"]


def test_is_tls_error_by_cause_or_text():
    import ssl

    from fastweb_firecrawl.heuristics import is_tls_error
    e = OSError("handshake went wrong")
    e.__cause__ = ssl.SSLError(1, "x")
    assert is_tls_error(e)                                     # by cause, no telltale text
    assert is_tls_error(OSError("[SSL: WRONG_VERSION_NUMBER] wrong version number"))  # by text alone
    assert not is_tls_error(ConnectionResetError(54, "Connection reset by peer"))
