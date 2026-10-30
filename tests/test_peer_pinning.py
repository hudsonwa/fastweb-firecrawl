"""Connected-peer pinning: curl pin key, concurrency and cross-host/port redirects."""
import asyncio
import socket
import threading
import time

import fakeredis
import pytest

from .test_api import scrape
from .test_browser_tier import run


def listener(fam, host, port=0, script=None, delay=0.0):
    """Records (host header, path) of what reaches it; script(path) -> (status, location|None)."""
    s = socket.socket(fam, socket.SOCK_STREAM)
    if fam == socket.AF_INET6:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(64)
    got = []

    def handle(c):
        c.settimeout(3)
        try:
            d = c.recv(65536)
            got.append(d)
            lines = d.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1] if lines and " " in lines[0] else "/"
            hh = next((ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("host:")), "")
            time.sleep(delay)
            st, loc = script(path) if script else (200, None)
            if loc:
                c.sendall(f"HTTP/1.1 {st} Found\r\nLocation: {loc}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            else:
                body = (f"<html><head><title>{hh}{path}</title></head><body><article><h1>Served {hh} {path}</h1>"
                        + f"<p>This page was served for host {hh} and path {path} by family {fam.name}. " * 12
                        + "</p></article></body></html>").encode()
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                          + str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            pass
        c.close()

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1], got


def fake_dns(monkeypatch, table):
    real = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def fake(self, host, port, *a, **k):
        h = host.decode() if isinstance(host, bytes) else host
        if h in table:
            fam, ip = table[h]
            return [(fam, socket.SOCK_STREAM, 6, "", (ip, port) if fam == socket.AF_INET else (ip, port, 0, 0))]
        return await real(self, host, port, *a, **k)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake)


def curl_resolver(monkeypatch, entries):
    """Simulate curl's own DNS answer (the rebound one) for the names curl looks up. fastweb's pin is appended
    after it, so if fastweb pins the same host:port key, its entry wins (later RESOLVE entries replace)."""
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession
    real = AsyncSession.request

    async def request(self, *a, **kw):
        pins = list(self.curl_options.get(CurlOpt.RESOLVE, []))
        self.curl_options = {**self.curl_options, CurlOpt.RESOLVE: list(entries) + pins}
        return await real(self, *a, **kw)
    monkeypatch.setattr(AsyncSession, "request", request)


def imp_app(ctx, hosts, **kw):
    from fastweb_firecrawl.cache import HOST_PREFIX, NEEDS_RENDER

    from .test_fetch_ladder import make
    server = fakeredis.FakeServer()
    for h in hosts:
        run(fakeredis.FakeAsyncRedis(server=server).set(HOST_PREFIX + h, NEEDS_RENDER))
    up, furl, _ = ctx
    (srv, aurl), _ = make(furl, impersonate=True, impersonator=None, reprobe_rate=0.0,
                          redis=fakeredis.FakeAsyncRedis(server=server), **kw)
    return srv, aurl


@pytest.mark.parametrize("name,lookup", [("rebind.example", "rebind.example"),
                                         ("xn--tda.rebind.example", "xn--tda.rebind.example"),
                                         ("rebind.example.", "rebind.example.")])
@pytest.mark.slow
def test_curl_pin_vs_rebound_resolver(ctx, monkeypatch, name, lookup):
    """fastweb's check sees a public address; curl's own resolver answers 127.0.0.1. With a working pin the private
    listener gets 0 bytes."""
    s, port, got = listener(socket.AF_INET, "127.0.0.1")
    fake_dns(monkeypatch, {name: (socket.AF_INET, "93.184.215.14"), name.rstrip("."): (socket.AF_INET, "93.184.215.14"),
                           "ü.rebind.example": (socket.AF_INET, "93.184.215.14")})
    curl_resolver(monkeypatch, [f"{lookup}:{port}:127.0.0.1"])
    srv, aurl = imp_app(ctx, [name.rstrip("."), "ü.rebind.example"])
    try:
        r = scrape(aurl, f"http://{name}:{port}/admin/delete?id=7&token=abc", timeout=3000)
        time.sleep(0.3)
        print(name, r.status_code, r.text[:160], "PEER GOT:", got)
        assert "secret" not in r.text
        assert all(g == b"" for g in got), got
    finally:
        srv.should_exit = True
        s.close()


def test_concurrent_pins_do_not_cross(ctx, monkeypatch):
    """hosta.test -> 127.0.0.1 only, hostb.test -> ::1 only, on the same two ports. Chains redirect
    a:P1 -> b:P2 -> a:P2 (and b:P1 -> a:P2 -> b:P2). 40 scrapes at once. Any hop that runs with the other request's
    RESOLVE list either fails (curl can't resolve *.test) or lands on the wrong family's listener."""
    from fastweb_firecrawl import guard
    monkeypatch.setattr(guard, "is_private_ip", lambda a: False)  # loopback counts as "public" here

    def script_for(me, other):
        def script(path):
            if path.startswith("/s/"):  # start: -> other host, port 2
                return 302, f"http://{other}:{P2}/m/{path[3:]}"
            if path.startswith("/m/"):  # middle: -> back to the first host, port 2
                return 302, f"http://{other}:{P2}/e/{path[3:]}"
            return 200, None
        return script
    a1, P1, ga1 = listener(socket.AF_INET, "127.0.0.1", 0, script_for("hosta.test", "hostb.test"), delay=0.05)
    # bind port 2 for both families, then port 1 for v6
    a2, P2, ga2 = listener(socket.AF_INET, "127.0.0.1", 0, script_for("hosta.test", "hostb.test"), delay=0.05)
    b1, _, gb1 = listener(socket.AF_INET6, "::1", P1, script_for("hostb.test", "hosta.test"), delay=0.05)
    b2, _, gb2 = listener(socket.AF_INET6, "::1", P2, script_for("hostb.test", "hosta.test"), delay=0.05)
    fake_dns(monkeypatch, {"hosta.test": (socket.AF_INET, "127.0.0.1"), "hostb.test": (socket.AF_INET6, "::1")})
    srv, aurl = imp_app(ctx, ["hosta.test", "hostb.test"], total_timeout=20.0)
    from concurrent.futures import ThreadPoolExecutor
    jobs = [(f"http://{'hosta' if i % 2 == 0 else 'hostb'}.test:{P1}/s/{i}") for i in range(40)]
    try:
        with ThreadPoolExecutor(40) as ex:
            res = list(ex.map(lambda u: scrape(u and aurl, u, timeout=30000), jobs))
        ok = sum(r.status_code == 200 for r in res)
        engines = {r.json().get("data", {}).get("metadata", {}).get("fastweb", {}).get("engine") for r in res
                   if r.status_code == 200}
        print("ok", ok, "/", len(jobs), engines, [r.text[:120] for r in res if r.status_code != 200][:3])
        def hosts(g):
            out = []
            for d in g:
                for ln in d.decode("latin-1").split("\r\n"):
                    if ln.lower().startswith("host:"):
                        out.append(ln.split(":", 1)[1].strip().split(":")[0])
            return out
        v4 = hosts(ga1) + hosts(ga2)
        v6 = hosts(gb1) + hosts(gb2)
        print("v4 hosts", sorted(set(v4)), len(v4), "v6 hosts", sorted(set(v6)), len(v6))
        assert set(v4) == {"hosta.test"} and set(v6) == {"hostb.test"}
        assert len(v4) + len(v6) == 3 * len(jobs)
        assert ok == len(jobs)
        for i, r in enumerate(res):  # each caller got its own chain's final page
            me = "hosta.test" if i % 2 == 0 else "hostb.test"
            assert f"Served {me}:{P2} /e/{i}" in r.text, r.text[:300]
    finally:
        srv.should_exit = True
        for x in (a1, a2, b1, b2):
            x.close()
