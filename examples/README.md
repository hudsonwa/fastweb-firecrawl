# Examples

| File | What it shows |
|---|---|
| [`sdk_search_then_scrape.py`](sdk_search_then_scrape.py) | the official `firecrawl-py` SDK against fastweb: search, then scrape the top results (usually prefetched cache hits) |
| [`curl.sh`](curl.sh) | the raw API with curl and jq: search, scrape, an honest error, the stats counters |
| [`compose.remote-firecrawl.yml`](compose.remote-firecrawl.yml) | a compose override that uses an existing Firecrawl as the fallback |

All of them expect a stack started with `docker compose up -d` on `http://127.0.0.1:3002`.

The SDK example uses firecrawl-py's v2 client (`Firecrawl`). With firecrawl-py 4.x, v1-style search goes through
`V1FirecrawlApp(api_url=...)`; `Firecrawl().v1` has no `search`.
