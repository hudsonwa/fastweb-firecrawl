#!/bin/sh
# fastweb's API with plain curl (needs jq). Run against a stack started with `docker compose up -d`.
set -eu
API=${FASTWEB_API:-http://127.0.0.1:3002}

echo "== search (v2)"
curl -s "$API/v2/search" -H 'content-type: application/json' \
  -d '{"query": "python asyncio gather documentation", "limit": 5}' | jq -r '.data.web[] | "\(.position). \(.url)"'

echo "== scrape: markdown plus which tier answered and how long it took"
curl -s "$API/v2/scrape" -H 'content-type: application/json' \
  -d '{"url": "https://example.com/", "formats": ["markdown"]}' \
  | jq '{markdown: .data.markdown[0:200], fastweb: .data.metadata.fastweb}'

echo "== an honest error: a missing page is a 422, not a success"
curl -s "$API/v2/scrape" -H 'content-type: application/json' \
  -d '{"url": "https://example.com/this-page-does-not-exist"}' | jq .

echo "== counters"
curl -s "$API/v1/fastweb/stats" | jq '{scrape, prefetch: {scheduled: .prefetch.scheduled, done: .prefetch.done}}'
