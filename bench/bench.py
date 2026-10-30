#!/usr/bin/env python3
"""Latency and completeness benchmark for any Firecrawl-compatible endpoint (fastweb, Firecrawl, ...).

    python bench/bench.py search --target fastweb=http://127.0.0.1:3002 --target firecrawl=http://127.0.0.1:3012
    python bench/bench.py scrape --target fastweb=http://127.0.0.1:3002 [--pack bench/data/js_pack.json]

search  POST {target}/v2/search {"query", "limit": 10} for every query in data/search_queries.yaml.
        hit@5 = any expected domain / URL prefix appears in the top 5 results.
scrape  POST {target}/v2/scrape with the body firecrawl-py sends for scrape(url, formats=["markdown", "html"]),
        for every page in data/fetch_urls.yaml (or a JSON pack). found = the reference phrase is in the markdown
        (case/whitespace-insensitive); for error pages, correct = the 404/410 is reported as an error.

Targets are interleaved per item and paced (--gap, default 2.5 s) so no site or engine is hammered; caches are
bypassed with maxAge 0 on scrape. Rows go to --out as JSONL; a markdown summary is printed. Results depend heavily
on network location, time of day and the live state of every site: compare targets within one run, not across runs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import yaml

HERE = Path(__file__).resolve().parent
SCRAPE_BODY = {"onlyMainContent": True, "skipTlsVerification": True, "removeBase64Images": True, "fastMode": False,
               "blockAds": True, "storeInCache": True, "formats": ["markdown", "html"], "timeout": 60000,
               "mobile": False}


def post(url: str, body: dict, timeout: float) -> tuple[int, dict, float]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    except Exception as e:  # timeouts, refused connections
        return 0, {"error": f"{type(e).__name__}: {e}"}, (time.perf_counter() - t0) * 1000
    ms = (time.perf_counter() - t0) * 1000
    try:
        return code, json.loads(raw), ms
    except ValueError:
        return code, {"error": "non-JSON answer"}, ms


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def hit(urls: list[str], expected: list[str]) -> bool:
    for u in urls:
        p = urlsplit(u)
        host = (p.hostname or "").lower().removeprefix("www.")
        full = host + p.path.lower()
        if any(host == e.lower() or host.endswith("." + e.lower()) or full.startswith(e.lower()) for e in expected):
            return True
    return False


def result_urls(resp: dict) -> list[str]:
    data = resp.get("data")
    items = data.get("web", []) if isinstance(data, dict) else (data or [])
    return [i.get("url", "") for i in items if isinstance(i, dict)]


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def load_pages(path: Path) -> list[dict]:
    if path.suffix == ".json":
        return json.loads(path.read_text())["pages"]
    return yaml.safe_load(path.read_text())


def run_search(targets, args, out):
    queries = yaml.safe_load((HERE / "data" / "search_queries.yaml").read_text())["queries"]
    for q in queries:
        for name, base in targets:
            code, resp, ms = post(base.rstrip("/") + "/v2/search", {"query": q["q"], "limit": 10}, args.timeout)
            urls = result_urls(resp) if code == 200 else []
            row = {"kind": "search", "target": name, "id": q["id"], "cat": q.get("cat"), "code": code,
                   "ms": round(ms), "n": len(urls), "hit5": hit(urls[:5], q["expected"]),
                   "error": None if code == 200 else resp.get("error")}
            out.write(json.dumps(row) + "\n")
            yield row
            time.sleep(args.gap)


def run_scrape(targets, args, out):
    for page in load_pages(Path(args.pack)):
        for name, base in targets:
            body = dict(SCRAPE_BODY, url=page["url"], maxAge=0)
            code, resp, ms = post(base.rstrip("/") + "/v2/scrape", body, args.timeout)
            data = resp.get("data") or {}
            md = data.get("markdown") or "" if isinstance(data, dict) else ""
            if page.get("phrase") is None:  # an error page: the 404/410 must come back as an error
                ok = code in (404, 410) or bool(re.search(r"HTTP 4(04|10)", str(resp.get("error", ""))))
            else:
                text = norm(md)
                ok = norm(page["phrase"]) in text and (not page.get("body") or norm(page["body"]) in text)
            engine = ((data.get("metadata") or {}).get("fastweb") or {}).get("engine") if isinstance(data, dict) else None
            row = {"kind": "scrape", "target": name, "url": page["url"], "cls": page.get("cls"), "code": code,
                   "ms": round(ms), "chars": len(md), "ok": ok, "engine": engine,
                   "error": None if code == 200 else resp.get("error")}
            out.write(json.dumps(row) + "\n")
            yield row
            time.sleep(args.gap)


def summary(rows: list[dict]) -> str:
    lines = ["| target | n | ok | p50 ms | p90 ms | max ms |", "|---|---|---|---|---|---|"]
    for name in dict.fromkeys(r["target"] for r in rows):
        rs = [r for r in rows if r["target"] == name]
        ms = [r["ms"] for r in rs]
        ok = sum(bool(r.get("hit5", r.get("ok"))) for r in rs)
        lines.append(f"| {name} | {len(rs)} | {ok} | {percentile(ms, 50):.0f} | {percentile(ms, 90):.0f} | {max(ms)} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["search", "scrape"])
    ap.add_argument("--target", action="append", default=[], metavar="NAME=URL",
                    help="endpoint to measure (repeatable); default fastweb=http://127.0.0.1:3002")
    ap.add_argument("--pack", default=str(HERE / "data" / "fetch_urls.yaml"), help="scrape pages (YAML or JSON pack)")
    ap.add_argument("--gap", type=float, default=2.5, help="seconds between requests")
    ap.add_argument("--timeout", type=float, default=70.0)
    ap.add_argument("--out", default=str(HERE / "out" / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
    args = ap.parse_args(argv)
    targets = [tuple(t.split("=", 1)) for t in args.target] or [("fastweb", "http://127.0.0.1:3002")]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with open(args.out, "w") as out:
        gen = run_search(targets, args, out) if args.mode == "search" else run_scrape(targets, args, out)
        for row in gen:
            rows.append(row)
            print(json.dumps(row), file=sys.stderr)
    print(summary(rows))
    print(f"\nrows: {args.out}")


if __name__ == "__main__":
    main()
