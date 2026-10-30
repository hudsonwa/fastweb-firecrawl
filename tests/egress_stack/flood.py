"""Connection flood on the egress test stack: a page from the flood origin opens 300 fetches, 300 EventSources and 300
WebSockets (from a blob worker), rendered three times in a row; then a page of another site must still render at once
(no proxy 503, no tunnel failure), and the proxy's active count must drop back soon after each dispose.
Run from fastweb_firecrawl/ after `tests/egress_stack/stack.sh up`: python tests/egress_stack/flood.py"""
import asyncio
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from fastweb_firecrawl.browser import Browser

CDP, STATS = "http://127.0.0.1:3970", "http://127.0.0.1:3971/stats"


def stats():
    r = httpx.get(STATS, timeout=5)
    return r.status_code, (r.json() if r.status_code == 200 else {"active": "?", "busy": "?", "body": r.text.strip()})


async def anything(url):
    return True


async def main():
    b = Browser(CDP, guard=anything, settle=3.0, timeout=6.0)
    ok = True
    try:
        for i in range(3):
            r = await b.render(f"http://fwtest-flood:8080/flood?{i}")
            code, s = stats()
            print(f"flood {i}: page {r.status}; proxy /stats {code} active={s['active']} busy={s['busy']}")
            ok &= code == 200
        await asyncio.sleep(1.0)
        code, s = stats()
        print(f"1 s after the last dispose: active={s['active']} busy={s['busy']}")
        ok &= code == 200 and s["active"] < 20
        t = time.monotonic()
        try:
            r = await b.render("http://fwtest-origin2:8080/vectors.html?t=http://fwtest-target")
            dt = time.monotonic() - t
            print(f"another site: {r.status}, {len(r.html)} chars, 'vectors loaded' {'vectors loaded' in r.html}, "
                  f"{dt:.2f} s")
            ok &= r.status == 200 and "vectors loaded" in r.html
        except Exception as e:
            print("another site FAILED:", type(e).__name__, e)
            ok = False
        print("busy answers during the run:", stats()[1]["busy"])
    finally:
        await b.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
