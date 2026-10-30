"""A warm Chrome headless-shell tier (raw CDP) between the cheap tiers and Firecrawl, for thin/blocked pages.
Unit tests run against tests/fake_cdp.py; test_real_headless_shell needs a real container (marked `browser`)."""
import asyncio
import os
import threading
import time

import httpx
import pytest

from . import test_prefetch as tp
from .conftest import serve
from .fake_cdp import FakeCDP
from .test_api import scrape
from .test_fetch_ladder import make

RENDERED = ("<html><head><title>Rendered App</title></head><body><main><h1>Rendered App</h1>"
            + "".join(f"<p>Rendered paragraph {i}: tide tables for the harbour and the council's budget.</p>"
                      for i in range(20)) + "</main></body></html>")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def cdp():
    f = FakeCDP()
    f.endpoint = f.start()
    yield f
    f.stop()


async def allow_local(url):
    """Test guard: like fastweb's check_target with allow_hosts={127.0.0.1}: only 127.0.0.1 is allowed."""
    return httpx.URL(url).host == "127.0.0.1"


def browser(cdp, **kw):
    from fastweb_firecrawl.browser import Browser
    return Browser(cdp.endpoint, guard=kw.pop("guard", allow_local), **kw)


# ---------------------------------------------------------------- client: settle, cap, SSRF, blocking
def test_settle_waits_for_text_stable_and_xhr_quiet(cdp):
    url = "http://127.0.0.1:1/app"
    cdp.pages[url] = cdp.scenario(timeline=[(0, "<p>a</p>"), (150, "<p>aaaa</p>"), (300, RENDERED)], xhr=[(0, 700)])

    async def go():
        b = browser(cdp, settle=10.0, timeout=12.0)
        try:
            return await b.render(url)
        finally:
            await b.close()
    t = time.monotonic()
    r = run(go())
    wall = time.monotonic() - t
    assert r.html == RENDERED and r.status == 200 and r.url == url
    # Waited for the XHR (700 ms) + 200 ms stable (the lower bound proves the settle logic), not the 10 s cap: the
    # upper bound leaves about 5x headroom so a loaded CI runner does not fail it.
    assert 0.7 <= wall < 5.0, wall
    assert cdp.contexts == set() and cdp.created == 1  # fresh context, disposed


def test_empty_body_is_not_settled(cdp):
    """innerText 0 for 200 ms is a page whose script has not run yet (a 300 ms timer on real Chrome), not a result."""
    url = "http://127.0.0.1:1/late"
    cdp.pages[url] = cdp.scenario(timeline=[(0, "<div id=root></div>"), (400, RENDERED)])

    async def go():
        b = browser(cdp, settle=2.0, timeout=3.0)
        try:
            return await b.render(url)
        finally:
            await b.close()
    assert run(go()).html == RENDERED


def test_settle_cap_takes_best_html_so_far(cdp):
    url = "http://127.0.0.1:1/ticker"
    cdp.pages[url] = cdp.scenario(timeline=[(i * 60, f"<p>{'x' * i}</p>") for i in range(200)])

    async def go():
        b = browser(cdp, settle=0.8, timeout=20.0)
        try:
            return await b.render(url)
        finally:
            await b.close()
    t = time.monotonic()
    r = run(go())
    assert time.monotonic() - t < 10.0  # the 0.8 s settle cap ended it, not the 20 s page budget
    assert r.html.startswith("<p>x") and len(r.html) > 10


def test_page_budget_bounds_a_slow_dcl(cdp):
    url = "http://127.0.0.1:1/slowdcl"
    cdp.pages[url] = cdp.scenario(html=RENDERED, dcl_ms=5000)

    async def go():
        b = browser(cdp, settle=3.0, timeout=1.0)
        try:
            return await b.render(url)
        finally:
            await b.close()
    t = time.monotonic()
    r = run(go())
    assert time.monotonic() - t < 1.8 and r.status == 200


def test_redirect_to_private_ip_fails_the_document(cdp):
    from fastweb_firecrawl.browser import PrivateTarget
    url = "http://127.0.0.1:1/hop"
    cdp.pages[url] = cdp.scenario(html=RENDERED, redirects=["http://10.0.0.5/admin"])

    async def go():
        b = browser(cdp)
        try:
            return await b.render(url)
        finally:
            await b.close()
    with pytest.raises(PrivateTarget):
        run(go())
    assert ("http://10.0.0.5/admin", "Document", "fail:BlockedByClient") in cdp.decisions
    assert cdp.contexts == set()


def test_subresources_iframes_types_and_trackers(cdp):
    url = "http://127.0.0.1:1/subs"
    subs = [("http://192.168.1.1/x.js", "Script"), ("http://127.0.0.2/frame", "Document"),
            ("http://169.254.169.254/latest", "XHR"), ("http://127.0.0.1:1/a.png", "Image"),
            ("http://127.0.0.1:1/f.woff2", "Font"), ("http://127.0.0.1:1/v.mp4", "Media"),
            ("http://127.0.0.1:1/s.css", "Stylesheet"), ("http://127.0.0.1:1/app.js", "Script"),
            ("https://www.google-analytics.com/analytics.js", "Script")]
    cdp.pages[url] = cdp.scenario(html=RENDERED, subs=subs)

    async def go():
        b = browser(cdp, settle=0.5, timeout=1.5)
        try:
            r = await b.render(url)
            await asyncio.sleep(0.2)
            return r, b.stats()
        finally:
            await b.close()
    r, st = run(go())
    got = {u: d for u, _, d in cdp.decisions}
    for u in ("http://192.168.1.1/x.js", "http://127.0.0.2/frame", "http://169.254.169.254/latest",
              "http://127.0.0.1:1/a.png", "http://127.0.0.1:1/f.woff2", "http://127.0.0.1:1/v.mp4",
              "https://www.google-analytics.com/analytics.js"):
        assert got[u].startswith("fail:"), (u, got[u])
    assert got["http://127.0.0.1:1/s.css"] == "continue"  # never CSS: module apps wait for it
    assert got["http://127.0.0.1:1/app.js"] == "continue"
    assert r.status == 200 and st["blocked_private"] >= 3
    assert cdp.bypass_sw == [True] and cdp.ua and "Headless" not in cdp.ua[0]


def test_tabs_bound_and_busy(cdp):
    from fastweb_firecrawl.browser import BrowserBusy
    for i in range(3):
        cdp.pages[f"http://127.0.0.1:1/s{i}"] = cdp.scenario(html=RENDERED, nav_delay=1.0)

    async def go():
        b = browser(cdp, tabs=2, settle=0.3, timeout=3.0, wait=0.3)
        try:
            return await asyncio.gather(*(b.render(f"http://127.0.0.1:1/s{i}") for i in range(3)),
                                        return_exceptions=True)
        finally:
            await b.close()
    res = run(go())
    assert sum(isinstance(x, BrowserBusy) for x in res) == 1
    assert cdp.max_targets == 2


def test_queued_renders_are_capped(cdp):
    """1 tab: 1 renders, WAITERS_PER_TAB queue for it, the rest fail fast instead of waiting their whole budget."""
    from fastweb_firecrawl.browser import WAITERS_PER_TAB, BrowserBusy
    n = WAITERS_PER_TAB + 3
    for i in range(n):
        cdp.pages[f"http://127.0.0.1:1/q{i}"] = cdp.scenario(html=RENDERED, nav_delay=0.2)

    async def go():
        b = browser(cdp, tabs=1, settle=0.1, timeout=3.0, wait=10.0)
        try:
            tasks = [asyncio.create_task(b.render(f"http://127.0.0.1:1/q{i}")) for i in range(n)]
            await asyncio.sleep(0.2)
            fast = sum(t.done() and isinstance(t.exception(), BrowserBusy) for t in tasks)
            res = await asyncio.gather(*tasks, return_exceptions=True)
            assert sum(isinstance(x, BrowserBusy) for x in res) == fast  # the queued ones all rendered
            return fast, b.waiting
        finally:
            await b.close()
    fast, waiting = run(go())
    assert fast == 2 and waiting == 0


def test_crash_raises_down_and_reconnects(cdp):
    from fastweb_firecrawl.browser import BrowserDown
    cdp.pages["http://127.0.0.1:1/boom"] = cdp.scenario(html=RENDERED, crash=True)
    cdp.pages["http://127.0.0.1:1/ok"] = cdp.scenario(html=RENDERED)

    async def go():
        b = browser(cdp, settle=0.3, timeout=2.0, backoff=0.0)
        try:
            with pytest.raises(BrowserDown):
                await b.render("http://127.0.0.1:1/boom")
            return await b.render("http://127.0.0.1:1/ok")
        finally:
            await b.close()
    assert run(go()).html == RENDERED and cdp.connections == 2


def test_unreachable_is_down_fast():
    from fastweb_firecrawl.browser import Browser, BrowserDown

    async def go():
        b = Browser("http://127.0.0.1:9", guard=allow_local)
        t = time.monotonic()
        with pytest.raises(BrowserDown):
            await b.render("http://127.0.0.1:1/x")
        return time.monotonic() - t
    assert run(go()) < 10.0  # refused at once, not a retry loop over the default budget


def test_recycle_closes_browser_after_n_pages(cdp):
    cdp.pages["http://127.0.0.1:1/r"] = cdp.scenario(html=RENDERED)

    async def go():
        b = browser(cdp, settle=0.2, timeout=1.0, recycle=2, backoff=0.0)
        try:
            for _ in range(3):
                await b.render("http://127.0.0.1:1/r")
        finally:
            await b.close()
    run(go())
    assert cdp.browser_closes == 1 and cdp.connections == 2


def test_cancellation_disposes_context(cdp):
    cdp.pages["http://127.0.0.1:1/c"] = cdp.scenario(html=RENDERED, dcl_ms=3000)

    async def go():
        b = browser(cdp, settle=3.0, timeout=4.0)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(b.render("http://127.0.0.1:1/c"), 0.5)
            await asyncio.sleep(0.3)
        finally:
            await b.close()
    run(go())
    assert cdp.contexts == set() and cdp.targets == 0


@pytest.mark.slow
def test_dispose_hang_is_off_the_critical_path_and_recycles(cdp):
    """Chrome's disposeBrowserContext can hang 5 s; the result comes back first, the next page is not
    delayed, and the hung browser is recycled."""
    cdp.pages["http://127.0.0.1:1/d"] = cdp.scenario(html=RENDERED)

    async def go():
        b = browser(cdp, settle=0.2, timeout=2.0, backoff=0.0)
        try:
            t = time.monotonic()
            await b.render("http://127.0.0.1:1/d")
            base = time.monotonic() - t
            cdp.hang_once.add("Target.disposeBrowserContext")
            t = time.monotonic()
            await b.render("http://127.0.0.1:1/d")
            first = time.monotonic() - t
            t = time.monotonic()
            await b.render("http://127.0.0.1:1/d")
            nxt = time.monotonic() - t
            await asyncio.sleep(1.3)
            return base, first, nxt, b.stats()
        finally:
            await b.close()
    base, first, nxt, st = run(go())
    assert first < base + 2.5 and nxt < base + 2.5, (base, first, nxt)  # the hung dispose is never waited on
    assert cdp.browser_closes == 1 and st["recycles"] == 1


def test_create_hang_recycles_and_fails_fast(cdp):
    from fastweb_firecrawl.browser import BrowserDown
    cdp.pages["http://127.0.0.1:1/c"] = cdp.scenario(html=RENDERED)

    async def go():
        b = browser(cdp, settle=0.2, timeout=20.0, backoff=0.0)
        try:
            cdp.hang_once.add("Target.createBrowserContext")
            t = time.monotonic()
            with pytest.raises(BrowserDown):
                await b.render("http://127.0.0.1:1/c")
            took = time.monotonic() - t
            await asyncio.sleep(0.2)
            r = await b.render("http://127.0.0.1:1/c")
            return took, r
        finally:
            await b.close()
    took, r = run(go())
    assert took < 10.0 and r.html == RENDERED and cdp.browser_closes == 1  # SETUP_S, not the 20 s budget


def test_hung_evaluate_is_bounded_by_the_page_budget(cdp):
    cdp.pages["http://127.0.0.1:1/e"] = cdp.scenario(html=RENDERED)
    cdp.eval_hang = True

    async def go():
        b = browser(cdp, settle=0.8, timeout=1.2)
        try:
            return await b.render("http://127.0.0.1:1/e")
        finally:
            await b.close()
    t = time.monotonic()
    r = run(go())
    assert time.monotonic() - t < 10.0 and r.html == RENDERED  # bounded at all (the hang is forever)


# ---------------------------------------------------------------- the tier inside fastweb
def app(ctx, cdp, **kw):
    up, furl, _ = ctx
    kw = {"browser_url": cdp.endpoint if cdp else "", "browser_settle": 0.3, "browser_timeout": 1.5,
          "total_timeout": 2.0, **kw}
    (srv, aurl), r = make(furl, **kw)
    return srv, aurl


def test_shell_rendered_by_browser_not_firecrawl(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html=RENDERED)
    srv, aurl = app(ctx, cdp)
    try:
        r = scrape(aurl, url)
        d = r.json()["data"]
        fw = d["metadata"]["fastweb"]
        assert r.status_code == 200 and fw["engine"] == "browser" and fw["render_ms"] >= 0
        assert "Rendered paragraph 19" in d["markdown"] and not up.scrapes
        st = httpx.get(aurl + "/v1/fastweb/stats").json()["browser"]
        assert st["ok"] == 1 and st["p50_render_ms"] is not None
    finally:
        srv.should_exit = True


def test_blocked_page_rendered_by_browser(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/challenge"
    cdp.pages[url] = cdp.scenario(html=RENDERED)
    srv, aurl = app(ctx, cdp)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 200 and r.json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
        assert not up.scrapes
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("sc,why", [
    ({"status": 404, "html": "<html><body><h1>Not found</h1></body></html>"}, "HTTP 404"),
    ({"status": 403, "html": "<html><body>Just a moment... cf-chl</body></html>"}, "HTTP 403"),
    ({"status": 200, "html": "<html><body><div id=root></div><noscript>enable JavaScript</noscript></body></html>"},
     "thin"),
    ({"status": 200, "html": "<html><body><p>Just a moment... Checking your browser</p></body></html>"}, "thin"),
])
def test_rendered_but_still_bad_is_honest_422_without_firecrawl(ctx, cdp, sc, why):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(**sc)
    srv, aurl = app(ctx, cdp)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 422 and why in r.json()["error"], r.json()
        assert not up.scrapes
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("page,bot", [("shell", True), ("challenge", False)])
def test_browser_error_status_after_a_plain_page_reads_as_a_bot_block(ctx, cdp, page, bot):
    """Plain got the page (a JS shell) but the browser got 404: the browser was refused, the page is not gone. A
    page plain could not get either keeps the plain HTTP-status message."""
    up, furl, _ = ctx
    url = furl + f"/p/{page}"
    cdp.pages[url] = cdp.scenario(status=404, html="<html><body><h1>Not found</h1></body></html>")
    srv, aurl = app(ctx, cdp)
    try:
        err = scrape(aurl, url).json()["error"]
        assert "HTTP 404" in err and ("bot defence" in err) is bot, err
    finally:
        srv.should_exit = True


def test_private_redirect_in_browser_is_422(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html=RENDERED, redirects=["http://10.1.2.3/"])
    srv, aurl = app(ctx, cdp)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 422 and "private" in r.json()["error"] and not up.scrapes
    finally:
        srv.should_exit = True


def test_busy_goes_to_firecrawl(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    for q in ("a", "b"):  # two URLs: one would be single-flighted
        cdp.pages[f"{url}?{q}"] = cdp.scenario(html=RENDERED, nav_delay=1.0)
    srv, aurl = app(ctx, cdp, browser_tabs=1, browser_wait=0.2)
    try:
        res = [None, None]

        def go(i):
            res[i] = scrape(aurl, f"{url}?{'ab'[i]}", timeout=30000)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        engines = sorted(x.json()["data"]["metadata"]["fastweb"]["engine"] for x in res)
        assert engines == ["browser", "firecrawl"], engines
        fc = [x for x in res if x.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"][0]
        assert "browser busy" in fc.json()["data"]["metadata"]["fastweb"]["fallback_reason"]
        assert httpx.get(aurl + "/v1/fastweb/stats").json()["browser"]["busy"] == 1
    finally:
        srv.should_exit = True


def _two_at_once(aurl, urls, timeouts):
    res = [None] * len(urls)

    def go(i):
        res[i] = scrape(aurl, urls[i], timeout=timeouts[i])
    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(urls))]
    for i, t in enumerate(ts):
        t.start()
        if i == 0:
            time.sleep(0.3)  # the first request holds the only tab
    for t in ts:
        t.join()
    return res


@pytest.mark.slow
def test_busy_without_firecrawl_waits_for_a_tab(ctx, cdp):
    """No Firecrawl to hand over to: a page queues for a tab instead of failing at once."""
    up, furl, _ = ctx
    url = furl + "/p/shell"
    for q in ("a", "b"):
        cdp.pages[f"{url}?{q}"] = cdp.scenario(html=RENDERED, nav_delay=0.8)
    srv, aurl = app(ctx, cdp, upstream="", browser_tabs=1, browser_wait=0.2)
    try:
        res = _two_at_once(aurl, [f"{url}?a", f"{url}?b"], [30000, 30000])
        assert [r.status_code for r in res] == [200, 200], [r.text for r in res]
        assert all(r.json()["data"]["metadata"]["fastweb"]["engine"] == "browser" for r in res)
        assert not up.scrapes
    finally:
        srv.should_exit = True


@pytest.mark.slow
def test_busy_without_firecrawl_waits_for_a_tab_within_the_callers_budget(ctx, cdp):
    """With no Firecrawl, a page queues for a tab for most of its own budget, well past browser_wait: a 6 s render
    ahead of it in the only tab does not make it busy."""
    up, furl, _ = ctx
    url = furl + "/p/shell"
    for q in ("a", "b"):
        cdp.pages[f"{url}?{q}"] = cdp.scenario(html=RENDERED, nav_delay=6.0 if q == "a" else 0.0)
    srv, aurl = app(ctx, cdp, upstream="", browser_tabs=1, browser_wait=0.2, browser_timeout=8.0)
    try:
        _, second = _two_at_once(aurl, [f"{url}?a", f"{url}?b"], [30000, 15000])
        assert second.status_code == 200, second.text
        assert second.json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
    finally:
        srv.should_exit = True


@pytest.mark.slow
def test_busy_without_firecrawl_is_a_retryable_502(ctx, cdp):
    """Still no tab within the caller's budget: 502 with Retry-After (capacity; firecrawl-py retries 502), never a
    final 422."""
    up, furl, _ = ctx
    url = furl + "/p/shell"
    for q in ("a", "b"):
        cdp.pages[f"{url}?{q}"] = cdp.scenario(html=RENDERED, nav_delay=3.0)
    srv, aurl = app(ctx, cdp, upstream="", browser_tabs=1, browser_wait=0.2, browser_timeout=3.0)
    try:
        first, second = _two_at_once(aurl, [f"{url}?a", f"{url}?b"], [30000, 2500])
        assert second.status_code == 502 and "browser busy" in second.json()["error"], second.text
        assert int(second.headers["Retry-After"]) > 0
        assert first.status_code in (200, 422)  # the slow page itself is not under test
    finally:
        srv.should_exit = True


def test_crash_and_unreachable_go_to_firecrawl(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html=RENDERED, crash=True)
    srv, aurl = app(ctx, cdp)
    srv2, aurl2 = app(ctx, None, browser_url="http://127.0.0.1:9")
    try:
        for a in (aurl, aurl2):
            r = scrape(a, url)
            fw = r.json()["data"]["metadata"]["fastweb"]
            assert r.status_code == 200 and fw["engine"] == "firecrawl" and "browser" in fw["fallback_reason"]
        assert httpx.get(aurl + "/v1/fastweb/stats").json()["browser"]["errors"] == 1
    finally:
        srv.should_exit = srv2.should_exit = True


def test_thin_fallback_to_firecrawl_sends_waitfor(ctx):
    """a thin page handed to Firecrawl (browser off/busy/down) gets waitFor 3000 unless the caller set
    one; a blocked page does not."""
    up, furl, _ = ctx
    srv, aurl = app(ctx, None, browser=False)
    try:
        scrape(aurl, furl + "/p/shell")
        scrape(aurl, furl + "/p/shell?w", waitFor=500)
        scrape(aurl, furl + "/p/challenge")
        assert [x.get("waitFor") for x in up.scrapes] == [3000, 500, None]
        assert all(x["timeout"] <= 12_000 for x in up.scrapes)
    finally:
        srv.should_exit = True


SPA_SEO = ("<html><head><title>Garden Monitor</title><script type=module src='/assets/index-abc.js'></script></head>"
           "<body><div id=root><h1>Garden Monitor</h1>" + "<p>Old static summary of the garden season.</p>" * 12
           + "</div></body></html>")
SPA_FULL = ("<html><head><title>Garden Monitor</title></head><body><div id=root><main><h1>Garden Monitor</h1>"
            + "".join(f"<p>Live item {i}: plantings, visitors and opening hours updated today.</p>"
                      for i in range(80)) + "</main></div></body></html>")


def spa_app(ctx, cdp, **kw):
    from .conftest import EXTRA
    EXTRA["spa_seo"] = (SPA_SEO.encode(), "text/html", 200)
    return app(ctx, cdp, browser_suspect=True, **kw)


def test_suspect_spa_rendered_when_much_bigger(ctx, cdp):
    """a JS app whose plain HTML is a small stale SEO summary is served as 'ok' today; with the suspect
    signal on, the browser render replaces it when its main-only text is >= 2x and +1000 chars."""
    up, furl, _ = ctx
    url = furl + "/p/spa_seo"
    cdp.pages[url] = cdp.scenario(html=SPA_FULL)
    srv, aurl = spa_app(ctx, cdp)
    try:
        d = scrape(aurl, url).json()["data"]
        assert d["metadata"]["fastweb"]["engine"] == "browser" and "Live item 79" in d["markdown"]
        assert not up.scrapes
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("render", ["same", "crash", "notfound"])
def test_suspect_spa_keeps_plain_when_render_not_better(ctx, cdp, render):
    up, furl, _ = ctx
    url = furl + "/p/spa_seo"
    cdp.pages[url] = {"same": cdp.scenario(html=SPA_SEO), "crash": cdp.scenario(html=SPA_FULL, crash=True),
                      "notfound": cdp.scenario(html=SPA_FULL, status=404)}[render]
    srv, aurl = spa_app(ctx, cdp)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 200 and r.json()["data"]["metadata"]["fastweb"]["engine"] == "plain"
        assert "Old static summary" in r.json()["data"]["markdown"] and not up.scrapes
    finally:
        srv.should_exit = True


def test_suspect_spa_off_by_flag_and_never_for_articles(ctx, cdp):
    up, furl, _ = ctx
    from .conftest import EXTRA
    EXTRA["spa_seo"] = (SPA_SEO.encode(), "text/html", 200)
    srv, aurl = app(ctx, cdp, browser_suspect=False)
    srv2, aurl2 = spa_app(ctx, cdp)
    try:
        assert scrape(aurl, furl + "/p/spa_seo").json()["data"]["metadata"]["fastweb"]["engine"] == "plain"
        assert scrape(aurl2, furl + "/p/article").json()["data"]["metadata"]["fastweb"]["engine"] == "plain"
        assert cdp.navigations == []
    finally:
        srv.should_exit = srv2.should_exit = True


def test_suspect_spa_never_in_prefetch(cdp):
    f = tp.Fake()
    fsrv, f.url = serve(f.app())
    try:
        f.results = [f.url + "/p/art1"]
        aurl, _, _ = tp.make(f, browser_url=cdp.endpoint, browser_suspect=True)
        assert tp.search(aurl).status_code == 200
        tp.settle(aurl, 1)
        assert cdp.navigations == []
    finally:
        fsrv.should_exit = True


def test_suspect_rule():
    from fastweb_firecrawl.heuristics import suspect_spa
    assert suspect_spa(SPA_SEO, "Old static summary of the garden season. " * 12)
    from .conftest import ARTICLE
    assert not suspect_spa(ARTICLE, "the council voted " * 200)  # long text: not suspect
    assert not suspect_spa("<html><body><p>short page</p></body></html>", "short page")  # no app evidence
    assert suspect_spa("<html><body><div id=__next></div><script id=__NEXT_DATA__>{}</script>x</body></html>", "x")
    # a Next.js app router site, no root div, the plain text is a <noscript> summary
    assert suspect_spa('<html><head><script src="/_next/static/chunks/1627bf2f.js" async=""></script></head><body>'
                       '<noscript><article><h1>City Library Live Events Tracker</h1></article></noscript>'
                       '<script>self.__next_f.push([1,""])</script></body></html>', "City Library Live " * 90)


def test_disabled_is_todays_behaviour(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    srv, aurl = app(ctx, cdp, browser=False)
    try:
        assert scrape(aurl, url).json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"
        assert cdp.navigations == []
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("page", ["article", "missing", "pdf", "png", "huge"])
def test_pages_the_browser_cannot_fix_never_reach_it(ctx, cdp, page):
    up, furl, _ = ctx
    srv, aurl = app(ctx, cdp, max_bytes=1024 * 1024)
    try:
        scrape(aurl, furl + f"/p/{page}")
        assert cdp.navigations == []
    finally:
        srv.should_exit = True


def test_refused_port_never_reaches_browser(ctx, cdp):
    srv, aurl = app(ctx, cdp)
    try:
        assert scrape(aurl, "http://127.0.0.1:9/x").status_code == 422
        assert cdp.navigations == []
    finally:
        srv.should_exit = True


def test_prefetch_never_uses_the_browser(cdp):
    f = tp.Fake()
    fsrv, f.url = serve(f.app())
    try:
        f.results = tp.urls(f, "shell", "challenge", "art1")
        aurl, _, _ = tp.make(f, browser_url=cdp.endpoint)
        assert tp.search(aurl).status_code == 200
        tp.settle(aurl, 3)
        assert cdp.navigations == [] and f.fc == []
    finally:
        fsrv.should_exit = True


# ---------------------------------------------------------------- real headless-shell (run by hand)
@pytest.mark.browser
@pytest.mark.skipif(not os.environ.get("FASTWEB_TEST_CDP"), reason="set FASTWEB_TEST_CDP=http://127.0.0.1:PORT")
def test_real_headless_shell():
    """A real chromedp/headless-shell renders a JS page served from this host (host.docker.internal); its image,
    font and private-IP script/iframe/XHR are failed, a redirect to a private IP fails the navigation."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, RedirectResponse, Response
    from starlette.routing import Route

    from fastweb_firecrawl.browser import Browser, PrivateTarget
    hits = []

    async def page(request):
        name = request.path_params["name"]
        hits.append(name)
        if name == "app":
            return HTMLResponse("<html><head><title>JS</title></head><body><div id=root></div>"
                                "<img src='/p/img.png'><iframe src='http://10.9.9.9/frame'></iframe>"
                                "<script src='http://192.168.77.1/x.js'></script>"
                                "<script>setTimeout(() => { document.getElementById('root').innerHTML = "
                                "'<h1>Rendered by script</h1>' + '<p>harbour tide text</p>'.repeat(30); "
                                "fetch('http://169.254.169.254/latest').catch(() => {}); }, 300);</script>"
                                "</body></html>")
        if name == "hop":
            return RedirectResponse("http://10.9.9.9/admin", status_code=302)
        return Response(b"x", media_type="image/png")
    srv, local = serve(Starlette(routes=[Route("/p/{name}", page)]))
    port = httpx.URL(local).port
    seen = []

    async def guard(url):
        seen.append(url)
        h = httpx.URL(url).host
        return h == "host.docker.internal"

    async def go():
        b = Browser(os.environ["FASTWEB_TEST_CDP"], guard=guard, settle=2.0, timeout=4.0)
        try:
            r = await b.render(f"http://host.docker.internal:{port}/p/app")
            with pytest.raises(PrivateTarget):
                await b.render(f"http://host.docker.internal:{port}/p/hop")
            return r, b.stats()
        finally:
            await b.close()
    try:
        r, st = run(go())
    finally:
        srv.should_exit = True
    assert r.status == 200 and "Rendered by script" in r.html and r.html.count("harbour tide text") == 31  # 30 rendered + the script source
    assert "img.png" not in hits  # Image failed before it left Chrome
    # the /admin redirect hop raised PrivateTarget above; its host's verdict came from the guard cache
    assert {"http://10.9.9.9/frame", "http://192.168.77.1/x.js", "http://169.254.169.254/latest"} <= set(seen)
    assert st["blocked_private"] >= 4 and st["blocked_type"] >= 1 and st["private"] == 1
    print("real-chrome stats", st)


def test_thin_render_with_xhr_in_flight_gets_one_more_settle(cdp):
    """my.gov.au-like: a services list that arrives by a late XHR after the settle cap. The render is still thin with
    the XHR in flight at the cap, so it settles once more (within the page budget) and gets the list."""
    url = "http://127.0.0.1:1/late"
    cdp.pages[url] = cdp.scenario(timeline=[(0, "<p>Menu</p>"), (1300, RENDERED)], xhr=[(0, 1300)])

    async def go():
        b = browser(cdp, settle=1.0, timeout=20.0)
        try:
            return await b.render(url), b.stats()
        finally:
            await b.close()
    r, st = run(go())
    assert r.html == RENDERED and st["settle_extended"] == 1 and st["settle_stable"] == 1


def test_settle_end_reasons_are_counted(cdp):
    url = "http://127.0.0.1:1/ok"
    cdp.pages[url] = cdp.scenario(html=RENDERED)
    ticker = "http://127.0.0.1:1/tick"
    cdp.pages[ticker] = cdp.scenario(timeline=[(i * 60, f"<p>{'x' * (i + 2000)}</p>") for i in range(200)])

    async def go():
        b = browser(cdp, settle=0.5, timeout=20.0)
        try:
            await b.render(url)
            await b.render(ticker)  # never stable, but not thin: the cap ends it, no extension
            return b.stats()
        finally:
            await b.close()
    st = run(go())
    assert st["settle_stable"] == 1 and st["settle_cap"] == 1 and st["settle_extended"] == 0
