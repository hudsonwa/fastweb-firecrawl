"""Suspect-SPA losses are remembered per host, cheap tiers check the connected peer,
egress refusals are logged without query strings."""
import asyncio
import http.server
import socket
import threading
import time

import fakeredis
import httpx
import pytest

from .test_api import scrape
from .test_browser_tier import SPA_FULL, SPA_SEO, app, cdp, run
from .test_egress_proxy import ask, proxy_and_origin


def two_spa_pages():
    from .conftest import EXTRA
    EXTRA["spa_seo"] = (SPA_SEO.encode(), "text/html", 200)
    EXTRA["spa_seo2"] = (SPA_SEO.encode(), "text/html", 200)


def stats(aurl):
    return httpx.get(aurl + "/v1/fastweb/stats").json()["browser"]


# ---------------------------------------------------------------- a suspect loss is remembered
def test_suspect_loss_is_remembered_and_skipped(ctx, cdp):
    """weatherzone-like: the render is slow and not bigger. The second cold fetch of the host is plain-tier fast."""
    up, furl, _ = ctx
    two_spa_pages()
    for p in ("spa_seo", "spa_seo2"):
        cdp.pages[furl + "/p/" + p] = cdp.scenario(html=SPA_SEO, nav_delay=0.8)
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis())
    try:
        r = scrape(aurl, furl + "/p/spa_seo")
        assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "plain" and len(cdp.navigations) == 1
        r = scrape(aurl, furl + "/p/spa_seo2")
        assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "plain"
        assert len(cdp.navigations) == 1  # no second render: the remembered loss skipped the browser
        st = stats(aurl)
        assert st["suspect_skipped_known_loss"] == 1 and st["suspect_tried"] == 1
    finally:
        srv.should_exit = True


def test_suspect_browser_error_is_not_a_loss_and_a_win_clears(ctx, cdp):
    up, furl, _ = ctx
    two_spa_pages()
    u1, u2 = furl + "/p/spa_seo", furl + "/p/spa_seo2"
    cdp.pages[u1] = cdp.scenario(html=SPA_FULL, crash=True)
    cdp.pages[u2] = cdp.scenario(html=SPA_FULL)
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis())
    try:
        assert scrape(aurl, u1).json()["data"]["metadata"]["fastweb"]["engine"] == "plain"
        # a crash is not a loss: the next page of the host still renders and wins
        assert scrape(aurl, u2).json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
        assert stats(aurl)["suspect_skipped_known_loss"] == 0
    finally:
        srv.should_exit = True


def test_store_suspect_loss_ttl_and_clear():
    from fastweb_firecrawl.cache import Store

    async def go():
        st = Store(fakeredis.FakeAsyncRedis())
        assert not await st.suspect_lost("a.example")
        await st.set_suspect_loss("a.example", "wa")
        assert await st.suspect_lost("a.example", "wa") and not await st.suspect_lost("b.example", "wa")
        assert not await st.suspect_lost("a.example", "nsw")  # another path class still tries the render
        ttl = await st.redis.ttl("fastweb:suspect_loss:a.example/wa")
        await st.clear_suspect_loss("a.example", "wa")
        return ttl, await st.suspect_lost("a.example", "wa")
    ttl, after = run(go())
    assert 3000 < ttl <= 3600 and not after
    assert run(Store(None).suspect_lost("a.example")) is False


# ---------------------------------------------------------------- the connected peer is checked
def rebinding(monkeypatch, port):
    """rebind.example answers public on the first lookup (fastweb's check), 127.0.0.1 afterwards (the connect)."""
    real = asyncio.base_events.BaseEventLoop.getaddrinfo
    seen = []

    async def fake(self, host, *a, **k):
        if host in ("rebind.example", b"rebind.example"):  # anyio passes the idna-encoded bytes
            seen.append(host)
            ip = "93.184.215.14" if len(seen) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return await real(self, host, *a, **k)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake)
    return seen


def test_plain_tier_refuses_a_rebound_peer(ctx, monkeypatch):
    up, furl, _ = ctx
    port = httpx.URL(furl).port
    seen = rebinding(monkeypatch, port)
    srv, aurl = app(ctx, None)
    try:
        r = scrape(aurl, f"http://rebind.example:{port}/p/article")
        assert len(seen) >= 2, (seen, r.text)
        assert r.status_code == 422 and "private" in r.json()["error"], r.text
    finally:
        srv.should_exit = True


# ---------------------------------------------------------------- no query strings in refusal logs
def test_egress_refusals_logged_without_query(capsys):
    async def go():
        e, o, oport, pport, srvs = await proxy_and_origin()
        await ask(pport, b"GET http://10.0.0.1/p/a?token=SECRET123&x=1 HTTP/1.1\r\n\r\n")
        await ask(pport, b"GET http://10.0.0.1:8080/b#frag HTTP/1.1\r\n\r\n")
        return e
    e = run(go())
    assert "SECRET123" not in "".join(e.recent) and "SECRET123" not in capsys.readouterr().out
    assert any("http://10.0.0.1:80/p/a" in x for x in e.recent), list(e.recent)
    assert any("http://10.0.0.1:8080/b" in x and "frag" not in x for x in e.recent)


# ---------------------------------------------------------------- pinning, redirects and per-class keys
def v6_server():
    """A dual-stack private listener that records what reaches it."""
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", 0))
    s.listen(8)
    got = []

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            c.settimeout(1)
            try:
                got.append(c.recv(4096))
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 40\r\n\r\n"
                          b"<html><body><p>secret</p></body></html>\n")
            except OSError:
                pass
            c.close()
    threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1], got


def rebind_to(monkeypatch, fam, addr):
    real = asyncio.base_events.BaseEventLoop.getaddrinfo
    seen = []

    async def fake(self, host, *a, **k):
        if host in ("rebind.example", b"rebind.example"):
            seen.append(host)
            if len(seen) == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", addr[1]))]
            return [(fam, socket.SOCK_STREAM, 6, "", addr)]
        return await real(self, host, *a, **k)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake)
    return seen


@pytest.mark.parametrize("peer", ["::1", "::ffff:127.0.0.1"])
def test_plain_tier_refuses_ipv6_and_v4_mapped_peers(ctx, monkeypatch, peer):
    """an IPv6 loopback or v4-mapped peer is refused before a request byte is sent."""
    s, port, got = v6_server()
    seen = rebind_to(monkeypatch, socket.AF_INET6, (peer, port, 0, 0))
    srv, aurl = app(ctx, None)
    try:
        r = scrape(aurl, f"http://rebind.example:{port}/p/x?tok=1")
        assert len(seen) >= 2, r.text
        assert r.status_code == 422 and "private" in r.json()["error"], r.text
        assert all(g == b"" for g in got), got
    finally:
        srv.should_exit = True
        s.close()


def test_plain_tier_missing_peer_fails_closed(ctx, monkeypatch):
    """no peer address to check is not provably public."""
    import httpcore._backends.anyio as ab
    real = ab.AnyIOStream.get_extra_info
    monkeypatch.setattr(ab.AnyIOStream, "get_extra_info",
                        lambda self, k: None if k == "server_addr" else real(self, k))
    up, furl, _ = ctx
    srv, aurl = app(ctx, None)
    try:
        r = scrape(aurl, furl.replace("127.0.0.1", "localhost") + "/p/article")
        assert r.status_code == 422 and "private" in r.json()["error"], r.text
    finally:
        srv.should_exit = True


@pytest.mark.slow
def test_curl_tier_pins_the_checked_address(ctx, monkeypatch):
    """curl's own resolver says 127.0.0.1 after fastweb's check said public. curl is pinned to the checked
    address, so the private listener receives 0 request bytes (it used to get a full blind GET)."""
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession

    from fastweb_firecrawl.cache import HOST_PREFIX, NEEDS_RENDER

    from .test_fetch_ladder import make
    s, port, got = v6_server()
    real = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def fake(self, host, *a, **k):
        if host in ("rebind.example", b"rebind.example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]
        return await real(self, host, *a, **k)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake)
    init = AsyncSession.__init__

    def patched(self, *a, **kw):
        init(self, *a, curl_options={CurlOpt.RESOLVE: [f"rebind.example:{port}:127.0.0.1"]}, **kw)
    monkeypatch.setattr(AsyncSession, "__init__", patched)
    server = fakeredis.FakeServer()
    run(fakeredis.FakeAsyncRedis(server=server).set(HOST_PREFIX + "rebind.example", NEEDS_RENDER))
    up, furl, _ = ctx
    (srv, aurl), _ = make(furl, impersonate=True, impersonator=None, reprobe_rate=0.0,
                          redis=fakeredis.FakeAsyncRedis(server=server))
    try:
        r = scrape(aurl, f"http://rebind.example:{port}/admin/delete?id=7&token=abc", timeout=3000)
        assert r.status_code != 200 and "secret" not in r.text, r.text
        time.sleep(0.2)
        assert all(g == b"" for g in got), got
    finally:
        srv.should_exit = True
        s.close()


class Redir(http.server.BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", Redir.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_hostile_host_cannot_poison_victim_key(ctx, cdp):
    """evil 302s the plain tier to the victim's SPA page; the browser renders evil's own small page and loses.
    The loss must not land on the victim's key."""
    up, furl, _ = ctx
    two_spa_pages()
    hs = http.server.HTTPServer(("127.0.0.1", 0), Redir)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    Redir.target = furl + "/p/spa_seo"
    evil = f"http://localhost:{hs.server_address[1]}/x/evil"
    cdp.pages[evil] = cdp.scenario(html="<html><body><p>hi</p></body></html>")
    victim = furl + "/p/spa_seo2"
    cdp.pages[victim] = cdp.scenario(html=SPA_FULL)
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis(),
                    allow_hosts=frozenset({"127.0.0.1", "localhost"}))
    try:
        scrape(aurl, evil)
        assert scrape(aurl, victim).json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
    finally:
        srv.should_exit = True
        hs.shutdown()


def test_control_victim_wins_alone(ctx, cdp):
    up, furl, _ = ctx
    two_spa_pages()
    cdp.pages[furl + "/p/spa_seo2"] = cdp.scenario(html=SPA_FULL)
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis())
    try:
        assert scrape(aurl, furl + "/p/spa_seo2").json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
    finally:
        srv.should_exit = True


@pytest.mark.slow
def test_truncated_budget_render_is_not_a_loss(ctx, cdp):
    """a short-timeout caller's truncated render (the SEO shell so far) must not suppress the winner."""
    up, furl, _ = ctx
    two_spa_pages()
    for p in ("spa_seo", "spa_seo2"):
        cdp.pages[furl + "/p/" + p] = cdp.scenario(timeline=[(0, SPA_SEO), (1500, SPA_FULL)], xhr=[(0, 1550)])
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis(), browser_timeout=2.5,
                    browser_settle=2.4)
    try:
        scrape(aurl, furl + "/p/spa_seo", timeout=2300)
        assert scrape(aurl, furl + "/p/spa_seo2").json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
    finally:
        srv.should_exit = True


def test_control_hydrating_winner_wins_with_normal_budget(ctx, cdp):
    up, furl, _ = ctx
    two_spa_pages()
    cdp.pages[furl + "/p/spa_seo2"] = cdp.scenario(timeline=[(0, SPA_SEO), (1500, SPA_FULL)], xhr=[(0, 1550)])
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis(), browser_timeout=2.5,
                    browser_settle=2.4)
    try:
        assert scrape(aurl, furl + "/p/spa_seo2").json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
    finally:
        srv.should_exit = True


def test_nav_failed_render_is_not_a_loss(ctx, cdp):
    """net::ERR_* in the browser is transient, not an hour-long loss."""
    up, furl, _ = ctx
    two_spa_pages()
    cdp.pages[furl + "/p/spa_seo"] = cdp.scenario(nav_error="net::ERR_CONNECTION_RESET")
    cdp.pages[furl + "/p/spa_seo2"] = cdp.scenario(html=SPA_FULL)
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis())
    try:
        assert scrape(aurl, furl + "/p/spa_seo").json()["data"]["metadata"]["fastweb"]["engine"] == "plain"
        assert scrape(aurl, furl + "/p/spa_seo2").json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
    finally:
        srv.should_exit = True


def test_loss_key_is_host_and_path_class_in_the_app(ctx, cdp):
    """the app writes the per-class key (/p/x → class "p"), not a host-wide one."""
    up, furl, _ = ctx
    two_spa_pages()
    cdp.pages[furl + "/p/spa_seo"] = cdp.scenario(html=SPA_SEO)
    r = fakeredis.FakeAsyncRedis()
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=r)
    try:
        scrape(aurl, furl + "/p/spa_seo")
        keys = {k.decode() for k in run(r.keys("fastweb:suspect_loss:*"))}
        assert keys == {"fastweb:suspect_loss:127.0.0.1/p"}, keys
    finally:
        srv.should_exit = True


def test_a_win_clears_a_loss_set_meanwhile(ctx, cdp):
    """a loss written by a concurrent request while this render runs is cleared by its win."""
    up, furl, _ = ctx
    two_spa_pages()
    cdp.pages[furl + "/p/spa_seo"] = cdp.scenario(html=SPA_FULL, nav_delay=0.8)
    server = fakeredis.FakeServer()
    srv, aurl = app(ctx, cdp, browser_suspect=True, redis=fakeredis.FakeAsyncRedis(server=server))
    out = {}
    try:
        t = threading.Thread(target=lambda: out.update(r=scrape(aurl, furl + "/p/spa_seo")))
        t.start()
        time.sleep(0.4)
        run(fakeredis.FakeAsyncRedis(server=server).set("fastweb:suspect_loss:127.0.0.1/p", "1"))
        t.join()
        assert out["r"].json()["data"]["metadata"]["fastweb"]["engine"] == "browser"
        assert not run(fakeredis.FakeAsyncRedis(server=server).exists("fastweb:suspect_loss:127.0.0.1/p"))
    finally:
        srv.should_exit = True
