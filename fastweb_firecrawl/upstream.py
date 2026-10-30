"""The optional last-resort tier: a self-hosted Firecrawl's /v2/scrape (FIRECRAWL_UPSTREAM).

Firecrawl fetches and renders the page itself, so fastweb's per-hop SSRF checks do not apply inside it: fastweb only
checks the URL it hands over and the URL Firecrawl ended on. The compose file contains this by putting Firecrawl on an
internal network whose only way out is a guarding egress proxy (see SECURITY.md).
"""
import asyncio
from typing import Any

import httpx

from .cache import wanted_formats
from .errors import Fail
from .extract import absolutize_links
from .fetchers import CONNECT_TIMEOUT, left
from .guard import PRIVATE_ERROR, TargetGuard
from .heuristics import CHALLENGE_MARKERS, safe_url
from .links import is_unsafe
from .settings import Settings

FIRECRAWL_WAIT_MS = 3000  # waitFor on thin fallbacks to Firecrawl
UPSTREAM_MARGIN = 1.0     # seconds kept back from the caller's budget for our own answer
# Scrape options passed through to Firecrawl; everything else in the caller's body is dropped.
PASSED_OPTIONS = ("url", "formats", "onlyMainContent", "waitFor", "headers", "includeTags", "excludeTags", "mobile")
# Request headers a caller may have Firecrawl send to the target. Host, hop-by-hop, proxy and forwarding headers are
# never passed on.
PASSED_HEADERS = frozenset({"user-agent", "accept", "accept-language", "referer", "cookie", "authorization",
                            "cache-control", "pragma"})
MAX_HEADER_VALUE = 4096
# Firecrawl metadata is an allow-list of page metadata: the fixed page keys below, plus the page's own <meta> tags,
# which Firecrawl copies under their names (og:/twitter:/article:/Dublin Core prefixes). Anything else, such as
# Firecrawl's run accounting (scrapeId, creditsUsed, proxyUsed, cacheState, ... and keys a later version adds), is
# dropped so its answers look like fastweb's.
PAGE_METADATA = frozenset({"title", "description", "language", "keywords", "robots", "url", "sourceURL",
                           "statusCode", "contentType", "favicon", "author", "publishedTime", "modifiedTime",
                           "viewport", "generator", "themeColor", "ogTitle", "ogDescription", "ogUrl", "ogImage",
                           "ogSiteName", "ogLocale", "ogLocaleAlternate", "ogAudio", "ogDeterminer", "ogVideo"})
PAGE_METADATA_PREFIXES = ("og:", "twitter:", "article:", "dc.", "dcterms.", "fb:", "al:")


def _unsafe_url_value(key: str, value: object) -> bool:
    """A URL-valued key (ogUrl, ogImage, og:image, favicon, ...) holding a javascript:/data:/vbscript: target."""
    k = key.lower()
    words = ("url", "image", "favicon", "video", "audio", "player", "see_also")
    return (isinstance(value, str) and any(w in k for w in words)
            and is_unsafe(value))


def page_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """The allow-listed page metadata of a Firecrawl answer (see PAGE_METADATA), without URL-valued keys whose
    scheme is unsafe."""
    return {k: v for k, v in meta.items()
            if (k in PAGE_METADATA or k.lower().startswith(PAGE_METADATA_PREFIXES)) and not _unsafe_url_value(k, v)}


def http_links(links: object) -> list[str] | None:
    """Firecrawl's `links` array held to fastweb's own rule: http(s) URLs only (None when it is not a list)."""
    if not isinstance(links, list):
        return None
    return [u for u in links if isinstance(u, str) and u.lower().startswith(("http://", "https://"))]


def passed_headers(headers: object) -> dict[str, str] | None:
    """The allow-listed subset of a caller's `headers` option (None when nothing is left)."""
    if not isinstance(headers, dict):
        return None
    out = {k: v for k, v in headers.items()
           if isinstance(k, str) and isinstance(v, str) and k.lower() in PASSED_HEADERS and len(v) <= MAX_HEADER_VALUE}
    return out or None


def clean_content(data: dict[str, Any], formats: list[str]) -> None:
    """Hold Firecrawl's content to fastweb's rules in place: text formats only, html/rawHtml served as asked, blank
    content dropped, no javascript:/data: markdown link targets, http(s)-only `links`."""
    for k in ("markdown", "html", "rawHtml"):  # a malformed answer: drop what is not text
        if k in data and not isinstance(data[k], str):
            del data[k]
    if "rawHtml" in formats and "rawHtml" not in data and "html" in data:
        data["rawHtml"] = data["html"]  # Firecrawl sent only html: serve it as asked rather than under another key
    if "html" in formats and "html" not in data and "rawHtml" in data:
        data["html"] = data["rawHtml"]  # fastweb's html is the full page markup anyway (README)
    for k in ("markdown", "html", "rawHtml"):  # blank content is no content: never stored, never served
        if k in data and not data[k].strip():
            del data[k]
    if "markdown" in data:  # the same link rule as fastweb's own markdown: no javascript:/data: targets
        data["markdown"] = absolutize_links(data["markdown"], "")
    if "links" in data:
        links = http_links(data["links"])
        if links is None:
            del data["links"]
        else:
            data["links"] = links


class FirecrawlFallback:
    """Calls Firecrawl with a capped budget and turns its answer into fastweb's honest-error contract."""

    def __init__(self, settings: Settings, guard: TargetGuard):
        self.settings, self.guard = settings, guard
        self.sem = asyncio.Semaphore(settings.upstream_concurrency)
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=CONNECT_TIMEOUT))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def post(self, body: dict[str, Any], timeout_s: float) -> tuple[dict[str, Any] | None, int]:
        """(answer, status): answer is None unless Firecrawl said success. A status < 500 means Firecrawl answered
        and the target failed; 5xx is Firecrawl itself (restart, crash)."""
        if self.client is None:
            raise RuntimeError("FirecrawlFallback.start() was not called")
        async with self.sem:
            r = await self.client.post(self.settings.upstream.rstrip("/") + "/v2/scrape", json=body, timeout=timeout_s)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code == 200 and isinstance(j, dict) and j.get("success"):
            return j, r.status_code
        return None, r.status_code

    def request_body(self, body: dict[str, Any], budget: float, reason: str) -> dict[str, Any]:
        up = {k: v for k, v in body.items() if k in PASSED_OPTIONS and k != "headers"}
        headers = passed_headers(body.get("headers"))
        if headers:
            up["headers"] = headers
        up["timeout"] = int(budget * 1000)
        if reason.startswith("thin") and "waitFor" not in body:  # JS needs time after load
            up["waitFor"] = min(FIRECRAWL_WAIT_MS, max(0, int(budget * 1000) - 2000))
        return up

    async def scrape(self, body: dict[str, Any], formats: list[str], deadline: float, reason: str,
                     plain_err: Fail | None) -> dict[str, Any]:
        """Firecrawl fallback, capped at fallback_timeout regardless of the caller's budget."""
        budget = min(left(deadline) - UPSTREAM_MARGIN, self.settings.fallback_timeout)
        if budget <= 0:
            raise plain_err or Fail(504, f"Fetching {safe_url(body['url'])} timed out")
        if not self.settings.upstream:  # no Firecrawl configured: the cheap tiers' answer is final
            raise plain_err or Fail(422, f"{reason}; no fallback configured (FIRECRAWL_UPSTREAM)")
        j: dict[str, Any] | None
        try:
            j, _ = await asyncio.wait_for(self.post(self.request_body(body, budget, reason), budget), budget)
        except TimeoutError:
            raise Fail(504, f"{reason}; Firecrawl fallback timed out after {budget:.0f} s") from None
        except httpx.HTTPError as e:
            j, reason = None, f"{reason}; Firecrawl fallback failed: {type(e).__name__}"
        if not j:
            if plain_err:
                raise plain_err
            # success:false is never target evidence: Firecrawl's own 408/400/DNS errors look the same.
            raise Fail(422, f"{reason}; Firecrawl fallback could not fetch it")
        return await self._vet(j, formats, reason, body["url"])

    async def _vet(self, j: dict[str, Any], formats: list[str], reason: str, requested_url: str) -> dict[str, Any]:
        """Apply fastweb's own rules to Firecrawl's success: final URL public, no error status, real content."""
        data: dict[str, Any] = j["data"] if isinstance(j.get("data"), dict) else {}
        j["data"] = data
        meta = data.get("metadata")
        if not isinstance(meta, dict):
            meta = data["metadata"] = {}
        # Where Firecrawl ended up (falling back to what it reports as the source): the same SSRF rule as our own
        # hops, and any refusal rejects the answer.
        final = next((meta[k] for k in ("url", "sourceURL") if isinstance(meta.get(k), str) and meta[k]), None)
        if final is not None and final != requested_url:  # the requested URL itself passed the guard already
            try:
                await self.guard.check(final)
            except Fail as f:
                if f.error == PRIVATE_ERROR:
                    raise Fail(422, f"{PRIVATE_ERROR} (Firecrawl was redirected there)") from f
                raise Fail(422, f"{reason}; Firecrawl ended on a refused URL: {f.error}") from f
        meta = data["metadata"] = page_metadata(meta)  # Firecrawl's own accounting is not page metadata
        up_status = meta.get("statusCode")
        if isinstance(up_status, int) and up_status >= 400:
            raise Fail(422, f"Target page returned HTTP {up_status}", target_side=up_status in (401, 403))
        clean_content(data, formats)
        up_md = data.get("markdown") or ""
        if not wanted_formats(formats) & data.keys():  # none of the content the caller asked for (or the default)
            raise Fail(422, f"{reason}; Firecrawl fallback returned no content")
        if len(up_md) < 2000 and CHALLENGE_MARKERS.search(up_md):
            raise Fail(422, f"{reason}; Firecrawl fallback returned a bot challenge", target_side=True)
        return j
