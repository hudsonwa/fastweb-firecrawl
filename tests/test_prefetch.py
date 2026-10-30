"""Prefetch-on-search — top-N search results fetched into the scrape cache in the background."""
import asyncio
import json
import threading
import time
from pathlib import Path

import fakeredis
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .conftest import ARTICLE, CHALLENGE, JS_SHELL, make_pdf, serve

SDK_BODY = json.loads((Path(__file__).parent / "data" / "sdk_scrape_body_4.17.json").read_text())


class Fake:
    """Page server + SearXNG + Firecrawl upstream in one; records everything."""

    def __init__(self):
        self.results = []      # URLs the fake SearXNG returns
        self.hits = []         # page names fetched
        self.inflight = 0
        self.max_inflight = 0
        self.fc = []           # Firecrawl upstream calls
        self.delay = {}        # page name -> seconds
        self.pages = {}        # page name -> HTML served instead of the built-in pages
        self.extra = {}        # page name -> HTML served with 200
        self.gate = None       # threading.Event: when set to one, pages wait for it before answering

    def app(self):
        pdf = make_pdf() + b"%" + b"0" * 20_000

        async def page(request):
            name = request.path_params["name"]
            self.hits.append(name)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                if self.gate is not None:
                    await asyncio.to_thread(self.gate.wait, 30)
                await asyncio.sleep(self.delay.get(name, self.delay.get("*", 0)))
                if name in self.pages:
                    return HTMLResponse(self.pages[name])
                if name in self.extra:
                    return HTMLResponse(self.extra[name])
                if name.startswith("art"):
                    return HTMLResponse(ARTICLE.replace("Big Story", f"Story {name}"))
                if name == "shell":
                    return HTMLResponse(JS_SHELL)
                if name == "challenge":
                    return HTMLResponse(CHALLENGE, status_code=403)
                if name == "err":
                    return HTMLResponse("boom", status_code=500)
                if name == "bigpdf":
                    return Response(pdf, media_type="application/pdf")
                if name == "streampdf":
                    async def gen():
                        yield pdf[:10_000]
                        yield pdf[10_000:]
                    return StreamingResponse(gen(), media_type="application/pdf")
                if name == "to_internal":
                    return RedirectResponse(f"http://localhost:{request.url.port}/p/art-internal", status_code=302)
                return HTMLResponse("?", status_code=500)
            finally:
                self.inflight -= 1

        async def searx(request):
            return JSONResponse({"results": [{"url": u, "title": f"R{i}", "content": "s"}
                                             for i, u in enumerate(self.results)]})

        async def fc(request):
            body = await request.json()
            self.fc.append(body)
            return JSONResponse({"success": True, "data": {"markdown": "# Rendered by Firecrawl\n\nfull text",
                                                             "metadata": {"sourceURL": body["url"], "statusCode": 200}}})

        return Starlette(routes=[Route("/p/{name}", page), Route("/search", searx),
                                 Route("/v2/scrape", fc, methods=["POST"])])


@pytest.fixture
def fake():
    f = Fake()
    srv, url = serve(f.app())
    f.url = url
    yield f
    srv.should_exit = True


def make(fake, **kw):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    server = fakeredis.FakeServer()
    r = fakeredis.FakeAsyncRedis(server=server)
    imp_calls = []

    async def imp(url, deadline):  # impersonation refused like plain
        imp_calls.append(url)
        return 403, url, {"content-type": "text/html"}, b"<html><body>Access denied</body></html>"
    st = Settings(**{"upstream": fake.url, "search_relevance": False,  # synthetic results
                     "searxng_url": fake.url, "max_bytes": 2 * 1024 * 1024,
                     "total_timeout": 3.0, "redis_url": "", "allow_hosts": frozenset({"127.0.0.1"}),
                     "prefetch_n": 5, "prefetch_concurrency": 4, "prefetch_timeout": 3.0, **kw})
    srv, aurl = serve(create_app(st, redis=r, impersonator=imp))
    return aurl, fakeredis.FakeRedis(server=server), imp_calls


def search(aurl, q="harbour", path="/v2/search", limit=5):
    return httpx.post(aurl + path, json={"query": q, "limit": limit}, timeout=10)


def sdk_scrape(aurl, url):
    return httpx.post(aurl + "/v2/scrape", json={**SDK_BODY, "url": url}, timeout=30)


def stats(aurl):
    return httpx.get(aurl + "/v1/fastweb/stats", timeout=5).json()


def settle(aurl, n, timeout=20.0):
    """Wait until n prefetch jobs have finished (done + failed + skipped after scheduling + gave up)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        p = stats(aurl)["prefetch"]
        if p["done"] + p["failed"] + p["skipped_late"] + p.get("uncached", 0) + p.get("gave_up", 0) >= n:
            return p
        time.sleep(0.05)
    raise AssertionError(f"prefetch did not settle: {stats(aurl)}")


def urls(fake, *names):
    return [f"{fake.url}/p/{n}" for n in names]


# ---------- fire-and-forget ----------
def test_search_returns_while_prefetch_pending_and_body_unchanged(fake):
    """The search answer does not wait for its prefetches: every page is held back (gate closed) until the search
    has answered, so a search that awaited them could not return before they finish or time out."""
    fake.results = urls(fake, "art1", "art2", "art3")
    fake.gate = threading.Event()
    on, _, _ = make(fake, prefetch_timeout=20.0)
    off, _, _ = make(fake, prefetch_n=0)
    try:
        r_on = search(on)
        assert r_on.status_code == 200
        p = stats(on)["prefetch"]
        assert p["scheduled"] == 3
        assert p["done"] + p["failed"] + p["gave_up"] + p["uncached"] == 0, p  # all still pending behind the gate
        r_off = search(off)
        assert r_on.json() == r_off.json()
    finally:
        fake.gate.set()
    p = settle(on, 3, timeout=30.0)
    assert p["done"] == 3, p


def test_v1_search_prefetches_too(fake):
    fake.results = urls(fake, "art1")
    aurl, _, _ = make(fake)
    assert search(aurl, path="/v1/search").status_code == 200
    assert settle(aurl, 1)["done"] == 1


def test_n_zero_disables(fake):
    fake.results = urls(fake, "art1", "art2")
    aurl, _, _ = make(fake, prefetch_n=0)
    search(aurl)
    time.sleep(0.3)
    assert fake.hits == [] and stats(aurl)["prefetch"]["scheduled"] == 0


def test_at_most_n_per_search(fake):
    fake.results = urls(fake, *[f"art{i}" for i in range(8)])
    aurl, _, _ = make(fake, prefetch_n=3)
    search(aurl, limit=8)
    settle(aurl, 3)
    time.sleep(0.2)
    assert sorted(fake.hits) == ["art0", "art1", "art2"]


# ---------- cache key + attribution ----------
def test_sdk_body_scrape_after_prefetch_is_a_hit(fake):
    fake.results = urls(fake, "art1")
    aurl, _, _ = make(fake)
    search(aurl)
    settle(aurl, 1)
    r = sdk_scrape(aurl, fake.results[0])
    assert r.status_code == 200 and r.headers["X-Fastweb-Cache"] == "HIT-PREFETCH"
    assert "Story art1" in r.json()["data"]["markdown"] and "html" in r.json()["data"]
    assert fake.hits == ["art1"] and fake.fc == []
    s = stats(aurl)
    assert s["scrape"]["hit_prefetched"] == 1 and s["scrape"]["hit_other"] == 0


def test_markdown_only_scrape_after_search_is_a_prefetch_hit(fake):
    """The documented usage: search, then scrape a result with formats ["markdown"] (README, examples) or with no
    formats at all (the SDK's scrape(url)). Both are served from the prefetched page."""
    fake.results = urls(fake, "art1", "art2", "art3")
    aurl, _, _ = make(fake)
    search(aurl)
    settle(aurl, 3)
    md = httpx.post(aurl + "/v2/scrape", json={"url": fake.results[0], "formats": ["markdown"]}, timeout=30)
    assert md.status_code == 200 and md.headers["X-Fastweb-Cache"] == "HIT-PREFETCH"
    assert set(md.json()["data"]) == {"markdown", "metadata"} and "Story art1" in md.json()["data"]["markdown"]
    bare = httpx.post(aurl + "/v1/scrape", json={"url": fake.results[1]}, timeout=30)
    assert bare.headers["X-Fastweb-Cache"] == "HIT-PREFETCH" and "Story art2" in bare.json()["data"]["markdown"]
    html = httpx.post(aurl + "/v2/scrape", json={"url": fake.results[2], "formats": ["html"]}, timeout=30)
    assert html.headers["X-Fastweb-Cache"] == "HIT-PREFETCH" and set(html.json()["data"]) == {"html", "metadata"}
    assert sorted(fake.hits) == ["art1", "art2", "art3"] and fake.fc == []
    assert stats(aurl)["scrape"]["hit_prefetched"] == 3


# ---------- dedupe ----------
@pytest.mark.slow
def test_dedupe_cached_and_repeated_searches(fake):
    fake.results = urls(fake, "art1", "art2")
    aurl, _, _ = make(fake)
    sdk_scrape(aurl, fake.results[0])  # already cached on demand
    fake.hits.clear()
    fake.delay["*"] = 0.5
    search(aurl)
    search(aurl)  # same URLs while the first prefetch is in flight
    time.sleep(1.5)
    search(aurl)  # same URLs after they are cached
    time.sleep(0.5)
    assert fake.hits == ["art2"]
    assert stats(aurl)["prefetch"]["skipped"].get("cached", 0) >= 1


# ---------- bounds ----------
def test_per_host_cap(fake):
    fake.results = urls(fake, *[f"art{i}" for i in range(5)])
    fake.delay["*"] = 0.4
    aurl, _, _ = make(fake, prefetch_per_host=2)
    search(aurl)
    p = settle(aurl, 5)
    assert p["done"] == 5 and fake.max_inflight == 2


def test_queue_full_drops_counted(fake):
    fake.results = urls(fake, *[f"art{i}" for i in range(5)])
    fake.delay["*"] = 0.5
    aurl, _, _ = make(fake, prefetch_concurrency=1, prefetch_queue=2)
    search(aurl)
    time.sleep(0.1)
    p = stats(aurl)["prefetch"]
    assert p["dropped"] == 3 and p["scheduled"] == 5  # submit is synchronous: no worker ran yet


# ---------- single-flight ----------
def test_single_flight_on_demand_joins_prefetch(fake):
    fake.results = urls(fake, "art1")
    fake.delay["art1"] = 1.0
    aurl, _, _ = make(fake)
    search(aurl)
    time.sleep(0.2)
    r = sdk_scrape(aurl, fake.results[0])
    assert r.status_code == 200 and "Story art1" in r.json()["data"]["markdown"]
    assert r.headers["X-Fastweb-Cache"] == "JOIN-PREFETCH"
    assert fake.hits == ["art1"]
    assert stats(aurl)["scrape"]["singleflight_joins"] == 1


def test_single_flight_two_on_demand(fake):
    fake.delay["art1"] = 0.8
    aurl, _, _ = make(fake, prefetch_n=0)
    u = urls(fake, "art1")[0]

    async def both():
        async with httpx.AsyncClient(timeout=30) as c:
            return await asyncio.gather(*[c.post(aurl + "/v2/scrape", json={**SDK_BODY, "url": u}) for _ in range(2)])
    rs = asyncio.new_event_loop().run_until_complete(both())
    assert all(r.status_code == 200 and "Story art1" in r.json()["data"]["markdown"] for r in rs)
    assert fake.hits == ["art1"]
    assert sorted(r.headers["X-Fastweb-Cache"] for r in rs) == ["JOIN", "MISS"]


# ---------- failures, cheap tiers only ----------
@pytest.mark.parametrize("name", ["shell", "challenge", "err"])
def test_failed_prefetch_not_cached_then_full_chain(fake, name):
    fake.results = urls(fake, name)
    aurl, rs, imp_calls = make(fake)
    search(aurl)
    p = settle(aurl, 1)
    assert p["failed"] == 1 and p["done"] == 0
    assert fake.fc == []  # prefetch never calls Firecrawl
    assert not [k for k in rs.keys("fastweb:host:*")]  # no host memory from a cheap-tier failure
    if name == "challenge":
        assert imp_calls  # impersonation is a cheap tier
    r = sdk_scrape(aurl, fake.results[0])
    assert fake.hits == [name, name]  # the on-demand scrape fetched again: nothing was cached
    if name == "err":  # 500 is an honest target error, not a fallback case
        assert r.status_code == 422
    else:
        assert r.status_code == 200 and len(fake.fc) == 1 and r.headers["X-Fastweb-Cache"] == "MISS"
        assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"


def test_ssrf_skipped(fake):
    port = fake.url.rsplit(":", 1)[1]
    fake.results = [f"http://localhost:{port}/p/art-a", f"http://10.0.0.1:{port}/p/art-b"] + urls(fake, "to_internal")
    aurl, rs, _ = make(fake)
    search(aurl)
    p = settle(aurl, 3)
    assert p["done"] == 0 and p["failed"] == 3
    assert fake.hits == ["to_internal"] and fake.fc == []
    assert not rs.keys("fastweb:scrape:*")


def test_pdf_over_cap_aborted(fake):
    fake.results = urls(fake, "bigpdf", "streampdf")
    aurl, rs, _ = make(fake, prefetch_max_bytes=12_000)
    search(aurl)
    p = settle(aurl, 2)
    assert p["failed"] == 2 and p["failed_by"].get("413") == 2  # the cap, not a fallback reason
    assert not rs.keys("fastweb:scrape:*")


def test_polite_skips(fake):
    rs_urls = urls(fake, "art1", "art2")
    fake.results = ["https://x.com/someone/status/1", "https://mobile.twitter.com/a", f"{fake.url}/p/pic.png",
                    f"{fake.url}/p/clip.MP4?x=1", "ftp://example.com/file"] + rs_urls
    aurl, rs, _ = make(fake, prefetch_n=10)
    search(aurl, limit=10)
    time.sleep(0.5)
    s = stats(aurl)["prefetch"]
    assert s["skipped"].get("x") == 2 and s["skipped"].get("type") == 2 and s["skipped"].get("scheme") == 1
    rs.flushall()
    # host memory: DEAD and NEEDS_RENDER hosts are skipped
    fake.hits.clear()
    aurl, rs, _ = make(fake)
    rs.set("fastweb:host:127.0.0.1", "dead")
    fake.results = rs_urls
    search(aurl)
    time.sleep(0.5)
    assert fake.hits == [] and stats(aurl)["prefetch"]["skipped"].get("host-dead") == 2
    rs.set("fastweb:host:127.0.0.1", "firecrawl")
    search(aurl, q="other")
    time.sleep(0.5)
    assert fake.hits == [] and stats(aurl)["prefetch"]["skipped"].get("host-firecrawl") == 2


# ---------- lanes: prefetch never delays an on-demand small page ----------
def test_on_demand_not_delayed_by_prefetch(fake):
    aurl, _, _ = make(fake, prefetch_concurrency=5, prefetch_per_host=5)
    t = time.monotonic()
    assert sdk_scrape(aurl, urls(fake, "art-idle")[0]).status_code == 200
    idle = time.monotonic() - t
    fake.results = urls(fake, *[f"art-p{i}" for i in range(5)])
    for i in range(5):
        fake.delay[f"art-p{i}"] = 6.0
    search(aurl)
    time.sleep(0.2)
    assert fake.inflight == 5
    t = time.monotonic()
    r = sdk_scrape(aurl, urls(fake, "art-busy")[0])
    busy = time.monotonic() - t
    assert r.status_code == 200 and busy - idle < 3.0, (idle, busy)  # not queued behind the 6 s prefetches
