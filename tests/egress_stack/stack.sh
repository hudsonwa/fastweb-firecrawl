#!/usr/bin/env bash
# Throwaway real-Chrome stack for the browser egress tests, the same topology as docker-compose.yml:
#   fwtest-bnet (--internal): fwtest-browser (pinned Chrome, compose flags, supervise.sh, uid 65534, read-only) and
#     fwtest-target, a private canary on Chrome's own network that logs every request (so a proxy bypass would hit it)
#   fwtest-out: fwtest-origin (+ alias fwtest-origin2: a second site for OOPIFs), fwtest-flood (the flood origin)
#   fwtest-egress (both): python -m fastweb_firecrawl.egress, test allowlist = the harness origins + fwtest-target:8081 (positive control)
#   relays: CDP -> 127.0.0.1:3970, the proxy -> 127.0.0.1:3971 (GET /stats)
# The vector page is served by fwtest-origin2 and frames fwtest-origin (?o=), so its iframes are cross-site OOPIFs.
# Usage: stack.sh up | env | down. `env` prints the FASTWEB_TEST_EGRESS_* exports for test_egress_proxy.py; then run
#   pytest tests/test_egress_proxy.py -k real_chrome   and   python tests/egress_stack/flood.py   (from the repo root).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
LOGDIR=${EGRESS_STACK_LOG:-$HERE/log}
IMG=fastweb:egress-test
CHROME=chromedp/headless-shell:157.0.8103.27@sha256:7bee201478b2f6266aae01243740ee85c16281e32f42d3aed9fdf04f4eace7a1
NAMES="fwtest-relay-cdp fwtest-relay-stats fwtest-egress fwtest-browser fwtest-target fwtest-origin fwtest-flood"
case "${1:-}" in
up)
  mkdir -p "$LOGDIR"; : > "$LOGDIR/target.log"; : > "$LOGDIR/evil.log"
  docker build -q -t "$IMG" "$ROOT" >/dev/null
  docker network create --internal fwtest-bnet >/dev/null
  docker network create fwtest-out >/dev/null
  docker network create fwtest-pub >/dev/null
  py="docker run -d --rm -v $HERE:/h:ro -v $LOGDIR:/log -v $HERE/www:/www:ro"
  $py --name fwtest-target --network fwtest-bnet "$IMG" python /h/servers.py target >/dev/null
  $py --name fwtest-origin --network fwtest-out --network-alias fwtest-origin2 "$IMG" python /h/servers.py origin >/dev/null
  $py --name fwtest-flood --network fwtest-out "$IMG" python /h/flood_origin.py >/dev/null
  docker run -d --rm --name fwtest-egress --network fwtest-out \
    -e EGRESS_ALLOW=fwtest-origin:8080,fwtest-origin2:8080,fwtest-flood:8080,fwtest-target:8081 "$IMG" python -m fastweb_firecrawl.egress >/dev/null
  docker network connect fwtest-bnet fwtest-egress
  docker run -d --rm --name fwtest-browser --network fwtest-bnet -v "$ROOT/browser/supervise.sh:/supervise.sh:ro" \
    --entrypoint bash --user 65534:65534 -e HOME=/tmp --read-only --tmpfs /tmp:size=256m,mode=1777 --pids-limit 256 \
    --shm-size 512m --cap-drop ALL --security-opt no-new-privileges:true --memory 1g \
    "$CHROME" /supervise.sh --disable-background-networking --disable-sync --disable-component-update --no-first-run \
    --mute-audio --disable-gpu --proxy-server=http://fwtest-egress:3128 "--proxy-bypass-list=<-loopback>" --disable-quic \
    --force-webrtc-ip-handling-policy=disable_non_proxied_udp --block-new-web-contents >/dev/null
  for r in "cdp 3970 fwtest-browser 9222" "stats 3971 fwtest-egress 3128"; do
    set -- $r
    docker run -d --rm --name "fwtest-relay-$1" --network fwtest-pub -p "127.0.0.1:$2:9000" -v "$HERE:/h:ro" "$IMG" \
      python /h/relay.py 9000 "$3" "$4" >/dev/null
    docker network connect fwtest-bnet "fwtest-relay-$1"
  done
  ready() { curl -sf -m 2 http://127.0.0.1:3970/json/version >/dev/null && curl -sf -m 2 http://127.0.0.1:3971/stats >/dev/null; }
  for _ in $(seq 60); do ready && break; sleep 1; done
  ready || { echo "egress test stack not ready (see docker logs fwtest-*)" >&2; exit 1; }
  echo "egress test stack up"
  ;;
env)
  echo "export FASTWEB_TEST_EGRESS_CDP=http://127.0.0.1:3970 FASTWEB_TEST_EGRESS_STATS=http://127.0.0.1:3971/stats" \
    "FASTWEB_TEST_EGRESS_ORIGIN=http://fwtest-origin2:8080 FASTWEB_TEST_EGRESS_TARGET='http://fwtest-target&o=http://fwtest-origin:8080'" \
    "FASTWEB_TEST_EGRESS_TARGET_LOG=$LOGDIR/target.log"
  ;;
down)
  for n in $NAMES; do docker rm -f "$n" >/dev/null 2>&1 || true; done
  for n in fwtest-bnet fwtest-out fwtest-pub; do docker network rm "$n" >/dev/null 2>&1 || true; done
  docker image rm "$IMG" >/dev/null 2>&1 || true
  echo "egress test stack removed"
  ;;
*) echo "usage: $0 up|env|down" >&2; exit 2 ;;
esac
