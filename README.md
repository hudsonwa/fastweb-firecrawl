# fastweb-firecrawl

[![CI](https://github.com/hudsonwa/fastweb-firecrawl/actions/workflows/ci.yml/badge.svg)](https://github.com/hudsonwa/fastweb-firecrawl/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)

**A fast, self-hosted web search and scrape service that speaks Firecrawl's `/v1` and `/v2` API.**

Point any Firecrawl client (the official `firecrawl-py` / JS SDKs, or AI agents built on them) at fastweb-firecrawl instead of
a Firecrawl server and it keeps working unchanged, just much faster for the common case: most pages do not need a
browser, and most searches do not need a scraping pipeline.

fastweb-firecrawl is an independent project: it is not affiliated with Firecrawl (it only implements the same HTTP API) or with
any company or product of the same name. It is a server you run yourself, not a hosted service or a Python library
for scraping.

- **Search** goes straight to a private [SearXNG](https://github.com/searxng/searxng) with a hedged fallback engine,
  a relevance guard against off-topic or cross-query-leaked results, and a Redis cache.
- **Scrape** walks a tiered ladder and stops at the first tier that produces real content:
  plain HTTP + [trafilatura](https://github.com/adbar/trafilatura) → Chrome TLS impersonation
  ([curl_cffi](https://github.com/lexiforest/curl_cffi)) on 401/403 → a warm headless Chrome over raw CDP for
  JavaScript-built, thin or blocked pages → optionally, a real Firecrawl as the last resort.
- **Honest errors.** A 404 is reported as a 404-shaped error, a consent wall or bot challenge is never returned as
  "success" (nor a sign-in prompt repeated over a teaser profile page), and a dead host fails in seconds instead of burning the caller's whole timeout.
- **SSRF-guarded.** Every redirect hop is resolved and checked; the browser (and the optional Firecrawl) live on
  networks with no route out except a guarding egress proxy.
- **Prefetch-on-search.** The top results of each search are fetched into the scrape cache in the background, so
  the scrape that usually follows a search is a cache hit. The cache keeps the whole page (markdown and markup), so
  a follow-up scrape with any of the `markdown` / `html` / `rawHtml` formats is served from it.

> Status: 0.1.0, a working service extracted from a personal deployment. It is used daily by AI agents, but it
> has a small user base: expect rough edges and read [Limitations](#limitations).

## Contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [API compatibility](#api-compatibility)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Benchmarks](#benchmarks)
- [Limitations](#limitations)
- [Development](#development)
- [Licence](#licence)

## Architecture

```
                      client (firecrawl-py, JS SDK, curl, an AI agent ...)
                                         │  POST /v1|v2/search, /v1|v2/scrape
                                         ▼
 ┌───────────────────────────── fastweb-firecrawl (Starlette + uvicorn) ──────────────────────────────┐
 │  search:  Redis cache ─► SearXNG (primary engine) ──hedge after 1.1 s──► fallback engines │
 │           relevance guard (drops off-topic / leaked results) ─► prefetch top N            │
 │                                                                                           │
 │  scrape:  scrape cache / single-flight join                                               │
 │             └─► 1. plain HTTP/2 GET + SSRF check per hop ─► extraction worker process     │
 │                    (trafilatura / selectolax / markdownify / pypdf, recycled per lane)    │
 │             └─► 2. curl_cffi Chrome impersonation (on 401/403)                            │
 │             └─► 3. warm headless Chrome over CDP (thin, JS shell, wall, 401/403/429,      │
 │                    TLS stall, suspect SPA) ─► same extraction + honesty checks            │
 │             └─► 4. Firecrawl /v2/scrape (optional; only if the browser is off/busy/down)  │
 │                                                                                           │
 │  per-host memory in Redis: hosts that need a browser skip plain; repeatedly refusing      │
 │  hosts fail fast; any success clears it                                                   │
 └───────────────┬──────────────────────┬─────────────────────────────┬─────────────────────┘
                 │ backend network      │ browsernet (internal: no gateway)
                 ▼                      ▼
     searxng, fastweb-redis       browser (chromedp/headless-shell, non-root, read-only)
                                        │ --proxy-server
                                        ▼
                                  browser-egress (egress.py): resolves, refuses private
                                  addresses / odd ports, connects to the checked IP ─► internet
```

The fast path is fast because it does very little: one HTTP request and a C-backed HTML parser in a pre-forked
worker. Only pages that demonstrably need JavaScript (an app shell, a "please enable JavaScript" page, a challenge,
or a 401/403/429) pay for a browser, and that browser is already warm, gets a fresh context per page, blocks
images/fonts/media and trackers, and stops as soon as the page's text stops changing with no XHR in flight.

Code map (the `fastweb_firecrawl` package):

| Module | Role |
|---|---|
| `app.py` | HTTP API: request limits, cache lookup, single-flight, status codes, logging, wiring |
| `pipeline.py` | the scrape tier ladder: deadlines, byte budget, host memory, browser and Firecrawl hand-off |
| `fetchers.py` | plain and Chrome-impersonating GETs with manual, guarded redirects |
| `guard.py` | SSRF checks on every hop and on the connected peer |
| `upstream.py` | the optional Firecrawl fallback and the checks on its answers |
| `heuristics.py` | thin / walled / blocked / binary / app-shell page checks |
| `search.py` | SearXNG client, hedging, fallback, relevance guard, search cache |
| `extract.py` | charset detection, main-content / whole-page / listing extraction, wall and binary detection |
| `links.py` | link-target safety: unsafe URL schemes neutralised in page HTML and in every markdown link form |
| `worker.py` | extraction lanes in recycled worker processes, in-flight byte budget |
| `browser.py` | raw-CDP client for the warm Chrome: contexts, settle loop, request interception |
| `egress.py` | the guarding forward proxy for Chrome and Firecrawl (also the single source of the "private address" rule) |
| `prefetch.py` | prefetch-on-search, single-flight |
| `cache.py` | Redis scrape cache, host memory |
| `settings.py` | every tunable, read from the environment |

[`docs/design.md`](docs/design.md) documents every tier, threshold and trade-off in detail.

## Quick start

Requirements: Docker with Compose v2, about 2 GB of free RAM.

```bash
git clone https://github.com/hudsonwa/fastweb-firecrawl.git && cd fastweb-firecrawl
scripts/init-env.sh          # writes .env with a generated SEARXNG_SECRET (needs openssl)
docker compose up -d --build
```

fastweb-firecrawl listens on `http://127.0.0.1:3002`. If that port is taken, set `FASTWEB_PORT` in `.env`. The image is named
after the compose project, so a second copy of the stack (`docker compose -p other up -d`, with another
`FASTWEB_PORT`) never overwrites the first one's image.

```bash
curl -s localhost:3002/v2/search -H 'content-type: application/json' \
  -d '{"query": "python asyncio gather documentation", "limit": 5}' | jq '.data.web[].url'

curl -s localhost:3002/v2/scrape -H 'content-type: application/json' \
  -d '{"url": "https://example.com/", "formats": ["markdown"]}' | jq -r .data.markdown
```

A scrape answer (real output, abridged; `X-Fastweb-Cache` says `MISS`, `HIT`, `HIT-PREFETCH` when a search's
prefetch already fetched the page, or `JOIN` when it shared an in-flight fetch):

```text
HTTP/1.1 200 OK
x-fastweb-cache: MISS

{"success":true,"data":{"markdown":"# Example Domain\n\nThis domain is for use in documentation examples ...",
 "metadata":{"title":"Example Domain","sourceURL":"https://example.com/","url":"https://example.com/",
 "statusCode":200,"contentType":"text/html; charset=utf-8",
 "fastweb":{"engine":"plain","fetch_ms":80,"extract_ms":80,"lane":"small"}}}}
```

With the Firecrawl SDK:

```python
from firecrawl import Firecrawl

fc = Firecrawl(api_key="unused", api_url="http://127.0.0.1:3002")
print(fc.search("tokio spawn blocking docs", limit=5))
print(fc.scrape("https://docs.python.org/3/library/json.html", formats=["markdown"]).markdown[:500])
```

Optional Firecrawl fallback (heavy: about 10 GB of images and several GB of RAM). Uncomment `FIRECRAWL_UPSTREAM`
and the two `FIRECRAWL_*` secrets in `.env` (`scripts/init-env.sh` generated them), then:

```bash
docker compose --profile firecrawl up -d
```

fastweb-firecrawl hands a page to Firecrawl only when the browser tier is off, busy or down, so with the default
`FASTWEB_BROWSER=1` a JS page still answers `engine: "browser"`. To check the profile itself (its `api` has no host
port), ask it directly from inside the fastweb-firecrawl container:

```bash
docker compose exec fastweb python -c "import httpx; print(httpx.post('http://api:3002/v2/scrape', \
  json={'url': 'https://example.com'}, timeout=60).text[:200])"
```

`scripts/smoke-firecrawl.sh` does the whole check in a throwaway compose project: it brings the profile up with the
browser tier off, expects `engine: "firecrawl"` for a JS page and a refusal for an internal address (direct, and after a real 302 from
a redirector container in the throwaway project), then removes it.

Running without Docker: `pip install .` installs the `fastweb_firecrawl` package and two commands, `fastweb-firecrawl` (the API on
127.0.0.1:3002; `--host` / `--port`, or `FASTWEB_HOST` / `FASTWEB_LISTEN_PORT`, change that) and `fastweb-firecrawl-egress`
(the egress proxy, 127.0.0.1:3128; `--host 0.0.0.0` or `EGRESS_HOST` to serve other machines, which the compose file
does inside its internal network). Both take `--help` and `--version`. You then
need to provide SearXNG, Redis and, for the browser tier, a headless Chrome behind the egress proxy yourself. The code
defaults name the compose services, so set `SEARXNG_URL` (for example `http://127.0.0.1:8080`) and `FASTWEB_REDIS_URL`
(empty = no cache); fastweb-firecrawl logs a `startup_config` warning for any backend host it cannot resolve. More in
[`examples/`](examples/).

## API compatibility

| Endpoint | Notes |
|---|---|
| `POST /v2/search`, `POST /v1/search` | `query`, `limit` (capped at 50; one SearXNG results page is returned, so with the default `yahoo` engine expect about 7 results whatever the `limit`; add engines in `SEARXNG_ENGINES` for more). v2 answers `data.web[{url,title,description,position}]`, v1 `data[{url,title,description}]`. No scraping of results inline (`scrapeOptions` is ignored). |
| `POST /v2/scrape`, `POST /v1/scrape` | `url`, `formats` (`markdown`, `html`, `rawHtml`, `links`; other formats are accepted but not produced, and are listed in `metadata.fastweb.ignored_formats`), `onlyMainContent`, `timeout` (capped at 120 s), `maxAge`, `storeInCache`, `zeroDataRetention`; `waitFor`, `includeTags`, `excludeTags`, `mobile` and allow-listed `headers` (`User-Agent`, `Accept`, `Accept-Language`, `Referer`, `Cookie`, `Authorization`, `Cache-Control`, `Pragma`) are passed to the Firecrawl fallback. `markdown` is returned when requested (or when no other content format is). **`html` is the same full page markup as `rawHtml`**, not Firecrawl's cleaned main-content HTML. Links in `markdown` and `links` are absolute, resolved against the final page URL (or its `<base href>`); markdown link targets with a scheme other than `http`, `https`, `mailto` or `tel` (`javascript:`, `vbscript:`, `data:`) are replaced by `#` (every markdown answer, including the Firecrawl fallback's: inline and image links, reference definitions (also inside block quotes and list items), autolinks and raw-HTML `href`/`src` attributes (any quoting, also `/`-separated), with entities, backslash escapes and embedded whitespace/control characters decoded before the scheme is checked; also inside code blocks), and the page's own `href`/`src` attributes get the same check before conversion; `html` and `rawHtml` are returned unmodified; `links` holds every `http(s)` link on the page, deduplicated, without fragments. Both are page-controlled content: sanitise before rendering, and scrape a returned link through fastweb-firecrawl rather than fetching it blindly. `metadata` carries `title`, `description`, `sourceURL`, `url`, `statusCode` (Firecrawl fallback answers keep an allow-list of page metadata: those keys, `language`, `keywords`, Open Graph and other page `<meta>` tags) and a `fastweb` object (`engine`, timings, fallback reason, `truncated` when the markdown hit the 500,000-character cap). |
| `GET /health`, `GET /` | liveness |
| `GET /v1/fastweb/stats` | counters: tiers, browser, prefetch, cache |

Not implemented: crawl, map, extract, batch scrape, actions, screenshots, change tracking, and API keys (any
`Authorization` header is ignored). Request bodies over 64 KiB are refused with 413.

Status codes are chosen so SDK retry logic does the right thing. firecrawl-py (v1 and v2 clients) retries exactly one
status, 502, with backoff, so fastweb-firecrawl answers 502 only when its own capacity is full and a retry can help: every browser
tab stayed busy for the request's budget, or a search queued past `FASTWEB_SEARCH_QUEUE_TIMEOUT`. Both carry
`Retry-After`. Failures are `{"success": false, "error": "..."}` with 400 (bad request), 413 (request body or page too
large), 422 (the target failed, e.g. `Target page returned HTTP 404`, a bot wall nothing could pass, a private address,
an unsupported type: retrying would not help), 429 (too many searches queued), 502 (busy, see above), 503 (search
backend down or no results) or 504 (timeout). Concurrent identical searches share one SearXNG call, and at most
`FASTWEB_SEARCH_CONCURRENCY` SearXNG calls run at once; the rest queue without their deadline running. A search with no
results after the fallback is a 503, not an empty success, so a client's own fallback runs and the empty answer is not
cached.

## Configuration

All settings are environment variables. With compose, set them in `.env`: compose passes every variable in this table
through to the container (a test keeps the two in step), except the in-network addresses it fixes itself (noted).
Defaults shown are the code defaults; compose overrides a few (noted).

| Variable | Default | Meaning |
|---|---|---|
| `FASTWEB_PORT` | `3002` | host port (compose only, bound to 127.0.0.1) |
| `SEARXNG_SECRET` | (required) | SearXNG secret key (compose only) |
| `FIRECRAWL_UPSTREAM` | empty | Firecrawl base URL for the last-resort fallback; empty = no fallback |
| `FASTWEB_FALLBACK_TIMEOUT` | `12` | seconds the Firecrawl fallback may take, whatever the caller's timeout |
| `FASTWEB_UPSTREAM_CONCURRENCY` | `3` | concurrent Firecrawl calls |
| `SEARXNG_URL` | `http://searxng:8080` (compose: always the `searxng` service) | SearXNG base URL; set it when running without Docker |
| `SEARXNG_ENGINES` | empty (compose: `yahoo`) | primary engines (comma list); empty = SearXNG defaults |
| `SEARXNG_CATEGORIES` | empty | SearXNG categories |
| `FASTWEB_SEARCH_FALLBACK_ENGINES` | empty (compose: `privacywall,brave`) | engines for the one retry / hedge |
| `FASTWEB_SEARCH_HEDGE` | `1.1` | seconds before the fallback starts in parallel with a slow primary |
| `FASTWEB_SEARCH_DEADLINE` | `3.0` | overall search deadline (seconds) |
| `FASTWEB_SEARCH_RELEVANCE` | `1` | relevance guard on/off |
| `FASTWEB_SEARCH_WARM` | `1` | at startup, one throwaway SearXNG query per engine set, so the first real search does not pay the engines' connection setup |
| `FASTWEB_SEARCH_CONCURRENCY` | `6` | SearXNG calls in flight at once; more searches queue, and each one's deadline starts when it gets a slot |
| `FASTWEB_SEARCH_QUEUE` | `128` | searches allowed to wait for a slot; beyond it `429` with `Retry-After` |
| `FASTWEB_SEARCH_QUEUE_TIMEOUT` | `10` | seconds a search may wait for a slot; beyond it `502` with `Retry-After` |
| `FASTWEB_SEARCH_TTL` / `_FALLBACK_TTL` | `900` / `120` | search cache TTL (seconds) for primary / fallback answers |
| `FASTWEB_REDIS_URL` | `redis://fastweb-redis:6379/0` (compose: always `fastweb-redis`) | cache Redis; empty = no cache or host memory; set it (or empty it) when running without Docker |
| `FASTWEB_FETCH_TIMEOUT` | `8` | plain-tier total timeout (seconds) |
| `FASTWEB_MAX_BYTES` | `6291456` | download cap per page |
| `FASTWEB_PLAIN_CONCURRENCY` | `8` | concurrent on-demand fetches |
| `FASTWEB_IMPERSONATE` | `1` | curl_cffi Chrome-impersonation tier on 401/403 |
| `FASTWEB_INFLIGHT_BYTES` | `50331648` | memory budget for page bodies held at once; over it, pages queue or fall back |
| `FASTWEB_EXTRACT_TIMEOUT` | `20` | hard limit per extraction (the worker is killed) |
| `FASTWEB_SCRAPE_TTL` | `600` | scrape cache TTL (seconds) |
| `FASTWEB_HOST_TTL` / `FASTWEB_FIRECRAWL_TTL` | `3600` / `1800` | host-memory TTLs (seconds) |
| `FASTWEB_BROWSER` | `1` | browser tier on/off |
| `FASTWEB_BROWSER_URL` | empty (compose: `http://browser:9222`) | Chrome DevTools endpoint; empty = no browser tier |
| `FASTWEB_BROWSER_EGRESS` | empty (compose: `http://browser-egress:3128`) | egress proxy, used to verify its refusal marker |
| `FASTWEB_BROWSER_TABS` | `2` | pages rendered at once |
| `FASTWEB_BROWSER_WAIT` | `0.5` | seconds a page waits for a tab before Firecrawl takes it; with no Firecrawl it waits for the rest of its budget (keeping 3 s to render), then `502` with `Retry-After` |
| `FASTWEB_BROWSER_SETTLE` / `_TIMEOUT` | `3.0` / `4.0` | settle cap / whole-render cap (seconds) |
| `FASTWEB_BROWSER_SUSPECT` | `1` | also render small app-shell pages that came back "ok" |
| `FASTWEB_BROWSER_RECYCLE` | `200` | restart Chrome every N pages |
| `FASTWEB_PREFETCH_N` | `5` | results prefetched per search; `0` disables prefetch |
| `FASTWEB_PREFETCH_CONCURRENCY` / `_QUEUE` / `_PER_HOST` | `4` / `64` / `2` | prefetch bounds |
| `FASTWEB_PREFETCH_TIMEOUT` / `_MAX_AGE` | `6` / `12` | per-page timeout / max queueing time (seconds) |
| `FASTWEB_PREFETCH_MAX_BYTES` / `_MAX_HTML` | `4194304` / `3500000` | prefetch download cap / markup cap per page |
| `FASTWEB_PREFETCH_MEM_FRAC` | `0.6` | skip prefetch above this share of the container memory limit |
| `FASTWEB_MAX_BODY_BYTES` | `65536` | largest accepted request body |
| `FASTWEB_MAX_TIMEOUT` / `FASTWEB_MAX_SEARCH_LIMIT` | `120` / `50` | caps on a scrape's `timeout` (seconds) and a search's `limit` |
| `FASTWEB_USER_AGENT` | Chrome 157 on macOS | User-Agent of the plain tier |
| `FIRECRAWL_POSTGRES_PASSWORD` / `FIRECRAWL_BULL_AUTH_KEY` | (required for the `firecrawl` profile) | Firecrawl's internal secrets (compose only) |

`docs/design.md` lists the remaining knobs. SearXNG engine choice and timeouts live in `searxng/settings.yml`; engine
behaviour varies by network location, so re-check them for yours.

## Security model

fastweb-firecrawl fetches arbitrary URLs on behalf of its callers, so the main risk is server-side request forgery (SSRF):
a caller (or a redirect, or JavaScript on a rendered page) making it reach internal services.

- **No authentication.** Compose publishes ports on `127.0.0.1` only. Treat fastweb-firecrawl like a local tool: if you expose
  it beyond the host, put an authenticating reverse proxy in front.
- **Cheap tiers:** redirects are followed manually; every hop's host is resolved and refused if any address is
  loopback, private, link-local, CGNAT, ULA, multicast, unspecified, an IPv6 form embedding IPv4 (mapped, NAT64,
  SIIT), site-local, or a compose service name. The connection is then pinned to the checked address (curl tier) or
  the connected peer is re-checked before any byte is sent (plain tier), which closes DNS rebinding between check
  and connect.
- **Browser tier:** Chrome sits on an `internal: true` Docker network with no gateway. Its only way out is
  `browser-egress`, a small proxy that resolves each host, refuses private addresses and ports other than
  80/443/8080/8443, and connects to the address it checked. QUIC and non-proxied WebRTC are disabled. Inside Chrome,
  fastweb-firecrawl also pauses every request (including those of workers and out-of-process iframes) over CDP and applies the
  same rule. Chrome runs as `nobody`, read-only, with all capabilities dropped and bounded pids; it keeps
  `--no-sandbox` because its sandbox needs privileges the container does not get, so the network boundary is the
  containment.
- **Firecrawl fallback (optional):** URLs refused by the guard are never handed to Firecrawl, and Firecrawl's final
  URL is checked by the same rule. Firecrawl fetches and renders by itself, though, so redirects in the middle of a
  chain, subresources and page JavaScript are **not** checked by fastweb. The compose `firecrawl` profile contains
  this the same way as the browser: Firecrawl runs on its own internal network with its own Redis, RabbitMQ and
  Postgres, and its only route out is a second instance of the egress proxy. It cannot reach fastweb-firecrawl's Redis, SearXNG
  or the browser. A Firecrawl you run elsewhere has whatever network access you give it.
- **Logs** record scheme, host and path only: no query strings, `;` path parameters or search queries. A token
  written as a plain path segment (some webhook URLs) is still logged.
- **Secrets** come only from `.env` (git-ignored). The committed SearXNG `secret_key` is a placeholder that
  `SEARXNG_SECRET` overrides.

Known residuals are listed in [SECURITY.md](SECURITY.md), with how to report a vulnerability.

## Benchmarks

Measured by the author on one machine, one residential network location, over a few days, comparing fastweb-firecrawl with a
self-hosted Firecrawl (default Docker setup, Playwright engine) run in the same session. Small samples: read them as
"roughly how much faster", not as precise figures. The harness is in [`bench/`](bench/) so you can reproduce them
for your own network.

| What | fastweb-firecrawl | Self-hosted Firecrawl |
|---|---|---|
| Search, cold (n = 8 and 14) | p50 0.83–0.86 s, p90 about 1.7 s | Firecrawl's own search hop added p50 0.70 s on top of SearXNG, tails to 1.5 s, 17.8 s when busy |
| Search, repeated query | about 10 ms (cache) | not cached |
| Scrape, 16-URL pack (`bench/data/fetch_urls.yaml`: 13 content pages, 2 missing pages, 1 bot-walled page) | 13 of 13 with the expected text; news homepages p50 0.62 s, max 1.8 s | 10 of 13; news homepages p50 4.6 s, a large Wikipedia list 30 s |
| Scrape, 11 JavaScript-heavy pages (`bench/data/js_pack.json`) | 7 of 11 found by the browser tier, p50 about 3.7 s, bounded at about 5.5 s | not re-measured on this pack (on an earlier 16-page version: 5 of 16, worst 30.9 s, against fastweb-firecrawl's 6–7 of 16) |
| Search, 50 concurrent distinct queries | p50 4.3 s, wall 8 s, 49 of 50 answered (one 503: deadline or no results); the queue holds SearXNG at `FASTWEB_SEARCH_CONCURRENCY` calls | n/a |
| Memory under a burst of 14 large pages | peak about 390 MiB inside a 768 MiB limit, no OOM | n/a |

The raw per-request rows of these runs were not kept, so the table cannot be checked row by row; `bench/bench.py`
writes JSONL rows (`--out`) and prints the summary, so a rerun on your network gives you checkable numbers.

Caveats:

- The comparison is against **self-hosted** Firecrawl, not Firecrawl's cloud service, which has more engines,
  proxies and anti-bot capability than either.
- Latency is dominated by the target sites and the search engine; numbers from another network will differ.
- Live sites change daily. A "found" result means a reference phrase was present in the returned markdown, checked
  automatically; it is not a human quality judgement.
- fastweb-firecrawl deliberately returns errors where it cannot get real content, so success counts compare honesty as much
  as capability.

## Limitations

- **Bot protection.** fastweb-firecrawl does not use residential proxies or CAPTCHA solving. Sites behind strong bot
  protection stay blocked (you get an honest 422).
- **JavaScript pages** take seconds, not milliseconds, and a page that needs more than about 4 s to settle is
  returned as rendered so far. Only `FASTWEB_BROWSER_TABS` (default 2) render at once: a burst of JavaScript pages
  queues for tabs, and what cannot get one within its own timeout gets a retryable 502.
- **Search quality depends on SearXNG's upstream engines**, which rate-limit and change behaviour. The defaults
  (yahoo first, privacywall/brave fallback) reflect what worked from one location; adjust `searxng/settings.yml`.
- **Only search and scrape** of the Firecrawl API are implemented, and only the commonly used options.
- **Single node.** State is per-process plus one Redis; there is no horizontal scaling story yet.
- **Long pages are cut** at 500,000 characters of markdown (and of PDF text); `metadata.fastweb.truncated` says so.
- **`html` is not cleaned:** it is the full page markup, the same as `rawHtml`.
- **Charset heuristics** favour Western pages: undeclared legacy Central European pages can come out as cp1252
  mojibake.
- **Heavy single-page apps** (some government portals, for example) can still be thin after the browser settles
  and get a 422; the optional Firecrawl fallback (`FIRECRAWL_UPSTREAM`) can help there.
- **Ports on the plain tier:** the browser and Firecrawl egress proxies refuse unusual ports, but plain HTTP
  fetches of public hosts do not, so an unreachable odd port times out (504) instead of a quick refusal.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install --no-deps -e .
.venv/bin/ruff check . && .venv/bin/mypy
.venv/bin/pytest            # unit + local-server tests in parallel, no network needed (about 30 s)
.venv/bin/pytest -m ""      # also the slow timing tests, as CI runs it (about 2 min)
```

Tests that need a real Chrome or the throwaway egress stack are skipped unless their environment variables are set
(`FASTWEB_TEST_CDP`, `FASTWEB_TEST_EGRESS_*`; see `tests/egress_stack/stack.sh`).

## Licence

MIT, see [LICENSE](LICENSE). Third-party components and their licences are listed in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
