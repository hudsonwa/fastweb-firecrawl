#!/bin/bash
# Liveness supervisor for the browser container: runs headless-shell's own run.sh with the compose
# flags and asks CDP /json/version (HTTP/1.1) every 2 s. After a startup grace, 3 failures in a row (a wedged or
# stopped Chrome) kill it: the container exits and `restart: unless-stopped` brings it back in 1-2 s. A Chrome that
# exits by itself (fastweb's Browser.close recycle) ends the container the same way. No docker socket needed.
GRACE=${SUPERVISE_GRACE:-8}; EVERY=${SUPERVISE_EVERY:-2}; MAX=${SUPERVISE_FAILS:-3}
/headless-shell/run.sh "$@" & CH=$!
trap 'kill -TERM "$CH" 2>/dev/null; wait "$CH"; exit 0' TERM INT
probe() {
  timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/9223 && printf 'GET /json/version HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3 && grep -q webSocketDebuggerUrl <&3" 2>/dev/null
}
sleep "$GRACE"; fails=0
while kill -0 "$CH" 2>/dev/null; do
  if probe; then fails=0
  else
    fails=$((fails + 1))
    if [ "$fails" -ge "$MAX" ]; then
      echo "supervise: CDP did not answer $MAX times in a row: killing Chrome (the container restarts)"
      kill -KILL "$CH" 2>/dev/null; exit 1
    fi
  fi
  sleep "$EVERY"
done
wait "$CH"; rc=$?; echo "supervise: Chrome exited ($rc)"; exit "$rc"
