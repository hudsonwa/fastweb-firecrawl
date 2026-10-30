"""Redis helpers (fastweb's own Redis): per-host fallback memory and the scrape cache.
Every failure degrades to "no cache"."""
import asyncio
import hashlib
import json
import struct
import time
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastweb_firecrawl.extract import page_links

HOST_PREFIX = "fastweb:host:"
OBS_PREFIX = "fastweb:hostobs:"   # sorted set of target-side refusals: member "path|ts", score ts
SCRAPE_PREFIX = "fastweb:scrape:"
SUSPECT_PREFIX = "fastweb:suspect_loss:"  # the suspect-SPA render did not beat the plain page here
PF_PREFIX = "fastweb:pf:"  # marker next to a scrape entry written by prefetch (attribution of later hits)
DEAD = "dead"            # target refused every tier repeatedly -> skip Firecrawl, cheap tiers only
# The cheap tiers are refused here: skip plain, go to the browser (or Firecrawl). The stored value stays
# "firecrawl" (it predates the browser tier) so existing Redis entries keep their meaning.
NEEDS_RENDER = "firecrawl"
RETRY_AFTER = 30.0       # seconds to leave Redis alone after an error
MAX_ENTRY = 1024 * 1024  # compressed bytes
DEAD_MIN_OBS, DEAD_MIN_PATHS, DEAD_MIN_SPAN = 3, 2, 600
# Body keys that never change what the page looks like (firecrawl-py 4.17 sends all of these).
NEUTRAL_KEYS = {"origin", "integration", "maxAge", "storeInCache", "skipTlsVerification", "removeBase64Images",
                "blockAds", "fastMode", "timeout", "zeroDataRetention"}
KEYED = {"url", "formats", "onlyMainContent"}
# Formats served from one stored document: requests asking only for these share one cache key per (url,
# onlyMainContent), and each gets its own projection of the stored page (markdown, html and rawHtml).
CONTENT_FORMATS = frozenset({"markdown", "html", "rawHtml"})
BIG_PAYLOAD = 512 * 1024  # payloads over this are compressed at zlib level 1
ENTRY_MAGIC = b"FW1"     # entry = magic + stored-at (float64, epoch s) + zlib(payload)


def may_store(body: dict[str, Any]) -> bool:
    """storeInCache:false and zeroDataRetention:true forbid keeping the page."""
    return body.get("storeInCache", True) is not False and body.get("zeroDataRetention") is not True


def fresh_enough(body: dict[str, Any], age_s: float) -> bool:
    """maxAge (ms) bounds the age of a served entry; maxAge 0 means a fresh fetch."""
    m = body.get("maxAge")
    if isinstance(m, bool) or not isinstance(m, (int, float)):
        return True
    return age_s * 1000 <= m


def is_dead(observations: list[tuple[str, float]]) -> bool:
    """observations: [(path, ts)]. DEAD needs >= 3 refusals on >= 2 paths spread over >= 10 min."""
    if len(observations) < DEAD_MIN_OBS or len({p for p, _ in observations}) < DEAD_MIN_PATHS:
        return False
    ts = [t for _, t in observations]
    return max(ts) - min(ts) >= DEAD_MIN_SPAN


def scrape_key_for_request(body: dict[str, Any]) -> str | None:
    """Cache key for a /scrape body, or None when the body asks for anything that changes content."""
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        return None
    for k, v in body.items():
        if k in KEYED or k in NEUTRAL_KEYS or (k == "mobile" and not v):
            continue
        return None
    fmts = body.get("formats") or ["markdown"]
    if not isinstance(fmts, list):
        return None
    names = []
    for f in fmts:
        if isinstance(f, dict):
            if set(f) != {"type"}:
                return None
            f = f["type"]
        if not isinstance(f, str):
            return None
        names.append(f)
    # Only content formats are produced (others are accepted and ignored), and one entry serves any subset of them.
    raw = json.dumps([body["url"], "content", bool(body.get("onlyMainContent", True))], separators=(",", ":"))
    return SCRAPE_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def wanted_formats(formats: list[str]) -> set[str]:
    """The content keys an answer for `formats` carries: those asked for, and markdown when no other content format
    was asked for (Firecrawl's default)."""
    want = {f for f in formats if f in CONTENT_FORMATS}
    if not want & {"html", "rawHtml"}:
        want.add("markdown")
    return want


def available_formats(doc: dict[str, Any]) -> set[str]:
    """The content formats a stored document can serve. fastweb's own tiers store the page markup once as `html`;
    for them rawHtml is the same markup. A Firecrawl answer serves exactly the keys it carries."""
    data = doc.get("data") or {}
    have = {f for f in CONTENT_FORMATS if isinstance(data.get(f), str) and data[f].strip()}
    engine = ((data.get("metadata") or {}).get("fastweb") or {}).get("engine")
    if "html" in have and engine != "firecrawl":
        have.add("rawHtml")
    return have


def project(doc: dict[str, Any], formats: list[str]) -> dict[str, Any] | None:
    """The answer for `formats` from a stored document, or None when the document lacks a requested content format.
    Only the content keys change; anything else (metadata, links, ...) is passed through."""
    want, have = wanted_formats(formats), available_formats(doc)
    if not want <= have:
        return None
    data = dict(doc["data"])
    markup = data.get("html") or data.get("rawHtml")
    if "links" in formats and "links" not in data and isinstance(markup, str) and markup:
        meta0 = data.get("metadata")
        meta0 = meta0 if isinstance(meta0, dict) else {}
        page = next((meta0[k] for k in ("url", "sourceURL") if isinstance(meta0.get(k), str) and meta0[k]), "")
        data["links"] = page_links(markup, page)
    if "rawHtml" in want and "rawHtml" in have and "rawHtml" not in data:
        data["rawHtml"] = data.get("html")
    for f in CONTENT_FORMATS - want:
        data.pop(f, None)
    ignored = sorted({f for f in formats if f not in CONTENT_FORMATS and f not in data})
    meta = data.get("metadata")
    if ignored and isinstance(meta, dict) and isinstance(meta.get("fastweb"), dict):
        # requested formats fastweb does not produce (links, screenshot, ...): say so instead of a silent None
        data["metadata"] = {**meta, "fastweb": {**meta["fastweb"], "ignored_formats": ignored}}
    return {**doc, "data": data}


class Store:
    """fastweb's Redis state: scrape cache entries, per-host memory, suspect-SPA losses. redis None = no state."""

    def __init__(self, redis: Any, host_ttl: int = 3600, needs_render_ttl: int = 1800, scrape_ttl: int = 600,
                 max_entry: int = MAX_ENTRY,
                 clock: Callable[[], float] = time.time):
        self.redis, self.host_ttl, self.needs_render_ttl = redis, host_ttl, needs_render_ttl
        self.scrape_ttl, self.max_entry, self.clock = scrape_ttl, max_entry, clock
        self.down_until = 0.0

    async def _call(self, fn: Callable[..., Awaitable[Any]], *a: Any, **kw: Any) -> Any:
        if self.redis is None or time.monotonic() < self.down_until:
            return None
        try:
            return await fn(*a, **kw)
        except Exception:  # noqa: BLE001 - any Redis failure degrades to no cache for RETRY_AFTER s
            self.down_until = time.monotonic() + RETRY_AFTER
            return None

    async def host(self, host: str) -> str | None:
        v = await self._call(self.redis.get, HOST_PREFIX + host) if self.redis is not None else None
        return v.decode() if isinstance(v, bytes) else v

    async def set_needs_render(self, host: str) -> None:
        if self.redis is not None:
            await self._call(self.redis.set, HOST_PREFIX + host, NEEDS_RENDER, ex=self.needs_render_ttl)

    async def observe_block(self, host: str, path: str) -> bool:
        """Record one target-side refusal; mark the host DEAD when the rule holds. Returns True if DEAD."""
        if self.redis is None:
            return False
        now, key = self.clock(), OBS_PREFIX + host

        async def go() -> bool:
            await self.redis.zadd(key, {f"{path}|{now}": now})
            await self.redis.zremrangebyscore(key, "-inf", now - self.host_ttl)
            await self.redis.expire(key, self.host_ttl)
            rows = await self.redis.zrange(key, 0, -1, withscores=True)
            obs = [((m.decode() if isinstance(m, bytes) else m).rsplit("|", 1)[0], t) for m, t in rows]
            if is_dead(obs):
                await self.redis.set(HOST_PREFIX + host, DEAD, ex=self.host_ttl)
                return True
            return False
        return bool(await self._call(go))

    async def forget_host(self, host: str) -> None:
        if self.redis is not None:
            await self._call(self.redis.delete, HOST_PREFIX + host, OBS_PREFIX + host)

    # key: host + path class (up to two parent directories): weatherzone's /au/wa/ pages lose the render, /au/nsw/ win
    async def suspect_lost(self, host: str, cls: str = "") -> bool:
        if self.redis is None:
            return False
        return bool(await self._call(self.redis.exists, f"{SUSPECT_PREFIX}{host}/{cls}"))

    async def set_suspect_loss(self, host: str, cls: str = "") -> None:
        if self.redis is not None:
            await self._call(self.redis.set, f"{SUSPECT_PREFIX}{host}/{cls}", "1", ex=self.host_ttl)

    async def clear_suspect_loss(self, host: str, cls: str = "") -> None:
        if self.redis is not None:
            await self._call(self.redis.delete, f"{SUSPECT_PREFIX}{host}/{cls}")

    async def get_scrape(self, key: str) -> tuple[bytes, float] | None:
        """(payload, age in seconds) or None."""
        if self.redis is None or self.scrape_ttl <= 0:
            return None
        v = await self._call(self.redis.get, key)
        if not isinstance(v, bytes) or not v.startswith(ENTRY_MAGIC) or len(v) < 11:
            return None
        (ts,) = struct.unpack(">d", v[3:11])
        try:
            return zlib.decompress(v[11:]), max(0.0, self.clock() - ts)
        except zlib.error:
            return None

    async def has_scrape(self, key: str) -> bool:
        if self.redis is None or self.scrape_ttl <= 0:
            return False
        return bool(await self._call(self.redis.exists, key))

    async def put_scrape(self, key: str, payload: bytes) -> bool:
        """True when the entry was written."""
        if self.redis is None or self.scrape_ttl <= 0:
            return False
        level = 1 if len(payload) > BIG_PAYLOAD else 6  # level 6 costs ~65 ms on a 3 MB page
        z = await asyncio.to_thread(zlib.compress, payload, level)  # off the event loop
        if len(z) > self.max_entry:
            return False
        return bool(await self._call(self.redis.set, key, ENTRY_MAGIC + struct.pack(">d", self.clock()) + z,
                                     ex=self.scrape_ttl))

    async def mark_prefetched(self, key: str) -> None:
        """Mark a scrape entry as written by prefetch (for hit attribution in /v1/fastweb/stats)."""
        if self.redis is not None:
            await self._call(self.redis.set, PF_PREFIX + key, b"1", ex=self.scrape_ttl)

    async def is_prefetched(self, key: str) -> bool:
        if self.redis is None:
            return False
        return bool(await self._call(self.redis.exists, PF_PREFIX + key))
