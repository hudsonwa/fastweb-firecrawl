"""Browser egress proxy: the browser container's only route out.

The browser sits on an internal docker network (no gateway); Chrome sends every request through this forward proxy
(--proxy-server). That covers what CDP interception does not see: workers, out-of-process iframes, popups, prefetch,
beacons, WebSocket. Per request: resolve the host, refuse it unless every address is public and the port is 80/443/
8080/8443, then connect to the checked address itself (no second resolution, so DNS rebinding cannot swap it) and
splice bytes with idle/total timeouts and a connection cap. `CONNECT host:port` (https, wss) and absolute-URI plain
HTTP (rewritten to origin form, `Connection: close`: one response per client connection). Either side's EOF closes
both, so a disposed page's tunnels free their slots at once. Refusals answer 403, are counted and
logged. Every answer of the proxy's own carries `X-Fastweb-Egress: refused|busy <token>`, so fastweb can tell the tier's
failure from the target's; the token is random per start and served only at `GET /token` here, so a target that
sends the header itself can't fake it blindly. Accepted residual: page JS
can learn the token, so a hostile page can fake the marker for its own load; that only turns its own result into a
browser-tier failure (busy/down -> Firecrawl).

`GET /stats` (origin form, i.e. not a proxied request) returns the counters as JSON: the healthcheck and tests use it.
EGRESS_ALLOW (comma-separated host:port, tests only; production compose never sets it) skips the checks for those.
"""
import argparse
import asyncio
import collections
import contextlib
import ipaddress
import json
import os
import secrets
import socket
import sys
import time
from collections.abc import Iterable
from typing import Any, TypedDict
from urllib.parse import urlsplit

PORTS = frozenset({80, 443, 8080, 8443})
# Forms that embed an IPv4 address (NAT64, IPv4-compatible, SIIT) are private whatever they embed
EMBEDS_V4 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96"))
SITE_LOCAL = ipaddress.ip_network("fec0::/10")  # deprecated, but still not the internet
HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


def is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in SITE_LOCAL or any(ip in n for n in EMBEDS_V4):
            return True
    return not ip.is_global or ip.is_multicast or ip.is_unspecified


class Refused(Exception):
    """A request the proxy refuses; answered with `status` and the X-Fastweb-Egress marker."""

    def __init__(self, why: str, status: int = 403):
        super().__init__(why)
        self.why, self.status = why, status


def split_hostport(s: str, default: int) -> tuple[str, int]:
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = s.rpartition(":") if s.count(":") == 1 else (s, "", "")
    try:
        return host.lower().rstrip("."), int(port) if port else default
    except ValueError:
        raise Refused("bad port", 400) from None


class Egress:
    """The proxy: per-connection handler, address and port checks, counters. `allow` (tests only) skips the checks
    for those host:port pairs."""

    def __init__(self, ports: Iterable[int] = PORTS, allow: Iterable[str] = (), idle: float = 30.0,
                 total: float = 300.0, max_conns: int = 1024, head_timeout: float = 10.0,
                 connect_timeout: float = 5.0):
        self.ports, self.allow = frozenset(ports), frozenset(a.lower() for a in allow)
        self.idle, self.total, self.max_conns = idle, total, max_conns
        self.head_timeout, self.connect_timeout = head_timeout, connect_timeout
        self.token = secrets.token_urlsafe(18)
        self.counters = {"allowed": 0, "refused": 0, "refused_private": 0, "refused_port": 0, "refused_bad": 0,
                         "busy": 0,
                  "errors": 0, "timeouts": 0, "internal": 0, "active": 0}
        self.recent: collections.deque[str] = collections.deque(maxlen=200)  # last refusals: "METHOD host:port path"

    def stats(self) -> dict[str, Any]:
        return {**self.counters, "recent_refusals": list(self.recent)}

    async def target(self, host: str, port: int) -> str:
        """The address to connect to: checked and pinned. Raises Refused."""
        if f"{host}:{port}" in self.allow:
            return host
        if port not in self.ports:
            raise Refused("port not allowed")
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM), self.connect_timeout)
            except (TimeoutError, OSError, UnicodeError):
                raise Refused("could not resolve host", 502) from None
            addrs = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
        if not addrs or any(is_private_ip(a) for a in addrs):
            raise Refused("private address")
        return str(addrs[0])

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # still read the head when full: GET /stats (the healthcheck) and /token are answered
        full = self.counters["active"] >= self.max_conns
        self.counters["active"] += 1
        try:
            await asyncio.wait_for(self._serve(reader, writer, full), self.total)
        except TimeoutError:  # the connection outlived its total budget
            self.counters["timeouts"] += 1
        except (ConnectionError, OSError):  # the client or target went away
            pass
        except Exception as e:  # noqa: BLE001 - a defect here closes only this connection, but is counted and logged
            self.counters["internal"] += 1
            log(event="egress_internal_error", error=type(e).__name__)
        finally:
            self.counters["active"] -= 1
            with contextlib.suppress(Exception):  # the loop is already closing (shutdown): nothing to flush
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, full: bool = False) -> None:
        """One client connection: read the request head, answer locally or refuse, else connect and relay.

        full: the connection cap is reached; only GET /stats and /token (healthcheck, browser token) are answered,
        everything else gets 503 busy. Invariants: the target is resolved and checked once (Egress.target) and the
        connection goes to that checked address, never to a name resolved again; every refusal is counted, logged
        with the URI's query redacted, and answered with the proxy's token so the browser can tell it from a page."""
        what = "?"
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                              min(self.head_timeout, 1.0) if full else self.head_timeout)
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                raise Refused("bad request", 400)
            method, uri, version = parts
            what = f"{method} {redact(uri)[:200]}"
            if method == "GET" and uri in ("/stats", "/token"):
                await self._local(writer, uri)
                return
            if full:
                self.counters["busy"] += 1
                await self._answer(writer, 503, "busy")
                return
            host, port, out = self._target_of(method, uri, version, lines[1:])
            ip = await self.target(host, port)
            try:
                r2, w2 = await asyncio.wait_for(asyncio.open_connection(ip, port), self.connect_timeout)
            except (TimeoutError, OSError):
                raise Refused("connect failed", 502) from None
        except Refused as e:
            self.counters["refused"] += 1
            self.counters["refused_private" if e.why == "private address" else
                          "refused_port" if e.why == "port not allowed" else "refused_bad"] += 1
            self.recent.append(what)
            log(egress="refused", why=e.why, req=what)
            await self._answer(writer, e.status, e.why)
            return
        self.counters["allowed"] += 1
        await self._relay(reader, writer, r2, w2, out)

    @staticmethod
    def _target_of(method: str, uri: str, version: str, headers: list[str]) -> tuple[str, int, bytes | None]:
        """(host, port, request to send) for CONNECT (None: the tunnel answers 200 itself) or an absolute-URI
        http:// request, rewritten to origin form without hop-by-hop headers and with Connection: close."""
        if method == "CONNECT":
            host, port = split_hostport(uri, 443)
            return host, port, None
        if not uri.startswith("http://"):
            raise Refused("bad request", 400)
        u = urlsplit(uri)
        if not u.hostname:
            raise Refused("bad request", 400)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        kept = [h for h in headers if h and h.split(":", 1)[0].strip().lower() not in HOP]
        out = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""]).encode("latin-1")
        return u.hostname.lower().rstrip("."), u.port or 80, out

    async def _local(self, writer: asyncio.StreamWriter, uri: str) -> None:
        """The proxy's own endpoints: /stats (JSON counters) and /token."""
        body = json.dumps(self.stats()).encode() if uri == "/stats" else self.token.encode()
        ctype = b"application/json" if uri == "/stats" else b"text/plain"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype + b"\r\nConnection: close\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, r2: asyncio.StreamReader,
                     w2: asyncio.StreamWriter, out: bytes | None) -> None:
        """Pump bytes both ways until either side ends (or idles out); then both are closed."""
        try:
            if out is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                w2.write(out)
            pumps = [asyncio.ensure_future(self._pump(reader, w2)), asyncio.ensure_future(self._pump(r2, writer))]
            done, rest = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)  # either EOF ends both
            for p in rest:
                p.cancel()
            if any(not p.cancelled() and p.exception() for p in done):
                self.counters["errors"] += 1
        finally:
            w2.close()

    async def _pump(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        while True:
            data = await asyncio.wait_for(r.read(65536), self.idle)
            if not data:
                return
            w.write(data)
            await w.drain()

    async def _answer(self, writer: asyncio.StreamWriter, status: int, why: str) -> None:
        body = f"fastweb egress: {why}\n".encode()
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway", 503: "Service Unavailable"}[status]
        mark = "busy" if status == 503 else "refused"
        try:
            writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                         f"X-Fastweb-Egress: {mark} {self.token}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:  # noqa: BLE001 - the client went away while we answered
            pass


def redact(uri: str) -> str:
    """scheme://host:port/path of an absolute URI, no query or fragment: refusals are logged and kept in /stats, and
    query strings carry tokens. Anything else (CONNECT host:port, origin form) loses the same."""
    if "://" not in uri:
        return uri.split("?", 1)[0].split("#", 1)[0]
    try:
        u = urlsplit(uri)
        port: int | str = u.port or {"http": 80, "https": 443}.get(u.scheme) or ""
        return f"{u.scheme}://{u.hostname or ''}:{port}{u.path or '/'}"
    except ValueError:
        return uri.split("://", 1)[0] + "://?"


def log(**kw: Any) -> None:
    print(json.dumps({"t": round(time.time(), 3), **kw}, separators=(",", ":")), file=sys.stdout, flush=True)


async def serve(egress: Egress, host: str = "127.0.0.1", port: int = 3128) -> asyncio.Server:
    return await asyncio.start_server(egress.handle, host, port, limit=32 * 1024)


class Config(TypedDict):
    allow: list[str]
    max_conns: int
    port: int


def config(env: dict[str, str] | os._Environ[str]) -> Config:
    return {"allow": [a.strip() for a in env.get("EGRESS_ALLOW", "").split(",") if a.strip()],
            "max_conns": int(env.get("EGRESS_MAX_CONNS", 1024)), "port": int(env.get("EGRESS_PORT", 3128))}


async def main(host: str = "127.0.0.1", port: int | None = None, max_conns: int | None = None) -> None:
    c = config(os.environ)
    port = c["port"] if port is None else port
    max_conns = c["max_conns"] if max_conns is None else max_conns
    e = Egress(allow=c["allow"], max_conns=max_conns)
    srv = await serve(e, host=host, port=port)
    log(egress="listening", host=host, port=port, allow=c["allow"], max_conns=max_conns)
    async with srv:
        await srv.serve_forever()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from . import __version__
    p = argparse.ArgumentParser(
        prog="fastweb-firecrawl-egress",
        description="fastweb's guarding forward proxy: the only route out for the browser (and optional Firecrawl). "
                    "Refuses private addresses and ports other than 80/443/8080/8443.")
    p.add_argument("--host", default=os.environ.get("EGRESS_HOST", "127.0.0.1"),
                   help="address to listen on (env EGRESS_HOST, default 127.0.0.1; the compose stack passes "
                        "0.0.0.0 inside its internal network)")
    p.add_argument("--port", type=int, default=None, help="port to listen on (env EGRESS_PORT, default 3128)")
    p.add_argument("--max-conns", type=int, default=None,
                   help="concurrent connections before answering 503 (env EGRESS_MAX_CONNS, default 1024)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def cli(argv: list[str] | None = None) -> None:
    """Console entry point (`fastweb-egress`, or `python -m fastweb_firecrawl.egress`)."""
    a = parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(a.host, a.port, a.max_conns))


if __name__ == "__main__":
    cli()
