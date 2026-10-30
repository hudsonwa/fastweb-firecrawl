"""Completeness, charset, bounded fallback, host memory, scrape cache, impersonation tier."""
import time

import fakeredis
import httpx
import pytest

from .conftest import ARTICLE, EXTRA, serve
from .test_api import scrape

NAV = "".join(f"<li><a href='/s/{i}'>Section {i} headline about the harbour and markets today</a></li>" for i in range(80))
HUB = ("<html><head><title>Daily News</title></head><body><header><a href='/'>Daily News Network</a></header>"
       "<main><ul>" + NAV + "</ul><article><h2>Lead</h2><p>One short teaser paragraph about the vote.</p></article>"
       "</main><footer>© Daily News Network 2026</footer><script>var x='" + "s" * 5000 + "'</script></body></html>")
TABLE = ("<html><head><title>Denver forecast</title></head><body><nav>Home | Radar</nav><main><h1>Denver Forecast</h1>"
         "<table><tr><th>Day</th><th>Max</th><th>Summary</th></tr>"
         + "".join(f"<tr><td>Day {i}</td><td>{20 + i}</td><td>Sunny with shower chance {i}</td></tr>" for i in range(7))
         + "</table></main></body></html>")
CP1252 = ("<html><head><meta charset='windows-1252'><title>Café</title></head><body><article>"
          + "<p>Le café crème coûte €3 — naïve déjà vu.</p>" * 10
          + "</article></body></html>").encode("cp1252")
SJIS = ("<html><head><meta http-equiv='Content-Type' content='text/html; charset=Shift_JIS'><title>日本語</title>"
        "</head><body><article>" + "<p>吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。</p>" * 10
        + "</article></body></html>").encode("shift_jis")
BOM_SHELL = ("<html><head><title>BOM</title></head><body><noscript>Javascript is disabled in your browser.</noscript>"
             "<div class='mega'>" + "".join(f"<p>Weather and climate menu item {i} for the region</p>" for i in range(30))
             + "</div><div id='app'></div></body></html>")


# ---------- extract.py units ----------
def test_decode_meta_cp1252():
    from fastweb_firecrawl.extract import decode_html
    assert "café crème coûte €3 — naïve déjà vu" in decode_html(CP1252, "text/html")


def test_decode_meta_shift_jis():
    from fastweb_firecrawl.extract import decode_html
    assert "吾輩は猫である" in decode_html(SJIS, "text/html")


def test_decode_header_wins_and_bom():
    from fastweb_firecrawl.extract import decode_html
    assert decode_html("é".encode("latin-1"), "text/html; charset=iso-8859-1") == "é"
    assert decode_html(b"\xef\xbb\xbf<p>\xc3\xa9</p>", "text/html") == "<p>é</p>"


def test_decode_sniff_without_declaration():
    from fastweb_firecrawl.extract import decode_html
    raw = ("<html><body>" + "<p>Grüße aus München, schöne Straße, Fußgänger überall.</p>" * 20).encode("cp1252")
    assert "Grüße aus München" in decode_html(raw, "text/html")


def test_link_hub_returns_whole_page():
    from fastweb_firecrawl.extract import extract_html
    md, title, _ = extract_html(HUB, "https://news.example/", True)
    assert len(md) >= 4000 and "Section 79 headline" in md and "Daily News Network" in md
    assert "sssss" not in md


def test_table_survives():
    from fastweb_firecrawl.extract import extract_html
    md, _, _ = extract_html(TABLE, "https://bom.example/denver", True)
    assert "Sunny with shower chance 6" in md and "| 26 |" in md.replace("  ", " ")


def test_article_main_content_only():
    from fastweb_firecrawl.extract import extract_html
    md, title, _ = extract_html(ARTICLE, "https://news.example/a", True)
    assert "Paragraph 19" in md and "footer" not in md and "menu" not in md


def test_github_whole_page():
    from fastweb_firecrawl.extract import extract_html
    html = ("<html><body><div class='files'><a>README.md</a></div><article class='markdown-body'>"
            + "<p>Firecrawl turns websites into LLM-ready markdown and structured data.</p>" * 30
            + "</article></body></html>")
    md, _, _ = extract_html(html, "https://github.com/firecrawl/firecrawl", True)
    assert "README" in md and "LLM-ready" in md


def test_js_shell_padded_with_menu_is_thin():
    from fastweb_firecrawl.extract import extract_html
    from fastweb_firecrawl.heuristics import is_thin
    md, _, _ = extract_html(BOM_SHELL, "https://www.bom.gov.au/", True)
    assert is_thin(BOM_SHELL, md)


# ---------- service ----------
def make(furl, **kw):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    r = kw.pop("redis", None) or fakeredis.FakeAsyncRedis()
    imp = kw.pop("impersonator", None)
    st = Settings(upstream=furl, searxng_url=furl, max_bytes=2 * 1024 * 1024, total_timeout=2.0,
                  redis_url="", allow_hosts=frozenset({"127.0.0.1"}), dead_reprobe_rate=0.0, **kw)
    return serve(create_app(st, redis=r, impersonator=imp)), r


def test_meta_charset_over_http(ctx):
    up, furl, aurl = ctx
    EXTRA["cp"] = (CP1252, "text/html", 200)
    d = scrape(aurl, furl + "/p/cp").json()["data"]
    assert "café crème" in d["markdown"]


def test_fallback_capped(ctx):
    up, furl, _ = ctx
    up.mode = "slow"
    (srv, aurl), _ = make(furl, fallback_timeout=1.0)
    try:
        t = time.monotonic()
        r = scrape(aurl, furl + "/p/challenge", timeout=60000)
        assert time.monotonic() - t < 20 and r.status_code == 504  # capped, not the 60 s caller budget
    finally:
        srv.should_exit = True


def test_dead_host_fails_fast(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    import asyncio
    asyncio.new_event_loop().run_until_complete(r.set("fastweb:host:127.0.0.1", "dead", ex=3600))
    try:
        res = scrape(aurl, furl + "/p/challenge")
        assert res.status_code == 422  # failed without the Firecrawl call (up.scrapes below)
        assert up.scrapes == [] and "blocked" in res.json()["error"]
    finally:
        srv.should_exit = True


def test_firecrawl_host_skips_plain(ctx):
    up, furl, _ = ctx
    (srv, aurl), _ = make(furl, reprobe_rate=0.0, impersonate=False)
    try:
        assert scrape(aurl, furl + "/p/challenge").json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"
        up.hits.clear()
        d = scrape(aurl, furl + "/p/challenge", onlyMainContent=False).json()["data"]  # not a cache hit
        assert d["metadata"]["fastweb"]["engine"] == "firecrawl" and up.hits == []
        assert d["metadata"]["fastweb"]["fallback_reason"] == "host known to need Firecrawl"
    finally:
        srv.should_exit = True


def test_redis_down_behaves_as_before(ctx):
    up, furl, _ = ctx
    import redis.asyncio as ra
    dead = ra.Redis.from_url("redis://127.0.0.1:1/2", socket_connect_timeout=0.2, socket_timeout=0.2)
    (srv, aurl), _ = make(furl, redis=dead)
    try:
        r = scrape(aurl, furl + "/p/article")
        assert r.status_code == 200 and r.headers["X-Fastweb-Cache"] == "MISS"
        assert scrape(aurl, furl + "/p/challenge").json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"
    finally:
        srv.should_exit = True


def test_scrape_cache(ctx):
    up, furl, _ = ctx
    (srv, aurl), r = make(furl)
    try:
        a = scrape(aurl, furl + "/p/article")
        assert a.headers["X-Fastweb-Cache"] == "MISS"
        up.hits.clear()
        b = scrape(aurl, furl + "/p/article")
        assert b.headers["X-Fastweb-Cache"] == "HIT" and up.hits == []  # served from cache: no fetch at all
        assert b.json() == a.json()
        c = scrape(aurl, furl + "/p/article", onlyMainContent=False)
        assert c.headers["X-Fastweb-Cache"] == "MISS"
        assert scrape(aurl, furl + "/p/missing").status_code == 422
        assert scrape(aurl, furl + "/p/missing").headers.get("X-Fastweb-Cache") != "HIT"
    finally:
        srv.should_exit = True


def test_impersonation_tier_rescues_403(ctx):
    up, furl, _ = ctx
    calls = []

    async def imp(url, deadline):
        calls.append(url)
        return 200, url, {"content-type": "text/html"}, ARTICLE.encode()

    (srv, aurl), _ = make(furl, impersonator=imp)
    try:
        d = scrape(aurl, furl + "/p/challenge").json()["data"]
        assert calls and d["metadata"]["fastweb"]["engine"] == "impersonate" and "Paragraph 19" in d["markdown"]
        assert up.scrapes == []
    finally:
        srv.should_exit = True


def test_heading_kept_when_trafilatura_drops_it():
    from fastweb_firecrawl.extract import extract_html
    html = ("<html><head><title>Quick Start – React</title></head><body><div class='hdr'><h1>Quick Start</h1></div>"
            "<article>" + "<p>Welcome to the React documentation and its components tour.</p>" * 20 + "</article></body></html>")
    md, _, _ = extract_html(html, "https://react.dev/learn", True)
    assert md.startswith("# Quick Start")
    assert md.count("Quick Start") == 1 or "Quick Start" in md.split("\n")[0]
    nohead = "<html><head><title>Reuters | News</title></head><body><article>" + "<p>Story text here.</p>" * 30 + "</article></body></html>"
    assert "Reuters" in extract_html(nohead, "https://www.reuters.com/", True)[0]
