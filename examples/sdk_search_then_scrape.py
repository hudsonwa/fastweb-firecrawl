"""Search, then scrape the top results, with the official Firecrawl SDK pointed at fastweb.

    pip install firecrawl-py
    python examples/sdk_search_then_scrape.py "python asyncio taskgroup"

The scrapes are usually cache hits: fastweb prefetches the top results of every search in the background.
"""
import sys
import time

from firecrawl import Firecrawl

API_URL = "http://127.0.0.1:3002"


def main(query: str) -> None:
    fc = Firecrawl(api_key="unused", api_url=API_URL)  # fastweb has no API keys; the SDK wants one anyway
    t = time.monotonic()
    results = fc.search(query, limit=5)
    web = results.web or []
    print(f"search: {len(web)} results in {time.monotonic() - t:.2f} s")
    for r in web[:3]:
        t = time.monotonic()
        try:
            doc = fc.scrape(r.url, formats=["markdown"])
        except Exception as e:  # noqa: BLE001 - fastweb answers honest errors (422/504) instead of walls or empty successes
            print(f"  {r.url}: {e}")
            continue
        engine = (doc.metadata_dict or {}).get("fastweb", {}).get("engine")  # which tier answered
        print(f"  {r.url}: {len(doc.markdown or '')} chars in {time.monotonic() - t:.2f} s (engine: {engine})")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "python asyncio taskgroup")
