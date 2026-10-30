"""SSRF guard for the cheap tiers: is this URL's host public, and did we really connect to a public address?

The address rule itself (`is_private_ip`) lives in egress.py, so fastweb and the browser's egress proxy share one
definition of "private".
"""
import asyncio
import ipaddress
import socket

import httpx

from .egress import is_private_ip
from .errors import Fail

# Compose service names and Docker's own host aliases: refused by name, whatever they resolve to.
INTERNAL_NAMES = frozenset({"api", "redis", "searxng", "nuq-postgres", "rabbitmq", "playwright-service", "fastweb",
                            "browser", "browser-egress", "firecrawl-egress", "fastweb-redis", "firecrawl-redis",
                            "host.docker.internal", "gateway.docker.internal", "localhost"})
PRIVATE_ERROR = "URL targets a private or internal network address"

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class TargetGuard:
    """Refuses URLs whose host is internal. `allow_hosts` (tests only) are exempt."""

    def __init__(self, allow_hosts: frozenset[str] = frozenset()):
        self.allow_hosts = allow_hosts

    async def check(self, url: str) -> Address | None:
        """Refuse private targets (raises Fail). Returns the public address it approved (None for allow_hosts), so a
        tier that resolves on its own can be pinned to it."""
        try:
            u = httpx.URL(url)
        except (httpx.InvalidURL, ValueError):
            raise Fail(400, "Invalid URL") from None
        if u.scheme not in ("http", "https") or not u.host:
            raise Fail(400, "URL must be http(s) with a host")
        host = u.host.lower().rstrip(".")
        if host in self.allow_hosts:
            return None
        if host in INTERNAL_NAMES or host.endswith((".localhost", ".internal", ".local")):
            raise Fail(422, PRIVATE_ERROR)
        addrs: list[Address]
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or 443, type=socket.SOCK_STREAM)
            except (OSError, UnicodeError):
                raise Fail(422, f"Could not resolve host {host}", transient=True) from None
            addrs = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
        if not addrs or any(is_private_ip(a) for a in addrs):
            raise Fail(422, PRIVATE_ERROR)
        return addrs[0]

    async def allows(self, url: str) -> bool:
        """check() as a yes/no (the browser asks this for every request a page makes)."""
        try:
            await self.check(url)
            return True
        except Fail:
            return False

    def check_peer(self, url: str, ip: object) -> None:
        """The address a cheap tier actually connected to: its resolver ran again after check(), so a rebinding name
        could have swapped a public answer for a private one in between. The plain tier checks at connect_tcp (0
        request bytes sent); curl is pinned to check()'s address, so this is a backstop there.
        Residual: for a rebinding name, "private" vs "connection refused" still tells an open private port from a
        closed one."""
        host = (httpx.URL(url).host or "").lower().rstrip(".")
        if host in self.allow_hosts:
            return
        try:
            bad = is_private_ip(ipaddress.ip_address(str(ip).split("%")[0]))
        except ValueError:
            bad = True  # no peer address to show: not provably public
        if bad:
            raise Fail(422, PRIVATE_ERROR)
