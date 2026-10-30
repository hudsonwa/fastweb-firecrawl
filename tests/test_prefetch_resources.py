"""Prefetch: memory, own fetch slots, off-loop serialisation, joiner probes,
stale jobs, check-then-lead race and the nits."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import httpx
import pytest
from starlette.responses import StreamingResponse
from starlette.routing import Route

from .conftest import serve
from .test_prefetch import SDK_BODY, Fake, make, sdk_scrape, search, settle, stats, urls


@pytest.fixture
def fake():
    f = Fake()
    srv, url = serve(f.app())
    f.url = url
    yield f
    srv.should_exit = True


class Pools:
    """lane_factory recording which lane ran each extraction; lanes in `slow` sleep first."""

    def __init__(self, slow=(), delay=1.0):
        self.used, self.slow, self.delay = [], set(slow), delay

    def __call__(self, name):
        pools = self

        class Pool(ThreadPoolExecutor):
            def submit(self, fn, *a, **kw):
                pools.used.append(name)
                if name in pools.slow:
                    def slow(*a, **kw):
                        time.sleep(pools.delay)
                        return fn(*a, **kw)
                    return super().submit(slow, *a, **kw)
                return super().submit(fn, *a, **kw)
        return Pool(1)


# ---------- memory ----------
def test_prefetch_skips_extraction_of_big_markup_on_demand_still_extracts(fake):
    pools = Pools()
    fake.results = urls(fake, "art1")
    aurl, rs, _ = make(fake, prefetch_max_html=1000, lane_factory=pools)
    search(aurl)
    p = settle(aurl, 1)
    assert p["failed"] == 1 and p["failed_by"] == {"413": 1}
    assert pools.used == [] and not rs.keys("fastweb:scrape:*")
    r = sdk_scrape(aurl, fake.results[0])
    assert r.status_code == 200 and "Story art1" in r.json()["data"]["markdown"]
    assert pools.used == ["small"]


def test_prefetch_extracts_in_its_own_lane(fake):
    pools = Pools()
    fake.results = urls(fake, "art1")
    aurl, rs, _ = make(fake, lane_factory=pools)
    search(aurl)
    assert settle(aurl, 1)["done"] == 1
    assert pools.used == ["pf"]


@pytest.mark.slow
def test_prefetch_never_waits_for_byte_budget(fake):
    # The on-demand extraction outlasts prefetch's hold-backs (2 s before the job, then until 1 s before its
    # 3 s deadline), so prefetch then meets the full budget.
    pools = Pools(slow={"small"}, delay=6.0)
    aurl, _, _ = make(fake, inflight_bytes=100, lane_factory=pools)
    t = threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, "art-od")[0]))
    t.start()
    time.sleep(0.4)  # the on-demand body now holds the whole budget
    fake.results = urls(fake, "art1")
    t0 = time.monotonic()
    search(aurl)
    p = settle(aurl, 1)
    # a job that meets an on-demand page in flight is skipped ("busy") before it fetches.
    assert time.monotonic() - t0 < 5.0 and p["failed"] + p["skipped_late"] == 1 and "pf" not in pools.used
    t.join()


def test_memory_guard_skips_prefetch(fake):
    fake.results = urls(fake, "art1", "art2")
    aurl, _, _ = make(fake, mem_usage=lambda: (700, 1000))
    search(aurl)
    time.sleep(0.5)
    p = stats(aurl)["prefetch"]
    assert fake.hits == [] and p["skipped"].get("memory") == 2


def test_memory_guard_below_threshold_prefetches(fake):
    fake.results = urls(fake, "art1")
    aurl, _, _ = make(fake, mem_usage=lambda: (500, 1000))
    search(aurl)
    assert settle(aurl, 1)["done"] == 1


def test_mem_usage_reads_cgroup_then_falls_back(tmp_path):
    from fastweb_firecrawl.prefetch import mem_usage
    (tmp_path / "memory.current").write_text("600\n")
    (tmp_path / "memory.max").write_text("1000\n")
    assert mem_usage(str(tmp_path), fallback_limit=5) == (600, 1000)
    (tmp_path / "memory.max").write_text("max\n")
    cur, lim = mem_usage(str(tmp_path), fallback_limit=5)
    assert lim == 5 and cur >= 0  # no cgroup limit: process RSS (0 without /proc) against the configured limit
    cur, lim = mem_usage(str(tmp_path / "nope"), fallback_limit=5)
    assert lim == 5 and cur >= 0


# ---------- prefetch has its own fetch slots ----------
def test_on_demand_keeps_all_plain_slots(fake):
    aurl, _, _ = make(fake, plain_concurrency=2, prefetch_concurrency=2, prefetch_per_host=2)
    fake.results = urls(fake, "art-p0", "art-p1")
    fake.delay.update({"art-p0": 2.0, "art-p1": 2.0})
    search(aurl)
    time.sleep(0.3)
    assert fake.inflight == 2
    out = []

    def od(i):
        t = time.monotonic()
        r = sdk_scrape(aurl, urls(fake, f"art-od{i}")[0])
        out.append((r.status_code, time.monotonic() - t))
    ts = [threading.Thread(target=od, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(c == 200 and el < 1.0 for c, el in out), out


def test_prefetch_yields_while_on_demand_busy(fake):
    aurl, _, _ = make(fake, plain_concurrency=4, prefetch_concurrency=2)  # busy at 2 on-demand scrapes
    fake.delay.update({"art-slow0": 1.5, "art-slow1": 1.5})
    ts = [threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, f"art-slow{i}")[0])) for i in range(2)]
    [t.start() for t in ts]
    time.sleep(0.3)
    fake.results = urls(fake, "art1")
    search(aurl)
    time.sleep(0.5)
    assert "art1" not in fake.hits
    [t.join() for t in ts]
    assert settle(aurl, 1)["done"] == 1


# ---------- serialisation and compression off the event loop ----------
def test_prefetch_payload_serialised_off_the_event_loop(fake, monkeypatch):
    seen = []
    real = json.dumps

    def spy(obj, *a, **kw):
        if isinstance(obj, dict) and "Story art-offloop" in str(obj.get("data", {}).get("markdown")):
            seen.append(threading.current_thread().name)  # only this test's page (other tests' servers live on)
        return real(obj, *a, **kw)
    monkeypatch.setattr(json, "dumps", spy)
    fake.results = urls(fake, "art-offloop")
    aurl, _, _ = make(fake)
    search(aurl)
    assert settle(aurl, 1)["done"] == 1
    assert seen and all(n.startswith("asyncio") for n in seen), seen


def test_big_payload_compressed_at_a_cheap_level(monkeypatch):
    import types
    import zlib

    from fastweb_firecrawl import cache
    from fastweb_firecrawl.cache import Store
    levels = []
    # only the cache module's zlib: patching zlib itself would also count other threads' compressions (servers
    # left running by other tests in this worker)
    spy = types.SimpleNamespace(compress=lambda d, level=-1: levels.append((len(d), level)) or zlib.compress(d, level),
                                decompress=zlib.decompress, error=zlib.error)
    monkeypatch.setattr(cache, "zlib", spy)
    st = Store(fakeredis.FakeAsyncRedis(), max_entry=10 ** 8)
    asyncio.run(st.put_scrape("k1", b"x" * 1000))
    asyncio.run(st.put_scrape("k2", b"x" * 1_000_000))
    assert levels == [(1000, 6), (1_000_000, 1)]


# ---------- joiner probes ----------
@pytest.mark.slow
@pytest.mark.parametrize("name", ["shell", "challenge"])
def test_joiner_of_failing_prefetch_runs_full_chain(fake, name):
    fake.results = urls(fake, name)
    fake.delay[name] = 0.8
    aurl, _, _ = make(fake)
    search(aurl)
    time.sleep(0.2)
    r = sdk_scrape(aurl, fake.results[0])  # joins the in-flight prefetch, which then fails
    assert r.status_code == 200, r.text
    assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"
    assert len(fake.fc) == 1 and fake.hits == [name, name]
    assert stats(aurl)["scrape"]["singleflight_joins"] == 0


def test_joiner_timeout_bounded_by_request_timeout(fake):
    fake.results = urls(fake, "art1")
    fake.delay["art1"] = 2.5
    aurl, _, _ = make(fake)
    search(aurl)
    time.sleep(0.2)
    t = time.monotonic()
    r = httpx.post(aurl + "/v2/scrape", json={**SDK_BODY, "url": fake.results[0], "timeout": 1000}, timeout=10)
    assert time.monotonic() - t < 1.6 and r.status_code == 504


def test_singleflight_unit():
    from fastweb_firecrawl.prefetch import SingleFlight

    async def go():
        sf = SingleFlight()

        async def leader(res, delay):
            with sf.lead("k") as fut:
                await asyncio.sleep(delay)
                if res is not None:
                    fut.set_result(res)
        t = asyncio.create_task(leader(b"x", 1.0))
        await asyncio.sleep(0.01)
        j = asyncio.create_task(sf.join("k", 5))
        await asyncio.sleep(0.05)
        t.cancel()
        assert await j is None and not sf.flights  # leader cancelled -> joiner gets None, map cleaned
        t = asyncio.create_task(leader(b"y", 0.3))
        await asyncio.sleep(0.01)
        assert await sf.join("k", 0.05) is None  # joiner timeout leaves the leader alone
        assert await sf.join("k", 5) == (b"y", False)
        await t
        assert not sf.flights
        t = asyncio.create_task(leader(b"z", 0.3))
        await asyncio.sleep(0.01)
        j = asyncio.create_task(sf.join("k", 5))
        await asyncio.sleep(0.05)
        j.cancel()  # joiner cancelled does not cancel the leader's future
        await t
        assert not sf.flights
    asyncio.run(go())


def test_prefetch_timeout_bounds_the_job(fake):
    fake.results = urls(fake, "art1")
    fake.delay["art1"] = 10.0  # bad case: the job runs to the page's own time
    aurl, rs, _ = make(fake, prefetch_timeout=0.8, total_timeout=20.0)
    t = time.monotonic()
    search(aurl)
    p = settle(aurl, 1)
    assert time.monotonic() - t < 6 and p["failed"] == 1 and not rs.keys("fastweb:scrape:*")


@pytest.mark.slow
def test_queue_full_drops_oldest(fake):
    fake.delay["*"] = 1.0
    fake.results = urls(fake, "art-busy")
    aurl, _, _ = make(fake, prefetch_concurrency=1, prefetch_queue=2)
    search(aurl)
    time.sleep(0.2)
    fake.results = urls(fake, "art-a", "art-b", "art-c")
    search(aurl, q="second")
    p = settle(aurl, 3, timeout=10)
    assert p["dropped"] == 1 and fake.hits == ["art-busy", "art-b", "art-c"]


# ---------- stale jobs ----------
def test_stale_jobs_dropped(fake):
    fake.delay["*"] = 1.0
    fake.results = urls(fake, "art1", "art2", "art3")
    aurl, _, _ = make(fake, prefetch_concurrency=1, prefetch_max_age=0.5)
    search(aurl)
    end = time.monotonic() + 5
    while time.monotonic() < end and stats(aurl)["prefetch"].get("stale", 0) < 2:
        time.sleep(0.05)
    p = stats(aurl)["prefetch"]
    assert p.get("stale") == 2 and fake.hits == ["art1"]


def test_max_age_default():
    from fastweb_firecrawl.settings import Settings
    assert Settings().prefetch_max_age == 12.0


# ---------- nits ----------
class SlowGetRedis:
    """fakeredis with a slow GET to widen the prefetch check-then-lead window."""

    def __init__(self, r, delay):
        self.redis, self.delay = r, delay

    def __getattr__(self, k):
        return getattr(self.redis, k)

    async def get(self, key):
        await asyncio.sleep(self.delay)
        return await self.redis.get(key)


def test_race_on_demand_leads_during_prefetch_redis_checks(fake):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    fake.results = urls(fake, "art1")
    fake.delay["art1"] = 0.5
    r = SlowGetRedis(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()), 0.2)
    st = Settings(upstream=fake.url, search_relevance=False, searxng_url=fake.url, redis_url="", allow_hosts=frozenset({"127.0.0.1"}),
                  total_timeout=3.0, prefetch_n=5, prefetch_timeout=3.0)
    srv, aurl = serve(create_app(st, redis=r))
    search(aurl)
    time.sleep(0.05)
    sdk_scrape(aurl, fake.results[0])
    settle(aurl, 1)
    assert fake.hits == ["art1"]


def test_uncached_prefetch_not_counted_as_cached(fake):
    fake.results = urls(fake, "art1")
    aurl, rs, _ = make(fake, scrape_ttl=0)
    search(aurl)
    p = settle(aurl, 1)
    assert p["done"] == 0 and p["uncached"] == 1 and not rs.keys("fastweb:pf:*")


def test_prefetch_does_not_download_unsupported_types():
    chunks = []

    async def blob(request):
        async def gen():
            for _ in range(20):
                chunks.append(1)
                yield b"\0" * 100_000
                await asyncio.sleep(0.05)
        return StreamingResponse(gen(), media_type="application/octet-stream")
    f = Fake()
    app = f.app()
    app.router.routes.insert(0, Route("/bin/{name}", blob))
    srv, url = serve(app)
    f.url = url
    f.results = [f"{url}/bin/download"]
    aurl, _, _ = make(f)
    search(aurl)
    p = settle(aurl, 1)
    time.sleep(0.3)
    srv.should_exit = True
    assert p["failed"] == 1 and len(chunks) < 5, len(chunks)


# ---------- remaining mutants ----------
def test_search_headers_unchanged_by_prefetch(fake):
    fake.results = urls(fake, "art1", "art2")
    on, _, _ = make(fake)
    off, _, _ = make(fake, prefetch_n=0)
    drop = {"date", "server"}
    h_on = {k: v for k, v in search(on).headers.items() if k not in drop}
    h_off = {k: v for k, v in search(off).headers.items() if k not in drop}
    assert h_on == h_off


def test_seen_is_bounded(monkeypatch):
    from fastweb_firecrawl import prefetch
    monkeypatch.setattr(prefetch, "SEEN_MAX", 2)

    class Store:
        async def has_scrape(self, key):
            return False

        async def host(self, host):
            return None

    async def run(body, key):
        return b"x", True

    async def go():
        p = prefetch.Prefetcher(Store(), prefetch.SingleFlight(), run, lambda **kw: None, concurrency=1)
        p.submit([f"http://h{i}.example/a" for i in range(5)])
        for _ in range(100):
            await asyncio.sleep(0.01)
            if p.stats["done"] == 5:
                break
        await p.stop()
        return p
    p = asyncio.run(go())
    assert p.stats["done"] == 5 and len(p.seen) == 2


def test_content_length_over_cap_refused_before_reading():
    chunks = []

    async def big(request):
        async def gen():
            for _ in range(10):
                chunks.append(1)
                yield b"<p>" + b"x" * 4997
                await asyncio.sleep(0.2)
        return StreamingResponse(gen(), media_type="text/html", headers={"content-length": "50000"})
    f = Fake()
    app = f.app()
    app.router.routes.insert(0, Route("/big/{name}", big))
    srv, url = serve(app)
    f.url = url
    f.results = [f"{url}/big/page"]
    aurl, _, _ = make(f, prefetch_max_bytes=12_000)
    search(aurl)
    p = settle(aurl, 1)
    time.sleep(0.3)
    srv.should_exit = True
    assert p["failed_by"] == {"413": 1} and len(chunks) <= 1, len(chunks)
