"""Browser tier: the tier's own proxy failures go to Firecrawl, mutation-sensitive tests, one response
per absolute-URI connection, more IPv6 forms are private."""
import asyncio
import ipaddress
import json
import os
import socket
import time
from pathlib import Path

import httpx
import pytest

from .test_api import scrape
from .test_browser_tier import RENDERED, app, browser, cdp, run
from .test_egress_proxy import ask, proxy_and_origin
from .test_egress_token import TOKEN, egress_url


# ---------------------------------------------------------------- proxy failures are the tier's, not the target's
@pytest.mark.parametrize("err", ["net::ERR_PROXY_CONNECTION_FAILED", "net::ERR_TUNNEL_CONNECTION_FAILED"])
def test_proxy_nav_errors_are_browser_down(cdp, err):
    from fastweb_firecrawl.browser import BrowserDown
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED, nav_error=err)

    async def go():
        b = browser(cdp, settle=0.3)
        try:
            with pytest.raises(BrowserDown):
                await b.render(url)
        finally:
            await b.close()
    run(go())


def test_proxy_busy_page_is_browser_busy(cdp, egress_url):
    from fastweb_firecrawl.browser import BrowserBusy
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html="fastweb egress: busy", status=503,
                                  headers={"X-Fastweb-Egress": f"busy {TOKEN}"})

    async def go():
        b = browser(cdp, settle=0.3, egress=egress_url)
        try:
            with pytest.raises(BrowserBusy):
                await b.render(url)
        finally:
            await b.close()
    run(go())


def test_target_503_without_the_marker_is_still_the_target(cdp):
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED, status=503)

    async def go():
        b = browser(cdp, settle=0.3)
        try:
            return await b.render(url)
        finally:
            await b.close()
    assert run(go()).status == 503


@pytest.mark.parametrize("kw", [{"nav_error": "net::ERR_PROXY_CONNECTION_FAILED"},
                                {"status": 503, "headers": {"x-fastweb-egress": "busy " + TOKEN}}])
def test_proxy_failures_go_to_firecrawl(ctx, cdp, egress_url, kw):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html="fastweb egress: busy", **kw)
    srv, aurl = app(ctx, cdp, browser_egress=egress_url)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "firecrawl" and up.scrapes
    finally:
        srv.should_exit = True


def test_proxy_marks_its_own_answers():
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin(max_conns=1)
        refused = (await ask(pport, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n"))[0]
        _, r1, w1 = await ask(pport, b"", read=False)  # holds the only slot (waiting for a head)
        await asyncio.sleep(0.1)
        busy = (await ask(pport, b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n\r\n"))[0]
        stats = (await ask(pport, b"GET /stats HTTP/1.1\r\n\r\n"))[0]  # the healthcheck answers even when full
        return e, refused, busy, stats
    e, refused, busy, stats = run(go())
    assert refused.startswith(b"HTTP/1.1 403") and f"\r\nX-Fastweb-Egress: refused {e.token}\r\n".encode() in refused
    assert busy.startswith(b"HTTP/1.1 503") and f"\r\nX-Fastweb-Egress: busy {e.token}\r\n".encode() in busy
    assert stats.startswith(b"HTTP/1.1 200") and json.loads(stats.split(b"\r\n\r\n", 1)[1])["busy"] == 1


class Holder:
    """An origin that never closes: it reads until EOF and then keeps the socket open."""

    def __init__(self):
        self.eof = asyncio.Event()

    async def handle(self, r, w):
        while await r.read(65536):
            pass
        self.eof.set()
        await asyncio.sleep(30)


def test_tunnel_closes_both_ways_on_either_eof():
    """Connection flood: a disposed render's tunnels lived on for the 30 s idle timeout and filled the proxy."""
    from fastweb_firecrawl import egress

    async def go():
        h = Holder()
        osrv = await asyncio.start_server(h.handle, "127.0.0.1", 0)
        oport = osrv.sockets[0].getsockname()[1]
        e = egress.Egress(allow=[f"127.0.0.1:{oport}"], idle=30)
        psrv = await egress.serve(e, "127.0.0.1", 0)
        pport = psrv.sockets[0].getsockname()[1]
        data, r, w = await ask(pport, f"CONNECT 127.0.0.1:{oport} HTTP/1.1\r\n\r\n".encode())
        t = time.monotonic()
        w.write_eof()
        await asyncio.wait_for(h.eof.wait(), 25)
        tail = await asyncio.wait_for(r.read(10), 25)
        took = time.monotonic() - t
        await asyncio.sleep(0.1)
        return data, tail, took, e.counters["active"]
    data, tail, took, active = run(go())
    assert data.startswith(b"HTTP/1.1 200") and tail == b"" and took < 15 and active == 0  # not the 30 s idle


def test_default_cap_survives_two_floods():
    """Chrome lets ~134 proxy connections through per hostile render; two in a row must not fill the proxy."""
    from fastweb_firecrawl import egress
    assert egress.Egress().max_conns >= 1024
    assert egress.config({})["max_conns"] >= 1024


# ---------------------------------------------------------------- one response per absolute-URI connection
def test_absolute_uri_connection_closes_after_one_response():
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin()
        e.allow = frozenset({f"127.0.0.1:{oport}"})
        r, w = await asyncio.open_connection("127.0.0.1", pport)
        w.write(f"GET http://127.0.0.1:{oport}/a HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        got = b""
        while chunk := await asyncio.wait_for(r.read(65536), 3):
            got += chunk
        await asyncio.sleep(0.1)
        return got, e.counters["active"]
    got, active = run(go())
    assert got.endswith(b"ok") and b"\r\nConnection: close\r\n" in got and active == 0


# ---------------------------------------------------------------- more IPv6 forms embed IPv4 or are site-local
@pytest.mark.parametrize("a", ["::10.0.0.1", "::127.0.0.1", "::8.8.8.8", "::ffff:0:10.0.0.1", "::ffff:0:8.8.8.8",
                               "fec0::1", "feff::1"])
def test_more_ipv6_forms_are_private(a):
    from fastweb_firecrawl.egress import is_private_ip
    assert is_private_ip(ipaddress.ip_address(a))


def test_public_ipv6_is_still_public():
    from fastweb_firecrawl.egress import is_private_ip
    assert not is_private_ip(ipaddress.ip_address("2606:4700:4700::1111"))


# ---------------------------------------------------------------- tests that fail under known mutants
def test_serve_connects_to_the_checked_address():
    """`_serve` must connect to what `target()` returned, not re-resolve the name."""
    from fastweb_firecrawl import egress

    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin()

        async def pinned(host, port):
            return "127.0.0.1"
        e.target = pinned
        data = (await ask(pport, f"GET http://rebind.invalid:{oport}/x HTTP/1.1\r\n\r\n".encode()))[0]
        return data, o.heads
    data, heads = run(go())
    assert data.endswith(b"ok") and len(heads) == 1


def test_unresolvable_host_is_refused():
    """a name that does not resolve is a 502 refusal, never passed on to be resolved again by the connect."""
    from fastweb_firecrawl import egress

    async def go():
        e = egress.Egress()

        async def fail(*a, **kw):
            raise socket.gaierror("no such host")
        asyncio.get_running_loop().getaddrinfo = fail
        with pytest.raises(egress.Refused) as x:
            await e.target("nothing.invalid", 443)
        return x.value.status
    assert run(go()) == 502


def test_allowlist_is_per_port():
    """allowing host:A does not allow host:B."""
    from fastweb_firecrawl import egress

    async def go():
        e = egress.Egress(allow=["127.0.0.1:8081"])
        assert await e.target("127.0.0.1", 8081) == "127.0.0.1"
        with pytest.raises(egress.Refused):
            await e.target("127.0.0.1", 80)
    run(go())


def test_slow_head_is_cut_at_head_timeout():
    """a client that never finishes its head is dropped at head_timeout."""
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin(head_timeout=0.3)
        r, w = await asyncio.open_connection("127.0.0.1", pport)
        w.write(b"GET http://example.com/ HTTP/1.1\r\n")
        await w.drain()
        t = time.monotonic()
        tail = await asyncio.wait_for(r.read(10), 30)
        took = time.monotonic() - t
        w.close()
        for _ in range(100):  # the proxy's handler ends before this loop is dropped
            if not e.counters["active"]:
                break
            await asyncio.sleep(0.01)
        return tail, took
    tail, took = run(go())
    assert tail == b"" and took < 15  # cut by the proxy, not by the test's own wait


def test_no_allowlist_without_the_env():
    """production never sets EGRESS_ALLOW; then nothing is allowlisted."""
    from fastweb_firecrawl import egress
    assert egress.config({})["allow"] == [] and egress.Egress().allow == frozenset()
    assert egress.config({"EGRESS_ALLOW": "a:1, b:2"})["allow"] == ["a:1", "b:2"]


def test_compose_gives_the_egress_no_env_at_all():
    """an env_file (or any environment) on browser-egress would let .env switch the test allowlist on."""
    import yaml
    here = os.path.dirname(os.path.abspath(__file__))
    e = yaml.safe_load(Path(os.path.join(here, "..", "docker-compose.yml")).read_text())["services"]["browser-egress"]
    assert "env_file" not in e and "environment" not in e and "EGRESS_ALLOW" not in json.dumps(e)
