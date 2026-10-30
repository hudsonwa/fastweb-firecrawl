import time

import httpx
import pytest

from .conftest import _free_port, serve
from .test_api import scrape


# SSRF
def test_direct_private_rejected(ctx):
    up, furl, _ = ctx
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    srv, aurl = serve(create_app(Settings(upstream=furl, searxng_url=furl, redis_url="")))
    try:
        for u in (furl + "/p/article", "http://localhost:1/", "http://[::1]/", "http://169.254.169.254/",
                  "http://10.0.0.1/", "http://100.64.1.1/", "http://searxng:8080/", "http://api:3002/",
                  "http://host.docker.internal:3002/", "http://0.0.0.0/"):
            r = scrape(aurl, u)
            assert r.status_code == 422, u
            assert r.json() == {"success": False, "error": "URL targets a private or internal network address"}
        assert up.scrapes == []
    finally:
        srv.should_exit = True


def test_redirect_to_private_rejected(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/to_localhost")
    assert r.status_code == 422 and "private" in r.json()["error"]
    assert up.scrapes == []


# empty successes
def test_text_plain(ctx):
    up, furl, aurl = ctx
    d = scrape(aurl, furl + "/p/text").json()["data"]
    assert "This is plain text." in d["markdown"] and up.scrapes == []


def test_json_fenced(ctx):
    _, furl, aurl = ctx
    md = scrape(aurl, furl + "/p/json").json()["data"]["markdown"]
    assert md.startswith("```json") and '"full_name": "python/cpython"' in md


def test_png_unsupported(ctx):
    _, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/png")
    assert r.status_code == 422 and r.json()["error"].startswith("Unsupported content type")


def test_spa_without_markers_falls_back(ctx):
    up, furl, aurl = ctx
    d = scrape(aurl, furl + "/p/spa").json()["data"]
    assert "Rendered by Firecrawl" in d["markdown"] and len(up.scrapes) == 1


def test_spa_fallback_failed_never_empty_success(ctx):
    up, furl, aurl = ctx
    up.fail = True
    r = scrape(aurl, furl + "/p/spa")
    assert r.status_code == 422 and r.json()["success"] is False


# edge cases found by mutation testing
def test_consent_thin_fallback_failed_422(ctx):
    up, furl, aurl = ctx
    up.fail = True
    r = scrape(aurl, furl + "/p/consent")
    assert r.status_code == 422 and len(up.scrapes) == 1


def test_upstream_error_status_422(ctx):
    up, furl, aurl = ctx
    up.mode = "error404"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 422 and "404" in r.json()["error"]


def test_upstream_challenge_422(ctx):
    up, furl, aurl = ctx
    up.mode = "challenge"
    r = scrape(aurl, furl + "/p/challenge")
    assert r.status_code == 422 and r.json()["success"] is False


def test_upstream_502_never_502(ctx):
    up, furl, aurl = ctx
    up.mode = "502"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 422


# one deadline
@pytest.mark.slow
def test_caller_budget_honoured(ctx):
    up, furl, aurl = ctx
    up.mode = "slow"
    t = time.monotonic()
    r = scrape(aurl, furl + "/p/shell", timeout=3000)
    assert time.monotonic() - t < 3.0 + 4.0  # the caller's 3 s, not the slow upstream's own time
    assert r.status_code == 504 and r.json()["success"] is False
    assert up.scrapes[0]["timeout"] < 3000


# plain errors fall back
@pytest.mark.slow
def test_plain_timeout_falls_back(ctx):
    up, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/slow", timeout=20000)
    assert r.status_code == 200 and r.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl"


def test_connect_error_fails_fast(ctx):
    """A refused port is a dead host, Firecrawl would only burn the caller's budget."""
    up, _, aurl = ctx
    r = scrape(aurl, f"http://127.0.0.1:{_free_port()}/x", timeout=20000)
    assert r.status_code == 422 and up.scrapes == []


# a corrupt PDF is an honest 422
def test_corrupt_pdf_422(ctx):
    _, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/badpdf")
    assert r.status_code == 422 and r.json()["success"] is False


def test_invalid_inputs_400(ctx):
    _, furl, aurl = ctx
    for kw in ({"timeout": "abc"}, {"timeout": -5}, {"formats": [1]}, {"formats": "markdown"}):
        r = scrape(aurl, furl + "/p/article", **kw)
        assert r.status_code == 400, kw
    r = httpx.post(aurl + "/v2/search", json={"query": "x", "limit": "many"})
    assert r.status_code == 400
