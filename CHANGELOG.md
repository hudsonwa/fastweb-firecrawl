# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/) and
the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] - 2026-10-31

### Security

- Markdown reference definitions inside block quotes and list items no longer backtrack: a crafted page with many
  nested `>` markers could keep link rewriting busy for seconds per page. The container prefix is now matched
  atomically, and a regression test bounds it.

### Fixed

- The startup warm-up gives an engine set up after 3 engine-timeout answers instead of retrying for minutes when an
  engine always times out.
- The prefetch tests no longer read the host's memory, which held every prefetch back on Linux CI runners.

## [0.1.0] - 2026-10-30

First public release.

### API

- Firecrawl-compatible `POST /v1|v2/search` and `POST /v1|v2/scrape`, `GET /health`, `GET /v1/fastweb/stats`.
- Scrape formats `markdown`, `html`, `rawHtml` and `links`; `markdown` is returned when requested (or when no other
  content format is). `metadata.fastweb` carries the engine, timings, the fallback reason and `truncated` when
  markdown or PDF text hit the 500,000-character cap.
- Links in markdown and in `links` are absolute, resolved against the final page URL (or its `<base href>`).
  Table-layout listings (Hacker News style) come back one row per line with their links, and card-style news hubs
  (story cards as block links, or an empty overlay link beside the headline) as one block per story with its
  headline link. Markdown link targets with other schemes (`javascript:`, `vbscript:`, `data:`) are replaced by `#`
  in every link form (inline, image, reference definition also inside block quotes and list items, autolink,
  raw-HTML `href`/`src` with any quoting or a `/` separator), after entity, escape
  and whitespace decoding, on every markdown answer including the Firecrawl fallback's; page `href`/`src`
  attributes are checked the same way before conversion.
- Sign-in walls that repeat their prompt over a teaser (social profile pages) are treated as walls, not success;
  whitespace-only lines are collapsed outside code blocks.
- Requested formats fastweb-firecrawl does not produce (`screenshot`, `json`, ...) are listed in
  `metadata.fastweb.ignored_formats`. Fallback answers keep an allow-list of page metadata (Firecrawl's own
  accounting keys such as `scrapeId` and `creditsUsed` are dropped, as are URL-valued keys with an unsafe scheme)
  and an `http(s)`-only `links` array.
- A page the plain tier fetched but the browser got an error status for is reported as blocked (bot defence), not
  as a missing page.
- Status codes chosen for SDK retry logic: 422 for target failures; 502 with `Retry-After` (the one status
  firecrawl-py retries) only when fastweb-firecrawl's own capacity is full (every browser tab, the search queue wait); 429 when
  too many searches are queued; 503 when the search backend fails or finds nothing. Logs and error bodies never
  carry a URL's query string or a search query.

### Search

- SearXNG with a hedged fallback, an overall deadline, a relevance guard against off-topic and cross-query-leaked
  results, and a Redis cache.
- Concurrent identical searches share one SearXNG call; at most `FASTWEB_SEARCH_CONCURRENCY` SearXNG calls run at
  once and the rest queue, with each search's deadline starting once it has a slot.
- When the primary and fallback engines both come back empty with at least 0.5 s of the deadline left, the primary
  engines get one retry before the 503 (`source=retry`).
- At startup each engine set gets one throwaway query (`FASTWEB_SEARCH_WARM`): on a fresh SearXNG an engine's
  first call took about its own timeout, so the first real search could lose both attempts and answer 503.

### Scrape

- Tier ladder: plain HTTP/2 + trafilatura, curl_cffi Chrome impersonation on 401/403, a warm headless Chrome over
  raw CDP for thin, JavaScript-built, walled or blocked pages, and an optional Firecrawl fallback.
- Extraction in recycled worker processes under an in-flight byte budget; listing-page, whole-page, PDF, XML/JSON
  and large-page text paths; consent-wall, sign-in-wall, bot-challenge and binary-body detection.
- Per-host memory: tiers that never work for a host are skipped, and hosts that keep refusing fail fast.
- Scrape cache keyed per URL: the whole page is stored once and every request for a subset of the content formats
  is served from it. Concurrent requests for the same page share one fetch (and one failure).
- Prefetch-on-search: the top results are fetched into the scrape cache in the background, so the scrape that
  follows a search, with any content format, is a cache hit.
- Without a Firecrawl fallback, a page waits briefly for a free browser tab instead of failing.
- The browser settle loop counts how each settle ended (`settle_stable`, `settle_cap`, `settle_page_busy` in
  stats) and gives a render that is still thin with XHRs in flight at the cap one more settle within its budget
  (`settle_extended`). An app shell whose plain text is nearly all menu and footer links is tried in the browser
  too (suspect SPA).

### Security

- SSRF guard on every redirect hop, with the connection pinned to (or the peer re-checked against) the checked
  address. The browser and the optional Firecrawl run on internal networks whose only route out is a guarding egress
  proxy.
- Request bodies over 64 KiB are refused with 413 before they are buffered; scrape `timeout` is capped at 120 s and
  search `limit` at 50; only allow-listed request headers are passed to Firecrawl.
- Compose publishes ports on 127.0.0.1 only, drops capabilities, sets `no-new-privileges`, bounds memory and pids,
  pins every third-party image by digest, mounts SearXNG's config read-only and generates secrets with
  `scripts/init-env.sh`. Images are named after the compose project.

### Packaging and tooling

- Installable, typed package (`pip install .`, ships `py.typed`) with `fastweb-firecrawl` and `fastweb-firecrawl-egress` commands
  (`--host`, `--port`, `--version`, `--help`); both listen on 127.0.0.1 unless told otherwise.
- Fully typed (`mypy` with `disallow_untyped_defs`), clean under ruff's bugbear, pyupgrade, simplify, Ruff and pylint
  rule sets, and a parallel (pytest-xdist) test suite that runs without network access. CI pins actions by commit, runs
  pip-audit and a Docker build and boot smoke test; Dependabot is configured.
- Benchmark harness (`bench/bench.py`), `examples/`, issue and pull request templates and a code of conduct.

[Unreleased]: https://github.com/hudsonwa/fastweb-firecrawl/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/hudsonwa/fastweb-firecrawl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/hudsonwa/fastweb-firecrawl/releases/tag/v0.1.0
