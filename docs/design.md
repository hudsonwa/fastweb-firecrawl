# fastweb-firecrawl design notes

How fastweb-firecrawl works inside, for contributors. The [README](../README.md) has the overview and the API; this document
explains each tier, when it runs, what bounds it, and why. Every tunable mentioned here is listed once in
[Thresholds](#thresholds), with its environment variable.

Wherever this document says a page "goes to Firecrawl", that is the optional last tier: with `FIRECRAWL_UPSTREAM`
unset the request ends with the cheap tiers' own honest error instead (422 with the reason, or 504 on timeout).

## Contents

- [Code map](#code-map)
- [A scrape request, end to end](#a-scrape-request-end-to-end)
- [Tier 1: plain GET](#tier-1-plain-get)
- [Tier 2: Chrome impersonation](#tier-2-chrome-impersonation)
- [Tier 3: the browser](#tier-3-the-browser)
- [Tier 4: Firecrawl](#tier-4-firecrawl)
- [Extraction](#extraction)
- [Memory: worker lanes and the byte budget](#memory-worker-lanes-and-the-byte-budget)
- [Host memory](#host-memory)
- [Scrape cache and single-flight](#scrape-cache-and-single-flight)
- [Search](#search)
- [Prefetch-on-search](#prefetch-on-search)
- [Design decisions](#design-decisions)
- [Thresholds](#thresholds)

## Code map

| Module | Role |
|---|---|
| `fastweb_firecrawl/app.py` | HTTP layer: request limits, cache lookup, single-flight, status codes, logging, wiring |
| `fastweb_firecrawl/pipeline.py` | the tier ladder (`ScrapeService`, `Job`, `Fetched`, `Extracted`) |
| `fastweb_firecrawl/fetchers.py` | tiers 1 and 2: plain httpx GET and curl_cffi impersonation, manual redirects, download caps |
| `fastweb_firecrawl/guard.py` | SSRF guard: target check per hop, peer check after connect |
| `fastweb_firecrawl/upstream.py` | tier 4: the Firecrawl call and the checks on its answer |
| `fastweb_firecrawl/browser.py` | tier 3: raw-CDP client for the warm Chrome |
| `fastweb_firecrawl/egress.py` | the guarding forward proxy for Chrome and Firecrawl; owner of the "private address" rule |
| `fastweb_firecrawl/heuristics.py` | pure page checks: thin, walled, blocked, binary, suspect SPA |
| `fastweb_firecrawl/extract.py` | charset detection and HTML/PDF to markdown (runs in worker processes) |
| `fastweb_firecrawl/links.py` | link-target safety: unsafe URL schemes neutralised in page HTML and in every markdown link form |
| `fastweb_firecrawl/worker.py` | extraction lanes (recycled worker processes) and the in-flight byte budget |
| `fastweb_firecrawl/cache.py` | Redis: scrape cache, host memory, suspect-SPA losses |
| `fastweb_firecrawl/search.py` | SearXNG client, hedging, relevance guard, search cache |
| `fastweb_firecrawl/prefetch.py` | prefetch-on-search and single-flight |
| `fastweb_firecrawl/settings.py` | every tunable, read from the environment |

## A scrape request, end to end

```
POST /v2/scrape
  │ body ≤ 64 KiB, valid url/formats/timeout                  (else 400 / 413)
  ▼
scrape cache hit? ──yes──► cached answer (X-Fastweb-Cache: HIT | HIT-PREFETCH)
  │ no
  ▼
same key in flight? ──yes──► wait for it, reuse its answer or its failure (JOIN | JOIN-PREFETCH)
  │ no
  ▼
1. plain GET ──401/403──► 2. impersonated GET
  │
  ▼
extract in a worker lane ─► good content? ──yes──► answer (suspect SPA: maybe render first, keep the bigger)
  │ thin / walled / blocked / TLS stall / busy / too large
  ▼
3. browser render ─► same extraction + honesty checks ─► answer, or an honest 422
  │ browser off, busy, unreachable or crashed
  ▼
4. Firecrawl (capped) ─► same honesty checks ─► answer, or the cheap tiers' error
```

**One deadline.** The caller's `timeout` (default 30 s, clamped to 120 s) covers everything: download, extraction,
the browser, the Firecrawl queue and call. Every wait in the cheap tiers (for the byte budget, a download slot, an
extraction lane) stops while Firecrawl still has its cap, or half of what is left if less, so a busy fastweb-firecrawl hands the
page on instead of timing out. A lane's hard timeout is bounded the same way. When the deadline runs out the answer
is 504.

**Status codes.** firecrawl-py's v1 and v2 clients retry one status, 502, with exponential backoff (0.5 s, 1 s, ...)
and no other. fastweb-firecrawl therefore answers 502 only when a retry can help: its own capacity is full. Failures are
`{"success": false, "error": "..."}`:

| Status | Meaning |
|---|---|
| 400 | bad body: not JSON, no `url`, bad `formats` or `timeout` |
| 413 | request body over the limit, or the target page over the download cap with no tier left to try |
| 422 | the target failed: 404/410 or another error status, a wall nothing could pass, a private address, an unsupported or binary type, no extractable content, a dead host |
| 429 | too many searches already queued |
| 502 | busy, with `Retry-After`: no browser tab within the request's budget (no Firecrawl configured), or a search queued past `FASTWEB_SEARCH_QUEUE_TIMEOUT` |
| 503 | search backend down or no results (clients fall back to another provider; an empty success would be final) |
| 504 | timeout: the caller's deadline, a connect timeout, or a busy fastweb-firecrawl with no fallback |

Unexpected exceptions (odd URLs, IDNA errors, malformed upstream answers) are 422, never 500. Logs are one JSON line
per request with the URL reduced to scheme, host and path (no query strings, `;` path parameters or search queries). Exceptions are
described by class name (plus the status code of an HTTP error), never by their message: httpx and URL-parsing
errors quote the full request URL, and with it a search query or a page's query string.

**Answer shape.** `markdown` is returned when asked for (or when no other content format was); `html` and `rawHtml`
are both the page markup as fetched or rendered (fastweb-firecrawl does not produce Firecrawl's cleaned `html`).
`metadata.fastweb` carries `engine` (`plain`, `impersonate`, `browser`, `firecrawl`), timings, `lane`, any
`fallback_reason`, and the flags `truncated` (markdown hit the 500k-character cap) and `text_only_large`.

## Tier 1: plain GET

**Purpose.** Most pages need no browser: one HTTP/2 request with a desktop Chrome header set and a C-backed parser
answer them in well under a second.

**Behaviour.** Redirects are followed by hand, at most 10 hops. Before each hop the [guard](#ssrf-guard) resolves the
host and refuses private targets; after the TCP connect the peer address is checked again, before any request byte is
sent, so a name that rebinds between the check and the connect is caught. Bodies over the download cap are refused
from `Content-Length` or aborted mid-stream.

**Failure modes.**
- Connection refused or unreachable, or a connect timeout *before* the TLS handshake started: a dead host. 422 or 504
  at once, no later tier (it would only burn the caller's budget).
- A stall or error *after* the ClientHello (JA3 filtering), TLS errors, read timeouts, HTTP/2 resets: the browser or
  Firecrawl may get through, so the ladder continues.
- Over the download cap: Firecrawl may still render it (on demand only).

**Content types.** HTML goes to extraction; PDF to pypdf; `text/plain`, markdown, CSV, JSON (pretty-printed and
fenced), XML feeds and sitemaps, and JavaScript are returned decoded (XML that is really XHTML is treated as HTML).
Anything else is 422 `Unsupported content type`. A text-typed body that is really binary (a known magic number;
more than 2 % control characters or 25 % U+FFFD in any 8 KB window; any NUL in a body under 512 characters) is 422
`Unsupported content: binary body`, with no later tier: a browser renders it no better. Mislabelled cp1252 text and
ANSI logs stay text.

### SSRF guard

A target is refused if its host is a compose service name or Docker host alias, ends in `.localhost`, `.internal` or
`.local`, or resolves to any address that is loopback, private (RFC 1918), link-local, CGNAT, ULA, site-local,
multicast, unspecified, or an IPv6 form that embeds IPv4 (mapped, NAT64, SIIT, IPv4-compatible, 6to4). The address
rule lives in `egress.py`, so fastweb-firecrawl, the browser's proxy and Firecrawl's proxy share one definition.

## Tier 2: Chrome impersonation

**Purpose.** Some sites refuse non-browser TLS and HTTP/2 fingerprints with 401/403.

**Trigger.** Plain got 401 or 403, or the host is remembered as needing more than plain (`NEEDS_RENDER` host memory).

**Behaviour.** curl_cffi with Chrome's fingerprint, sharing the plain tier's time budget rather than getting a fresh
one. Same manual redirects, caps and guard; curl is pinned to the address the guard approved (`CURLOPT_RESOLVE`), so
a rebinding name gets no request bytes, and the connected peer is checked as a backstop. Its result replaces the
plain one only when it got past the refusal.

## Tier 3: the browser

**Purpose.** JavaScript-built pages, consent walls and challenges that a real browser can pass.

**Trigger.** On demand only (never for prefetch), and only when the cheap tiers ended in something a browser can fix:
a thin page (JS shell, consent, challenge), 401/403/429 or a challenge 503, a host remembered as needing more than
plain, or a TLS-handshake stall. Never for PDFs, binary or too-large pages, 404/410, DNS or connect failures, or dead
hosts.

**After the render** the HTML goes through the same extraction lanes and honesty checks. A render that is still thin,
walled or an error is an honest 422, **not** a Firecrawl attempt: in measurements Firecrawl almost never found content
Chrome missed, and each such failure cost it up to 9 s. Firecrawl is used only when the browser tier is off, busy,
unreachable or crashed.

**Client.** One persistent CDP websocket (lazy, reconnecting with backoff from 1 s to 30 s), a fresh browser context
per page (disposed even on cancellation, in the background with a 1 s timeout), at most 2 pages at once; if no tab
frees within 0.5 s the page goes to Firecrawl as "browser busy". Chrome is recycled every 200 pages and Docker
restarts it in about a second.

**Settle.** After DOMContentLoaded, `innerText.length` is polled every 50 ms until it is non-zero and stable for
200 ms with no XHR or fetch in flight, capped at 3 s; the whole render is capped at 4 s, then the HTML so far is used.
Every `Runtime.evaluate` has its own 0.5 s timeout inside that budget.

**Suspect SPA.** A plain 200 whose main text is under 2,500 characters on an app shell (a root `div` such as
`#root`/`#app`/`#__next`, `__NEXT_DATA__`, `/_next/static/`, `ng-version`, `data-reactroot`, or a module-script page
whose markup is over 30 times its text) is rendered too. The render replaces the plain result only if its text is at
least twice as long and 1,000 characters longer; otherwise, and on any browser failure, the plain result stands
(never a 422). A loss is remembered for an hour per host and path class (up to two parent directories), so the next
request does not pay the render again. On 60 real agent-requested URLs it fired on 4: three app shells won (one went
from 1,967 to 222,265 characters), one news listing kept the plain page.

### Network containment

`browsernet` is an `internal: true` Docker network: Chrome has no route to the internet, the Docker host, the LAN or
a VPN. Its only way out is `--proxy-server=http://browser-egress:3128`, with `--proxy-bypass-list=<-loopback>`,
`--disable-quic`, `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` and `--block-new-web-contents`.

`browser-egress` (`egress.py`, same image, its own 128 MB container) accepts `CONNECT host:port` and absolute-URI
HTTP. Per request it resolves the host, refuses (403, counted, logged) unless every address is public and the port is
80, 443, 8080 or 8443, then connects to the address it checked, so DNS rebinding cannot swap it. It splices bytes with
an idle timeout (30 s), a total timeout (300 s) and a connection cap (1024); either side's EOF closes both, so a
disposed page frees its tunnels at once. `GET /stats` returns counters.

The proxy marks its own answers `X-Fastweb-Egress: refused|busy <token>`, with a random per-start token served only
at its `GET /token`. That marker, `ERR_PROXY_CONNECTION_FAILED` and `ERR_TUNNEL_CONNECTION_FAILED` are the tier's own
failures (BrowserDown/BrowserBusy, then Firecrawl), never "the target returned 503".

Inside Chrome, as defence in depth, `Fetch.enable` pauses the page's requests and fastweb-firecrawl's guard decides; workers and
out-of-process iframes are auto-attached paused and get the same guard before they run. The same handler fails
images, fonts, media and tracker hosts (never CSS). WebSocket and WebRTC are removed from pages and `ws://`/`wss://`
are blocked in page sessions; a worker's WebSocket is guarded by the proxy. The final document URL is checked again
after the render. Interception costs about 0.1 ms per request at p50.

The real-Chrome test (`test_real_chrome_every_vector_goes_through_the_proxy`, harness in `tests/egress_stack/`)
checks that workers, out-of-process iframes, popups, prefetch, beacons, forms, meta refresh, WebSocket and WebRTC all
go through the proxy: 9 of 9 vectors refused, 0 hits on the private canary.

The container runs as uid 65534, read-only with a tmpfs `/tmp`, `pids_limit` 256, all capabilities dropped,
`no-new-privileges`, and no published port. `--no-sandbox` stays (the image sets it): Chrome's sandbox needs user
namespaces or a setuid helper, which an unprivileged container does not get, so the network boundary is the
containment.

**Liveness.** `browser/supervise.sh` runs Chrome and asks CDP `/json/version` every 2 s; three failures in a row after
an 8 s grace kill it, the container exits and Docker restarts it. In fastweb-firecrawl, a render that times out while
`Browser.getVersion` also does not answer drops the connection (`wedged` in stats) and reconnects with backoff.

**Settle.** After DOMContentLoaded the body text length is polled until it holds for 0.2 s with no XHR/fetch in
flight, capped at the settle time. Stats count how each settle ended (`settle_stable`, `settle_cap`,
`settle_page_busy`); a render still under 1,000 characters with requests in flight at the cap settles once more,
within the page budget (`settle_extended`), for listings filled by a late XHR.

## Tier 4: Firecrawl

**Purpose.** A last resort when the browser tier cannot run, and for pages the browser cannot help with but
Firecrawl might (over the download cap, or refused by fastweb-firecrawl's own budget or extraction limits).

**Behaviour.** `POST {FIRECRAWL_UPSTREAM}/v2/scrape`, at most 3 at once, capped at 12 s whatever the caller's timeout.
Only a fixed set of scrape options is passed on (`url`, `formats`, `onlyMainContent`, `waitFor`, `includeTags`,
`excludeTags`, `mobile`, and allow-listed request `headers`: never `Host`, hop-by-hop, proxy or forwarding headers).
A *thin* page is sent `waitFor: 3000` unless the caller set one.

**Checks on its answer.** Firecrawl's final URL must pass the SSRF guard; a target status ≥ 400 is a 422; empty
markdown (when markdown was asked for) and short bot-challenge pages are 422. `success: false` from Firecrawl is
never treated as evidence about the target (its own timeouts and DNS errors look the same).

**Containment.** Firecrawl fetches and renders pages itself, so fastweb-firecrawl's per-hop checks do not apply inside it. In
the compose `firecrawl` profile it therefore runs on its own `internal: true` network with its own Redis, RabbitMQ
and Postgres; its only route out is `firecrawl-egress`, a second instance of the guarding proxy, configured as its
HTTP(S) proxy. It cannot reach fastweb-firecrawl's Redis, SearXNG or the browser, and no host port is published. A Firecrawl
request that ignores the proxy settings has no route out and fails. A Firecrawl you run elsewhere has whatever
network access you give it; see SECURITY.md.

## Extraction

`extract.py` runs in worker processes, never in the server process.

**Charset.** BOM, then the HTTP header, then `<meta charset>` in the first 4 KB, then strict UTF-8, then
windows-1252 when it decodes cleanly with sparse non-ASCII bytes (the WHATWG default), then charset_normalizer.
Trade-off: undeclared cp1250/cp1257/cp1254 pages that also decode cleanly as cp1252 come out as mojibake; undeclared
legacy pages are rare and Western ones the commoner case.

**Main content.** trafilatura's main content for articles. Whole-page markdown (selectolax strips script, style, nav,
aside, hidden elements and buttons; markdownify converts the `<body>`, keeping header, footer and tables) is used when
`onlyMainContent` is false, on github.com, when the main content is under 15 % of a body over 3,000 characters, or on
link hubs (over 50 % of body text in links, main under 30 %). Listing pages keep their linked items. When trafilatura's output is a
long run of short link-free lines (a table layout flattened one cell per line, as on Hacker News), header-less tables
are rendered as rows of inline text with their links instead. trafilatura is called without the page URL (given one,
it resolves relative links against the site root), and every markdown link target is then resolved against the final
URL or its `<base href>`; walls keep their short text unchanged. Title and description come from `<head>`.

**Thin and walled.** A result is thin, and goes to the next tier, when: it has under 500 characters on a page with
JS-shell markers; under 2,000 characters with "enable JavaScript" or bot-challenge text; under 3,000 characters with
IAB/TCF consent text; under 300 characters from over 50 KB of HTML; or under 100 characters on any HTML page. Never
whole-page when the main or non-link text carries such a marker: walls stay thin.

**Linear time.** No regex with lazy `.*?` runs on page content. `lighten()` is a linear forward search that stops at
the first unclosed block; whitespace runs over 64 characters are squeezed before markdownify, whose own whitespace
regexes are quadratic. 2,000 unclosed tags or a 50,000-space `<pre>` take under a second.

**Big pages.** HTML still over 3 MB after `lighten()` gets lexbor-only text of the whole page in the big lane
(`text_only_large`), after the same binary and wall checks. Whole-page markdownify only runs on an attribute-stripped
`<body>` of at most 1.5 MB. Markdown and PDF text are capped at 500,000 characters (`truncated`).

## Memory: worker lanes and the byte budget

**Lanes.** `worker.py` keeps one worker process per lane, forked from a forkserver with the libraries preloaded
(about 25 ms per fresh worker): `small` (HTML up to 500 KB after `lighten()`, recycled every 25 pages), `big`, `pdf`
and prefetch's `pf` (each recycled after every page; `pf` runs at nice 10). Lanes are chosen by the markup left after
`lighten()`, so a script-heavy 1.4 MB homepage is a small page. A page that runs past the extraction timeout has its
worker killed; a worker that dies (OOM) is replaced; either way the page goes to the next tier with that reason.
Memory goes back to the OS with each recycled process. Idle RSS is about 90 MiB, by design: the first page does not
pay the forkserver start. Compose `init: true` reaps workers orphaned by a forkserver death.

**In-flight byte budget** (48 MB). Every page held in the server process counts from before its body is read
(`Content-Length`, or 256 KB grown as chunks arrive) until the response is built. Before decoding it grows to the
bytes plus twice the decoded string (1, 2 or 4 bytes per character: one emoji makes the whole page 4 bytes per
character); the raw bytes are dropped once decoded. A request that does not fit waits while Firecrawl still has its
cap, then goes on ("fastweb busy"). A request alone always fits, and so does the oldest holder. Heavy bursts degrade
to the later tiers, not to an OOM.

**Measured.** A burst harness (10 Wikipedia list pages of 1.3–3.4 MB plus 4 script-heavy 5.2 MB homepages, 14
requests at once, 3 bursts, 1 CPU, two runs) peaked at 381–395 MiB inside the 768 MiB limit and retained 123–127 MiB,
with no OOM. 512 MiB passed that mix but not a heavier 16-page one, so 768 MiB is the supported limit. glibc is told to
keep few arenas and trim early (`MALLOC_ARENA_MAX=2`, 128 KB trim and mmap thresholds, set in the Dockerfile).

**Request bodies** are capped at 64 KiB (`Content-Length` first, then while streaming), so a large POST is refused
before it is buffered.

## Host memory

Kept in fastweb-firecrawl's own Redis (192 MB, `allkeys-lru`, no persistence). Read on the request's host, written on the
*final* host after redirects, so a redirecting URL (shorteners redirect everywhere) never gets skip-plain or
fail-fast.

| Key | Set when | Effect | TTL |
|---|---|---|---|
| `fastweb:host:<host>=firecrawl` (`NEEDS_RENDER` in cache.py; the value predates the browser tier) | plain was blocked and a later tier (browser or Firecrawl) worked | skip plain (curl_cffi still runs); 1 in 10 requests re-probes plain; a later-tier failure deletes it | 30 min |
| `fastweb:hostobs:<host>` | the target itself refused: plain *and* curl_cffi got 401/403 *and* the browser or Firecrawl saw 401/403 or a challenge | observations towards DEAD | 1 h |
| `fastweb:host:<host>=dead` | ≥ 3 observations on ≥ 2 paths across ≥ 10 min | skip Firecrawl; plain + curl_cffi get 1.2 s; otherwise 422 `Host … is blocked`; 1 in 20 requests still tries the fallback | 1 h |
| `fastweb:suspect_loss:<host>/<path class>` | a suspect-SPA render lost to the plain page | skip the render | 1 h |

Any success clears the host's memory and pending observations. Never evidence: 429, transport errors, budget or cap
timeouts, `success: false` from Firecrawl, or anything from a prefetch. Redis errors disable Redis for 30 s and
fastweb-firecrawl behaves as without it.

## Scrape cache and single-flight

The cache key hashes `url`, `formats` and `onlyMainContent`. Content-neutral keys (`origin`, `integration`, `maxAge`,
`storeInCache`, `skipTlsVerification`, `removeBase64Images`, `blockAds`, `fastMode`, `mobile: false`, `timeout`) are
ignored, so firecrawl-py's real body is cached; any other key (`waitFor`, `actions`, `headers`, tag filters, format
options) bypasses the cache. Entries are zlib-compressed, at most 1 MB compressed, successes only, 600 s TTL, and carry
their age: one is served only if its age is within `maxAge` (`maxAge: 0` forces a fresh fetch).
`storeInCache: false` and `zeroDataRetention: true` skip the write.

**Single-flight.** An on-demand scrape of a key already being fetched (by prefetch or another request) waits for that
fetch, within its own timeout, and reuses the answer (`X-Fastweb-Cache: JOIN | JOIN-PREFETCH`). When an on-demand
leader fails with an answer about the page (a 413 or 422 that is not transient or "busy"), its joiners return the
same failure instead of each re-running the ladder. Leader timeouts and prefetch failures are not shared: a joiner
with a longer budget, or the full ladder, may still succeed.

## Search

SearXNG with `SEARXNG_ENGINES` (compose: `yahoo`). If that returns nothing, errors, or is still running after 1.1 s,
one second attempt with the fallback engines (compose: `privacywall,brave`) runs in parallel ("hedged": the first
non-empty answer wins, the primary preferred within 50 ms), all inside a 3 s deadline. If both come back empty or
fail with at least 0.5 s of the deadline left, the primary engines get one retry (an engine's empty answer is often
transient: the same query answers seconds later). If that fails too the answer is 503
`success: false`, so clients fall back to another backend; an empty success would be final and might be cached by
the client. `limit` is clamped to 50 (SearXNG rarely returns more than about 20).

Non-empty answers are cached in Redis for 900 s (fallback, hedge and retry answers for 120 s), keyed on the engines, a
limit bucket and the normalised query. Headers: `X-Fastweb-Cache: HIT | MISS | JOIN` and
`X-Fastweb-Search: source=primary|fallback|hedge|retry|hit|none;attempts=N`.

**Engine choice.** bing is disabled: in testing it answered some queries with unrelated retail pages carrying bing's
`msockid` session parameter (another query's results leaking through a shared session) and others with silent
empties. yahoo (Bing-backed, so the same failure can return) is primary. yahoo answers in about 0.75–0.8 s at p50 with
a tail past 1.0 s, so `searxng/settings.yml` gives it a 1.5 s timeout and the hedge starts at 1.1 s. From one network
location privacywall answered 3 of 3 fallback probes and brave 0 of 3 (HTTP 429, then suspended); brave stays in the
set for when it is not suspended.

**Relevance guard.** Query content tokens (NFKC, casefold, accents stripped, hyphenated words joined, no stopwords,
one-character or non-Latin tokens; numbers kept) are matched against each result's title, snippet and URL tokens,
with prefix, stem and compound matching. A result with no match is off-topic; with four or more query terms, so is a
result whose only match is a generic word ("news", "latest", ...). An `msockid` URL, or a host outside a `site:`
query's site, is off-topic whatever its text. An engine with at least half clear misses in the top 10 (and at least 3
results) loses all its results; if what remains is still at least half misses, the answer is poisoned: dropped, never
cached, and the fallback runs. Limitation: a leaked page that shares a distinctive query token is kept.

## Prefetch-on-search

After a search answer is built, the top 5 result URLs are queued and fetched in the background into the scrape cache
under the key of firecrawl-py's scrape body, so the follow-up scrape is a cache hit. Queuing is synchronous: the
search answer is never delayed or changed.

**Cheap tiers only.** Plain and impersonation, 6 s per page, downloads capped at 4 MB, non-page content types not
downloaded, markup over 3.5 MB after `lighten()` not extracted. Never the browser or Firecrawl, no retries, the same
SSRF guard and honesty checks. Only successes are cached, and a prefetch never writes host memory.

**Bounds.** 4 workers with their own download slots (on-demand keeps all of its own), a 64-job queue (oldest dropped),
2 in flight per host, jobs older than 12 s dropped. Prefetch is skipped above 60 % of the container's memory limit.
Its bodies count against the byte budget, and it skips rather than waits for it.

**On-demand first.** A job waits (up to 2 s) while on-demand pages are in their cheap tiers or download slots are
nearly full. After its download it waits again while an on-demand request is in its cheap tiers, holding no budget
bytes meanwhile, and gives up rather than extract beside one, or with under 3 s left (`gave_up`, not `failed`). A
job that queued for the `pf` lane gives up if, once it holds it, less time is left than its markup needs
(max(1.5 s, 0.75 s + 1 s per MB); a PDF needs 3 s). A started extraction gets up to 2 s past the prefetch deadline,
since at nice 10 beside an on-demand extraction it gets little CPU. A prefetch that an on-demand request has joined
is promoted: it no longer waits or gives up.

**Skipped:** non-HTTP(S) URLs, image/video/audio/archive/font paths, `x.com` and `twitter.com` (login walls), hosts
remembered as DEAD or NEEDS_RENDER, and URLs already cached, queued or in flight.

## Design decisions

**Why 502 only for "busy".** Firecrawl SDKs retry 502 with backoff. A page that failed gets a 422 with the real
reason, which ends the request at once and tells the agent why; retrying it would only repeat the failure. When
fastweb-firecrawl itself is full (every browser tab busy, the search queue too slow), a retry a moment later usually succeeds,
so that one case answers 502 and lets the SDK retry without the agent doing anything.

**Why honest 422s instead of "success".** An agent that receives a consent wall or a JS shell as content wastes
tokens and reasons about the wrong page. fastweb-firecrawl would rather say it could not get real content.

**Why the browser's failures do not go to Firecrawl.** Measured on the JS pack, Firecrawl almost never found content
the warm Chrome missed, and each such attempt cost up to 9 s. Firecrawl stays for the cases where the browser could
not run at all.

**Why dead hosts fail fast.** A connection refused or a connect timeout before TLS says the host is down; every later
tier would only burn the caller's budget on the same failure.

**Why host memory is written on the final host.** URL shorteners and tracking redirectors point everywhere; keying on
them would make one bad destination poison every link through them.

**Why extraction runs in recycled processes.** trafilatura, lxml and markdownify fragment the heap on multi-megabyte
pages; returning the memory to the OS by ending the process is the only reliable bound inside a 768 MiB container.

## Thresholds

| Setting | Default | Variable | Bounds |
|---|---|---|---|
| connect timeout | 3.6 s | (code) | one TCP connect; survives Linux SYN retransmits at 1 s and 3 s |
| plain-tier total | 8 s | `FASTWEB_FETCH_TIMEOUT` | plain + impersonation download time |
| download cap | 6 MB | `FASTWEB_MAX_BYTES` | one page body |
| redirect hops | 10 | (code) | manual redirects per tier |
| on-demand download slots | 8 | `FASTWEB_PLAIN_CONCURRENCY` | concurrent plain/impersonated fetches |
| in-flight byte budget | 48 MB | `FASTWEB_INFLIGHT_BYTES` | page bytes held at once (compose: 48 MiB) |
| extraction hard timeout | 20 s | `FASTWEB_EXTRACT_TIMEOUT` | one extraction; the worker is killed after it |
| text-only threshold | 3 MB | `FASTWEB_EXTRACT_MAX_HTML` | markup after `lighten()` above which only lexbor text is produced |
| markdown cap | 500,000 chars | (code) | markdown and PDF text (`truncated`) |
| request body | 64 KiB | `FASTWEB_MAX_BODY_BYTES` | search and scrape POST bodies |
| caller timeout cap | 120 s | `FASTWEB_MAX_TIMEOUT` | scrape `timeout` |
| search limit cap | 50 | `FASTWEB_MAX_SEARCH_LIMIT` | search `limit` |
| browser tabs | 2 | `FASTWEB_BROWSER_TABS` | concurrent renders |
| browser tab wait | 0.5 s | `FASTWEB_BROWSER_WAIT` | wait for a tab before "browser busy" with Firecrawl configured; without it a page waits for the rest of its own budget, keeping 3 s to render (`BUSY_MIN_RENDER`), then gets `502` with `Retry-After` |
| search upstream slots | 6 | `FASTWEB_SEARCH_CONCURRENCY` | SearXNG calls at once; identical in-flight searches share one call; the deadline starts once a search has a slot |
| settle cap / render cap | 3 s / 4 s | `FASTWEB_BROWSER_SETTLE` / `_TIMEOUT` | one render |
| browser recycle | 200 pages | `FASTWEB_BROWSER_RECYCLE` | Chrome restarts |
| Firecrawl cap | 12 s | `FASTWEB_FALLBACK_TIMEOUT` | one Firecrawl call, whatever the caller's timeout |
| Firecrawl concurrency | 3 | `FASTWEB_UPSTREAM_CONCURRENCY` | concurrent Firecrawl calls |
| Firecrawl `waitFor` on thin pages | 3 s | (code) | unless the caller set one |
| reserved for our own answer | 1 s | (code) | kept back from the caller's budget |
| scrape cache TTL | 600 s | `FASTWEB_SCRAPE_TTL` | |
| NEEDS_RENDER host memory | 30 min, 1 in 10 re-probed | `FASTWEB_FIRECRAWL_TTL`, `FASTWEB_REPROBE_RATE` | |
| DEAD host memory | 1 h, 1 in 20 re-probed, 1.2 s budget | `FASTWEB_HOST_TTL`, `FASTWEB_DEAD_REPROBE_RATE` | |
| search hedge / deadline | 1.1 s / 3 s | `FASTWEB_SEARCH_HEDGE` / `_DEADLINE` | |
| search cache TTL | 900 s / 120 s fallback | `FASTWEB_SEARCH_TTL` / `_FALLBACK_TTL` | |
| prefetch N / workers / queue / per host | 5 / 4 / 64 / 2 | `FASTWEB_PREFETCH_N` / `_CONCURRENCY` / `_QUEUE` / `_PER_HOST` | |
| prefetch timeout / max age | 6 s / 12 s | `FASTWEB_PREFETCH_TIMEOUT` / `_MAX_AGE` | |
| prefetch caps | 4 MB download, 3.5 MB markup | `FASTWEB_PREFETCH_MAX_BYTES` / `_MAX_HTML` | |
| prefetch memory gate | 60 % of the limit (768 MiB without a cgroup) | `FASTWEB_PREFETCH_MEM_FRAC`, `FASTWEB_PREFETCH_MEM_LIMIT` (the limit used when there is no cgroup limit) | |
| thin page | see [Extraction](#extraction) | (code, `heuristics.py`) | |
| suspect SPA | < 2,500 chars on an app shell; render must be ≥ 2× and +1,000 | `FASTWEB_BROWSER_SUSPECT` | |
