import io
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

# Prefetch off unless a test asks for it: no background fetches of other tests' search results.
os.environ.setdefault("FASTWEB_PREFETCH_N", "0")
os.environ.setdefault("FASTWEB_SEARCH_WARM", "0")  # tests count SearXNG calls; test_search covers warm()

ARTICLE = "<html><head><title>Big Story</title></head><body><nav>menu</nav><article><h1>Big Story</h1>" + "".join(
    f"<p>Paragraph {i}: the council voted on the harbour plan after a long debate about funding and tides.</p>"
    for i in range(20)) + "</article><footer>footer</footer></body></html>"
JS_SHELL = ("<html><head><title>App</title><script src='/app.js'></script></head><body>"
            "<noscript>You need to enable JavaScript to run this app.</noscript><div id='root'></div></body></html>")
NOT_FOUND = "<html><head><title>Not found</title></head><body><h1>Page not found</h1>" + "<p>Sorry.</p>" * 50 + "</body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser. cf-chl challenge</body></html>"
SHORT = "<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1><p>This domain is for use in illustrative examples in documents. You may use this domain in literature without prior coordination.</p><p><a href=\"https://www.iana.org/domains/example\">More information...</a></p></body></html>"


def make_pdf() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    s = DecodedStreamObject()
    s.set_data(b"BT /F1 12 Tf 72 720 Td (Quantum harbour tides paper) Tj ET")
    page[NameObject("/Contents")] = w._add_object(s)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


EXTRA = {}  # name -> (bytes, content-type, status); filled by tests


class Upstream:
    """Fake Firecrawl api + SearXNG; records calls."""

    def __init__(self):
        self.scrapes = []
        self.searches = []
        self.fail = False
        self.mode = ""  # "", "slow", "502", "challenge", "error404", "target403"
        self.hits = []


def fixture_app(up: Upstream) -> Starlette:
    pdf = make_pdf()

    async def page(request):
        name = request.path_params["name"]
        up.hits.append(name)
        if name in EXTRA:
            body, ctype, code = EXTRA[name]
            return Response(body, status_code=code, headers={"content-type": ctype})  # no added charset
        if name == "article":
            return HTMLResponse(ARTICLE)
        if name == "short":
            return HTMLResponse(SHORT)
        if name == "shell":
            return HTMLResponse(JS_SHELL)
        if name == "missing":
            return HTMLResponse(NOT_FOUND, status_code=404)
        if name == "challenge":
            return HTMLResponse(CHALLENGE, status_code=403)
        if name == "pdf":
            return Response(pdf, media_type="application/pdf")
        if name == "loop":
            return RedirectResponse("/p/loop", status_code=302)
        if name == "redirect":
            return RedirectResponse("/p/article", status_code=302)
        if name == "to_challenge":
            return RedirectResponse(f"http://127.0.0.1:{request.url.port}/p/challenge", status_code=302)
        if name == "to_localhost":
            port = request.url.port
            return RedirectResponse(f"http://localhost:{port}/p/article", status_code=302)
        if name == "text":
            return Response("Python README\n=============\n\nThis is plain text.\n", media_type="text/plain")
        if name == "json":
            return JSONResponse({"full_name": "python/cpython", "stars": 1})
        if name == "png":
            return Response(b"\x89PNG\r\n\x1a\n" + b"\0" * 64, media_type="image/png")
        if name == "spa":
            return HTMLResponse("<html><head><title>App</title><script src='/main.js'></script></head>"
                                "<body><div id='root'></div></body></html>")
        if name == "consent":
            return HTMLResponse("<html><body><p>We value your privacy. Accept all cookies to continue.</p></body></html>")
        if name == "badpdf":
            return Response(b"%PDF-1.4 garbage", media_type="application/pdf")
        if name == "huge":
            return HTMLResponse("<html><body>" + "x" * (3 * 1024 * 1024) + "</body></html>")
        if name == "slow":
            import asyncio
            await asyncio.sleep(5)
            return HTMLResponse(ARTICLE)
        return HTMLResponse("?", status_code=500)

    async def fc_scrape(request):
        body = await request.json()
        up.scrapes.append(body)
        if up.mode == "slow":
            import asyncio
            await asyncio.sleep(30)
        if up.mode == "502":
            return Response("bad gateway", status_code=502)
        if up.mode == "challenge":
            return JSONResponse({"success": True, "data": {"markdown": "Just a moment... Checking your browser",
                                                           "metadata": {"sourceURL": body["url"], "statusCode": 200}}})
        if up.mode == "target403":
            return JSONResponse({"success": True, "data": {"markdown": "Forbidden",
                                                           "metadata": {"sourceURL": body["url"], "statusCode": 403}}})
        if up.mode == "fc408":
            return JSONResponse({"success": False, "code": "SCRAPE_TIMEOUT", "error": "timed out"}, status_code=408)
        if up.mode == "target500":
            return JSONResponse({"success": True, "data": {"markdown": "Server error",
                                                           "metadata": {"sourceURL": body["url"], "statusCode": 500}}})
        if up.mode == "error404":
            return JSONResponse({"success": True, "data": {"markdown": "# Not found\n\nsorry",
                                                           "metadata": {"sourceURL": body["url"], "statusCode": 404}}})
        if up.mode == "empty_md":
            return JSONResponse({"success": True, "data": {"markdown": "", "metadata": {"statusCode": 200}}})
        if up.mode == "blank_md_html":  # blank markdown next to markup nobody asked for
            return JSONResponse({"success": True, "data": {"markdown": "  ", "html": "<p>only markup</p>",
                                                           "metadata": {"sourceURL": body["url"], "statusCode": 200}}})
        if up.mode == "private_source":  # no metadata.url: the sourceURL is checked instead
            return JSONResponse({"success": True, "data": {"markdown": "# Internal\n\nsecret", "metadata": {
                "sourceURL": "http://localhost:8080/admin", "statusCode": 200}}})
        if up.mode == "bad_scheme_final":
            return JSONResponse({"success": True, "data": {"markdown": "# Elsewhere\n\ntext", "metadata": {
                "sourceURL": body["url"], "url": "ftp://example.com/file", "statusCode": 200}}})
        if up.mode == "md_list":
            return JSONResponse({"success": True, "data": {"markdown": ["not", "text"], "metadata": {"statusCode": 200}}})
        if up.mode == "md_html":
            return JSONResponse({"success": True, "data": {"markdown": "# Rendered\n\ntext", "html": "<h1>Rendered</h1>",
                                                           "metadata": {"statusCode": 200}}})
        if up.mode == "null_meta":
            return JSONResponse({"success": True, "data": {"markdown": "# Rendered\n\ntext", "metadata": None}})
        if up.mode == "private_final":
            return JSONResponse({"success": True, "data": {"markdown": "# Internal\n\nsecret", "metadata": {
                "sourceURL": body["url"], "url": "http://localhost:8080/admin", "statusCode": 200}}})
        if up.fail:
            return JSONResponse({"success": False, "error": "upstream could not render"}, status_code=500)
        return JSONResponse({"success": True, "data": {
            "markdown": "# Rendered by Firecrawl\n\nfull text, [a script](javascript:alert(1)) and [a page](/x)",
            "links": ["https://site.example/a", "javascript:alert(1)", "data:text/html,x", 7],
            "metadata": {"title": "Rendered", "sourceURL": body["url"], "url": body["url"], "statusCode": 200,
                         "og:title": "Rendered OG", "scrapeId": "s-1", "creditsUsed": 1, "proxyUsed": "basic",
                         "concurrencyLimited": False, "someFutureRunKey": "x"}}})

    async def searx(request):
        up.searches.append(dict(request.query_params))
        if up.mode == "search_empty":
            return JSONResponse({"results": [], "unresponsive_engines": [["bing", "timeout"]]})
        if up.mode == "search_429":
            return JSONResponse({"error": "rate limited"}, status_code=429)
        return JSONResponse({"results": [
            {"url": f"https://r{i}.example/", "title": f"Result {i}", "content": f"snippet {i}"} for i in range(8)]})

    return Starlette(routes=[Route("/p/{name}", page), Route("/v2/scrape", fc_scrape, methods=["POST"]),
                             Route("/search", searx)])


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host=os.environ.get("FASTWEB_TEST_BIND", "127.0.0.1"), port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    return server, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def env():
    up = Upstream()
    fsrv, furl = serve(fixture_app(up))
    from fastweb_firecrawl.app import create_app
    from fastweb_firecrawl.settings import Settings
    settings = Settings(upstream=furl, searxng_url=furl, searxng_engines="bing,yahoo", search_relevance=False,  # synthetic results
                        max_bytes=2 * 1024 * 1024, total_timeout=2.0,
                        redis_url="", allow_hosts=frozenset({"127.0.0.1"}))
    asrv, aurl = serve(create_app(settings))
    yield up, furl, aurl
    asrv.should_exit = True
    fsrv.should_exit = True


@pytest.fixture
def ctx(env):
    up, furl, aurl = env
    up.scrapes.clear()
    up.searches.clear()
    up.fail = False
    up.mode = ""
    up.hits.clear()
    return env


def pytest_configure(config):
    config.addinivalue_line("markers", "browser: needs a real chromedp/headless-shell (FASTWEB_TEST_CDP)")


def pytest_xdist_auto_num_workers(config):
    """`-n auto`: at most 4 workers, half the CPUs. Many tests measure real timing and run CPU-heavy extraction in
    worker processes of their own; one xdist worker per core starves them into false timeouts."""
    return max(1, min(4, (os.cpu_count() or 2) // 2))
