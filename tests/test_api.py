import httpx
import pytest


def scrape(aurl, url, path="/v2/scrape", **kw):
    return httpx.post(aurl + path, json={"url": url, **kw}, timeout=30)


def test_health(ctx):
    _, _, aurl = ctx
    assert httpx.get(aurl + "/").status_code == 200
    assert httpx.get(aurl + "/health").json()["ok"] is True


def test_article_plain(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/article", formats=["markdown", "html"])
    assert r.status_code == 200
    d = r.json()["data"]
    assert "harbour plan" in d["markdown"] and "menu" not in d["markdown"]
    assert "<article>" in d["html"]
    m = d["metadata"]
    assert m["title"] == "Big Story" and m["statusCode"] == 200
    assert m["sourceURL"] == furl + "/p/article" and m["fastweb"]["engine"] == "plain"
    assert up.scrapes == []


def test_v1_short_page_no_fallback(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/short", path="/v1/scrape")
    assert r.json()["success"] and "illustrative" in r.json()["data"]["markdown"]
    assert up.scrapes == []


def test_redirect_final_url(ctx):
    up, furl, aurl = ctx
    m = scrape(aurl, furl + "/p/redirect").json()["data"]["metadata"]
    assert m["url"] == furl + "/p/article" and m["sourceURL"] == furl + "/p/redirect"


def test_shell_falls_back(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/shell", timeout=20000)
    d = r.json()["data"]
    assert r.status_code == 200 and "Rendered by Firecrawl" in d["markdown"]
    assert d["metadata"]["fastweb"]["engine"] == "firecrawl"
    assert len(up.scrapes) == 1 and up.scrapes[0]["url"] == furl + "/p/shell"
    assert up.scrapes[0]["timeout"] <= 20000


def test_404_honest(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/missing")
    assert r.status_code == 422 and r.json() == {"success": False, "error": "Target page returned HTTP 404"}
    assert up.scrapes == []


def test_challenge_fallback_then_fail(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/challenge")
    assert len(up.scrapes) == 1
    assert r.status_code == 200 and r.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"
    up.fail = True
    r = scrape(aurl, furl + "/p/challenge")
    assert r.status_code == 422 and r.json()["success"] is False and "403" in r.json()["error"]


def test_pdf(ctx):
    _, furl, aurl = ctx
    d = scrape(aurl, furl + "/p/pdf").json()["data"]
    assert "Quantum harbour" in d["markdown"] and d["metadata"]["contentType"].startswith("application/pdf")


def test_huge_rejected_not_502(ctx):
    up, furl, aurl = ctx
    up.fail = True  # over the cap goes to Firecrawl; when that fails too the cap error is the answer
    r = scrape(aurl, furl + "/p/huge")
    assert r.status_code == 413 and r.json()["success"] is False


@pytest.mark.slow
def test_timeout(ctx):
    up, furl, aurl = ctx
    up.fail = True
    r = scrape(aurl, furl + "/p/slow")
    assert r.status_code == 504 and "timed out" in r.json()["error"]


def test_bad_request(ctx):
    _, _, aurl = ctx
    r = httpx.post(aurl + "/v2/scrape", json={})
    assert r.status_code == 400 and r.json()["success"] is False


def test_search_v2_v1(ctx):
    up, _, aurl = ctx
    r = httpx.post(aurl + "/v2/search", json={"query": "harbour", "limit": 3}).json()
    assert r["success"] and len(r["data"]["web"]) == 3
    assert r["data"]["web"][0] == {"url": "https://r0.example/", "title": "Result 0", "description": "snippet 0", "position": 1}
    assert up.searches[0]["q"] == "harbour" and up.searches[0]["format"] == "json"
    assert up.searches[0]["engines"] == "bing,yahoo"
    r = httpx.post(aurl + "/v1/search", json={"query": "harbour"}).json()
    assert r["success"] and r["data"][1] == {"url": "https://r1.example/", "title": "Result 1", "description": "snippet 1"}


def test_search_header_reports_source_and_attempts(ctx):
    up, _, aurl = ctx
    r = httpx.post(aurl + "/v2/search", json={"query": "harbour header", "limit": 3})
    assert r.headers["X-Fastweb-Search"] == "source=primary;attempts=1"
    assert set(r.json()) == {"success", "data"} and set(r.json()["data"]) == {"web"}
    up.mode = "search_empty"
    r = httpx.post(aurl + "/v2/search", json={"query": "harbour header empty", "limit": 3})
    assert r.status_code == 503 and r.headers["X-Fastweb-Search"] == "source=none;attempts=3"


def test_no_fallback_configured_is_honest_422(env):
    """FIRECRAWL_UPSTREAM unset: a JS shell the cheap tiers can't serve is a 422 naming the reason, never a 5xx."""
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings

    from .conftest import serve
    up, furl, _ = env
    srv, aurl = serve(create_app(Settings(upstream="", searxng_url=furl, total_timeout=2.0, redis_url="",
                                          browser=False, allow_hosts=frozenset({"127.0.0.1"}))))
    try:
        n = len(up.scrapes)
        r = scrape(aurl, furl + "/p/shell", timeout=20000)
        assert r.status_code == 422 and r.json()["success"] is False
        assert "no fallback configured" in r.json()["error"]
        assert len(up.scrapes) == n
        assert scrape(aurl, furl + "/p/article").json()["success"]
    finally:
        srv.should_exit = True
