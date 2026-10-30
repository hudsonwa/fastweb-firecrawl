import asyncio
import json
import time

import fakeredis.aioredis
import httpx
import pytest

from fastweb_firecrawl.search import RedisCache, Searcher, bucket_limit, cache_key, normalize_query


def results(n, prefix="r"):
    return [{"url": f"https://{prefix}{i}.example/", "title": f"T{i}", "content": f"s{i}"} for i in range(n)]


class FakeSearx:
    """Programmable SearXNG: a list of responses consumed per call (callable or (status, payload))."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def handler(self, request):
        self.calls.append(dict(request.url.params))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        status, payload = r
        return httpx.Response(status, json=payload)


def make(fake, cache=None, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    kw.setdefault("engines", "bing,yahoo")
    kw.setdefault("fallback_engines", "mojeek,presearch")
    kw.setdefault("relevance", False)  # synthetic "T0"/"s0" results; the guard has its own tests (test_relevance.py)
    return Searcher(client, "http://searx", cache=cache, **kw)


def run(coro):
    return asyncio.run(coro)


def test_normalize_keeps_operators():
    assert normalize_query("  Python　 Ｓite:Docs.python.org   \"Async  IO\" ") == \
        'python site:docs.python.org "async io"'
    assert normalize_query("STRASSE") == normalize_query("straße")


def test_bucket_and_key():
    assert [bucket_limit(n) for n in (1, 10, 11, 20, 21, 50, 51, 100, 500)] == [10, 10, 20, 20, 50, 50, 100, 100, 100]
    assert cache_key("A  b", 5, "bing,yahoo") == cache_key("a b", 10, "bing,yahoo")
    assert cache_key("a b", 5, "bing") != cache_key("a b", 5, "bing,yahoo")
    assert cache_key("a b", 5, "bing").startswith("fastweb:search:")


def test_limit_dedupe_order():
    dup = results(3) + [{"url": "https://r1.example/", "title": "dup", "content": ""}] + results(5, "z")
    fake = FakeSearx((200, {"results": dup}))
    out = run(make(fake).search("q", 5))
    assert out.items is not None and [x["url"] for x in out.items] == [
        "https://r0.example/", "https://r1.example/", "https://r2.example/", "https://z0.example/", "https://z1.example/"]
    assert out.items[1]["title"] == "T1"
    assert fake.calls[0]["engines"] == "bing,yahoo"


def test_zero_results_retry_with_fallback_engines():
    fake = FakeSearx((200, {"results": [], "unresponsive_engines": [["bing", "timeout"]]}), (200, {"results": results(3)}))
    out = run(make(fake).search("q", 10))
    assert len(out.items) == 3 and out.attempts == 2
    assert fake.calls[1]["engines"] == "mojeek,presearch"


def test_error_retry_with_fallback():
    fake = FakeSearx(httpx.ConnectError("down"), (200, {"results": results(2)}))
    out = run(make(fake).search("q", 10))
    assert len(out.items) == 2


def test_all_engines_down_is_failure():
    """Nothing after the fallback and the one retry -> items None + error (the app answers success:false so clients
    fall back)."""
    fake = FakeSearx((200, {"results": []}))
    out = run(make(fake).search("q", 10))
    assert out.items is None and out.error and out.attempts == 3


class Sequenced:
    """Fake SearXNG answering each call from a list of result counts, in call order."""

    def __init__(self, counts):
        self.counts, self.calls = list(counts), []

    async def handler(self, request):
        self.calls.append(request.url.params.get("engines", ""))
        return httpx.Response(200, json={"results": results(self.counts.pop(0))})


def test_transient_empty_answer_is_retried_once():
    """'best hiking trails ...': both engine sets empty on the first try, the same query fine seconds later."""
    fake = Sequenced([0, 0, 4])
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    out = run(Searcher(client, "http://searx", engines="bing,yahoo", fallback_engines="privacywall",
                       deadline=5.0).search("q", 10))
    assert out.source == "retry" and len(out.items) == 4 and out.attempts == 3
    assert fake.calls == ["bing,yahoo", "privacywall", "bing,yahoo"]  # the retry is the primary engine set


def test_warm_queries_each_engine_set_until_searxng_answers():
    """A fresh SearXNG spends about an engine timeout on each engine's first call, so the first real search lost
    its primary and hedge and answered 503; startup sends each engine set one throwaway query instead."""
    cold = {"results": [], "unresponsive_engines": [["yahoo", "timeout"]]}
    fake = FakeSearx(httpx.ConnectError("starting"), (502, {}), (200, cold), (200, {"results": []}),
                     (200, {"results": [], "unresponsive_engines": [["presearch", "Suspended: too many requests"]]}))
    s = make(fake)
    assert run(s.warm(tries=5, pause=0)) is True
    assert [c.get("engines") for c in fake.calls] == ["bing,yahoo"] * 4 + ["mojeek,presearch"]
    assert s.slots._value == 6 and s.cache is None  # no upstream slot held, nothing cached


def test_warm_gives_up_when_searxng_stays_down():
    fake = FakeSearx(httpx.ConnectError("down"))
    assert run(make(fake, fallback_engines="").warm(tries=3, pause=0)) is False
    assert len(fake.calls) == 3


def test_app_warm_hook_and_switch(monkeypatch):
    from fastweb_firecrawl import app as appmod
    from fastweb_firecrawl.settings import Settings
    calls = []

    async def fake_warm(self):
        calls.append(self.engines)
        return True
    monkeypatch.setattr(Searcher, "warm", fake_warm)
    api = appmod.Api.__new__(appmod.Api)
    api.searcher = Searcher(httpx.AsyncClient(), "http://searx", engines="yahoo")
    run(api._warm_search())
    assert calls == ["yahoo"]
    monkeypatch.delenv("FASTWEB_SEARCH_WARM", raising=False)
    assert Settings().search_warm is True  # on in production
    monkeypatch.setenv("FASTWEB_SEARCH_WARM", "0")
    assert Settings().search_warm is False


def test_no_retry_without_deadline_left():
    plan = Timed({"bing,yahoo": (0.3, 0), "privacywall,yahoo": (0.3, 0)})
    out = run(timed(plan, deadline=1.0, hedge=5).search("q", 10))  # ~0.6 s used: under RETRY_MIN_LEFT left
    assert out.items is None and out.attempts == 2


def test_deadline_bounds_search():
    async def slow(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={"results": results(1)})
    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    s = Searcher(client, "http://searx", engines="bing", fallback_engines="yahoo", deadline=0.4)
    t0 = time.monotonic()
    out = run(s.search("q", 10))
    assert out.items is None and time.monotonic() - t0 < 10  # the 0.4 s deadline, not the 30 s engine


def test_cache_hit_and_only_nonempty_cached():
    r = fakeredis.aioredis.FakeRedis()
    cache = RedisCache(client=r)
    fake = FakeSearx((200, {"results": results(12)}))
    s = make(fake, cache=cache, ttl=900)
    a = run(s.search("Hello  World", 10))
    b = run(s.search("hello world", 7))
    assert a.cache == "miss" and b.cache == "hit" and len(fake.calls) == 1
    assert len(b.items) == 7 and b.items == a.items[:7]
    key = cache_key("hello world", 10, "bing,yahoo")
    ttl = run(r.ttl(key))
    assert 0 < ttl <= 900
    empty = FakeSearx((200, {"results": []}))
    s2 = make(empty, cache=cache)
    run(s2.search("nothing here", 10))
    assert run(r.exists(cache_key("nothing here", 10, "bing,yahoo"))) == 0


class BrokenRedis:
    def __getattr__(self, name):
        async def boom(*a, **k):
            raise ConnectionError("redis down")
        return boom


def test_redis_outage_does_not_break_search():
    fake = FakeSearx((200, {"results": results(3)}))
    out = run(make(fake, cache=RedisCache(client=BrokenRedis())).search("q", 10))
    assert len(out.items) == 3 and out.cache == "miss"


def test_redis_unreachable_url_does_not_break_search():
    fake = FakeSearx((200, {"results": results(3)}))
    cache = RedisCache(url="redis://127.0.0.1:1/2", timeout=0.2)
    out = run(make(fake, cache=cache).search("q", 10))
    assert len(out.items) == 3


def test_app_cache_header_and_failure_shape(ctx):
    up, furl, aurl = ctx
    r = httpx.post(aurl + "/v2/search", json={"query": "harbour cache", "limit": 3})
    assert r.headers["x-fastweb-cache"] in ("MISS", "HIT") and len(r.json()["data"]["web"]) == 3


def test_app_all_engines_empty_is_503_success_false(ctx):
    up, furl, aurl = ctx
    up.mode = "search_empty"
    for path in ("/v1/search", "/v2/search"):
        r = httpx.post(aurl + path, json={"query": f"empty {path}", "limit": 3})
        assert r.status_code == 503
        body = r.json()
        assert body["success"] is False and "no results" in body["error"]


def test_backend_error_does_not_leak_the_query_into_logs_or_the_answer(ctx, capfd):
    """httpx's HTTPStatusError quotes the whole request URL (query included): only the class and status survive."""
    up, furl, aurl = ctx
    up.mode = "search_429"
    marker = "zebra-quokka-7731 private"
    capfd.readouterr()
    r = httpx.post(aurl + "/v2/search", json={"query": marker, "limit": 3})
    out = capfd.readouterr()
    assert r.status_code == 503 and r.json()["error"] == "Search backend failed: HTTPStatusError: 429", r.text
    for text in (r.text, out.out, out.err):
        assert "zebra" not in text and "quokka" not in text and "/search?" not in text


def test_search_errors_name_the_class_never_the_url():
    marker = "https://searx.internal/search?q=zebra+quokka"
    fake = FakeSearx(httpx.ConnectError(f"cannot connect to {marker}"))
    out = run(make(fake).search("zebra quokka", 3))
    assert out.items is None and out.error == "ConnectError" and "zebra" not in out.error


def test_search_limit_zero_is_a_bad_request(ctx):
    up, furl, aurl = ctx
    for limit in (0, -1, "5", True):
        r = httpx.post(aurl + "/v2/search", json={"query": "harbour", "limit": limit})
        assert r.status_code == 400, (limit, r.text)


class Timed:
    """Fake SearXNG keyed by engine set: engines -> (delay_s, n_results or exception)."""

    def __init__(self, plan):
        self.plan, self.calls, self.cancelled, self.timeouts = plan, [], [], []

    async def handler(self, request):
        engines = request.url.params.get("engines", "")
        self.calls.append((engines, time.monotonic()))
        self.timeouts.append(request.extensions["timeout"]["read"])
        delay, n = self.plan[engines]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(engines)
            raise
        if isinstance(n, Exception):
            raise n
        return httpx.Response(200, json={"results": results(n, engines.split(",")[0])})


def timed(plan, cache=None, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(plan.handler))
    kw.setdefault("engines", "bing,yahoo")
    kw.setdefault("fallback_engines", "privacywall,yahoo")
    return Searcher(client, "http://searx", cache=cache, **kw)


@pytest.mark.slow
def test_primary_empty_at_1_1s_fallback_answers_within_deadline():
    """primary empty at ~1.1 s, privacywall needs 1.62 s -> hedge started at 0.9 s answers (no 503)."""
    plan = Timed({"bing,yahoo": (1.1, 0), "privacywall,yahoo": (1.62, 5)})
    s = timed(plan, deadline=3.0, hedge=0.9)
    t0 = time.monotonic()
    out = run(s.search("q", 10))
    dt = time.monotonic() - t0
    assert out.items is not None and out.items[0]["url"].startswith("https://privacywall0")
    assert dt < 5.0 and out.source == "hedge"  # answered at all (items) is the deadline check
    assert [c[0] for c in plan.calls].count("privacywall,yahoo") == 1 and len(plan.calls) == 2


def test_hedge_fires_after_delay_and_at_most_once():
    plan = Timed({"bing,yahoo": (5, 3), "privacywall,yahoo": (5, 3)})
    s = timed(plan, deadline=0.6, hedge=0.2)
    out = run(s.search("q", 10))
    assert out.items is None and len(plan.calls) == 2 and out.attempts == 2
    assert plan.calls[1][1] - plan.calls[0][1] >= 0.18
    assert sorted(plan.cancelled) == ["bing,yahoo", "privacywall,yahoo"]


def test_fast_primary_no_hedge():
    plan = Timed({"bing,yahoo": (0.05, 3), "privacywall,yahoo": (0.01, 3)})
    out = run(timed(plan, hedge=0.3).search("q", 10))
    assert out.source == "primary" and len(plan.calls) == 1


def test_prefer_primary_if_both_within_grace():
    plan = Timed({"bing,yahoo": (0.33, 3), "privacywall,yahoo": (0.1, 3)})
    out = run(timed(plan, hedge=0.2).search("q", 10))
    assert out.source == "primary" and out.items[0]["url"].startswith("https://bing0")


def test_hedge_wins_and_primary_cancelled():
    plan = Timed({"bing,yahoo": (30, 3), "privacywall,yahoo": (0.1, 3)})
    t0 = time.monotonic()
    out = run(timed(plan, hedge=0.2, deadline=30.0).search("q", 10))
    assert out.source == "hedge" and time.monotonic() - t0 < 10  # answered by the hedge, not after the primary
    assert plan.cancelled == ["bing,yahoo"]


def test_overall_deadline_not_per_attempt():
    """Primary errors fast-ish, fallback hangs: total must stay ~deadline (kills `left = self.deadline`)."""
    plan = Timed({"bing,yahoo": (1.8, httpx.ConnectError("x")), "privacywall,yahoo": (30, 3)})
    s = timed(plan, deadline=3.0, hedge=30)
    t0 = time.monotonic()
    out = run(s.search("q", 10))
    # ~3 s; a per-attempt deadline would take 1.8 + 3 = 4.8 s
    assert out.items is None and time.monotonic() - t0 < 4.2 and out.attempts == 2
    assert plan.timeouts[1] <= 3.0 - 1.5  # the fallback only gets what is left of the overall deadline


def test_no_fallback_started_when_deadline_spent():
    """Kills removal of the `left < 0.05` guard: no fallback request once the deadline is gone."""
    plan = Timed({"bing,yahoo": (0.48, 0), "privacywall,yahoo": (0.0, 3)})
    out = run(timed(plan, deadline=0.5, hedge=5).search("q", 10))
    assert out.items is None and len(plan.calls) == 1 and out.error


def test_fallback_cached_with_short_ttl():
    r = fakeredis.aioredis.FakeRedis()
    plan = Timed({"bing,yahoo": (0.0, 0), "privacywall,yahoo": (0.0, 3)})

    async def go():
        s = timed(plan, cache=RedisCache(client=r), ttl=900, fallback_ttl=120)
        out = await s.search("q", 10)
        await asyncio.sleep(0.05)
        return out, await r.ttl(cache_key("q", 10, "bing,yahoo"))
    out, ttl = run(go())
    assert out.source == "fallback" and 0 < ttl <= 120


class HungRedis:
    """A TCP socket that accepts and never replies."""

    def __enter__(self):
        import socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"redis://127.0.0.1:{self.sock.getsockname()[1]}/2"
        return self

    def __exit__(self, *a):
        self.sock.close()


def test_hung_redis_adds_little_and_breaker_skips_it():
    plan = Timed({"bing,yahoo": (0.1, 3), "privacywall,yahoo": (0.1, 3)})
    with HungRedis() as h:
        async def go():
            cache = RedisCache(url=h.url)
            gets = []
            real_get = cache.redis.get

            async def counted(key):
                gets.append(key)
                return await real_get(key)
            cache.redis.get = counted
            s = timed(plan, cache=cache)
            times = []
            for q in ("a", "b", "c"):
                t0 = time.monotonic()
                out = await s.search(q, 10)
                times.append(time.monotonic() - t0)
                assert len(out.items) == 3
            return cache, times, gets
        cache, times, gets = run(go())
    # one GET reaches the hung Redis (cut at the timeout); the open breaker skips it for the next two searches
    assert len(gets) == 1 and max(times) < 5, (gets, times)
    assert cache.open_until > time.monotonic()


def test_breaker_reopens_after_cooldown():
    calls = []

    class Flaky:
        async def get(self, key):
            calls.append(key)
            raise ConnectionError("down")

        async def set(self, *a, **k):
            raise ConnectionError("down")
    cache = RedisCache(client=Flaky(), breaker=0.2)
    assert run(cache.get("k")) is None and run(cache.get("k")) is None and len(calls) == 1
    time.sleep(0.25)
    run(cache.get("k"))
    assert len(calls) == 2


def test_cache_set_is_off_the_response_path():
    class SlowSet:
        def __init__(self):
            self.store = {}

        async def get(self, key):
            return None

        async def set(self, key, value, ex=None):
            await self.release.wait()
            self.store[key] = value
    slow = SlowSet()
    plan = Timed({"bing,yahoo": (0.0, 3), "privacywall,yahoo": (0.0, 3)})

    async def go():
        slow.release = asyncio.Event()
        s = timed(plan, cache=RedisCache(client=slow, timeout=30.0))
        out = await asyncio.wait_for(s.search("q", 10), 10)  # answered while the SET is still blocked
        stored_before = dict(slow.store)
        slow.release.set()
        for _ in range(200):
            if slow.store:
                break
            await asyncio.sleep(0.01)
        return out, stored_before
    out, stored_before = run(go())
    assert len(out.items) == 3 and stored_before == {} and slow.store


def test_hung_redis_bounded_without_socket_timeout():
    """wait_for alone bounds the GET even if the client itself has no socket timeout."""
    import redis.asyncio as aioredis
    plan = Timed({"bing,yahoo": (0.0, 3), "privacywall,yahoo": (0.0, 3)})
    with HungRedis() as h:
        async def go():
            cache = RedisCache(client=aioredis.from_url(h.url))
            t0 = time.monotonic()
            out = await timed(plan, cache=cache).search("q", 10)
            return out, time.monotonic() - t0
        out, dt = run(go())
    assert len(out.items) == 3 and dt < 5  # the hung GET is cut, not waited on


def test_redis_client_has_short_socket_timeouts():
    cache = RedisCache(url="redis://127.0.0.1:1/2")
    kw = cache.redis.connection_pool.connection_kwargs
    assert kw["socket_timeout"] <= 0.25 and kw["socket_connect_timeout"] <= 0.25


# ---------- error mapping ----------
def test_empty_primary_in_grace_window_keeps_hedge_answer():
    """the hedge answers 3 items; the primary comes back empty inside the 50 ms grace window -> hedge kept."""
    plan = Timed({"bing,yahoo": (0.33, 0), "privacywall,yahoo": (0.1, 3)})
    out = run(timed(plan, hedge=0.2).search("q", 10))
    assert out.source == "hedge" and len(out.items) == 3


def test_hedge_answer_cached_with_short_ttl():
    """hedge answers get the fallback TTL (120 s), not the primary's 900 s."""
    r = fakeredis.aioredis.FakeRedis()
    plan = Timed({"bing,yahoo": (3, 3), "privacywall,yahoo": (0.1, 3)})

    async def go():
        s = timed(plan, cache=RedisCache(client=r), ttl=900, fallback_ttl=120, hedge=0.2, deadline=3.0)
        out = await s.search("q", 10)
        await asyncio.sleep(0.05)
        return out, await r.ttl(cache_key("q", 10, "bing,yahoo"))
    out, ttl = run(go())
    assert out.source == "hedge" and 0 < ttl <= 120


class _HangingSet:
    def __init__(self):
        self.sets = 0

    async def get(self, key):
        return None

    async def set(self, *a, **k):
        self.sets += 1
        await asyncio.sleep(30)


def test_background_set_is_bounded_and_trips_breaker():
    """a SET that hangs is cut at `timeout`, opens the breaker, and the next SET is skipped."""
    client = _HangingSet()
    cache = RedisCache(client=client, timeout=0.2, breaker=30)

    async def go():
        t = time.monotonic()
        cache.set_background("k", [1], 60)
        while cache._tasks and time.monotonic() - t < 15:
            await asyncio.sleep(0.01)
        drained = time.monotonic() - t
        opened = cache.open_until > time.monotonic()
        await cache.set("k2", [1], 60)
        return drained, opened
    drained, opened = run(go())
    assert drained < 10 and opened and client.sets == 1  # cut at 0.2 s, not the 30 s hang


# ---------- concurrency: coalescing, bounded upstream, honest overload ----------
class LoadedSearx:
    """A SearXNG whose latency grows with the calls it is serving at once (its engines rate-limit a fan-out)."""

    def __init__(self, per_call=0.15):
        self.per_call, self.active, self.peak, self.calls = per_call, 0, 0, []

    async def handler(self, request):
        q = request.url.params["q"]
        self.calls.append(q)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.per_call * self.active)
        finally:
            self.active -= 1
        return httpx.Response(200, json={"results": results(3, prefix=q)})


def burst(searcher, queries):
    async def go():
        return await asyncio.gather(*(searcher.search(q, 5) for q in queries))
    return run(go())


def loaded(fake, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    kw.setdefault("deadline", 1.5)
    return Searcher(client, "http://searx", engines="bing", relevance=False, **kw)


@pytest.mark.slow
def test_burst_of_20_distinct_searches_all_succeed():
    fake = LoadedSearx()
    outs = burst(loaded(fake, concurrency=4), [f"q{i}" for i in range(20)])
    assert [o.error for o in outs if o.items is None] == []
    assert all(o.items and o.items[0]["url"].startswith(f"https://q{i}") for i, o in enumerate(outs))
    assert fake.peak <= 4


def test_unbounded_burst_would_time_out():
    """The control for the test above: the same burst with no upstream bound races into the deadline."""
    fake = LoadedSearx()
    outs = burst(loaded(fake, concurrency=100), [f"q{i}" for i in range(20)])
    assert sum(o.items is None for o in outs) > 0


def test_identical_concurrent_searches_share_one_upstream_call():
    fake = LoadedSearx(per_call=0.2)
    outs = burst(loaded(fake), ["same query"] * 10 + ["Same  Query"] * 5)
    assert fake.calls == ["same query"]
    assert all(o.items and len(o.items) == 3 for o in outs)
    assert sorted(o.cache for o in outs).count("join") == 14


def test_full_queue_is_429_with_retry_after():
    fake = LoadedSearx(per_call=0.3)
    outs = burst(loaded(fake, concurrency=1, queue_max=2, deadline=3.0), [f"q{i}" for i in range(6)])
    full = [o for o in outs if o.items is None]
    assert full and all(o.status == 429 and o.retry_after for o in full)
    assert sum(o.items is not None for o in outs) == 3  # one running + two queued


def test_queue_wait_past_timeout_is_a_retryable_502():
    fake = LoadedSearx(per_call=0.5)
    outs = burst(loaded(fake, concurrency=1, queue_timeout=0.2, deadline=3.0), ["a", "b"])
    late = [o for o in outs if o.items is None]
    assert len(late) == 1 and late[0].status == 502 and late[0].retry_after and "busy" in late[0].error


def test_no_hedge_without_a_free_slot():
    """Under load the hedge must not take a slot another search is queued for."""
    fake = LoadedSearx(per_call=0.7)
    s = loaded(fake, concurrency=1, deadline=3.0, hedge=0.3, fallback_engines="mojeek")
    outs = burst(s, ["a"])
    assert outs[0].items and outs[0].attempts == 1 and fake.calls == ["a"]


# ---------- same-round winners, malformed SearXNG answers ----------
@pytest.mark.parametrize("order", [("primary", "hedge"), ("hedge", "primary")])
def test_take_prefers_primary_in_same_round(order):
    from fastweb_firecrawl.search import Outcome, _Race

    async def go():
        race = _Race(make(FakeSearx((200, {"results": []}))), "q", 10, Outcome(None))
        done = []
        for src in order:
            fut = asyncio.get_running_loop().create_future()
            fut.set_result([{"url": f"https://{src}.example/"}])
            race.tasks[fut] = src
            done.append(fut)
        race.take(done)
        return race.winner
    assert run(go())[0] == "primary"


@pytest.mark.parametrize("payload", [
    [1, 2],
    {"results": "nope"},
    {"results": [{"url": 5, "title": "x"}, {"url": ["a"]}, "junk"]},
])
def test_malformed_searxng_answer_is_a_failed_attempt(payload):
    out = run(make(FakeSearx((200, payload)), fallback_engines="").search("q", 5))
    assert out.items is None and out.status == 503


def test_non_string_fields_are_skipped_or_blanked():
    fake = FakeSearx((200, {"results": [{"url": 5}, {"url": "https://ok.example/", "title": None, "content": 3}]}))
    out = run(make(fake).search("q", 5))
    assert out.items == [{"url": "https://ok.example/", "title": "", "description": ""}]


def test_malformed_searxng_answer_over_http_is_not_500(ctx, monkeypatch):
    _, _, aurl = ctx

    async def bad(self, *a, **kw):
        raise AttributeError("'list' object has no attribute 'get'")
    monkeypatch.setattr(Searcher, "_attempt", bad)
    r = httpx.post(aurl + "/v2/search", json={"query": "malformed answer", "limit": 3})
    assert r.status_code == 503 and r.json()["success"] is False
