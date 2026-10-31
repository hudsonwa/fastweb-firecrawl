"""fastweb search: SearXNG with one fallback retry, a hard deadline and a shared Redis cache.

Zero results after the retry is reported as a failure (items=None): agent clients typically fall back to
another backend only on success:false, while an empty success is final (and may be cached client-side).
"""
import asyncio
import itertools
import json
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from .errors import BUSY_STATUS, describe
from .settings import Settings

KEY_PREFIX = "fastweb:search:"
BUCKETS = (10, 20, 50, 100)
WARM_TIMEOUT_TRIES = 3  # warm-up answers with an engine timeout before giving that engine set up


def normalize_query(q: str) -> str:
    # casefold leaves operators (site:, quotes, -term) intact; NFKC folds full-width forms and spaces.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", q)).strip().casefold()


def bucket_limit(limit: int) -> int:
    return next((b for b in BUCKETS if limit <= b), BUCKETS[-1])


def cache_key(q: str, limit: int, engines: str) -> str:
    return f"{KEY_PREFIX}{engines or '*'}:{bucket_limit(limit)}:{normalize_query(q)}"


STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is", "it", "of", "on", "or", "that",
    "the", "this", "to", "was", "what", "when", "where", "which", "who", "why", "with", "vs", "via", "about", "into",
    "near", "me", "my", "your", "our", "new", "site", "intitle", "inurl", "filetype",
])
# Tokens many unrelated queries share (off-topic results slipped through on words like "news", "latest"). In a query
# with >= 4 terms, one of these alone does not make a result on-topic.
GENERIC = frozenset([
    "news", "latest", "today", "top", "best", "new", "price", "prices", "online", "official", "australia", "australian",
    "world", "update", "updates", "week", "weekly", "daily", "guide", "review", "reviews", "free", "shop", "app",
    "download", "scripts", "live", "breaking", "stories", "story", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
])
TRACKING = re.compile(r"^(utm_.*|msockid|fbclid|gclid|mc_[a-z]+|ref|ref_src|_hs[a-z]+|yclid|msclkid)$")
GUARD_TOP, POISON_SHARE, MIN_POISON = 10, 0.5, 3


def _latin(tok: str) -> bool:
    return all(c.isdigit() or "LATIN" in unicodedata.name(c, "") for c in tok)


def _fold(text: str) -> str:
    # NFKC (full-width, ligatures) + casefold, then NFKD without combining marks: "Beyoncé" == "beyonce".
    t = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", _fold(text))


def _query_text(q: str) -> str:
    q = re.sub(r"(^|\s)-\S+", " ", q)              # excluded terms
    q = re.sub(r"\b[a-z]+:\S+", " ", q, flags=re.I)  # site:/filetype: values (site: is checked against the host)
    return re.sub(r"(?<=[^\W_])-(?=[^\W_])", "", q)  # "e-bike" -> "ebike" (docs match it joined or split)


def query_terms(q: str) -> list[str]:
    """Content tokens of a query: no operators' values, stopwords, 1-char or non-Latin tokens; numbers kept."""
    out = []
    for t in tokens(_query_text(q)):
        if len(t) > 1 and t not in STOPWORDS and _latin(t) and t not in out:
            out.append(t)
    return out


def _non_latin(text: str) -> bool:
    return any(not _latin(t) for t in tokens(text))


SITE_RE = re.compile(r"(?:^|[\s(\[\"'])site:(\S+)", re.I)  # "-site:x" (exclusion) never matches
SITE_ALIASES = {"youtu.be": "youtube.com", "redd.it": "reddit.com", "x.com": "twitter.com"}
_MOBILE = {"www", "m", "mobile"}


def site_hosts(q: str) -> list[str]:
    """Hosts named by site: ("(site:a OR site:b)", scheme, path, port, leading "*."/".", trailing quotes stripped)."""
    out = []
    for v in SITE_RE.findall(q):
        v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v.strip("\"'()[],").lower()).split("/")[0]
        v = re.sub(r":\d*$", "", v.rstrip(")\"'],")).lstrip("*.")
        if v:
            out.append(v.removeprefix("www."))
    return out


def _site_key(host: str) -> str:
    labels = [x for x in host.lower().strip(".").split(".") if x not in _MOBILE]
    key = ".".join(labels)
    for alias, canon in SITE_ALIASES.items():
        if key == alias or key.endswith("." + alias):
            return key[: -len(alias)] + canon
    return key


def site_match(host: str, hosts: list[str]) -> bool:
    """host is on one of the site: hosts: equal or a subdomain (label boundary), www./m./mobile. labels ignored."""
    k = _site_key(host)
    return any(k == (h := _site_key(x)) or k.endswith("." + h) for x in hosts)


def _site_or(q: str) -> bool:
    return len(site_hosts(q)) > 1 and bool(re.search(r"\bOR\b|\|", q))


def _stem(t: str) -> str:
    for suf in ("ings", "ing", "es", "ed", "s"):
        if len(t) - len(suf) >= 4 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _matches(term: str, doc: set[str]) -> bool:
    if term in doc:
        return True
    if len(term) < 4:
        return False
    st = _stem(term)
    # prefix/stem ("forecast"/"forecasts") or compound ("metoffice" in "metofficeforecasts")
    return any(d.startswith(st) or (len(d) >= 4 and term.startswith(_stem(d))) or term in d for d in doc)


def _doc(r: dict[str, Any]) -> tuple[set[str], str]:
    u = urllib.parse.urlsplit(r.get("url", ""))
    text = " ".join((r.get("title") or "", r.get("content") or r.get("description") or "", u.netloc,
                     urllib.parse.unquote(u.path)))
    toks = tokens(text)
    # adjacent pairs joined: "Wi-Fi" matches "wifi", "X-Scap8" matches "xscap8"
    return set(toks) | {a + b for a, b in itertools.pairwise(toks)}, text


def _generic(t: str) -> bool:
    return t in GENERIC or bool(re.fullmatch(r"(19|20)\d\d", t))


def match_count(terms: list[str], r: dict[str, Any], non_latin_query: bool = False) -> int:
    """Strength of the match: 0 none, 1 weak (only a generic token of a >= 4-term query), 2 on-topic.

    A mixed-script query also accepts a result with non-Latin text (the native-language page carries no Latin token).
    """
    doc, text = _doc(r)
    if not terms or (non_latin_query and _non_latin(text)):
        return 2
    hit = [t for t in terms if _matches(t, doc)]
    if not hit:
        return 0
    if len(terms) >= 4 and len(hit) == 1 and _generic(hit[0]):
        return 1
    return 2


def on_topic(terms: list[str], r: dict[str, Any], non_latin_query: bool = False) -> bool:
    """>= 1 query term in the result; with >= 4 query terms, >= 2 when the only one is generic (GENERIC, years)."""
    return match_count(terms, r, non_latin_query) == 2


def is_leak(r: dict[str, Any]) -> bool:
    """Leak marker: a bing session id in the URL (every observed cross-query leak carried msockid)."""
    u = urllib.parse.urlsplit(r.get("url", ""))
    return any(k.lower() == "msockid" for k, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True))


def off_site(r: dict[str, Any], hosts: list[str]) -> bool:
    return bool(hosts) and not site_match(urllib.parse.urlsplit(r.get("url", "")).hostname or "", hosts)


def _engines(r: dict[str, Any]) -> list[str]:
    return r.get("engines") or ([r["engine"]] if r.get("engine") else ["?"])


def guard_results(q: str, results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Drop off-topic and leaked results.

    A result is off-topic without enough content-token overlap with the query (see on_topic); a leak marker (msockid) or
    a host outside site: makes it off-topic whatever its text. A site: mismatch only filters: it never counts toward
    poisoning (an odd spelling must not cost the whole answer), and with "site:a OR site:b" it is not logged as a leak.
    An engine whose top results are >= 50 % clear misses (leak or no query term; and that returned >= 3) is poisoned:
    all of its results go (bing leaks other queries' answers through a shared session). If the remaining answer is still
    >= 50 % clear misses (with >= 3 results), it all goes ([] -> the caller treats it like an empty answer). Smaller
    answers are only filtered. Returns (kept, log string).
    """
    terms, hosts = query_terms(q), site_hosts(q)
    if not terms and not hosts:
        return results, "skip:no-terms"
    nl = _non_latin(_query_text(q))
    leaks = [is_leak(r) for r in results]
    offs = [off_site(r, hosts) for r in results]
    hits = [match_count(terms, r, nl) for r in results]
    flags = [not lk and not o and h == 2 for lk, o, h in zip(leaks, offs, hits, strict=True)]
    # poisoning counts only clear misses (leak or no term at all): a weak result is filtered out but is not evidence
    # of a leaked answer
    clear = [not lk and h > 0 for lk, h in zip(leaks, hits, strict=True)]
    per: dict[str, list[bool]] = {}
    for r, ok in zip(results[:GUARD_TOP], clear[:GUARD_TOP], strict=True):
        for e in _engines(r):
            per.setdefault(e, []).append(ok)
    bad = {e for e, v in per.items() if len(v) >= MIN_POISON and v.count(False) / len(v) >= POISON_SHARE}
    keep = [(r, ok, c) for r, ok, c in zip(results, flags, clear, strict=True) if not set(_engines(r)) <= bad]
    top = flags[:GUARD_TOP]
    share = top.count(False) / len(top) if top else 0.0
    nleak = sum(leaks) + (0 if _site_or(q) else sum(offs))
    info = (f"off={share:.2f}" + (f" leak={nleak}" if nleak else "") + (f" offsite={sum(offs)}" if any(offs) else "")
            + (f" poisoned_engines={','.join(sorted(bad))}" if bad else ""))
    ktop = [c for _, _, c in keep[:GUARD_TOP]]
    if not ktop or (len(ktop) >= MIN_POISON and ktop.count(False) / len(ktop) >= POISON_SHARE):
        return [], info + (" poisoned" if results else "")
    return [r for r, ok, _ in keep if ok], info


def normalize_url(url: str) -> str:
    """Dedupe key: scheme/host case, www., default port, trailing slash and tracking params ignored."""
    try:
        u = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    host = (u.hostname or "").lower().removeprefix("www.")
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    qs = sorted((k, v) for k, v in pairs if not TRACKING.match(k.lower()))
    return f"{host}{u.path.rstrip('/')}?{urllib.parse.urlencode(qs)}"


class RedisCache:
    """Best-effort cache: every Redis error is swallowed (Redis down -> plain search).

    After any error the breaker skips Redis for `breaker` seconds, so a hung Redis costs one timeout, not one per
    search.
    """

    def __init__(self, url: str = "", client: Any = None, timeout: float = 0.25, breaker: float = 30.0):
        if client is None:
            import redis.asyncio as aioredis
            client = aioredis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout)
        self.redis = client
        self.timeout, self.breaker = timeout, breaker
        self.open_until = 0.0
        self._tasks: set[asyncio.Task[None]] = set()

    def _trip(self) -> None:
        self.open_until = time.monotonic() + self.breaker

    async def get(self, key: str) -> Any:
        if time.monotonic() < self.open_until:
            return None
        try:
            raw = await asyncio.wait_for(self.redis.get(key), self.timeout)
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 - any Redis failure trips the breaker; search goes on uncached
            self._trip()
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if time.monotonic() < self.open_until:
            return
        try:
            await asyncio.wait_for(self.redis.set(key, json.dumps(value), ex=ttl), self.timeout)
        except Exception:  # noqa: BLE001 - any Redis failure trips the breaker
            self._trip()

    def set_background(self, key: str, value: Any, ttl: int) -> None:
        """Fire-and-forget SET: never on the response path."""
        t = asyncio.create_task(self.set(key, value, ttl))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)


@dataclass
class Outcome:
    """One search answer: items (None when every attempt failed) and how it was obtained, for headers and logs."""

    items: list[dict[str, Any]] | None
    cache: str = "miss"   # miss | hit (Redis) | join (an identical in-flight search answered it)
    attempts: int = 0
    error: str = ""
    source: str = ""  # primary | fallback (after an empty/failed primary) | hedge (started while primary ran)
    guard: list[str] = field(default_factory=list)  # relevance-guard log per attempt, e.g. "primary:off=0.75 poisoned"
    status: int = 503  # HTTP status of a failure: 503 (backend failed), 502 (queued too long), 429 (queue full)
    retry_after: int | None = None  # set when the failure is fastweb's own capacity, worth a retry


def map_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen, items = set(), []
    for x in results:
        if not isinstance(x, dict):
            continue
        url, title, content = x.get("url"), x.get("title"), x.get("content")
        if not url or not isinstance(url, str):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        items.append({"url": url, "title": title if isinstance(title, str) else "",
                      "description": content if isinstance(content, str) else ""})
        if len(items) >= limit:
            break
    return items


class _Prefixed:
    """Log sink wrapper that prefixes every guard note with the attempt name."""

    def __init__(self, sink: list[str], prefix: str):
        self.sink, self.prefix = sink, prefix

    def append(self, info: str) -> None:
        self.sink.append(f"{self.prefix}:{info}")


def _timed_out(r: httpx.Response) -> bool:
    """SearXNG answered, but an engine timed out: its connection is still not warm."""
    j = r.json()
    bad = (j.get("unresponsive_engines") if isinstance(j, dict) else None) or []
    return any("timeout" in str(e).lower() for e in bad)


class Searcher:
    """SearXNG client: primary engines, a hedged fallback, the relevance guard and the Redis cache.

    Concurrency: identical in-flight searches (same cache key) share one upstream search. At most `concurrency`
    SearXNG calls run at once; a search queues for a slot (up to `queue_timeout` s, at most `queue_max` waiting) and
    its `deadline` starts once it has one, so a burst queues instead of every search racing the upstream engines
    into a timeout. A hedge is only started when a slot is free. A full queue answers 429, a queue wait past
    `queue_timeout` 502 (the status firecrawl-py retries), both with Retry-After.
    """

    @classmethod
    def from_settings(cls, s: Settings, client: httpx.AsyncClient) -> "Searcher":
        """A Searcher configured from Settings, sharing the app's HTTP client."""
        return cls(client, s.searxng_url, s.searxng_engines, s.search_fallback_engines, s.searxng_categories,
                   s.search_deadline, RedisCache(s.redis_url) if s.redis_url else None, s.search_ttl,
                   hedge=s.search_hedge, fallback_ttl=s.search_fallback_ttl, relevance=s.search_relevance,
                   concurrency=s.search_concurrency, queue_max=s.search_queue_max,
                   queue_timeout=s.search_queue_timeout)

    def __init__(self, client: httpx.AsyncClient, searxng_url: str, engines: str = "", fallback_engines: str = "",
                 categories: str = "", deadline: float = 3.0, cache: RedisCache | None = None, ttl: int = 900,
                 hedge: float = 1.1, fallback_ttl: int = 120, grace: float = 0.05, relevance: bool = True,
                 concurrency: int = 6, queue_max: int = 128, queue_timeout: float = 10.0):
        self.client, self.url = client, searxng_url.rstrip("/") + "/search"
        self.engines, self.fallback, self.categories = engines, fallback_engines, categories
        self.deadline, self.cache, self.ttl = deadline, cache, ttl
        self.hedge, self.fallback_ttl, self.grace = hedge, fallback_ttl, grace
        self.relevance = relevance
        self.slots = asyncio.Semaphore(max(1, concurrency))
        self.queue_max, self.queue_timeout = queue_max, queue_timeout
        self.waiting = 0
        self.inflight: dict[str, asyncio.Future[Outcome]] = {}

    async def warm(self, tries: int = 30, pause: float = 2.0, timeout: float = 10.0) -> bool:
        """Send one throwaway query to each engine set (primary, fallback), retrying until SearXNG is up.

        SearXNG opens its connection to an engine on that engine's first call, and on a fresh instance that call
        takes about as long as the engine's own timeout (yahoo: 1.3-1.5 s against 1.5 s): the first real search
        then lost its primary and its hedge to engine timeouts and answered 503, while the same query answered in
        0.7 s a few seconds later. A set counts as warm once SearXNG answers it without an engine timeout. Nothing
        is cached and no upstream slot is held. True when every set is warm."""
        sets = list(dict.fromkeys(e for e in (self.engines, self.fallback) if e)) or [""]
        params = {"q": "weather", "format": "json"}
        if self.categories:
            params["categories"] = self.categories
        ok = 0
        for engines in sets:
            slow = 0  # engine-timeout answers: an engine that always times out is not retried for minutes
            for i in range(tries):
                try:
                    r = await self.client.get(self.url, params={**params, **({"engines": engines} if engines else {})},
                                              headers={"Accept": "application/json"}, timeout=timeout)
                    if r.status_code == 200 and not _timed_out(r):
                        ok += 1
                        break
                    if r.status_code == 200:
                        slow += 1
                        if slow >= WARM_TIMEOUT_TRIES:
                            break
                except (httpx.HTTPError, ValueError):
                    pass
                if i + 1 < tries:
                    await asyncio.sleep(pause)
        return ok == len(sets)

    async def _query(self, q: str, engines: str, fetch_limit: int, timeout: float,
                     guard_log: _Prefixed | None = None) -> list[dict[str, Any]]:
        params = {"q": q, "format": "json"}
        if engines:
            params["engines"] = engines
        if self.categories:
            params["categories"] = self.categories
        r = await asyncio.wait_for(self.client.get(self.url, params=params, headers={"Accept": "application/json"},
                                                   timeout=timeout), timeout)
        r.raise_for_status()
        j = r.json()
        res = j.get("results") if isinstance(j, dict) else None
        if not isinstance(res, list):
            raise ValueError("malformed SearXNG answer")
        if self.relevance and res:
            # A poisoned answer comes back as [] (never cached; the fallback/hedge path takes over).
            res, info = guard_results(q, res)
            if guard_log is not None:
                guard_log.append(info)
        return map_results(res, fetch_limit)

    async def _attempt(self, q: str, engines: str, fetch_limit: int, timeout: float,
                       guard_log: _Prefixed) -> list[dict[str, Any]]:
        """One SearXNG call holding an upstream slot (acquired by the caller), released when it ends."""
        try:
            return await self._query(q, engines, fetch_limit, timeout, guard_log)
        finally:
            self.slots.release()

    async def search(self, q: str, limit: int) -> Outcome:
        key = cache_key(q, limit, self.engines)
        if self.cache:
            hit = await self.cache.get(key)
            if hit:
                return Outcome(hit[:limit], "hit")
        while (fut := self.inflight.get(key)) is not None:
            try:
                o = await asyncio.shield(fut)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if not fut.cancelled() or (task is not None and task.cancelling()):
                    raise
                continue  # the leader was cancelled (its client went away): lead or join the next one
            return replace(o, items=o.items[:limit] if o.items is not None else None, guard=list(o.guard),
                           cache="join" if o.items is not None else o.cache)
        fut = asyncio.get_running_loop().create_future()
        self.inflight[key] = fut
        try:
            out = await self._search(q, key, bucket_limit(limit))
            fut.set_result(out)
        finally:
            if not fut.done():
                fut.cancel()
            if self.inflight.get(key) is fut:
                del self.inflight[key]
        return replace(out, items=out.items[:limit] if out.items is not None else None)

    async def _slot(self) -> Outcome | None:
        """Queue for an upstream slot; an Outcome when fastweb itself is overloaded."""
        if self.waiting >= self.queue_max:
            return Outcome(None, error="search queue full", status=429, retry_after=1)
        self.waiting += 1
        try:
            await asyncio.wait_for(self.slots.acquire(), self.queue_timeout)
        except TimeoutError:
            return Outcome(None, error=f"search backend busy (queued {self.queue_timeout:.0f} s)",
                           status=BUSY_STATUS, retry_after=2)
        finally:
            self.waiting -= 1
        return None

    async def _search(self, q: str, key: str, fetch_limit: int) -> Outcome:
        """Queue for an upstream slot, race the attempts (see _Race) and cache a winner."""
        busy = await self._slot()
        if busy is not None:
            return busy
        out = Outcome(None)
        race = _Race(self, q, fetch_limit, out)  # the deadline starts once this search has an upstream slot
        winner = await race.run()
        if winner is None:
            return out
        out.source, out.items, out.error = winner[0], winner[1], ""
        if self.cache:
            self.cache.set_background(key, out.items, self.ttl if out.source == "primary" else self.fallback_ttl)
        return out


Task = asyncio.Task[list[dict[str, Any]]]
RETRY_MIN_LEFT = 0.5  # seconds of deadline left for the one retry after every attempt came back empty


class _Race:
    """The attempts of one search against SearXNG, all inside one deadline.

    The primary attempt runs on the slot the search queued for. At most one second attempt follows, on the
    fallback engine set (or the primary's again when none is configured: bing drops about 1 call in 7 with
    connection errors, and SearXNG then answers from the rest):

    - fallback: the primary came back empty or failed. It may queue for a slot until just before the deadline.
    - hedge: the primary is still running `hedge` s in. It starts only on a free slot, never queued behind other
      searches; if no slot is free then, no hedge is tried for this search.

    When every attempt has come back empty or failed with at least RETRY_MIN_LEFT s of the deadline left, the
    primary engines get one retry before the search answers 503.

    The first non-empty answer wins, but a primary landing within `grace` s of a second attempt's answer is
    preferred. Invariants: every slot is taken before its attempt starts and released by the attempt itself; every
    task still pending when the race ends is cancelled and awaited; `out.error` keeps the last failure.
    """

    def __init__(self, searcher: Searcher, q: str, fetch_limit: int, out: Outcome):
        self.sr, self.q, self.fetch_limit, self.out = searcher, q, fetch_limit, out
        self.end = time.monotonic() + searcher.deadline
        self.tasks: dict[Task, str] = {}  # task -> "primary" | "fallback" | "hedge"
        self.pending: set[Task] = set()
        self.winner: tuple[str, list[dict[str, Any]]] | None = None
        self.hedge_skipped = False

    def left(self) -> float:
        return self.end - time.monotonic()

    def start(self, source: str, engines: str) -> None:
        """Start an attempt; the caller already holds an upstream slot for it."""
        self.out.attempts += 1
        glog = _Prefixed(self.out.guard, source)
        t = asyncio.create_task(self.sr._attempt(self.q, engines, self.fetch_limit, self.left(), glog))
        self.tasks[t] = source
        self.pending.add(t)

    def take(self, done: set[Task]) -> None:
        """Record finished attempts: a non-empty answer wins, an empty or failed one is noted in out.error."""
        for t in done:
            try:
                items = t.result()
            except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError) as e:
                # a malformed SearXNG answer (a JSON list, odd item types) counts as a failed attempt, not a 500
                self.out.error = describe(e)  # never str(e): httpx errors quote the URL and so the query
                continue
            # a primary always takes the win; a hedge/fallback never replaces one landing in the same round
            if items and (self.winner is None or self.tasks[t] == "primary"):
                self.winner = (self.tasks[t], items)
            else:
                self.out.error = "no results from any search engine"

    async def run(self) -> tuple[str, list[dict[str, Any]]] | None:
        """(source, items) of the winning attempt, or None with out.error saying why."""
        self.start("primary", self.sr.engines)
        try:
            while self.pending and self.winner is None:
                await self._wait_round()
                if self.winner is not None:
                    break
                if len(self.tasks) == 1:  # no second attempt yet
                    if not await self._maybe_start_second():
                        break
                elif not self.pending or self.left() <= 0:
                    break
            if self.winner is None and not self.pending and await self._maybe_retry():
                await self._wait_round()
            if self.winner is None and self.pending:
                self.out.error = "search deadline exceeded"
        finally:
            for t in self.pending:
                t.cancel()
            if self.pending:
                await asyncio.gather(*self.pending, return_exceptions=True)
        return self.winner

    async def _wait_round(self) -> None:
        """Wait for the next attempt to finish: until the hedge time while only the primary runs (and a hedge is
        still possible), otherwise until the deadline. A second attempt's win waits `grace` s for the primary."""
        left = self.left()
        if len(self.tasks) > 1 or self.hedge_skipped:
            wait = left
        else:
            wait = min(left, max(self.sr.hedge - (self.sr.deadline - left), 0))
        done, self.pending = await asyncio.wait(self.pending, timeout=max(wait, 0),
                                                return_when=asyncio.FIRST_COMPLETED)
        self.take(done)
        if self.winner is not None and self.winner[0] != "primary" and self.pending:
            done, self.pending = await asyncio.wait(self.pending, timeout=min(self.sr.grace, self.left()))
            self.take(done)  # only a non-empty primary replaces the win

    async def _maybe_retry(self) -> bool:
        """Every attempt came back empty or failed well inside the deadline: one more try of the primary engines
        (an engine's empty answer is often transient: the same query answers seconds later). Only with
        RETRY_MIN_LEFT s left and a free slot within the deadline."""
        if self.left() < RETRY_MIN_LEFT or self.out.attempts > 2:
            return False
        try:
            await asyncio.wait_for(self.sr.slots.acquire(), self.left() - 0.05)
        except TimeoutError:
            return False
        self.start("retry", self.sr.engines)
        return True

    async def _maybe_start_second(self) -> bool:
        """Start the hedge or the fallback when it is due; False when the race should stop."""
        if self.left() < 0.05:
            if not self.pending:
                self.out.error = self.out.error or "search deadline exceeded"
                return False
            return True  # the primary is bounded by the deadline itself
        engines = self.sr.fallback or self.sr.engines
        if self.pending:  # hedge time: only with a free slot, never queued behind other searches
            if self.hedge_skipped or self.sr.slots.locked():
                self.hedge_skipped = True
                return True
            await self.sr.slots.acquire()
            self.start("hedge", engines)
            return True
        try:  # the primary failed or came back empty: the fallback may queue within the deadline
            await asyncio.wait_for(self.sr.slots.acquire(), self.left() - 0.05)
        except TimeoutError:
            self.out.error = self.out.error or "search deadline exceeded"
            return False
        self.start("fallback", engines)
        return True
