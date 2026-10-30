"""Browser egress: a target can't fake the proxy's X-Fastweb-Egress marker; the refused marker is
tested."""
import http.server
import threading

import pytest

from .test_api import scrape
from .test_browser_tier import RENDERED, app, browser, cdp, run
from .test_egress_proxy import ask, proxy_and_origin

TOKEN = "t0k3n-only-the-proxy-knows"
WALL = "<html><body>Just a moment... cf-chl</body></html>"


@pytest.fixture
def egress_url():
    """Stands in for browser-egress's GET /token."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = TOKEN.encode() if self.path == "/token" else b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def render(cdp, url, egress):
    async def go():
        b = browser(cdp, settle=0.3, egress=egress)
        try:
            return await b.render(url)
        finally:
            await b.close()
    return run(go())


# ---------------------------------------------------------------- spoofed markers are the target's page
@pytest.mark.parametrize("mark", ["busy", "refused", f"busy {TOKEN}x", "refused nope"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_target_sent_marker_is_ignored(cdp, egress_url, mark, scheme):
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED, headers={"X-Fastweb-Egress": mark},
                                  final_url=f"{scheme}://127.0.0.1:1/p/app")
    r = render(cdp, url, egress_url)
    assert r.status == 200 and r.html


@pytest.mark.parametrize("mark", ["busy", "refused"])
def test_real_marker_over_https_is_ignored(cdp, egress_url, mark):
    """The proxy never answers inside a tunnel: a token on an https document can only have leaked."""
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED, headers={"X-Fastweb-Egress": f"{mark} {TOKEN}"},
                                  final_url="https://127.0.0.1:1/p/app")
    assert render(cdp, url, egress_url).status == 200


def test_marker_without_a_configured_egress_is_ignored(cdp):
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED, headers={"X-Fastweb-Egress": f"busy {TOKEN}"})
    assert render(cdp, url, None).status == 200


@pytest.mark.parametrize("mark,status,exc", [("busy", 503, "BrowserBusy"), ("refused", 403, "BrowserDown"),
                                             ("refused", 502, "BrowserDown")])
def test_real_marker_is_the_tier(cdp, egress_url, mark, status, exc):
    from fastweb_firecrawl import browser as bmod
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=f"fastweb egress: {mark}", status=status,
                                  headers={"X-Fastweb-Egress": f"{mark} {TOKEN}"})
    with pytest.raises(getattr(bmod, exc)) as e:
        render(cdp, url, egress_url)
    assert type(e.value) is getattr(bmod, exc)  # refused is Down, not Busy and not the target's page


@pytest.mark.parametrize("mark", ["busy", "refused"])
@pytest.mark.parametrize("sc,code", [({"html": RENDERED}, 200), ({"html": WALL, "status": 403}, 422)])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_spoofing_target_gets_the_browser_verdict(ctx, cdp, egress_url, mark, sc, code, scheme):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(headers={"X-Fastweb-Egress": mark}, final_url=url.replace("http:", scheme + ":"),
                                  **sc)
    srv, aurl = app(ctx, cdp, browser_egress=egress_url)
    try:
        for _ in range(2):  # and no Firecrawl host memory: the second wall is the browser's verdict again
            r = scrape(aurl, url)
            assert r.status_code == code, r.text
            if code == 200:
                assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
            else:
                assert "HTTP 403 in the browser too" in r.json()["error"]
        assert not up.scrapes and len(cdp.navigations) == (2 if code == 422 else 1)  # a 200 is in the scrape cache
    finally:
        srv.should_exit = True


@pytest.mark.parametrize("mark,status", [("busy", 503), ("refused", 403)])
def test_real_marker_goes_to_firecrawl(ctx, cdp, egress_url, mark, status):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html=f"fastweb egress: {mark}", status=status,
                                  headers={"X-Fastweb-Egress": f"{mark} {TOKEN}"})
    srv, aurl = app(ctx, cdp, browser_egress=egress_url)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 200 and r.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl" and up.scrapes
    finally:
        srv.should_exit = True


def test_proxy_marks_its_answers_with_its_token_and_serves_it():
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin()
        refused = (await ask(pport, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n"))[0]
        tok = (await ask(pport, b"GET /token HTTP/1.1\r\n\r\n"))[0]
        stats = (await ask(pport, b"GET /stats HTTP/1.1\r\n\r\n"))[0]
        return e, refused, tok, stats
    e, refused, tok, stats = run(go())
    assert len(e.token) >= 20
    assert f"\r\nX-Fastweb-Egress: refused {e.token}\r\n".encode() in refused
    assert tok.startswith(b"HTTP/1.1 200") and tok.split(b"\r\n\r\n", 1)[1] == e.token.encode()
    assert e.token.encode() not in stats


def test_token_is_served_when_the_proxy_is_full():
    """Found on the real stack: fastweb reads the token just when the proxy is busy, so /token must answer then."""
    import asyncio

    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin(max_conns=1)
        _, r1, w1 = await ask(pport, b"", read=False)
        await asyncio.sleep(0.1)
        return e, (await ask(pport, b"GET /token HTTP/1.1\r\n\r\n"))[0]
    e, tok = run(go())
    assert tok.startswith(b"HTTP/1.1 200") and tok.split(b"\r\n\r\n", 1)[1] == e.token.encode()
