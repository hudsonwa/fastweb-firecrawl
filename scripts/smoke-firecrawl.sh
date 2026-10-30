#!/bin/sh
# Bring up the stack with the optional Firecrawl profile under its own compose project, prove a scrape that needs
# the fallback comes back from Firecrawl and an internal address is refused (through api, and straight at
# playwright-service), then tear that project down.
#
#   scripts/smoke-firecrawl.sh            # project fastweb-fcsmoke on 127.0.0.1:3092 (override: PROJECT, PORT)
#
# Needs Docker with several GB of free memory and network access to the target sites. Uses a throwaway env file,
# never your .env, and removes only the containers, volumes and images of its own project.
set -eu
cd "$(dirname "$0")/.."
PROJECT=${PROJECT:-fastweb-fcsmoke}
PORT=${PORT:-3092}
ENV_FILE=$(mktemp)
gen() { openssl rand -hex "$1"; }
cat > "$ENV_FILE" <<EOF
SEARXNG_SECRET=$(gen 32)
FASTWEB_PORT=$PORT
FIRECRAWL_UPSTREAM=http://api:3002
FIRECRAWL_POSTGRES_PASSWORD=$(gen 24)
FIRECRAWL_BULL_AUTH_KEY=$(gen 24)
FASTWEB_BROWSER=0
FASTWEB_PREFETCH_N=0
EOF
# A redirector this script controls, for the redirect check below: a plain-HTTP server that answers every request
# with a 302 to the metadata address. The egress proxy lets Chrome reach it by name and port only (EGRESS_ALLOW, the
# proxy's test hook); the redirect target still meets the normal private-address check. Public redirectors cannot
# be used: they now refuse to redirect to private addresses, so nothing would be tested.
OVERRIDE=$(mktemp)
cat > "$OVERRIDE" <<'EOF'
services:
  firecrawl-egress:
    environment:
      EGRESS_ALLOW: smoke-redirector:8080
  smoke-redirector:
    profiles: [firecrawl]
    build: .
    networks: [firecrawlnet]
    command:
      - python
      - -u
      - -c
      - |
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.send_header("Content-Length", "0")
                self.end_headers()
        HTTPServer(("0.0.0.0", 8080), H).serve_forever()
EOF
dc() {
  docker compose -p "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" --env-file "$ENV_FILE" --profile firecrawl "$@"
}
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    dc logs --tail 40 fastweb api playwright-service firecrawl-egress smoke-redirector || true
  fi
  dc down -v --rmi local || true
  rm -f "$ENV_FILE" "$OVERRIDE"
  exit "$status"
}
trap cleanup EXIT INT TERM

# Only fastweb and api (and what they depend on): no browser, so a JS page has to go to Firecrawl.
dc up -d --build fastweb api smoke-redirector

scrape() {
  curl -sS -m 90 -X POST "http://127.0.0.1:$PORT/v2/scrape" -H 'content-type: application/json' -d "$1"
}

# Firecrawl's api takes a while to accept jobs after its container starts.
i=0
until out=$(scrape '{"url":"https://quotes.toscrape.com/js/","maxAge":0}') \
    && printf '%s' "$out" | grep -q '"engine":"firecrawl"'; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "no Firecrawl answer: $out" >&2
    exit 1
  fi
  sleep 10
done
echo "ok: the Firecrawl fallback answers"

# A JavaScript page over both schemes through fastweb's Firecrawl fallback. The quotes reach the markdown only once
# the page's script has run (the raw markup holds them inside a <script>, which markdown drops), so Firecrawl's
# playwright engine must have rendered the page.
for url in https://quotes.toscrape.com/js/ http://quotes.toscrape.com/js/; do
  out=$(scrape "{\"url\":\"$url\",\"maxAge\":0}")
  printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)["data"]
assert d["metadata"]["fastweb"]["engine"] == "firecrawl", d["metadata"]["fastweb"]
assert "Albert Einstein" in d["markdown"], d["markdown"][:300]
' || { echo "JS page not rendered through the Firecrawl fallback: $url: $out" >&2; exit 1; }
  echo "ok: $url rendered by the Firecrawl fallback"
done

# Egress refusals of the metadata address for the private-address reason (not, say, a port rule).
refused() { dc logs firecrawl-egress | grep '169.254.169.254' | grep -c 'private address' || true; }

# Firecrawl itself must not reach an internal address.
before=$(refused)
out=$(dc exec -T fastweb python -c "import httpx; print(httpx.post('http://api:3002/v2/scrape', \
json={'url': 'http://169.254.169.254/latest/meta-data/'}, timeout=60).text)")
if printf '%s' "$out" | grep -q '"success":true'; then
  echo "Firecrawl fetched an internal address: $out" >&2
  exit 1
fi
[ "$(refused)" -gt "$before" ] || { echo "egress proxy did not refuse it as a private address: $out" >&2; exit 1; }
echo "ok: internal address refused by firecrawl-egress"

# Straight at playwright-service, whose own pre-check is off (ALLOW_LOCAL_WEBHOOKS): its Chrome leaves only through
# firecrawl-egress. A public JS page renders over both schemes; an internal address is refused as private.
render() {
  dc exec -T fastweb python -c "import sys, httpx; r = httpx.post('http://playwright-service:3000/scrape', \
json={'url': sys.argv[1]}, timeout=90); print(r.status_code, r.text)" "$1" || true
}
for url in https://quotes.toscrape.com/js/ http://quotes.toscrape.com/js/; do
  out=$(render "$url")
  if ! printf '%s' "$out" | grep -q '^200 ' || ! printf '%s' "$out" | grep -q 'class=\\*"quote\\*"'; then
    echo "playwright-service did not render $url: $(printf '%s' "$out" | head -c 300)" >&2
    exit 1
  fi
  echo "ok: playwright-service rendered $url"
done
before=$(refused)
out=$(render http://169.254.169.254/latest/meta-data/)
if printf '%s' "$out" | grep -Eq 'ami-id|instance-id'; then
  echo "playwright-service fetched an internal address: $out" >&2
  exit 1
fi
[ "$(refused)" -gt "$before" ] \
  || { echo "egress proxy did not refuse playwright-service's request as a private address: $out" >&2; exit 1; }
echo "ok: playwright-service's request for the internal address was refused as a private address"

# A redirect to a private address, followed by playwright-service's Chrome: the first hop reaches the redirector
# through the proxy and gets a real 302; the hop to its target must be refused as a private address.
before=$(refused)
out=$(render http://smoke-redirector:8080/start)
if printf '%s' "$out" | grep -Eq 'ami-id|instance-id'; then
  echo "the redirect to an internal address was followed: $out" >&2
  exit 1
fi
dc logs smoke-redirector | grep -Eq '"GET /start HTTP/1\.[01]" 302' \
  || { echo "the redirector was never asked, so the redirect path was not exercised: $out" >&2; exit 1; }
[ "$(refused)" -gt "$before" ] || { echo "egress proxy did not refuse the redirect target: $out" >&2; exit 1; }
echo "ok: a real 302 to the internal address, followed by playwright-service, was refused by firecrawl-egress"
