"""Extraction in recycled worker processes, no quadratic regex on page content,
host-memory evidence, lanes by markup cost, cache freshness rules, consent text, mutants."""
import asyncio
import json
import os
import time

import fakeredis
import pytest

from .conftest import ARTICLE, EXTRA
from .test_api import scrape
from .test_fetch_ladder import imp_site, make, make_real_imp, run, seed_dead


def timed(fn, *a):
    t = time.monotonic()
    out = fn(*a)
    return out, time.monotonic() - t


# ---------- linear stripping and whitespace clean-up ----------
@pytest.mark.parametrize("opener", ["<script>", "<style>", "<svg>", "<!--", "<SCRIPT type=x>"])
def test_lighten_unclosed_tags_is_linear(opener):
    from fastweb_firecrawl.extract import lighten
    page = "<html><body>" + "".join(f"<p>para {i}</p>{opener}" for i in range(2000)) + "x" * 1_000_000
    out, took = timed(lighten, page)
    assert took < 5.0, took  # quadratic: minutes
    assert "para 0" in out


def test_lighten_strips_closed_heavy_blocks():
    from fastweb_firecrawl.extract import lighten
    html = ("<html><body><p>keep 1</p><script src=a>var x='<p>gone</p>'</script ><STYLE>p{}</STYLE><svg><path/>"
            "</svg><!-- note --><p>keep 2</p><scripted>keep 3</scripted></body></html>")
    out = lighten(html)
    assert "gone" not in out and "p{}" not in out and "path" not in out and "note" not in out
    assert "keep 1" in out and "keep 2" in out and "keep 3" in out


def test_lighten_unclosed_keeps_rest():
    from fastweb_firecrawl.extract import lighten
    out = lighten("<p>a</p><script>var x;<p>b</p>")
    assert "<p>a</p>" in out


def test_whole_page_markdown_long_space_run_is_linear():
    from fastweb_firecrawl.extract import whole_page_markdown
    body = "<body><p>hello</p><pre>" + " " * 50_000 + "x\n" + "\t" * 50_000 + "\nend</pre></body>"
    md, took = timed(whole_page_markdown, body)
    assert took < 5.0, took
    assert "hello" in md and "end" in md and not any(line.endswith((" ", "\t")) for line in md.split("\n"))


def test_extract_html_long_space_pre_is_fast():
    from fastweb_firecrawl.extract import extract_html
    links = "".join(f"<li><a href='/s/{i}'>Section {i} headline about the harbour</a></li>" for i in range(80))
    page = "<html><body><ul>" + links + "</ul><pre>" + " " * 50_000 + "x\n</pre></body></html>"
    _, took = timed(extract_html, page, "https://hub.example/", False)
    assert took < 5.0, took


# ---------- worker processes, kill on hard timeout, recreate after a crash ----------
def _sleep(s):
    time.sleep(s)
    return os.getpid()


def _die():
    os._exit(9)


def test_lane_runs_in_another_process_and_recycles():
    from fastweb_firecrawl.worker import Lane
    lane = Lane("t", tasks_per_child=1)

    async def go():
        a = await lane.run(5, 5, _sleep, 0)
        b = await lane.run(5, 5, _sleep, 0)
        return a, b
    try:
        a, b = run(go())
        assert a != os.getpid() and b != os.getpid() and a != b
    finally:
        lane.close()


def test_lane_hard_timeout_kills_worker():
    from fastweb_firecrawl.worker import Lane, WorkerFailed
    lane = Lane("t", tasks_per_child=10)

    async def go():
        t = time.monotonic()
        with pytest.raises(WorkerFailed):
            await lane.run(5, 0.5, _sleep, 30)
        took = time.monotonic() - t
        return took, await lane.run(5, 5, _sleep, 0)
    try:
        took, pid = run(go())
        assert took < 12 and pid != os.getpid()  # 0.5 s hard timeout, not the 30 s task
    finally:
        lane.close()


def test_lane_recreated_after_worker_dies():
    from fastweb_firecrawl.worker import Lane, WorkerFailed
    lane = Lane("t", tasks_per_child=10)

    async def go():
        with pytest.raises(WorkerFailed):
            await lane.run(5, 5, _die)
        return await lane.run(5, 5, _sleep, 0)
    try:
        assert run(go()) != os.getpid()
    finally:
        lane.close()


def test_lane_table_has_pdf_lane():
    from fastweb_firecrawl.worker import LANES
    assert {"small", "big", "pdf"} <= set(LANES)


def test_byte_budget_waits_then_gives_up():
    from fastweb_firecrawl.worker import ByteBudget
    b = ByteBudget(100)

    async def go():
        assert await b.acquire(80, 1)
        t = time.monotonic()
        assert not await b.acquire(50, 0.3)
        assert time.monotonic() - t >= 0.25
        assert await b.acquire(20, 0.1)  # fits
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, b.release, 80)
        assert await b.acquire(50, 1)
        b.release(70)
        assert b.used == 0 and await b.acquire(500, 0.1)  # one oversized body is let through alone
    run(go())


def test_byte_budget_full_goes_to_firecrawl(ctx):
    up, furl, _ = ctx
    (srv, aurl), _ = make(furl, inflight_bytes=10)
    try:
        d = scrape(aurl, furl + "/p/article", timeout=3000).json()
        # one body alone is always allowed: the article extracts here
        assert d["data"]["metadata"]["fastweb"]["engine"] == "plain"
    finally:
        srv.should_exit = True


@pytest.mark.slow
def test_hostile_page_does_not_freeze_service(ctx):
    """2k unclosed <svg in a 1.5 MB page: answered quickly; /health keeps answering meanwhile."""
    up, furl, _ = ctx
    EXTRA["hostile"] = (("<html><body>" + "<p>Text about tides.</p><svg>" * 2000 + "<p>x</p>" * 180_000
                         + "</body></html>").encode(), "text/html", 200)
    (srv, aurl), _ = make(furl, total_timeout=5.0)
    try:
        t = time.monotonic()
        res = scrape(aurl, furl + "/p/hostile", timeout=8000)
        assert time.monotonic() - t < 12 and res.status_code in (200, 422, 504)
    finally:
        srv.should_exit = True


# ---------- lane by markup cost, PDFs their own lane ----------
def test_lane_choice():
    from fastweb_firecrawl.heuristics import choose_lane
    assert choose_lane(False, 300_000) == "small"
    assert choose_lane(False, 600_000) == "big"
    assert choose_lane(True, 1000) == "pdf"


def test_script_heavy_page_uses_small_lane(ctx):
    up, furl, _ = ctx
    EXTRA["scripty"] = ((ARTICLE.replace("</body>", "<script>" + "var a=1;" * 120_000 + "</script></body>")).encode(),
                        "text/html", 200)
    (srv, aurl), _ = make(furl)
    try:
        d = scrape(aurl, furl + "/p/scripty").json()["data"]
        assert d["metadata"]["fastweb"]["lane"] == "small"
        d = scrape(aurl, furl + "/p/pdf").json()["data"]
        assert d["metadata"]["fastweb"]["lane"] == "pdf"
    finally:
        srv.should_exit = True


# ---------- host-memory evidence ----------
def test_firecrawl_success_false_4xx_not_observed(ctx):
    """Firecrawl's own 408/400/200-success:false are not target evidence."""
    up, furl, _ = ctx
    up.mode = "fc408"
    (srv, aurl), r = make(furl)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("mode", ["error404", "target500"])
def test_firecrawl_other_target_status_not_observed(ctx, mode):
    up, furl, _ = ctx
    up.mode = mode
    (srv, aurl), r = make(furl)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_impersonation_exception_not_observed(ctx):
    up, furl, _ = ctx
    up.mode = "target403"

    async def imp(url, deadline):
        raise ConnectionError("blip")

    (srv, aurl), r = make(furl, impersonator=imp)
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_no_impersonation_tier_not_observed(ctx):
    up, furl, _ = ctx
    up.mode = "target403"
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings

    from .conftest import serve
    r = fakeredis.FakeAsyncRedis()
    st = Settings(upstream=furl, searxng_url=furl, total_timeout=2.0, redis_url="", impersonate=False,
                  allow_hosts=frozenset({"127.0.0.1"}))
    srv, aurl = serve(create_app(st, redis=r))
    try:
        scrape(aurl, furl + "/p/challenge")
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_success_clears_pending_observations(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    now = time.time()
    run(r.zadd("fastweb:hostobs:127.0.0.1", {f"/a|{now}": now, f"/b|{now}": now}))
    try:
        assert scrape(aurl, furl + "/p/article").status_code == 200
        assert run(r.zcard("fastweb:hostobs:127.0.0.1")) == 0
    finally:
        srv.should_exit = True


def test_dead_budget_bounds_slow_host(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl, total_timeout=8.0)
    seed_dead(r)
    try:
        t = time.monotonic()
        res = scrape(aurl, furl + "/p/slow", timeout=30000)
        # the 1.2 s dead budget; without it the 5 s page would answer 200
        assert time.monotonic() - t < 4.0 and res.status_code in (422, 504)
        assert up.scrapes == []
    finally:
        srv.should_exit = True


def test_dead_reprobe_tries_firecrawl(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl, dead_reprobe_rate=1.0)
    seed_dead(r)
    try:
        d = scrape(aurl, furl + "/p/challenge").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "firecrawl" and len(up.scrapes) == 1
    finally:
        srv.should_exit = True


def test_observation_window_pruned():
    from fastweb_firecrawl.cache import Store
    r = fakeredis.FakeAsyncRedis()
    now = [1_000_000.0]
    st = Store(r, host_ttl=3600, clock=lambda: now[0])

    async def go():
        await st.observe_block("h.example", "/a")
        now[0] += 3601
        await st.observe_block("h.example", "/b")
        return await r.zcard("fastweb:hostobs:h.example")
    assert run(go()) == 1


def test_setting_defaults(monkeypatch):
    for k in ("FASTWEB_HOST_TTL", "FASTWEB_SCRAPE_TTL", "FASTWEB_FALLBACK_TIMEOUT", "FASTWEB_INFLIGHT_BYTES"):
        monkeypatch.delenv(k, raising=False)
    from fastweb_firecrawl.settings import Settings
    s = Settings()
    assert s.host_ttl == 3600 and s.scrape_ttl == 600 and s.fallback_timeout == 12
    assert s.dead_budget == 1.2 and s.inflight_bytes == 48 * 1024 * 1024


def test_real_impersonation_body_cap(ctx, imp_site):
    """curl_cffi download stops at max_bytes: the oversized page goes to Firecrawl."""
    up, furl, _ = ctx
    site, hits = imp_site
    srv, aurl = make_real_imp(furl, max_bytes=20_000)[0]
    try:
        d = scrape(aurl, site + "/i/bigok").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "firecrawl"
    finally:
        srv.should_exit = True


def test_over_cap_page_goes_to_firecrawl(ctx):
    up, furl, _ = ctx
    (srv, aurl), _ = make(furl, max_bytes=1024 * 1024)
    try:
        d = scrape(aurl, furl + "/p/huge").json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "firecrawl"
    finally:
        srv.should_exit = True


# ---------- cache freshness and opt-outs ----------
def test_store_returns_age():
    from fastweb_firecrawl.cache import Store
    r = fakeredis.FakeAsyncRedis()
    now = [1_000_000.0]
    st = Store(r, scrape_ttl=600, clock=lambda: now[0])

    async def go():
        await st.put_scrape("k", b"payload")
        now[0] += 42
        return await st.get_scrape("k")
    payload, age = run(go())
    assert payload == b"payload" and 41.9 < age < 42.1


def test_cache_honours_max_age_and_opt_outs(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    try:
        u = furl + "/p/article"
        assert scrape(aurl, u, storeInCache=False).headers["X-Fastweb-Cache"] == "MISS"
        assert scrape(aurl, u, zeroDataRetention=True).headers["X-Fastweb-Cache"] == "MISS"
        assert scrape(aurl, u).headers["X-Fastweb-Cache"] == "MISS"   # first storing request
        assert scrape(aurl, u).headers["X-Fastweb-Cache"] == "HIT"
        time.sleep(0.05)
        assert scrape(aurl, u, maxAge=0).headers["X-Fastweb-Cache"] == "MISS"
        assert scrape(aurl, u, maxAge=14_400_000).headers["X-Fastweb-Cache"] == "HIT"
    finally:
        srv.should_exit = True


# ---------- IAB/TCF consent text ----------
TCF = ("We and our 842 partners store and/or access information on a device, such as cookies and process personal "
       "data, such as unique identifiers and standard information sent by a device for personalised advertising and "
       "content, advertising and content measurement, and audience research, as well as to develop and improve "
       "products. With your permission we and our partners may use precise geolocation data and identification "
       "through device scanning. You may click to consent to our and our 842 partners' processing as described "
       "above. Alternatively you may click to refuse to consent or access more detailed information and change your "
       "preferences before consenting. Please note that some processing of your personal data may not require your "
       "consent, but you have a right to object to such processing. Manage options. Accept all.")


def test_tcf_consent_text_is_thin():
    from fastweb_firecrawl.heuristics import is_thin
    assert is_thin("<html></html>", TCF)
    assert not is_thin("<html></html>", "Real article text about the harbour vote. " * 100)


# ---------- nits ----------
def test_pdf_markdown_capped():
    from fastweb_firecrawl.extract import pdf_job

    from .conftest import make_pdf
    md, _, pages = pdf_job(make_pdf(), 10)
    assert len(md) == 10 and pages == 1


def test_injected_heading_not_counted_as_text():
    from fastweb_firecrawl.extract import extract_job
    html = "<html><head><title>A Long Page Title For Counting</title></head><body><p>tiny text</p></body></html>"
    md, title, desc, n = extract_job(html, "https://x.example/", True)
    assert md.startswith("# ") and n < len(md.strip())
