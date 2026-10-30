"""The two cheap download tiers: a plain HTTP/2 GET (httpx) and a Chrome-impersonating GET (curl_cffi).

Both follow redirects by hand so every hop is checked by the SSRF guard, enforce the download cap, take the body's
bytes from the shared in-flight byte budget before reading it, and stop at the caller's deadline.
"""
import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from .errors import Busy, Fail
from .guard import TargetGuard
from .prefetch import PREFETCHING, prefetch_type_ok
from .settings import Settings
from .worker import Hold

CONNECT_TIMEOUT = 3.6  # survives Linux SYN retransmits at 1 s and 3 s; a dead port still fails < 4 s
NOCL_RESERVE = 256 * 1024  # a body without Content-Length reserves this up front, then grows as it arrives
MAX_HOPS = 10
REDIRECTS = (301, 302, 303, 307, 308)

# (url, deadline) -> (status, final_url, lower-cased headers, body)
Impersonator = Callable[..., Awaitable[tuple[int, str, dict[str, str], bytes]]]


def browser_headers(user_agent: str) -> dict[str, str]:
    """Request headers of a top-level Chrome navigation."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def left(deadline: float) -> float:
    """Seconds until a monotonic deadline (negative once it has passed)."""
    return deadline - time.monotonic()


class Fetcher:
    """Plain and impersonated GETs. On-demand fetches share `plain_sem`; prefetch has its own `pf_sem`, so it never
    takes an on-demand slot."""

    def __init__(self, settings: Settings, guard: TargetGuard):
        self.settings, self.guard = settings, guard
        self.plain_sem = asyncio.Semaphore(settings.plain_concurrency)
        self.pf_sem = asyncio.Semaphore(max(1, settings.prefetch_concurrency))
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(
            http2=True, follow_redirects=False, headers=browser_headers(self.settings.user_agent),
            timeout=httpx.Timeout(self.settings.total_timeout, connect=CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    def _sem(self) -> asyncio.Semaphore:
        return self.pf_sem if PREFETCHING.get() else self.plain_sem

    def _cap(self) -> int:
        return self.settings.prefetch_max_bytes if PREFETCHING.get() else self.settings.max_bytes

    def _precheck(self, status: int, headers: Mapping[str, str], cap: int) -> None:
        """Refuse before reading the body: a declared size over the cap, or (prefetch) a type it never keeps."""
        if int(headers.get("content-length") or 0) > cap:
            raise Fail(413, f"Target page exceeds {cap} bytes")
        if PREFETCHING.get() and status < 300 and not prefetch_type_ok(headers.get("content-type")):
            raise Fail(422, f"prefetch: unsupported content type {headers.get('content-type')}")

    async def reserve_body(self, hold: Hold, headers: Mapping[str, str], wait: float) -> None:
        """Take the byte budget for a body before reading it: its Content-Length, else a small amount that
        force() grows as chunks arrive, so a no-CL page doesn't wait for a whole cap inside plain_sem."""
        try:
            n = int(headers.get("content-length") or "")
        except ValueError:
            n = NOCL_RESERVE
        if not await hold.to(min(max(n, 0), self.settings.max_bytes), wait):
            raise Busy

    async def plain(self, url: str, deadline: float, hold: Hold, wait: float,
                    stage: dict[str, bool] | None = None) -> tuple[httpx.Response, bytes]:
        """GET with manual redirects; every hop's target is checked against private networks, and so is the peer
        each new connection reached. stage: a dict that gets "tls" once a TLS handshake has started."""
        if self.client is None:
            raise RuntimeError("Fetcher.start() was not called")
        client = self.client

        async def trace(event: str, info: dict[str, Any]) -> None:
            if event == "connection.connect_tcp.complete":
                peer = info["return_value"].get_extra_info("server_addr")
                try:
                    self.guard.check_peer(url, peer[0] if peer else "")
                except Fail:
                    await info["return_value"].aclose()  # don't leave the refused socket to GC
                    raise
            if event.startswith("connection.start_tls.") and stage is not None:
                stage["tls"] = True
        cap = self._cap()
        async with self._sem():
            for _ in range(MAX_HOPS + 1):
                await self.guard.check(url)
                async with client.stream("GET", url, extensions={"trace": trace}) as r:
                    loc = r.headers.get("location")
                    if r.status_code in REDIRECTS and loc:
                        url = str(r.url.join(loc))
                        continue
                    self._precheck(r.status_code, r.headers, cap)
                    await self.reserve_body(hold, r.headers, wait)
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        hold.force(len(buf))
                        if len(buf) > cap:
                            raise Fail(413, f"Target page exceeds {cap} bytes")
                        if time.monotonic() > deadline:
                            raise httpx.ReadTimeout("deadline")
                    await hold.to(len(buf), 0)
                    return r, bytes(buf)
            raise Fail(422, "Too many redirects")

    async def impersonated(self, url: str, deadline: float, hold: Hold | None = None,
                           wait: float = 0.0) -> tuple[int, str, dict[str, str], bytes]:
        """curl_cffi GET with a Chrome TLS/HTTP2 fingerprint; manual redirects re-checked like plain()."""
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession
        cap = self._cap()
        async with self._sem(), AsyncSession(impersonate="chrome") as sess:
            for _ in range(MAX_HOPS + 1):
                ip = await self.guard.check(url)
                if ip is not None:  # pin curl to the approved address: a rebound name gets 0 request bytes;
                    # keyed on the ASCII/punycode host curl looks up, not httpx's decoded IDN
                    u = httpx.URL(url)
                    a = f"[{ip}]" if ip.version == 6 else str(ip)
                    port = u.port or (443 if u.scheme == "https" else 80)
                    sess.curl_options = {**sess.curl_options,
                                         CurlOpt.RESOLVE: [f"{u.raw_host.decode('ascii')}:{port}:{a}"]}
                r = await sess.get(url, allow_redirects=False, stream=True, timeout=max(0.5, left(deadline)))
                try:
                    self.guard.check_peer(url, r.primary_ip)  # belt and braces behind the pin
                    loc = r.headers.get("location")
                    if r.status_code in REDIRECTS and loc:
                        url = str(httpx.URL(url).join(loc))
                        continue
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    self._precheck(r.status_code, headers, cap)
                    if hold is not None:
                        await self.reserve_body(hold, headers, wait)
                    buf = bytearray()
                    async for chunk in r.aiter_content():
                        buf += chunk
                        if hold is not None:
                            hold.force(len(buf))
                        if len(buf) > cap:
                            raise Fail(413, f"Target page exceeds {cap} bytes")
                        if time.monotonic() > deadline:
                            raise TimeoutError
                    return r.status_code, url, headers, bytes(buf)
                finally:
                    await r.aclose()
            raise Fail(422, "Too many redirects")
