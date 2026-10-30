"""HTTP API: Firecrawl-compatible /v1 and /v2 search and scrape endpoints, plus /health and /v1/fastweb/stats.

This module only wires things together and speaks HTTP: request parsing and limits, the scrape cache and
single-flight joining, logging and status codes. The scrape tiers live in pipeline.py, search in search.py.
Failures are {"success": false, "error": ...}; 502 is kept for fastweb's own capacity (Firecrawl SDKs retry it).
"""
import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .cache import Store, fresh_enough, may_store, project, scrape_key_for_request
from .errors import Fail, describe
from .fetchers import Impersonator, left
from .heuristics import safe_url
from .pipeline import Job, ScrapeService, counting, parse_scrape_body
from .prefetch import Prefetcher
from .search import Searcher
from .settings import Settings
from .worker import Hold


def log(**kw: Any) -> None:
    """One JSON line per event on stdout."""
    print(json.dumps(kw, separators=(",", ":")), file=sys.stdout, flush=True)


async def warn_unresolvable(s: Settings) -> None:
    """Warn at startup about backend URLs whose host does not resolve. The defaults name compose services
    (searxng, fastweb-redis), so a `pip install` run without SEARXNG_URL / FASTWEB_REDIS_URL set would otherwise
    boot silently and answer every search 503."""
    targets = {"SEARXNG_URL": s.searxng_url, "FASTWEB_REDIS_URL": s.redis_url, "FIRECRAWL_UPSTREAM": s.upstream,
               "FASTWEB_BROWSER_URL": s.browser_url if s.browser else ""}
    loop = asyncio.get_running_loop()
    for name, url in targets.items():
        if not url:
            continue
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if not host:
            log(warning="startup_config", variable=name, problem="no host in URL")
            continue
        try:
            await asyncio.wait_for(loop.getaddrinfo(host, None), 3)
        except (OSError, TimeoutError, UnicodeError):
            log(warning="startup_config", variable=name, host=host,
                problem="host does not resolve; set it (see README, Configuration)")


def fail_response(status: int, error: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse({"success": False, "error": error}, status_code=status, headers=headers)


class BodyTooLarge(Exception):
    """A request body over FASTWEB_MAX_BODY_BYTES."""


async def read_json(request: Request, limit: int) -> Any:
    """The request body parsed as JSON (None if it is not JSON). Raises BodyTooLarge past `limit` bytes, checked
    against Content-Length first and then while streaming, so an oversized body is never buffered."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise BodyTooLarge
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise BodyTooLarge
    try:
        return json.loads(buf)
    except ValueError:
        return None


def shareable(f: Fail) -> bool:
    """A leader's failure its single-flight joiners can return as their own: about the page, not about the leader's
    own budget (timeouts, busy) or a transient network error."""
    return f.status in (413, 422) and not f.transient and "busy" not in f.error


class Answer(NamedTuple):
    """A scrape answer and how it was obtained."""

    payload: bytes
    engine: str                # for the log: cache | join | the tier that fetched it
    log_extra: dict[str, Any]  # extra log fields
    cache_header: str          # X-Fastweb-Cache


def ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class Api:
    """The HTTP handlers and what they share: the scrape service, its cache and single-flight, the searcher and the
    prefetcher. create_app() wires one into a Starlette app."""

    def __init__(self, s: Settings, store: Store, service: ScrapeService):
        self.settings, self.store, self.service, self.flights = s, store, service, service.flights
        self.prefetcher = Prefetcher(store, service.flights, service.prefetch, log, n=s.prefetch_n,
                                     concurrency=s.prefetch_concurrency, queue_max=s.prefetch_queue,
                                     per_host=s.prefetch_per_host, max_age=s.prefetch_max_age,
                                     mem_high=service.mem_high, busy=service.prefetch_busy)
        self.scrape_stats = {"hit_prefetched": 0, "hit_other": 0, "miss_prefetched": 0, "miss_other": 0,
                             "singleflight_joins": 0}
        self.searcher: Searcher | None = None
        self.warm: asyncio.Task | None = None

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        s = self.settings
        await self.service.start()
        if not s.lane_factory:  # start the small lane's worker now, not on the first request
            self.warm = asyncio.get_running_loop().create_task(self.service.lanes["small"].warm())
        if self.service.fetcher.client is None:
            raise RuntimeError("HTTP client not started (Service.start() must run first)")
        self.searcher = Searcher.from_settings(s, self.service.fetcher.client)
        check = asyncio.get_running_loop().create_task(warn_unresolvable(s))
        warm_search = asyncio.get_running_loop().create_task(self._warm_search()) if s.search_warm else None
        yield
        check.cancel()
        if warm_search is not None:
            warm_search.cancel()
        await self.prefetcher.stop()
        await self.service.close()

    async def _warm_search(self) -> None:
        if self.searcher is not None:
            t0 = time.monotonic()
            log(event="search_warm", ok=await self.searcher.warm(), ms=ms_since(t0))

    # ---- scrape ----
    async def scrape(self, request: Request) -> Response:
        t0, path = time.monotonic(), request.url.path
        try:
            body = await read_json(request, self.settings.max_body_bytes)
        except BodyTooLarge:
            log(path=path, status=413, error="body too large")
            return fail_response(413, f"Request body exceeds {self.settings.max_body_bytes} bytes")
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            log(path=path, status=400, error="bad body")
            return fail_response(400, "Body must be JSON with a 'url' string")
        url = body["url"]
        hold = Hold(self.service.budget)
        try:
            url, formats, timeout_s = parse_scrape_body(body, self.settings.max_timeout)
            key = scrape_key_for_request(body)
            shared = bool(key) and may_store(body)
            answer = await self._cached(body, key, formats)
            if answer is None and shared:
                answer = await self._join(key, t0 + timeout_s, formats)
            if answer is None:
                if key:
                    self.scrape_stats["miss_prefetched" if self.prefetcher.was_prefetched(key) else "miss_other"] += 1
                answer = await self._lead(Job.create(body, url, formats, t0 + timeout_s, t0, hold), key, shared)
            log(path=path, url=safe_url(url), engine=answer.engine, status=200, ms=ms_since(t0), **answer.log_extra)
            return Response(answer.payload, media_type="application/json",
                            headers={"X-Fastweb-Cache": answer.cache_header})
        except Fail as f:
            log(path=path, url=safe_url(url), status=f.status, error=f.error, ms=ms_since(t0))
            return fail_response(f.status, f.error, f.retry_after)
        except UnicodeError as e:  # idna.IDNAError: a redirect to a host name IDNA 2008 does not allow
            log(path=path, url=safe_url(url), status=422, error=f"invalid host name: {describe(e)}",
                ms=ms_since(t0))
            return fail_response(422, "Could not scrape the page: a redirect went to an invalid host name")
        except Exception as e:  # noqa: BLE001 - never a 500: odd URLs, malformed upstream answers
            log(path=path, url=safe_url(url), status=422, error=f"unexpected {describe(e)}", ms=ms_since(t0))
            return fail_response(422, "Could not scrape the page: unexpected error")
        finally:
            hold.release()  # the body, decoded page and answer are held until the response is built

    async def _cached(self, body: dict[str, Any], key: str | None, formats: list[str]) -> Answer | None:
        """A fresh enough scrape-cache entry that holds every requested format, projected onto them."""
        cached = await self.store.get_scrape(key) if key else None
        if not key or not cached or not fresh_enough(body, cached[1]):
            return None
        payload = await answer_from_entry(cached[0], formats)
        if payload is None:
            return None
        pf = await self.store.is_prefetched(key)
        self.scrape_stats["hit_prefetched" if pf else "hit_other"] += 1
        return Answer(payload, "cache", {"prefetched": pf}, "HIT-PREFETCH" if pf else "HIT")

    async def _join(self, key: str | None, deadline: float, formats: list[str]) -> Answer | None:
        """The answer of an in-flight fetch of the same key; raises its failure when the leader shared one."""
        if key is None:
            return None
        joined = await self.flights.join(key, left(deadline))
        if not joined:
            return None
        result, by_prefetch = joined
        if isinstance(result, Exception):  # the leader's failure (a Fail) is this request's answer too
            self.scrape_stats["singleflight_joins"] += 1
            raise result
        payload = await answer_from_entry(result, formats)
        if payload is None:  # the leader's page lacks a format asked for here (a Firecrawl answer)
            return None
        self.scrape_stats["singleflight_joins"] += 1
        return Answer(payload, "join", {"prefetched": by_prefetch}, "JOIN-PREFETCH" if by_prefetch else "JOIN")

    async def _lead(self, job: Job, key: str | None, shared: bool) -> Answer:
        """Walk the ladder; when the result may be stored, as the single-flight leader for its key."""
        with counting(self.service), self.flights.lead(key) if shared and key else contextlib.nullcontext() as fut:
            try:
                resp = await self.service.run(job)
            except Fail as f:
                if fut is not None and shareable(f):
                    fut.set_result(f)
                raise
            answer = project(resp, job.formats) or resp
            if fut is not None and key is not None:
                entry = await dump_payload(resp)  # the whole page: later requests for any format subset hit it
                if not await self.store.put_scrape(key, entry) and answer is not resp:
                    await self.store.put_scrape(key, await dump_payload(answer))  # too big whole: this subset
                fut.set_result(entry)
            payload = await dump_payload(answer)
        return Answer(payload, resp["data"]["metadata"]["fastweb"]["engine"], {}, "MISS")

    # ---- search ----
    async def search(self, request: Request) -> Response:
        t0, path = time.monotonic(), request.url.path
        try:
            body = await read_json(request, self.settings.max_body_bytes)
        except BodyTooLarge:
            return fail_response(413, f"Request body exceeds {self.settings.max_body_bytes} bytes")
        if not isinstance(body, dict) or not isinstance(body.get("query"), str) or not body["query"].strip():
            return fail_response(400, "Body must be JSON with a 'query' string")
        q, limit = body["query"], body.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return fail_response(400, "'limit' must be a positive integer")
        if self.searcher is None:
            raise RuntimeError("search called before the app started")
        res = await self.searcher.search(q, min(limit, self.settings.max_search_limit))
        ms = ms_since(t0)
        if res.items is None:
            # success:false (not an empty success) so clients fall back to another backend and do not cache it.
            log(path=path, engine="searxng", status=res.status, error=res.error, cache=res.cache,
                attempts=res.attempts, guard=res.guard, ms=ms)
            r = fail_response(res.status, f"Search backend failed: {res.error}", res.retry_after)
            r.headers["X-Fastweb-Cache"] = res.cache.upper()
            r.headers["X-Fastweb-Search"] = f"source=none;attempts={res.attempts}"
            return r
        items, hdr = res.items, {"X-Fastweb-Cache": res.cache.upper(),
                                 "X-Fastweb-Search": f"source={res.source or res.cache};attempts={res.attempts}"}
        if path.startswith("/v1"):
            resp = JSONResponse({"success": True, "data": items}, headers=hdr)
        else:
            web = [{**x, "position": i + 1} for i, x in enumerate(items)]
            resp = JSONResponse({"success": True, "data": {"web": web}}, headers=hdr)
        sched, skipped = self.prefetcher.submit([x.get("url") for x in items])  # synchronous; never raises
        log(path=path, engine="searxng", status=200, n=len(items), cache=res.cache, attempts=res.attempts,
            source=res.source, guard=res.guard, ms=ms, prefetch_scheduled=sched, prefetch_skipped=skipped)
        return resp

    # ---- health and stats ----
    async def health(self, request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "fastweb"})

    async def stats(self, request: Request) -> Response:
        return JSONResponse({"prefetch": self.prefetcher.snapshot(), "scrape": self.scrape_stats,
                             "browser": self.service.browser_stats()})


def create_app(settings: Settings | None = None, redis: Any = None,
               impersonator: Impersonator | None = None) -> Starlette:
    """Build the ASGI app. redis: an async Redis client (default from settings.redis_url); impersonator: async
    (url, deadline) -> (status, final_url, headers, bytes) used on 401/403 (default curl_cffi Chrome when
    settings.impersonate)."""
    s = settings or Settings()
    if redis is None and s.redis_url:
        import redis.asyncio as aioredis
        redis = aioredis.Redis.from_url(s.redis_url, socket_connect_timeout=0.3, socket_timeout=0.3)
    store = Store(redis, host_ttl=s.host_ttl, needs_render_ttl=s.firecrawl_ttl, scrape_ttl=s.scrape_ttl)
    api = Api(s, store, ScrapeService(s, store, impersonator))
    app = Starlette(routes=[
        Route("/v1/fastweb/stats", api.stats), Route("/", api.health), Route("/health", api.health),
        Route("/v1/scrape", api.scrape, methods=["POST"]), Route("/v2/scrape", api.scrape, methods=["POST"]),
        Route("/v1/search", api.search, methods=["POST"]), Route("/v2/search", api.search, methods=["POST"]),
    ], lifespan=api.lifespan)
    app.state.api, app.state.budget, app.state.lanes = api, api.service.budget, api.service.lanes
    return app


async def dump_payload(resp: dict[str, Any]) -> bytes:
    """Serialise an answer; big ones off the event loop."""
    def dump() -> bytes:
        return json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode()
    big = len(resp["data"].get("markdown") or "") + len(resp["data"].get("html") or "") > 200_000
    return await asyncio.to_thread(dump) if big else dump()


async def answer_from_entry(entry: bytes, formats: list[str]) -> bytes | None:
    """A stored (or single-flight) page projected onto `formats`; None when it lacks one of them."""
    def go() -> bytes | None:
        try:
            doc = json.loads(entry)
        except ValueError:
            return None
        out = project(doc, formats) if isinstance(doc, dict) and isinstance(doc.get("data"), dict) else None
        if out is None:
            return None
        return json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()
    return await asyncio.to_thread(go) if len(entry) > 200_000 else go()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from . import __version__
    p = argparse.ArgumentParser(
        prog="fastweb-firecrawl",
        description="Serve fastweb's Firecrawl-compatible search and scrape API (/v1 and /v2). Every other setting "
                    "is an environment variable; see the README's Configuration table.")
    p.add_argument("--host", default=os.environ.get("FASTWEB_HOST", "127.0.0.1"),
                   help="address to listen on (env FASTWEB_HOST, default 127.0.0.1)")
    p.add_argument("--port", type=int, default=int(os.environ.get("FASTWEB_LISTEN_PORT", "3002")),
                   help="port to listen on (env FASTWEB_LISTEN_PORT, default 3002)")
    p.add_argument("--limit-concurrency", type=int, default=512,
                   help="open connections and tasks beyond which uvicorn answers 503 (default 512)")
    p.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"],
                   help="uvicorn's log level (default info)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Console entry point: `fastweb` serves the API with uvicorn."""
    a = parse_args(argv)
    import uvicorn
    uvicorn.run(create_app, factory=True, host=a.host, port=a.port, log_level=a.log_level, access_log=False,
                limit_concurrency=a.limit_concurrency)
