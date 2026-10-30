"""The browser egress proxy, dead hosts through the browser, tests that test their
claims, too-large renders, NAT64. Real-Chrome tests at the bottom need the throwaway stack
(`tests/egress_stack/stack.sh up`, then `eval "$(tests/egress_stack/stack.sh env)"`) and are skipped without FASTWEB_TEST_EGRESS_*."""
import asyncio
import ipaddress
import json
import os
import socket
import time
from pathlib import Path

import httpx
import pytest

from . import test_prefetch as tp
from .conftest import serve
from .test_api import scrape
from .test_browser_tier import RENDERED, SPA_FULL, SPA_SEO, app, browser, cdp, run, spa_app


# ---------------------------------------------------------------- NAT64 is private
@pytest.mark.parametrize("a", ["64:ff9b::a00:1", "64:ff9b::c0a8:101", "64:ff9b::808:808"])
def test_nat64_is_private(a):
    from fastweb_firecrawl.guard import is_private_ip
    assert is_private_ip(ipaddress.ip_address(a))


def test_app_and_proxy_share_one_rule():
    from fastweb_firecrawl import egress, guard
    assert guard.is_private_ip is egress.is_private_ip


# ---------------------------------------------------------------- the egress proxy
class Origin:
    """A tiny TCP origin: answers any HTTP request with 200 and records the request head; or echoes (CONNECT)."""

    def __init__(self, echo=False):
        self.heads, self.echo = [], echo

    async def handle(self, r, w):
        if self.echo:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
            w.close()
            return
        head = await r.readuntil(b"\r\n\r\n")
        self.heads.append(head.decode())
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await w.drain()
        w.close()


async def proxy_and_origin(echo=False, **kw):
    from fastweb_firecrawl import egress
    o = Origin(echo)
    osrv = await asyncio.start_server(o.handle, "127.0.0.1", 0)
    oport = osrv.sockets[0].getsockname()[1]
    e = egress.Egress(**{"allow": kw.pop("allow", ()), **kw})
    psrv = await egress.serve(e, "127.0.0.1", 0)
    return e, o, oport, psrv.sockets[0].getsockname()[1], (osrv, psrv)


async def ask(pport, raw, read=True):
    r, w = await asyncio.open_connection("127.0.0.1", pport)
    w.write(raw)
    await w.drain()
    data = await asyncio.wait_for(r.read(65536), 5) if read else b""
    return data, r, w


def test_proxy_refuses_private_targets_and_counts_them():
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin()
        out = []
        for raw in (f"GET http://127.0.0.1:{oport}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                    f"CONNECT 127.0.0.1:{oport} HTTP/1.1\r\n\r\n",
                    "GET http://localhost/x HTTP/1.1\r\n\r\n",
                    "GET http://169.254.169.254/latest HTTP/1.1\r\n\r\n",
                    "CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
                    "CONNECT [::ffff:10.0.0.1]:443 HTTP/1.1\r\n\r\n",
                    "CONNECT [64:ff9b::a00:1]:443 HTTP/1.1\r\n\r\n",
                    "GET http://100.64.12.34:80/ HTTP/1.1\r\n\r\n",
                    "CONNECT 8.8.8.8:22 HTTP/1.1\r\n\r\n",
                    "GET https://8.8.8.8/ HTTP/1.1\r\n\r\n"):
            out.append((await ask(pport, raw.encode()))[0])
        return e, o, out
    e, o, out = run(go())
    assert all(x.startswith(b"HTTP/1.1 403") or x.startswith(b"HTTP/1.1 400") for x in out), out
    assert o.heads == [] and e.counters["allowed"] == 0
    assert e.counters["refused"] == 10 and e.counters["refused_private"] == 6 and e.counters["refused_port"] == 3
    assert any("169.254.169.254" in x for x in e.recent)


def test_proxy_pins_the_checked_address():
    """A host that resolves public is connected to by that address, never re-resolved (DNS rebinding)."""
    from fastweb_firecrawl import egress

    async def go():
        e = egress.Egress()
        loop = asyncio.get_running_loop()
        answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))],
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
                         (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]])

        async def fake(host, port, **kw):
            return next(answers)
        loop.getaddrinfo = fake
        first = await e.target("rebind.example", 443)
        with pytest.raises(egress.Refused):
            await e.target("rebind.example", 443)  # any private address in the answer: refused
        return first
    assert run(go()) == "93.184.215.14"


def test_proxy_allowlist_plain_http_and_connect():
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin()
        e.allow = frozenset({f"127.0.0.1:{oport}"})
        data, _, _ = await ask(pport, f"GET http://127.0.0.1:{oport}/a?b=1 HTTP/1.1\r\nHost: x\r\n"
                                      f"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n".encode())
        stats = json.loads((await ask(pport, b"GET /stats HTTP/1.1\r\n\r\n"))[0].split(b"\r\n\r\n", 1)[1])
        return data, o.heads, stats
    data, heads, stats = run(go())
    assert data.endswith(b"ok") and heads[0].startswith("GET /a?b=1 HTTP/1.1\r\n")
    assert "Proxy-Connection" not in heads[0] and "Connection: close" in heads[0] and "keep-alive" not in heads[0]
    assert stats["allowed"] == 1 and stats["refused"] == 0

    async def tunnel():
        e, o, oport, pport, srvs = await proxy_and_origin(echo=True)
        e.allow = frozenset({f"127.0.0.1:{oport}"})
        data, r, w = await ask(pport, f"CONNECT 127.0.0.1:{oport} HTTP/1.1\r\n\r\n".encode())
        w.write(b"ping")
        await w.drain()
        return data, await asyncio.wait_for(r.read(4), 5)
    data, echo = run(tunnel())
    assert data.startswith(b"HTTP/1.1 200") and echo == b"ping"


def test_proxy_connection_cap_and_idle_timeout():
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin(echo=True, max_conns=1, idle=0.3)
        e.allow = frozenset({f"127.0.0.1:{oport}"})
        first, r1, w1 = await ask(pport, f"CONNECT 127.0.0.1:{oport} HTTP/1.1\r\n\r\n".encode())
        second = (await ask(pport, f"CONNECT 127.0.0.1:{oport} HTTP/1.1\r\n\r\n".encode()))[0]
        t = time.monotonic()
        closed = await asyncio.wait_for(r1.read(10), 30)  # idle tunnel closed by the proxy
        return first, second, closed, time.monotonic() - t, e.counters
    first, second, closed, dt, c = run(go())
    assert first.startswith(b"HTTP/1.1 200") and second.startswith(b"HTTP/1.1 503") and c["busy"] == 1
    assert closed == b"" and dt < 15


def test_compose_routes_chrome_through_the_proxy_only():
    import yaml
    here = os.path.dirname(os.path.abspath(__file__))
    dc = yaml.safe_load(Path(os.path.join(here, "..", "docker-compose.yml")).read_text())
    assert dc["networks"]["browsernet"].get("internal") is True
    b, e = dc["services"]["browser"], dc["services"]["browser-egress"]
    assert b["networks"] == ["browsernet"]
    flags = " ".join(b["command"])
    for f in ("--proxy-server=http://browser-egress:3128", "--proxy-bypass-list=<-loopback>", "--disable-quic",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--block-new-web-contents"):
        assert f in flags
    assert set(e["networks"]) == {"browsernet", "default"} and "EGRESS_ALLOW" not in json.dumps(e)
    assert e["mem_limit"].lower() == "128m" and e.get("healthcheck") and e.get("read_only") is True
    # non-root, read-only root fs, bounded pids
    assert b["user"] and not str(b["user"]).startswith("0") and b["read_only"] is True and b["pids_limit"] <= 256
    assert any(t.startswith("/tmp") for t in b["tmpfs"])
    # the healthcheck speaks HTTP/1.1 and asks Chrome to close
    hc = " ".join(b["healthcheck"]["test"])
    assert "HTTP/1.1" in hc and "Connection: close" in hc and "HTTP/1.0" not in hc


# ---------------------------------------------------------------- auto-attach children
def test_worker_requests_pass_the_guard_via_auto_attach(cdp):
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED, children=[("http://10.0.0.7/w", "Fetch"),
                                                           ("http://127.0.0.1:1/ok", "Fetch")])

    async def go():
        b = browser(cdp, settle=0.6)
        try:
            await b.render(url)
            await asyncio.sleep(0.2)
        finally:
            await b.close()
    run(go())
    assert cdp.unseen == []
    d = {u: dec for u, _, dec in cdp.decisions}
    assert d["http://10.0.0.7/w"].startswith("fail") and d["http://127.0.0.1:1/ok"] == "continue"


def test_page_session_gets_ws_block_and_harden_script(cdp):
    """the WebSocket/WebRTC/service-worker claims are sent (the real-Chrome test checks their effect)."""
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED)

    async def go():
        b = browser(cdp, settle=0.3)
        try:
            await b.render(url)
        finally:
            await b.close()
    run(go())
    blocked = [p for m, p, _ in cdp.calls if m == "Network.setBlockedURLs"]
    scripts = [p["source"] for m, p, _ in cdp.calls if m == "Page.addScriptToEvaluateOnNewDocument"]
    assert blocked and {"ws://*", "wss://*"} <= set(blocked[0]["urls"])
    assert scripts and all(w in scripts[0] for w in ("WebSocket", "RTCPeerConnection", "serviceWorker"))


# ---------------------------------------------------------------- browser refusals feed DEAD
def test_repeated_browser_refusals_make_the_host_dead(ctx, cdp):
    import fakeredis

    from fastweb_firecrawl.cache import OBS_PREFIX
    up, furl, _ = ctx
    server = fakeredis.FakeServer()
    r = fakeredis.FakeAsyncRedis(server=server)
    old = time.time() - 700
    run(fakeredis.FakeAsyncRedis(server=server).zadd(OBS_PREFIX + "127.0.0.1", {f"/p/a|{old}": old, f"/p/b|{old + 1}": old + 1}))
    url = furl + "/p/challenge"
    cdp.pages[url] = cdp.scenario(status=403, html="<html><body>Just a moment... cf-chl</body></html>")
    srv, aurl = app(ctx, cdp, redis=r)
    try:
        assert scrape(aurl, url).status_code == 422 and len(cdp.navigations) == 1  # the third refusal
        t = time.monotonic()
        x = scrape(aurl, url)
        assert x.status_code == 422 and "blocked" in x.json()["error"]
        assert time.monotonic() - t < 1.5 and len(cdp.navigations) == 1 and not up.scrapes
    finally:
        srv.should_exit = True


# ---------------------------------------------------------------- suspect SPA never rendered in prefetch
def test_suspect_spa_page_never_rendered_in_prefetch(cdp):
    f = tp.Fake()
    f.extra["spa_seo"] = SPA_SEO
    fsrv, f.url = serve(f.app())
    try:
        url = f.url + "/p/spa_seo"
        cdp.pages[url] = cdp.scenario(html=SPA_FULL)
        f.results = [url]
        aurl, _, _ = tp.make(f, browser_url=cdp.endpoint, browser_suspect=True, browser_settle=0.3,
                             browser_timeout=1.5)
        assert tp.search(aurl).status_code == 200
        p = tp.settle(aurl, 1)
        assert "spa_seo" in f.hits and p["done"] == 1 and cdp.navigations == []
    finally:
        fsrv.should_exit = True


def test_post_render_url_is_checked(ctx, cdp):
    """Chrome reports a final document URL the guard never paused on (e.g. a same-document history change)."""
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html=RENDERED, final_url="http://10.0.0.9/admin")
    srv, aurl = app(ctx, cdp)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 422 and "private" in r.json()["error"] and not up.scrapes
    finally:
        srv.should_exit = True


# ---------------------------------------------------------------- too large is not "still thin"
def test_too_large_render_says_so(ctx, cdp):
    up, furl, _ = ctx
    url = furl + "/p/shell"
    cdp.pages[url] = cdp.scenario(html=RENDERED * 60)  # ~170 KB
    srv, aurl = app(ctx, cdp, max_bytes=100_000)
    try:
        r = scrape(aurl, url)
        assert r.status_code == 422 and "too large" in r.json()["error"] and "thin" not in r.json()["error"]
    finally:
        srv.should_exit = True


# ---------------------------------------------------------------- real Chrome behind the real proxy (throwaway stack)
REAL = os.environ.get("FASTWEB_TEST_EGRESS_CDP")


@pytest.mark.browser
@pytest.mark.skipif(not REAL, reason="set FASTWEB_TEST_EGRESS_CDP, _STATS, _ORIGIN, _TARGET, _TARGET_LOG (see tests/egress_stack/stack.sh)")
def test_real_chrome_every_vector_goes_through_the_proxy():
    """A page from the test origin (reachable only through the proxy's test allowlist) aims a dedicated worker, a
    shared worker, a cross-site iframe's subresources, a popup, sendBeacon, prefetch, meta refresh, WebSocket and a
    form post at a private target on Chrome's own network. The CDP guard is switched off (allow everything): the proxy
    alone must refuse them all, and the target must log nothing."""
    from fastweb_firecrawl.browser import Browser
    stats_url, origin = os.environ["FASTWEB_TEST_EGRESS_STATS"], os.environ["FASTWEB_TEST_EGRESS_ORIGIN"]
    target, log = os.environ["FASTWEB_TEST_EGRESS_TARGET"], os.environ["FASTWEB_TEST_EGRESS_TARGET_LOG"]

    async def anything(url):
        return True

    def target_hits():
        return [x for x in open(log).read().splitlines() if "/v/" in x]
    before = httpx.get(stats_url, timeout=5).json()
    hits0 = len(target_hits())

    async def go():
        b = Browser(REAL, guard=anything, settle=3.0, timeout=8.0)
        try:
            return await b.render(f"{origin}/vectors.html?t={target}")
        finally:
            await b.close()
    r = run(go())
    time.sleep(3)
    after = httpx.get(stats_url, timeout=5).json()
    refused = after["recent_refusals"][len(before["recent_refusals"]):]  # < 200 entries in a throwaway stack
    print("rendered", r.status, len(r.html), "refused:", refused, "stats:", {k: after[k] for k in after
                                                                            if k != "recent_refusals"})
    assert r.status == 200 and "vectors loaded" in r.html
    assert after["allowed"] > before["allowed"]  # Chrome really uses the proxy for the origin
    assert len(target_hits()) == hits0, target_hits()[hits0:]
    joined = " ".join(refused)
    for v in ("worker", "shared", "oopif-fetch", "oopif-script", "beacon", "prefetch", "refresh", "form"):
        assert f"/v/{v}" in joined, (v, refused)
    tip = httpx.URL(target.split("&")[0])
    assert f"CONNECT {tip.host}:{tip.port or 80}" in refused  # the worker's WebSocket: a tunnel, refused
    # HARDEN on the main frame: WebSocket and RTCPeerConnection are gone there
    assert "main-ws:undefined" in r.html and "main-rtc:undefined" in r.html
    # popups: --block-new-web-contents
    assert "popup:null" in r.html


# ---------------------------------------------------------------- a wedged browser is down at once
def test_wedged_browser_is_down_fast_and_reconnects(cdp):
    """Chrome stops answering CDP on a live socket: the page fails BrowserDown within ~1 s (Firecrawl takes it), the
    connection is dropped and the next page reconnects (after the backoff) once Chrome answers again."""
    from fastweb_firecrawl.browser import BrowserDown
    url = "http://127.0.0.1:1/p/app"
    cdp.pages[url] = cdp.scenario(html=RENDERED)

    async def go():
        b = browser(cdp, settle=3.0, timeout=25.0, backoff=0.2)
        try:
            await b.render(url)
            cdp.eval_hang = True
            cdp.hang.add("Browser.getVersion")
            t = time.monotonic()
            with pytest.raises(BrowserDown):
                await b.render(url)
            took = time.monotonic() - t
            cdp.eval_hang = False
            cdp.hang.discard("Browser.getVersion")
            await asyncio.sleep(0.3)
            r = await b.render(url)
            return took, r, b.stats()
        finally:
            await b.close()
    took, r, st = run(go())
    assert took < 12 and r.html == RENDERED and st["wedged"] == 1 and st["connects"] == 2


@pytest.mark.parametrize("exc,counter", [(TimeoutError(), "timeouts"), (RuntimeError("bug"), "internal"),
                                         (ConnectionResetError(), None)])
def test_handle_counts_its_own_failures(monkeypatch, capsys, exc, counter):
    """A defect in the proxy closes only that connection, but is counted (and logged) rather than silently dropped."""
    from fastweb_firecrawl import egress
    e = egress.Egress()

    async def boom(*a, **kw):
        raise exc
    monkeypatch.setattr(e, "_serve", boom)

    class W:
        def close(self):
            pass
    run(e.handle(None, W()))
    assert e.counters["active"] == 0
    assert {k: e.counters[k] for k in ("timeouts", "internal")} == {
        k: int(k == counter) for k in ("timeouts", "internal")}
    assert ("egress_internal_error" in capsys.readouterr().out) == (counter == "internal")
