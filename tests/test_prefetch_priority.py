"""The prefetch worker runs at lower CPU priority, prefetch yields to any on-demand extraction, caps,
and Prefetcher internals."""
import asyncio
import os
import threading
import time

import fakeredis
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, StreamingResponse
from starlette.routing import Route

from .conftest import serve
from .test_prefetch import Fake, make, sdk_scrape, search, settle, urls
from .test_prefetch_resources import Pools


@pytest.fixture
def fake():
    f = Fake()
    srv, url = serve(f.app())
    f.url = url
    yield f
    srv.should_exit = True


# ---------- priority and yielding ----------
def test_pf_worker_runs_at_nice_10_on_demand_lanes_do_not():
    from fastweb_firecrawl.worker import LANES, Lane

    async def go():
        lanes = {n: Lane(n, LANES[n]) for n in ("pf", "small")}
        try:
            return {n: await lane.run(30, 30, os.nice, 0) for n, lane in lanes.items()}
        finally:
            for lane in lanes.values():
                lane.close()
    base = os.nice(0)
    got = asyncio.run(go())
    assert got == {"pf": base + 10, "small": base}
    assert LANES["pf"] == 1  # an idle pf worker holds no big page's heap


class TimedPools(Pools):
    """Pools that also record (name, start, end) of each extraction."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.spans = []

    def __call__(self, name):
        pools, inner = self, super().__call__(name)
        submit = inner.submit

        def timed(fn, *a, **kw):
            def run(*a, **kw):
                t0 = time.monotonic()
                try:
                    return fn(*a, **kw)
                finally:
                    pools.spans.append((name, t0, time.monotonic()))
            return submit(run, *a, **kw)
        inner.submit = timed
        return inner


def test_prefetch_waits_while_one_on_demand_request_extracts(fake):
    pools = TimedPools(slow={"small"}, delay=1.0)
    aurl, _, _ = make(fake, lane_factory=pools)
    t = threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, "art-od")[0]))
    t.start()
    time.sleep(0.3)  # one on-demand page is extracting (slowly) now
    fake.results = urls(fake, "art1")
    search(aurl)
    assert settle(aurl, 1)["done"] == 1
    t.join()
    small = [s for s in pools.spans if s[0] == "small"]
    pf = [s for s in pools.spans if s[0] == "pf"]
    assert small and pf and pf[0][1] >= small[0][2] - 0.05  # prefetch extraction started after on-demand ended


def test_prefetch_already_fetching_waits_before_its_cpu_phase(fake):
    """busy() is checked before a job starts; a job whose fetch is under way must still hold back its decode,
    lighten and extraction while an on-demand page is in its CPU phase."""
    pools = TimedPools(slow={"small"}, delay=1.5)
    aurl, _, _ = make(fake, lane_factory=pools, prefetch_timeout=5.0)
    fake.delay["art1"] = 0.6
    fake.results = urls(fake, "art1")
    search(aurl)  # the prefetch job starts now (nothing on demand yet) and waits 0.6 s for its page
    time.sleep(0.15)
    t = threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, "art-od")[0]))
    t.start()
    assert settle(aurl, 1)["done"] == 1
    t.join()
    small = [x for x in pools.spans if x[0] == "small"]
    pf = [x for x in pools.spans if x[0] == "pf"]
    assert small and pf and pf[0][1] >= small[0][2] - 0.05


@pytest.mark.slow
def test_prefetch_gives_up_rather_than_extract_beside_on_demand(fake):
    """An on-demand request still in its cheap tiers at the prefetch deadline: prefetch fails instead of extracting
    next to it (5 x 3 MB prefetch + 5 x 3 MB on demand OOMed a 512m container otherwise)."""
    pools = TimedPools(slow={"small"}, delay=4.0)
    aurl, rs, _ = make(fake, lane_factory=pools, prefetch_timeout=2.0)
    fake.delay["art1"] = 0.5
    fake.results = urls(fake, "art1")
    search(aurl)  # the job starts (nothing on demand yet) and waits for its page
    time.sleep(0.1)
    t = threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, "art-od")[0]))
    t.start()
    p = settle(aurl, 1)
    assert p["gave_up"] == 1 and p["failed"] == 0 and "pf" not in [x[0] for x in pools.spans]
    assert not rs.keys("fastweb:scrape:*")
    t.join()


def test_prefetch_rechecks_memory_before_its_cpu_phase(fake):
    calls = []

    def mem():
        calls.append(1)
        return (0 if len(calls) == 1 else 900, 1000)  # low when the job starts, high once the page is fetched
    pools = TimedPools()
    fake.delay["art1"] = 0.3
    fake.results = urls(fake, "art1")
    aurl, _, _ = make(fake, lane_factory=pools, mem_usage=mem)
    search(aurl)
    p = settle(aurl, 1)
    assert p["gave_up"] == 1 and p["failed"] == 0 and pools.spans == [] and len(calls) >= 2


# ---------- caps ----------
def test_prefetch_caps_default_4mb_body_3_5mb_markup():
    from fastweb_firecrawl.settings import Settings
    assert Settings().prefetch_max_bytes == 4 * 1024 * 1024 and Settings().prefetch_max_html == 3_500_000


# ---------- put_scrape dropped (over max_entry) ----------
def test_prefetch_payload_over_max_entry_is_uncached_and_unmarked(fake, monkeypatch):
    from fastweb_firecrawl import app as fastweb_app
    from fastweb_firecrawl.cache import Store
    monkeypatch.setattr(fastweb_app, "Store", lambda *a, **kw: Store(*a, **{**kw, "max_entry": 10}))
    fake.results = urls(fake, "art1")
    aurl, rs, _ = make(fake)
    search(aurl)
    p = settle(aurl, 1)
    assert p["uncached"] == 1 and p["done"] == 0
    assert not rs.keys("fastweb:scrape:*") and not rs.keys("fastweb:pf:*")


# ---------- impersonated tier under prefetch ----------
class ImpPages:
    """403 to the plain client, real pages to curl_cffi's Chrome (which sends sec-ch-ua; fastweb's plain client
    sends a Chrome User-Agent, so the UA tells them apart no longer). img and biglen stream their bodies
    slowly: only the pre-checks on the headers fail them at once."""

    def __init__(self):
        self.hits, self.plain_delay = [], 0.0

    def app(self):
        async def page(request):
            name = request.path_params["name"]
            if "sec-ch-ua" not in request.headers:
                await asyncio.sleep(self.plain_delay)
                return HTMLResponse("<html><body>Access denied</body></html>", status_code=403)
            self.hits.append(name)
            if name in ("img", "biglen"):
                async def slow_body():
                    yield b"\x89PNG" if name == "img" else b"<html>"
                    await asyncio.sleep(3.0)
                    yield b"0" * (50_000 if name == "img" else 300_000)
                return StreamingResponse(slow_body(), media_type="image/png" if name == "img" else "text/html",
                                         headers={"content-length": str(50_004 if name == "img" else 300_006)})
            if name == "big":
                return HTMLResponse("<html><body>" + "<p>word</p>" * 20_000 + "</body></html>")  # ~220 KB
            return HTMLResponse("<html><head><title>T</title></head><body><article>" + "<p>The harbour plan "
                                "passed after a long debate about funding and the tides near the old pier.</p>" * 120 + "</article></body></html>")
        return Starlette(routes=[Route("/p/{name}", page)])


@pytest.fixture
def imp_pages():
    f = ImpPages()
    srv, url = serve(f.app())
    f.url = url
    yield f
    srv.should_exit = True


def imp_app(fake, imp_pages, **kw):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    server = fakeredis.FakeServer()
    st = Settings(upstream=fake.url, searxng_url=fake.url, search_relevance=False, redis_url="",
                  allow_hosts=frozenset({"127.0.0.1"}), impersonate=True, total_timeout=5.0, max_bytes=2 * 1024 * 1024,
                  prefetch_n=5, prefetch_timeout=4.0, **kw)
    srv, aurl = serve(create_app(st, redis=fakeredis.FakeAsyncRedis(server=server)))
    return aurl


def test_prefetch_impersonated_tier_caches_a_page(fake, imp_pages):
    aurl = imp_app(fake, imp_pages)
    fake.results = [f"{imp_pages.url}/p/ok"]
    search(aurl)
    p = settle(aurl, 1)
    assert p["done"] == 1 and imp_pages.hits == ["ok"]


def test_prefetch_impersonated_tier_uses_prefetch_cap(fake, imp_pages):
    aurl = imp_app(fake, imp_pages, prefetch_max_bytes=100_000)
    fake.results = [f"{imp_pages.url}/p/big"]
    search(aurl)
    p = settle(aurl, 1)
    assert p["failed_by"] == {"413": 1}
    r = sdk_scrape(aurl, f"{imp_pages.url}/p/big")  # on demand: the normal cap
    assert r.status_code == 200


@pytest.mark.slow
def test_prefetch_impersonated_tier_checks_type_and_length_first(fake, imp_pages, capsys):
    aurl = imp_app(fake, imp_pages, prefetch_max_bytes=100_000)
    fake.results = [f"{imp_pages.url}/p/img", f"{imp_pages.url}/p/biglen"]
    search(aurl)
    p = settle(aurl, 2)
    assert p["failed_by"] == {"422": 1, "413": 1}
    assert "prefetch: unsupported content type image/png" in capsys.readouterr().out  # before the download


@pytest.mark.slow
def test_prefetch_impersonated_tier_uses_prefetch_slots(fake, imp_pages):
    """An on-demand request that takes the only plain slot after the prefetch job started must not hold up the
    prefetch's impersonated fetch."""
    aurl = imp_app(fake, imp_pages, plain_concurrency=1)
    imp_pages.plain_delay = 0.6
    fake.delay["art-slow"] = 5.0
    fake.results = [f"{imp_pages.url}/p/ok"]
    # bad case: the impersonated fetch waits for the plain slot, held about 5 s by art-slow
    t0 = time.monotonic()
    search(aurl)  # the job starts now and waits 0.6 s for its plain 403
    time.sleep(0.2)
    t = threading.Thread(target=sdk_scrape, args=(aurl, f"{fake.url}/p/art-slow"))
    t.start()
    while not imp_pages.hits and time.monotonic() - t0 < 6:
        time.sleep(0.05)
    assert imp_pages.hits == ["ok"] and time.monotonic() - t0 < 4.0  # fetched while the plain slot is still taken
    settle(aurl, 1)
    t.join()


@pytest.mark.slow
def test_prefetch_skips_when_on_demand_stays_busy():
    """Still busy after the 2 s hold-back: the job is skipped, not fetched beside on-demand pages (memory)."""
    from fastweb_firecrawl.prefetch import Prefetcher, SingleFlight
    ran = []

    async def run(body, key):
        ran.append(1)
        return b"{}", True

    async def go():
        p = Prefetcher(StubStore(), SingleFlight(), run, lambda **kw: None, busy=lambda: True)
        p.submit(["https://a.example/x"])
        for _ in range(150):
            await asyncio.sleep(0.02)
            if p.stats["skipped_late"]:
                break
        await p.stop()
        return p
    p = asyncio.run(go())
    assert ran == [] and p.stats["skipped"]["busy"] == 1


# ---------- Prefetcher internals ----------
class StubStore:
    def __init__(self, cached=()):
        self.cached, self.exists_calls = set(cached), []

    async def has_scrape(self, key):
        self.exists_calls.append(key)
        return key in self.cached

    async def get_scrape(self, key):
        raise AssertionError("dedupe must not GET + decompress the entry")

    async def host(self, host):
        return None


def run_prefetcher(store, urls_):
    from fastweb_firecrawl.prefetch import Prefetcher, SingleFlight
    ran = []

    async def run(body, key):
        ran.append(body["url"])
        return b"{}", True

    async def go():
        p = Prefetcher(store, SingleFlight(), run, lambda **kw: None)
        p.submit(urls_)
        for _ in range(100):
            await asyncio.sleep(0.02)
            st = p.snapshot()
            if st["done"] + st["skipped_late"] >= len(urls_):
                break
        await p.stop()
        return p, ran
    return asyncio.run(go())


def test_dedupe_uses_exists_not_get():
    from fastweb_firecrawl.cache import scrape_key_for_request
    from fastweb_firecrawl.prefetch import prefetch_body
    cached = scrape_key_for_request(prefetch_body("https://a.example/x"))
    store = StubStore({cached})
    p, ran = run_prefetcher(store, ["https://a.example/x", "https://b.example/y"])
    assert ran == ["https://b.example/y"] and p.stats["skipped"]["cached"] == 1 and len(store.exists_calls) == 2


def test_host_inflight_forgets_idle_hosts():
    p, ran = run_prefetcher(StubStore(), [f"https://h{i}.example/x" for i in range(5)])
    assert len(ran) == 5 and dict(p.host_inflight) == {}


def test_store_has_scrape():
    from fastweb_firecrawl.cache import Store

    async def go():
        st = Store(fakeredis.FakeAsyncRedis())
        assert not await st.has_scrape("k")
        await st.put_scrape("k", b"payload")
        assert await st.has_scrape("k")
        assert not await Store(None).has_scrape("k")
    asyncio.run(go())
