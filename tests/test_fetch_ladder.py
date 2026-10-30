"""Host-memory policy, walls never whole-page, mutation-killing tests,
the real SDK body hits the cache, compressed capped entries, size caps, extraction lanes."""
import asyncio
import json
import threading
import time
import zlib
from pathlib import Path

import fakeredis
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .conftest import ARTICLE, EXTRA, serve
from .test_api import scrape

SDK_BODY = json.loads((Path(__file__).parent / "data" / "sdk_scrape_body_4.17.json").read_text())
FOOTER = "<footer><ul>" + "".join(f"<li><a href='/f/{i}'>Footer link number {i} to a site section</a></li>"
                                   for i in range(50)) + "</ul></footer>"
CONSENT_WALL = ("<html><head><title>Daily News</title></head><body><div class='cmp'><p>We value your privacy. We and our "
                "partners use cookies to personalise content.</p><p>Accept all to continue.</p></div>" + FOOTER
                + "</body></html>")
CHALLENGE_200 = ("<html><head><title>Just a moment...</title></head><body><p>Checking your browser before accessing "
                 "the site.</p>" + FOOTER + "</body></html>")
# trafilatura keeps only the 82-char lead; the "related" grid is ~5k chars of mostly non-link text -> ratio rule only.
RELATED = "".join(f"<div class='related'><a href='/s/{i}'>Story {i}</a><div>Harbour vote row {i}, markets and tides "
                  f"today</div><div>Updated {i} hours ago</div></div>" for i in range(70))
RATIO_HUB = ("<html><head><title>Daily Hub</title></head><body><header><a href='/'>Daily Hub Network</a></header>"
             "<article><h1>Lead</h1>" + "<p>One lead paragraph about the harbour vote today and what the council "
             "said.</p>" * 2 + "</article><div class='related'>" + RELATED + "</div><script>var leak='SCRIPTLEAK'"
             "</script><footer>© Daily Hub 2026</footer></body></html>")
# trafilatura keeps the 1.2k lead (21 % of the body, above the ratio rule); 78 % of the body is links -> hub rule only.
LINKS = "".join(f"<li><a href='/s/{i}'>Section {i} headline about the harbour and markets today</a></li>"
                for i in range(80))
LINK_HUB = ("<html><head><title>Link Hub</title></head><body><div><ul>" + LINKS + "</ul></div><div class='lead'><p>"
            + "Lead story text about the council vote on harbour funding. " * 20 + "</p></div><script>var leak="
            "'SCRIPTLEAK'</script></body></html>")


def make(furl, **kw):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    r = kw.pop("redis", None) or fakeredis.FakeAsyncRedis()
    imp = kw.pop("impersonator", "none")
    st = Settings(**{"upstream": furl, "searxng_url": furl, "max_bytes": 2 * 1024 * 1024, "total_timeout": 2.0,
                     "redis_url": "", "dead_reprobe_rate": 0.0, "allow_hosts": frozenset({"127.0.0.1"}), **kw})
    if imp == "none":
        async def imp(url, deadline):  # default: impersonation refused like plain
            return 403, url, {"content-type": "text/html"}, b"<html><body>Access denied</body></html>"
    return serve(create_app(st, redis=r, impersonator=imp)), r


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------- walls and challenges never become whole-page successes ----------
def test_consent_wall_with_footer_is_thin():
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    md, _, _ = extract_html(CONSENT_WALL, "https://news.example/", True)
    assert "Footer link number 49" not in md and is_thin(CONSENT_WALL, md)


def test_challenge_200_with_footer_is_thin():
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    md, _, _ = extract_html(CHALLENGE_200, "https://news.example/", True)
    assert "Footer link number 49" not in md and is_thin(CHALLENGE_200, md)


def test_consent_wall_plain_goes_to_firecrawl(ctx):
    up, furl, _ = ctx
    EXTRA["wall"] = (CONSENT_WALL.encode(), "text/html", 200)
    EXTRA["chal200"] = (CHALLENGE_200.encode(), "text/html", 200)
    (srv, aurl), _ = make(furl)
    try:
        for name in ("wall", "chal200"):
            d = scrape(aurl, furl + "/p/" + name).json()["data"]
            assert d["metadata"]["fastweb"]["engine"] == "firecrawl", name
    finally:
        srv.should_exit = True


def test_impersonated_consent_wall_not_success(ctx):
    up, furl, _ = ctx

    async def imp(url, deadline):
        return 200, url, {"content-type": "text/html"}, CONSENT_WALL.encode()

    (srv, aurl), _ = make(furl, impersonator=imp)
    try:
        d = scrape(aurl, furl + "/p/challenge").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "firecrawl"
    finally:
        srv.should_exit = True


def test_whole_page_challenge_text_is_thin():
    from fastweb_firecrawl.heuristics import is_thin
    md = "Just a moment... Checking your browser\n\n" + "\n".join(f"- [Footer {i}](/f/{i})" for i in range(80))
    assert is_thin("<html></html>", md)


# ---------- whole-page rules, each on its own ----------
def test_ratio_rule_alone():
    from fastweb_firecrawl.extract import wants_whole_page
    assert wants_whole_page("https://x.example/", True, 100, 5000, 0.1)
    assert not wants_whole_page("https://x.example/", True, 1000, 5000, 0.1)


def test_hub_rule_alone():
    from fastweb_firecrawl.extract import wants_whole_page
    assert wants_whole_page("https://x.example/", True, 1000, 4000, 0.8)
    assert not wants_whole_page("https://x.example/", True, 1000, 4000, 0.3)


def test_ratio_hub_fixture_whole_page_without_script():
    import trafilatura

    from fastweb_firecrawl.extract import extract_html
    assert len(trafilatura.extract(RATIO_HUB, output_format="markdown") or "") < 300
    md, _, _ = extract_html(RATIO_HUB, "https://hub.example/", True)
    assert "Harbour vote row 69" in md and "SCRIPTLEAK" not in md


def test_link_hub_fixture_whole_page_without_script():
    from fastweb_firecrawl.extract import extract_html
    md, _, _ = extract_html(LINK_HUB, "https://hub.example/", True)
    assert "Section 79 headline" in md and "SCRIPTLEAK" not in md


# ---------- size caps ----------
@pytest.mark.slow
def test_whole_page_skipped_for_huge_html_and_markdown_capped():
    from fastweb_firecrawl.extract import MAX_MD_CHARS, WHOLE_PAGE_MAX_HTML, extract_html
    pad = "<div class='related'>" + RELATED * 20 + "</div>"
    big = RATIO_HUB.replace("</body>", pad * (2 * WHOLE_PAGE_MAX_HTML // len(pad)) + "</body>")
    md, _, _ = extract_html(big, "https://hub.example/", True)
    assert len(md) < 5000  # main content only
    huge = "<html><body><article>" + "<p>Long paragraph about the harbour vote and tides.</p>" * 14000 + \
           "</article></body></html>"
    assert len(extract_html(huge, "https://x.example/", True)[0]) <= MAX_MD_CHARS


def test_default_caps():
    from fastweb_firecrawl.settings import Settings
    assert Settings().max_bytes == 6 * 1024 * 1024 and Settings().extract_max_html == 3 * 1024 * 1024


def test_script_heavy_page_over_extract_cap_still_extracts(ctx):
    """apnews.com: 3.8 MB of HTML, mostly inline scripts/JSON -> stripped before extraction, not 413."""
    up, furl, _ = ctx
    EXTRA["scripty"] = (("<html><head><title>Scripty</title><script>var d='" + "j" * 3_500_000 + "'</script>"
                         "<style>.a{}" + " " * 200_000 + "</style></head><body><article>" + ARTICLE + "</article>"
                         "<svg>" + "<path d='M0 0'/>" * 1000 + "</svg><!-- " + "c" * 100_000 + " --></body></html>"
                         ).encode(), "text/html", 200)
    (srv, aurl), _ = make(furl, max_bytes=6 * 1024 * 1024, extract_max_html=3 * 1024 * 1024, total_timeout=5.0)
    try:
        d = scrape(aurl, furl + "/p/scripty", formats=["markdown", "html"]).json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "plain" and "Paragraph 19" in d["markdown"]
        assert len(d["html"]) > 3_500_000  # html format stays the original page
    finally:
        srv.should_exit = True


def test_markup_over_extract_cap_extracts_bounded_prefix(ctx):
    """over the cap -> bounded prefix extracted here, not Firecrawl."""
    up, furl, _ = ctx
    EXTRA["bigmarkup"] = (("<html><body><table>" + "<tr><td>cell</td><td>value</td></tr>" * 100_000
                           + "</table></body></html>").encode(), "text/html", 200)
    (srv, aurl), _ = make(furl, max_bytes=6 * 1024 * 1024, extract_max_html=2 * 1024 * 1024)
    try:
        d = scrape(aurl, furl + "/p/bigmarkup").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "plain" and d["metadata"]["fastweb"]["text_only_large"]
        assert not up.scrapes
    finally:
        srv.should_exit = True


# ---------- charset ----------
def test_header_charset_beats_meta():
    from fastweb_firecrawl.extract import decode_html
    data = "<meta charset='utf-8'><p>caf\xe9</p>".encode("cp1252")
    assert "café" in decode_html(data, "text/html; charset=windows-1252")


def test_latin1_label_decodes_as_cp1252():
    from fastweb_firecrawl.extract import decode_html
    assert decode_html(b"<p>\x80 \x93hi\x94</p>", "text/html; charset=iso-8859-1") == "<p>€ “hi”</p>"


def test_meta_utf16_means_utf8():
    from fastweb_firecrawl.extract import decode_html
    assert decode_html("<meta charset='utf-16'><p>café</p>".encode(), "text/html") == "<meta charset='utf-16'><p>café</p>"


# ---------- host memory ----------
def test_dead_rule():
    from fastweb_firecrawl.cache import is_dead
    t = 1_000_000.0
    assert not is_dead([("/a", t), ("/b", t + 700)])                       # 2 observations
    assert not is_dead([("/a", t), ("/a", t + 300), ("/a", t + 700)])      # one path
    assert not is_dead([("/a", t), ("/b", t + 100), ("/a", t + 500)])      # under 10 min
    assert is_dead([("/a", t), ("/b", t + 100), ("/a", t + 601)])


def test_store_observations_and_ttls():
    from fastweb_firecrawl.cache import DEAD, Store
    r = fakeredis.FakeAsyncRedis()
    now = [1_000_000.0]
    st = Store(r, clock=lambda: now[0])

    async def go():
        assert not await st.observe_block("h.example", "/a")
        now[0] += 300
        assert not await st.observe_block("h.example", "/b")
        now[0] += 301
        assert await st.observe_block("h.example", "/a")
        assert await st.host("h.example") == DEAD
        assert 3500 < await r.ttl("fastweb:host:h.example") <= 3600
        assert 3500 < await r.ttl("fastweb:hostobs:h.example") <= 3600
        await st.forget_host("h.example")
        assert await st.host("h.example") is None and not await r.exists("fastweb:hostobs:h.example")
    run(go())


def test_one_htaccess_403_does_not_block_host(ctx):
    """gnu.org: one 403 path, even with Firecrawl reporting the target's 403, leaves the host usable."""
    up, furl, _ = ctx
    up.mode = "target403"
    EXTRA["htaccess"] = (b"<html><body>Forbidden</body></html>", "text/html", 403)
    (srv, aurl), r = make(furl)
    try:
        assert scrape(aurl, furl + "/p/htaccess").status_code == 422
        up.mode = ""
        d = scrape(aurl, furl + "/p/article").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "plain"
        assert run(r.get("fastweb:host:127.0.0.1")) is None
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("mode", ["fail", "502", "slow"])
def test_our_side_firecrawl_failures_not_observed(ctx, mode):
    up, furl, _ = ctx
    if mode == "fail":
        up.fail = True
    else:
        up.mode = mode
    (srv, aurl), r = make(furl, fallback_timeout=0.5)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_429_not_observed(ctx):
    up, furl, _ = ctx
    up.mode = "target403"
    EXTRA["busy"] = (b"<html><body>slow down</body></html>", "text/html", 429)
    (srv, aurl), r = make(furl)
    try:
        scrape(aurl, furl + "/p/busy")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_thin_page_failure_not_observed(ctx):
    up, furl, _ = ctx
    up.mode = "target403"
    (srv, aurl), r = make(furl)
    try:
        scrape(aurl, furl + "/p/shell")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_observation_keyed_on_final_host(ctx):
    """A redirector (the localhost alias, allowed here) takes no blame for its target's 403."""
    up, furl, _ = ctx
    up.mode = "target403"
    port = httpx.URL(furl).port
    (srv, aurl), r = make(furl, allow_hosts=frozenset({"127.0.0.1", "localhost"}))
    try:
        scrape(aurl, f"http://localhost:{port}/p/to_challenge")
        assert run(r.zcard("fastweb:hostobs:localhost")) == 0
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 1
    finally:
        srv.should_exit = True


def seed_dead(r, host="127.0.0.1"):
    run(r.set("fastweb:host:" + host, "dead", ex=3600))


def test_dead_host_still_tries_cheap_tiers_and_heals(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    seed_dead(r)
    try:
        d = scrape(aurl, furl + "/p/article").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "plain"
        assert run(r.get("fastweb:host:127.0.0.1")) is None
    finally:
        srv.should_exit = True


def test_dead_host_skips_firecrawl_fast(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    seed_dead(r)
    try:
        res = scrape(aurl, furl + "/p/challenge")
        assert res.status_code == 422 and "blocked" in res.json()["error"]
        assert up.scrapes == []
    finally:
        srv.should_exit = True


def test_dead_after_three_target_side_failures(ctx):
    up, furl, _ = ctx
    up.mode = "target403"
    EXTRA["c2"] = (b"<html><body>Forbidden</body></html>", "text/html", 403)
    (srv, aurl), r = make(furl)
    old = time.time() - 700
    run(r.zadd("fastweb:hostobs:127.0.0.1", {f"/p/c2|{old}": old, f"/p/challenge|{old + 1}": old + 1}))
    try:
        assert scrape(aurl, furl + "/p/challenge").status_code == 422
        assert run(r.get("fastweb:host:127.0.0.1")) == b"dead"
    finally:
        srv.should_exit = True


def test_firecrawl_failure_on_remembered_host_forgets_it(ctx):
    up, furl, _ = ctx
    up.fail = True
    (srv, aurl), r = make(furl)
    run(r.set("fastweb:host:127.0.0.1", "firecrawl", ex=1800))
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.get("fastweb:host:127.0.0.1")) is None
    finally:
        srv.should_exit = True


def test_firecrawl_memory_still_tries_impersonation(ctx):
    up, furl, _ = ctx
    calls = []

    async def imp(url, deadline):
        calls.append(url)
        return 200, url, {"content-type": "text/html"}, ARTICLE.encode()

    (srv, aurl), r = make(furl, impersonator=imp, reprobe_rate=0.0)
    run(r.set("fastweb:host:127.0.0.1", "firecrawl", ex=1800))
    try:
        up.hits.clear()
        d = scrape(aurl, furl + "/p/challenge").json()["data"]
        assert calls and d["metadata"]["fastweb"]["engine"] == "impersonate" and up.hits == []
        assert run(r.get("fastweb:host:127.0.0.1")) is None
    finally:
        srv.should_exit = True


def test_firecrawl_memory_ttl_and_reprobe(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl, reprobe_rate=1.0)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert 1700 < run(r.ttl("fastweb:host:127.0.0.1")) <= 1800
        up.hits.clear()
        scrape(aurl, furl + "/p/challenge", onlyMainContent=False)
        assert up.hits == ["challenge"]  # re-probed plain
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("memory", ["dead", "firecrawl"])
def test_ssrf_on_memory_paths(ctx, memory):
    up, furl, _ = ctx
    port = httpx.URL(furl).port
    (srv, aurl), r = make(furl, reprobe_rate=0.0)
    run(r.set("fastweb:host:localhost", memory, ex=600))
    try:
        res = scrape(aurl, f"http://localhost:{port}/p/article")
        assert res.status_code == 422 and "private" in res.json()["error"]
        assert up.scrapes == [] and up.hits == []
    finally:
        srv.should_exit = True


def test_retry_after_skips_redis():
    from fastweb_firecrawl.cache import RETRY_AFTER, Store
    assert RETRY_AFTER >= 10
    calls = []

    class Broken:
        async def get(self, *a, **kw):
            calls.append(a)
            raise ConnectionError

    st = Store(Broken())

    async def go():
        await st.host("a")
        await st.host("a")
    run(go())
    assert len(calls) == 1


# ---------- cache key and entries ----------
def test_sdk_body_is_cacheable():
    from fastweb_firecrawl.cache import scrape_key_for_request
    k = scrape_key_for_request(SDK_BODY)
    assert k and k == scrape_key_for_request({**SDK_BODY, "maxAge": 1, "origin": "other", "timeout": 5})
    # every subset of the content formats shares one entry (the whole page; each request gets its projection)
    for fmts in (["markdown"], ["html"], ["rawHtml"], ["markdown", "rawHtml"], [{"type": "markdown"}], None):
        assert k == scrape_key_for_request({**SDK_BODY, "formats": fmts}), fmts
    assert k == scrape_key_for_request({**SDK_BODY, "formats": ["markdown", "links"]})  # links: not produced
    assert k == scrape_key_for_request({**SDK_BODY, "formats": ["links"]})
    assert k != scrape_key_for_request({**SDK_BODY, "onlyMainContent": False})
    assert k != scrape_key_for_request({**SDK_BODY, "url": "https://other.example/"})
    for extra in ({"mobile": True}, {"waitFor": 1000}, {"actions": []}, {"headers": {"a": "b"}},
                  {"includeTags": ["p"]}, {"formats": [{"type": "json", "schema": {}}]}):
        assert scrape_key_for_request({**SDK_BODY, **extra}) is None, extra


def test_sdk_body_cache_hit(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    body = {**SDK_BODY, "url": furl + "/p/article"}
    try:
        a = httpx.post(aurl + "/v2/scrape", json=body, timeout=30)
        assert a.headers["X-Fastweb-Cache"] == "MISS"
        up.hits.clear()
        b = httpx.post(aurl + "/v2/scrape", json={**body, "maxAge": 60000}, timeout=30)
        assert b.headers["X-Fastweb-Cache"] == "HIT" and up.hits == []
        assert b.json() == a.json()
        c = httpx.post(aurl + "/v2/scrape", json={**body, "formats": ["markdown"]}, timeout=30)
        assert c.headers["X-Fastweb-Cache"] == "HIT" and up.hits == []
        assert set(c.json()["data"]) == {"markdown", "metadata"}
        assert c.json()["data"]["markdown"] == a.json()["data"]["markdown"]
        raw = httpx.post(aurl + "/v2/scrape", json={**body, "formats": ["rawHtml"]}, timeout=30)
        assert raw.headers["X-Fastweb-Cache"] == "HIT" and set(raw.json()["data"]) == {"rawHtml", "metadata"}
        assert raw.json()["data"]["rawHtml"] == a.json()["data"]["html"]
        d = httpx.post(aurl + "/v2/scrape", json={**body, "waitFor": 100}, timeout=30)
        e = httpx.post(aurl + "/v2/scrape", json={**body, "waitFor": 100}, timeout=30)
        assert d.headers["X-Fastweb-Cache"] == e.headers["X-Fastweb-Cache"] == "MISS"
    finally:
        srv.should_exit = True


def test_entries_compressed_with_ttl_and_cap():
    from fastweb_firecrawl.cache import MAX_ENTRY, Store, scrape_key_for_request
    r = fakeredis.FakeAsyncRedis()
    st = Store(r, scrape_ttl=600)
    key = scrape_key_for_request({"url": "https://a.example/"})
    payload = json.dumps({"md": "harbour " * 20000}).encode()

    async def go():
        await st.put_scrape(key, payload)
        raw = await r.get(key)
        assert len(raw) < len(payload) / 10 and zlib.decompress(raw[11:]) == payload
        assert (await st.get_scrape(key))[0] == payload
        assert 590 < await r.ttl(key) <= 600
        import os
        big = os.urandom(MAX_ENTRY + 10)
        k2 = scrape_key_for_request({"url": "https://b.example/"})
        await st.put_scrape(k2, big)
        assert not await r.exists(k2)
    assert MAX_ENTRY == 1024 * 1024
    run(go())


def test_redis_default_is_dedicated_service():
    import os

    from fastweb_firecrawl.settings import Settings
    if "FASTWEB_REDIS_URL" not in os.environ:
        assert Settings().redis_url == "redis://fastweb-redis:6379/0"


# ---------- extraction lanes ----------
def threads(name):
    """Lane executor for tests that monkeypatch the extraction function (worker processes would not see it)."""
    from concurrent.futures import ThreadPoolExecutor
    return ThreadPoolExecutor(1)


@pytest.mark.slow
def test_small_page_not_stuck_behind_big_one(ctx, monkeypatch):
    up, furl, _ = ctx
    from fastweb_firecrawl import pipeline
    real = pipeline.extract_job

    def slow_for_big(html, url, main_only, max_html=0):
        if len(html) > 500_000:
            time.sleep(8.0)
        return real(html, url, main_only, max_html)

    monkeypatch.setattr(pipeline, "extract_job", slow_for_big)
    EXTRA["bigpage"] = (("<html><body><article>" + "<p>Big page paragraph about the tides.</p>" * 15000
                         + "</article></body></html>").encode(), "text/html", 200)
    (srv, aurl), _ = make(furl, max_bytes=3 * 1024 * 1024, total_timeout=15.0, lane_factory=threads)
    try:
        base = time.monotonic()
        scrape(aurl, furl + "/p/article", formats=["markdown"], onlyMainContent=False)
        base = time.monotonic() - base
        out = {}

        def big():
            out["big"] = scrape(aurl, furl + "/p/bigpage")

        th = threading.Thread(target=big)
        th.start()
        time.sleep(0.4)  # big page is now extracting
        t = time.monotonic()
        small = scrape(aurl, furl + "/p/article", formats=["html"])
        took = time.monotonic() - t
        th.join()
        assert small.status_code == 200 and out["big"].status_code == 200
        assert took < base + 4.0, (took, base)  # not queued behind the 8 s big-page extraction
    finally:
        srv.should_exit = True


@pytest.mark.slow
def test_semaphore_held_until_thread_finishes(ctx, monkeypatch):
    up, furl, _ = ctx
    from fastweb_firecrawl import pipeline
    running, peak = [0], [0]
    lock = threading.Lock()

    def slow(html, url, main_only, max_html=0):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(1.2)
        with lock:
            running[0] -= 1
        return "x" * 200, "t", None, 200

    monkeypatch.setattr(pipeline, "extract_job", slow)
    (srv, aurl), _ = make(furl, lane_factory=threads)
    try:
        res = []
        ths = [threading.Thread(target=lambda i=i: res.append(scrape(aurl, furl + "/p/article", timeout=1000,
                                                                     formats=["markdown"] + ["html"] * i)))
               for i in range(2)]
        for th in ths:
            th.start()
            time.sleep(0.1)
        for th in ths:
            th.join()
        time.sleep(1.5)
        assert peak[0] == 1
    finally:
        srv.should_exit = True


# ---------- the real curl_cffi tier ----------
@pytest.fixture
def imp_site():
    """403 to httpx; to curl_cffi (sends sec-ch-ua) /ok is an article, /hop redirects, /deny stays 403."""
    hits = []

    async def page(request):
        name = request.path_params["name"]
        hits.append(name)
        chrome = "sec-ch-ua" in request.headers
        if not chrome or name == "deny":
            return HTMLResponse("<html><body>Access denied</body></html>", status_code=403)
        if name == "ok":
            return HTMLResponse(ARTICLE)
        if name == "bigok":
            return HTMLResponse(ARTICLE.replace("</article>", "<p>pad</p>" * 5000 + "</article>"))
        if name == "hop":
            return Response(status_code=302, headers={"location": request.query_params["to"]})
        return HTMLResponse("secret", status_code=200)

    srv, url = serve(Starlette(routes=[Route("/i/{name}", page)]))
    yield url, hits
    srv.should_exit = True


def make_real_imp(furl, **kw):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    r = fakeredis.FakeAsyncRedis()
    st = Settings(upstream=furl, searxng_url=furl, total_timeout=4.0, redis_url="", impersonate=True,
                  allow_hosts=frozenset({"127.0.0.1"}), **kw)
    return serve(create_app(st, redis=r)), r


def test_real_impersonation_success(ctx, imp_site):
    up, furl, _ = ctx
    site, hits = imp_site
    srv, aurl = make_real_imp(furl)[0]
    try:
        d = scrape(aurl, site + "/i/ok").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "impersonate" and "Paragraph 19" in d["markdown"]
        d = scrape(aurl, site + "/i/hop?to=/i/ok", onlyMainContent=False).json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "impersonate" and d["metadata"]["url"].endswith("/i/ok")
        assert up.scrapes == []
    finally:
        srv.should_exit = True


def test_real_impersonation_refused_goes_to_firecrawl(ctx, imp_site):
    up, furl, _ = ctx
    site, hits = imp_site
    srv, aurl = make_real_imp(furl)[0]
    try:
        d = scrape(aurl, site + "/i/deny").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "firecrawl" and hits.count("deny") == 2
    finally:
        srv.should_exit = True


def test_real_impersonation_checks_every_hop(ctx, imp_site):
    up, furl, _ = ctx
    site, hits = imp_site
    port = httpx.URL(site).port
    srv, aurl = make_real_imp(furl)[0]
    try:
        res = scrape(aurl, site + f"/i/hop?to=http://localhost:{port}/i/secret")
        assert res.status_code == 422 and "private" in res.json()["error"]
        assert "secret" not in hits and up.scrapes == []
    finally:
        srv.should_exit = True


# ---------- each wall check on its own ----------
# Main text carries the marker but the page has > 1,000 chars of non-link text (only the main-text check catches it).
CHALLENGE_RELATED = ("<html><head><title>Just a moment...</title></head><body><article><p>Just a moment. Checking "
                     "your browser before accessing the site.</p><p>This process is automatic and takes a few "
                     "seconds.</p></article><div class='related'>" + RELATED + "</div></body></html>")
# trafilatura drops the cookie banner; only the visible-text check sees it (main text has no marker).
BANNER_HUB = ("<html><head><title>News</title></head><body><div id='cookie-banner' class='cookie-consent'><p>We value "
              "your privacy. Accept all cookies to continue.</p></div><main><p>Breaking: short teaser about the "
              "harbour.</p></main><footer><ul>" + "".join(f"<li><a href='/f/{i}'>Footer link number {i} to a site "
                                                          "section</a></li>" for i in range(50)) + "</ul></footer></body></html>")


def test_main_text_wall_check():
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    md, _, _ = extract_html(CHALLENGE_RELATED, "https://news.example/", True)
    assert "Harbour vote row 69" not in md and is_thin(CHALLENGE_RELATED, md)


def test_visible_text_wall_check():
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    md, _, _ = extract_html(BANNER_HUB, "https://news.example/", True)
    assert "Footer link number 49" not in md and is_thin(BANNER_HUB, md)


def test_page_stats_ignores_script_text():
    from selectolax.lexbor import LexborHTMLParser

    from fastweb_firecrawl.extract import page_stats
    n, _, _, _ = page_stats(LexborHTMLParser("<html><body><p>hello world</p><script>var x='" + "s" * 5000
                                          + "'</script></body></html>"))
    assert n < 100


@pytest.mark.parametrize("i_status", [404, 429, 500])
def test_other_impersonation_status_not_observed(ctx, i_status):
    up, furl, _ = ctx
    up.mode = "target403"

    async def imp(url, deadline):
        return i_status, url, {"content-type": "text/html"}, b"<html><body>nope</body></html>"

    (srv, aurl), r = make(furl, impersonator=imp)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_impersonation_403_with_target_side_firecrawl_is_observed(ctx):
    up, furl, _ = ctx
    up.mode = "target403"
    (srv, aurl), r = make(furl)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 1
    finally:
        srv.should_exit = True


def test_whole_page_gate_uses_slimmed_body():
    """apnews.com: 3.1 MB of HTML but 480 KB of body markup once attributes go -> still whole page."""
    from fastweb_firecrawl.extract import WHOLE_PAGE_MAX_HTML, extract_html
    fat = RATIO_HUB.replace("<div class='related'>", "<div class='related' data-props='" + "x" * 25000 + "'>")
    assert len(fat) > WHOLE_PAGE_MAX_HTML
    md, _, _ = extract_html(fat, "https://hub.example/", True)
    assert "Harbour vote row 69" in md


def test_whole_page_with_comments_and_doctype_nodes():
    """aljazeera.com: comment nodes inside <body> broke attribute stripping (TypeError -> 422)."""
    from fastweb_firecrawl.extract import extract_html
    md, _, _ = extract_html(RATIO_HUB.replace("<div class='related'>", "<!-- ad slot --><div class='related'>"),
                            "https://hub.example/", True)
    assert "Harbour vote row 69" in md


# linkedin.com/in/...-like: some public teaser text, the sign-in modal repeated, whitespace-only lines between.
MODAL = ("<div class='modal'><h2>Sign in to view Ann's full profile</h2><p>Welcome back</p>"
         "<p>By clicking Continue, you agree to the User Agreement.</p></div>")
AUTH_WALL = ("<html><head><title>Ann Example - Harbour Board | Network</title></head><body><main><h1>Ann Example</h1>"
             + MODAL + "<section><h2>About</h2>" + "".join(f"<p>Harbour board member and ferry planner, term {i}.</p>"
                                                           "\n \n" for i in range(12))
             + "</section>" + MODAL + "<section><h2>Experience</h2>" + "".join(
                 f"<p>Planner at the harbour authority, project {i}: the eastern wharf and its timetable.</p>\n\t\n"
                 for i in range(30)) + "</section>" + MODAL
             + "</main></body></html>")


def test_repeated_sign_in_modal_is_a_wall():
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    md, _, _ = extract_html(AUTH_WALL, "https://social.example/in/ann", True)
    assert len(md) < 600 and is_thin(AUTH_WALL, md)


def test_repeated_sign_in_modal_dialogs_are_a_wall():
    """linkedin.com/in/...-like: the prompt sits only in role=dialog modals (dropped by extraction) over a long public
    teaser; the plain answer must not be a success."""
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    dialog = ("<section role='dialog'><h2>Sign in to view Ann’s full profile</h2><p>Welcome back</p>"
              "<button>Sign in</button></section>")
    page = AUTH_WALL.replace(MODAL, dialog)
    md, _, _ = extract_html(page, "https://social.example/in/ann", True)
    assert "Sign in to view Ann" in md and len(md) < 600 and is_thin(page, md)
    one = AUTH_WALL.replace(MODAL, dialog, 1).replace(MODAL, "")
    md, _, _ = extract_html(one, "https://social.example/in/ann", True)
    assert "eastern wharf" in md  # one modal (a login button's dialog) is not a wall


def test_one_sign_in_prompt_is_not_a_wall():
    from fastweb_firecrawl.extract import extract_html
    page = AUTH_WALL.replace(MODAL, "", 2)
    md, _, _ = extract_html(page, "https://social.example/in/ann", True)
    assert "eastern wharf" in md and len(md) > 2000


def test_whitespace_only_lines_collapse():
    from fastweb_firecrawl.extract import extract_html
    md, _, _ = extract_html(AUTH_WALL.replace(MODAL, "", 2), "https://social.example/in/ann", True)
    assert "\n\n\n" not in md and not any(ln.strip() == "" and ln for ln in md.split("\n"))


def test_auth_wall_is_never_a_plain_success(ctx):
    up, furl, _ = ctx
    EXTRA["authwall"] = (AUTH_WALL.encode(), "text/html", 200)
    for kw, want in (({}, "firecrawl"), ({"upstream": ""}, None)):
        (srv, aurl), _ = make(furl, **kw)
        try:
            r = scrape(aurl, furl + "/p/authwall")
            if want:
                assert r.json()["data"]["metadata"]["fastweb"]["engine"] == want
            else:
                assert r.status_code >= 400 and not r.json()["success"], r.text
        finally:
            srv.should_exit = True


def test_tidy_blank_lines_keeps_code():
    from fastweb_firecrawl.extract import tidy_blank_lines
    assert tidy_blank_lines("a\n \n\t\n  \nb\n```\nx\n \n\n\ny\n```\n \nc") == "a\n\nb\n```\nx\n \n\n\ny\n```\n\nc"
