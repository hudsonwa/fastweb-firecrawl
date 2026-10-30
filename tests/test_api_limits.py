"""Request limits, Firecrawl answer shape, shared single-flight failures, and the compose containment of Firecrawl."""
import asyncio
import concurrent.futures
from pathlib import Path

import httpx
import yaml
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .conftest import serve

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def make_app(furl="", **kw):
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    st = Settings(**{"upstream": "", "searxng_url": furl, "total_timeout": 3.0, "redis_url": "", "browser": False,
                     "prefetch_n": 0, "search_relevance": False, "allow_hosts": frozenset({"127.0.0.1"}), **kw})
    return serve(create_app(st))


# ---------------------------------------------------------------- request body and option limits
def test_oversized_bodies_are_413_before_parsing(ctx):
    _, furl, aurl = ctx
    big = {"url": furl + "/p/article", "pad": "x" * 70_000}
    for path in ("/v2/scrape", "/v1/scrape", "/v2/search"):
        r = httpx.post(aurl + path, json={**big, "query": "q"}, timeout=10)
        assert r.status_code == 413 and r.json()["success"] is False, path


def test_oversized_chunked_body_without_content_length_is_413(ctx):
    _, furl, aurl = ctx

    def chunks():
        yield b'{"url": "' + furl.encode() + b'/p/article", "pad": "'
        for _ in range(100):
            yield b"x" * 1024
        yield b'"}'
    r = httpx.post(aurl + "/v2/scrape", content=chunks(), headers={"content-type": "application/json"}, timeout=10)
    assert r.status_code == 413


def test_small_bodies_still_work(ctx):
    _, furl, aurl = ctx
    r = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/article", "pad": "x" * 1000}, timeout=10)
    assert r.status_code == 200


def test_timeout_is_clamped():
    from fastweb_firecrawl.pipeline import parse_scrape_body
    assert parse_scrape_body({"url": "https://example.com/", "timeout": 10 ** 9}, 120.0)[2] == 120.0
    assert parse_scrape_body({"url": "https://example.com/", "timeout": 5000}, 120.0)[2] == 5.0


def test_search_limit_is_clamped(env):
    _, furl, _ = env
    srv, aurl = make_app(furl, max_search_limit=2)
    try:
        r = httpx.post(aurl + "/v2/search", json={"query": "harbour clamp", "limit": 1000}, timeout=10)
        assert r.status_code == 200 and 0 < len(r.json()["data"]["web"]) <= 2
    finally:
        srv.should_exit = True


def test_only_allow_listed_headers_reach_firecrawl():
    from fastweb_firecrawl.upstream import passed_headers
    got = passed_headers({"Host": "redis:6379", "User-Agent": "ua", "Cookie": "a=b", "Connection": "upgrade",
                          "X-Forwarded-For": "10.0.0.1", "Proxy-Authorization": "x", "Accept-Language": 5})
    assert got == {"User-Agent": "ua", "Cookie": "a=b"}
    assert passed_headers("not a dict") is None and passed_headers({"Host": "x"}) is None


def test_firecrawl_request_body_drops_unknown_options_and_bad_headers():
    from fastweb_firecrawl.settings import Settings
    from fastweb_firecrawl.upstream import FirecrawlFallback
    fc = FirecrawlFallback(Settings(upstream="http://fc"), guard=None)
    body = fc.request_body({"url": "https://example.com/", "formats": ["markdown"], "actions": [{"type": "click"}],
                            "headers": {"Host": "internal", "Referer": "https://example.com/"}}, 5.0, "blocked")
    assert body == {"url": "https://example.com/", "formats": ["markdown"],
                    "headers": {"Referer": "https://example.com/"}, "timeout": 5000}


# ---------------------------------------------------------------- answer shape
def test_markdown_only_when_asked_for(ctx):
    _, furl, aurl = ctx
    d = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/article", "formats": ["rawHtml"]}, timeout=10).json()
    assert "markdown" not in d["data"] and "<article>" in d["data"]["rawHtml"]
    d = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/article", "formats": ["markdown", "html"]},
                   timeout=10).json()
    assert "harbour plan" in d["data"]["markdown"] and "html" in d["data"]
    d = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/article", "formats": ["links"]}, timeout=10).json()
    assert "harbour plan" in d["data"]["markdown"]  # no content format we produce was asked for: markdown


def test_truncation_is_flagged():
    from fastweb_firecrawl.pipeline import document
    d = document(["markdown"], "x" * 10, "", {"title": "t"}, {"engine": "plain"}, max_chars=10)
    assert d["data"]["metadata"]["fastweb"] == {"engine": "plain", "truncated": True}
    d = document(["markdown"], "x" * 9, "", {"title": "t", "_text_only": True}, {"engine": "plain"}, max_chars=10)
    assert d["data"]["metadata"]["fastweb"] == {"engine": "plain", "text_only_large": True}
    assert "_text_only" not in d["data"]["metadata"]


# ---------------------------------------------------------------- single-flight shares a leader's failure
def test_joiners_share_the_leaders_page_failure():
    hits = []

    async def gone(request):
        hits.append(1)
        await asyncio.sleep(0.6)
        return HTMLResponse("gone", status_code=404)

    async def nothing(request):
        return JSONResponse({"results": []})
    osrv, ourl = serve(Starlette(routes=[Route("/gone", gone), Route("/search", nothing)]))
    asrv, aurl = make_app(ourl)
    try:
        def one(_):
            return httpx.post(aurl + "/v2/scrape", json={"url": ourl + "/gone"}, timeout=10)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            rs = list(pool.map(one, range(4)))
        assert [r.status_code for r in rs] == [422] * 4
        assert {r.json()["error"] for r in rs} == {"Target page returned HTTP 404"}
        assert len(hits) < 4  # joiners did not each fetch the page again
        stats = httpx.get(aurl + "/v1/fastweb/stats", timeout=5).json()
        assert stats["scrape"]["singleflight_joins"] == 4 - len(hits)
    finally:
        asrv.should_exit = True
        osrv.should_exit = True


def test_shareable_failures():
    from fastweb_firecrawl.app import shareable
    from fastweb_firecrawl.errors import Fail
    assert shareable(Fail(422, "Target page returned HTTP 404"))
    assert not shareable(Fail(504, "timed out"))
    assert not shareable(Fail(422, "Could not resolve host x", transient=True))
    assert not shareable(Fail(422, "fastweb busy (in-flight byte budget); no fallback configured"))


# ---------------------------------------------------------------- compose: image names and Firecrawl containment
def compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_no_shared_image_tag():
    """A fixed image tag lets a second copy of the stack overwrite another stack's image."""
    for name, svc in compose()["services"].items():
        if "build" in svc:
            assert "image" not in svc, name


def test_firecrawl_is_isolated_behind_its_own_egress_proxy():
    c = compose()
    svcs, nets = c["services"], c["networks"]
    assert nets["firecrawlnet"].get("internal") is True
    for name in ("api", "playwright-service", "firecrawl-redis", "rabbitmq", "nuq-postgres"):
        assert svcs[name]["networks"] == ["firecrawlnet"], name
        assert "ports" not in svcs[name], name
        assert svcs[name].get("cap_drop") == ["ALL"] and "no-new-privileges:true" in svcs[name]["security_opt"], name
    assert set(svcs["firecrawl-egress"]["networks"]) == {"firecrawlnet", "default"}
    assert svcs["firecrawl-egress"]["command"] == ["python", "-m", "fastweb_firecrawl.egress", "--host", "0.0.0.0",
                                                      "--max-conns", "256"]
    assert "environment" not in svcs["firecrawl-egress"]  # no EGRESS_ALLOW can reach it
    for name in ("api", "playwright-service"):
        env = svcs[name]["environment"]
        assert env["PROXY_SERVER"] == env["HTTPS_PROXY"] == "http://firecrawl-egress:3128", name
    # playwright-service's own SSRF proxy forwards to PROXY_SERVER; Node's env proxy must not proxy that hop again
    pw = svcs["playwright-service"]["environment"]
    assert pw["NO_PROXY"] == pw["no_proxy"] == "localhost,127.0.0.1,firecrawl-egress"
    # nothing on Firecrawl's network but Firecrawl, its proxy and fastweb (its only client)
    on_fc = {n for n, s in svcs.items() if "firecrawlnet" in (s.get("networks") or [])}
    assert on_fc == {"api", "playwright-service", "firecrawl-redis", "rabbitmq", "nuq-postgres", "firecrawl-egress",
                     "fastweb"}


def test_firecrawl_secrets_have_no_weak_defaults():
    text = COMPOSE.read_text()
    assert "change-me" not in text and ":-postgres}" not in text
    gate = compose()["services"]["firecrawl-secrets"]
    assert gate["network_mode"] == "none"
    assert compose()["services"]["api"]["depends_on"]["firecrawl-secrets"]["condition"] == \
        "service_completed_successfully"


def test_searxng_config_is_mounted_read_only():
    assert compose()["services"]["searxng"]["volumes"] == ["./searxng:/etc/searxng:ro"]


# ---------------------------------------------------------------- host names and command lines
def test_idna_2008_invalid_host_is_400(ctx):
    _, _, aurl = ctx
    r = httpx.post(aurl + "/v2/scrape", json={"url": "https://xn--n3h.com/"}, timeout=10)
    assert r.status_code == 400 and "host name" in r.json()["error"]


def test_cli_flags(capsys):
    import pytest

    from fastweb_firecrawl import __version__, app, egress
    a = app.parse_args(["--host", "0.0.0.0", "--port", "4000"])
    assert (a.host, a.port) == ("0.0.0.0", 4000)
    assert app.parse_args([]).port == 3002
    e = egress.parse_args(["--port", "3999", "--max-conns", "10"])
    assert (e.host, e.port, e.max_conns) == ("127.0.0.1", 3999, 10)  # loopback unless told otherwise
    for parse, prog in ((app.parse_args, "fastweb-firecrawl"), (egress.parse_args, "fastweb-firecrawl-egress")):
        with pytest.raises(SystemExit) as ex:
            parse(["--version"])
        assert ex.value.code == 0 and capsys.readouterr().out.strip() == f"{prog} {__version__}"
        with pytest.raises(SystemExit):
            parse(["--help"])
        assert "--port" in capsys.readouterr().out


def test_unexpected_exception_does_not_leak_the_url_query(ctx, capfd, monkeypatch):
    """The catch-all answers 422 and logs only the exception's class: messages can quote the full URL."""
    from fastweb_firecrawl.pipeline import ScrapeService

    async def boom(self, job):
        raise ValueError(f"cannot parse {job.url}")
    monkeypatch.setattr(ScrapeService, "run", boom)
    up, furl, aurl = ctx
    capfd.readouterr()
    r = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/article?token=zebra-quokka"}, timeout=30)
    out = capfd.readouterr()
    assert r.status_code == 422 and r.json()["error"] == "Could not scrape the page: unexpected error"
    assert '"error":"unexpected ValueError"' in out.out
    for text in (r.text, out.out, out.err):
        assert "zebra" not in text and "token=" not in text


def test_malformed_firecrawl_markdown_is_an_honest_422(ctx):
    up, furl, aurl = ctx
    up.mode = "md_list"
    r = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/shell", "timeout": 20000}, timeout=30)
    assert r.status_code == 422 and "returned no content" in r.json()["error"], r.text


def test_fallback_html_is_served_as_requested_rawhtml(ctx):
    """Firecrawl answered markdown + html for formats=["rawHtml"]: the caller gets rawHtml only, like the plain path."""
    up, furl, aurl = ctx
    up.mode = "md_html"
    r = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/shell", "formats": ["rawHtml"], "timeout": 20000},
                   timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["rawHtml"] == "<h1>Rendered</h1>" and "markdown" not in data and "html" not in data


def test_plain_text_page_reports_text_lane(ctx):
    _, furl, aurl = ctx
    r = httpx.post(aurl + "/v2/scrape", json={"url": furl + "/p/text"}, timeout=10)
    assert r.status_code == 200 and r.json()["data"]["metadata"]["fastweb"]["lane"] == "text"
