"""Settings: every knob, read from the environment when a Settings() is created.

Each field's default comes from its environment variable (see the README's Configuration table); tests build
Settings(...) with explicit values instead.
"""
import os
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any

# Chrome on macOS, the same major version as the pinned chromedp/headless-shell image in docker-compose.yml.
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/157.0.0.0 Safari/537.36")
OFF = ("0", "false", "off", "no")


def _str(name: str, default: str) -> Any:
    return field(default_factory=lambda: os.environ.get(name, default))


def _set(name: str) -> str | None:
    """A numeric or on/off variable's value; set but empty (compose passes unset .env keys through as "") means
    unset, so the code default applies."""
    v = os.environ.get(name, "").strip()
    return v or None


def _int(name: str, default: int) -> Any:
    return field(default_factory=lambda: int(_set(name) or default))


def _float(name: str, default: float) -> Any:
    return field(default_factory=lambda: float(_set(name) or default))


def _flag(name: str, default: bool) -> Any:
    return field(default_factory=lambda: (_set(name) or ("1" if default else "0")).lower() not in OFF)


@dataclass
class Settings:
    """All tunables. Field comments say what each one bounds; docs/design.md says why the defaults are what they are."""

    upstream: str = _str("FIRECRAWL_UPSTREAM", "")  # "" = no fallback
    searxng_url: str = _str("SEARXNG_URL", "http://searxng:8080")
    searxng_engines: str = _str("SEARXNG_ENGINES", "")
    searxng_categories: str = _str("SEARXNG_CATEGORIES", "")
    max_bytes: int = _int("FASTWEB_MAX_BYTES", 6 * 1024 * 1024)
    # HTML still bigger than this after lighten() gets lexbor-only text in the big lane, not trafilatura.
    extract_max_html: int = _int("FASTWEB_EXTRACT_MAX_HTML", 3 * 1024 * 1024)
    total_timeout: float = _float("FASTWEB_FETCH_TIMEOUT", 8)
    plain_concurrency: int = _int("FASTWEB_PLAIN_CONCURRENCY", 8)
    fallback_timeout: float = _float("FASTWEB_FALLBACK_TIMEOUT", 12)
    scrape_ttl: int = _int("FASTWEB_SCRAPE_TTL", 600)
    host_ttl: int = _int("FASTWEB_HOST_TTL", 3600)
    firecrawl_ttl: int = _int("FASTWEB_FIRECRAWL_TTL", 1800)
    reprobe_rate: float = _float("FASTWEB_REPROBE_RATE", 0.1)
    dead_budget: float = 1.2  # seconds for plain + impersonation on a DEAD host
    # 1 in N requests to a DEAD host still tries Firecrawl, so a wrong DEAD heals.
    dead_reprobe_rate: float = _float("FASTWEB_DEAD_REPROBE_RATE", 0.05)
    # Page bodies held for extraction across requests; above it a request waits, then goes to Firecrawl.
    inflight_bytes: int = _int("FASTWEB_INFLIGHT_BYTES", 48 * 1024 * 1024)
    # Hard limit on one extraction: the worker process is killed after it.
    extract_timeout: float = _float("FASTWEB_EXTRACT_TIMEOUT", 20)
    # lane name -> executor factory (tests use threads); default: recycled worker processes (worker.py).
    lane_factory: Callable[[str], Executor] | None = None
    redis_url: str = _str("FASTWEB_REDIS_URL", "redis://fastweb-redis:6379/0")
    impersonate: bool = _flag("FASTWEB_IMPERSONATE", True)
    upstream_concurrency: int = _int("FASTWEB_UPSTREAM_CONCURRENCY", 3)
    search_fallback_engines: str = _str("FASTWEB_SEARCH_FALLBACK_ENGINES", "")
    search_deadline: float = _float("FASTWEB_SEARCH_DEADLINE", 3.0)
    # yahoo p50 ~0.75-0.8 s, slow calls ~1.0 s: 1.1 s lets normal yahoo answers land unhedged.
    search_hedge: float = _float("FASTWEB_SEARCH_HEDGE", 1.1)
    search_fallback_ttl: int = _int("FASTWEB_SEARCH_FALLBACK_TTL", 120)
    search_ttl: int = _int("FASTWEB_SEARCH_TTL", 900)
    search_relevance: bool = _flag("FASTWEB_SEARCH_RELEVANCE", True)
    # One throwaway query per engine set at startup: SearXNG's first call to an engine pays its connection setup,
    # which pushes the first real search past the engine timeouts (a 503 on the first try).
    search_warm: bool = _flag("FASTWEB_SEARCH_WARM", True)
    # SearXNG calls in flight at once; further searches queue (their deadline starts once they have a slot).
    search_concurrency: int = _int("FASTWEB_SEARCH_CONCURRENCY", 6)
    # Searches waiting for a slot beyond this get 429; a wait longer than the timeout gets 503 (both Retry-After).
    search_queue_max: int = _int("FASTWEB_SEARCH_QUEUE", 128)
    search_queue_timeout: float = _float("FASTWEB_SEARCH_QUEUE_TIMEOUT", 10)
    # Prefetch-on-search: top-N results fetched into the scrape cache with cheap tiers only; 0 disables.
    prefetch_n: int = _int("FASTWEB_PREFETCH_N", 5)
    prefetch_concurrency: int = _int("FASTWEB_PREFETCH_CONCURRENCY", 4)
    prefetch_queue: int = _int("FASTWEB_PREFETCH_QUEUE", 64)
    prefetch_per_host: int = _int("FASTWEB_PREFETCH_PER_HOST", 2)
    prefetch_timeout: float = _float("FASTWEB_PREFETCH_TIMEOUT", 6)
    prefetch_max_bytes: int = _int("FASTWEB_PREFETCH_MAX_BYTES", 4 * 1024 * 1024)
    # Markup left after lighten() above this is not extracted by prefetch; on demand still is.
    # 4 MB / 3.5 MB keep long Wikipedia articles; the pf worker is recycled and niced, so they cost neither
    # memory nor on-demand CPU.
    prefetch_max_html: int = _int("FASTWEB_PREFETCH_MAX_HTML", 3_500_000)
    # Prefetch is skipped while the container uses more than this share of its memory limit.
    prefetch_mem_frac: float = _float("FASTWEB_PREFETCH_MEM_FRAC", 0.6)
    # Limit used when no cgroup limit is visible (process RSS is compared against it).
    prefetch_mem_limit: int = _int("FASTWEB_PREFETCH_MEM_LIMIT", 768 * 1024 * 1024)
    # Queued jobs older than this are dropped (the follow-up extract has come and gone).
    prefetch_max_age: float = _float("FASTWEB_PREFETCH_MAX_AGE", 12)
    # Browser tier: FASTWEB_BROWSER=0 (or no URL) sends thin/blocked pages straight to Firecrawl (the rollback).
    browser: bool = _flag("FASTWEB_BROWSER", True)
    browser_url: str = _str("FASTWEB_BROWSER_URL", "")
    browser_tabs: int = _int("FASTWEB_BROWSER_TABS", 2)
    browser_settle: float = _float("FASTWEB_BROWSER_SETTLE", 3.0)
    browser_timeout: float = _float("FASTWEB_BROWSER_TIMEOUT", 4.0)
    browser_recycle: int = _int("FASTWEB_BROWSER_RECYCLE", 200)
    # Suspect-SPA signal: render small app-shell pages served as "ok"; keep the render only when much bigger.
    browser_suspect: bool = _flag("FASTWEB_BROWSER_SUSPECT", True)
    browser_wait: float = _float("FASTWEB_BROWSER_WAIT", 0.5)
    # browser-egress, whose GET /token proves an X-Fastweb-Egress marker is the proxy's.
    browser_egress: str = _str("FASTWEB_BROWSER_EGRESS", "")
    # () -> (current, limit) bytes; tests only (default: cgroup, then RSS)
    mem_usage: Callable[[], tuple[int, int]] | None = None
    max_text_chars: int = 500_000
    # Hosts exempt from the private-address check (tests only; empty in production).
    allow_hosts: frozenset[str] = frozenset()
    # User-Agent of the plain tier; keep its Chrome version in step with the pinned browser image.
    user_agent: str = field(default_factory=lambda: _set("FASTWEB_USER_AGENT") or DEFAULT_UA)
    # Request bodies above this are refused with 413 before they are parsed (search and scrape bodies are tiny).
    max_body_bytes: int = _int("FASTWEB_MAX_BODY_BYTES", 64 * 1024)
    # A scrape `timeout` above this (seconds) is clamped to it.
    max_timeout: float = _float("FASTWEB_MAX_TIMEOUT", 120)
    # A search `limit` above this is clamped to it (SearXNG rarely returns more than ~20 anyway).
    max_search_limit: int = _int("FASTWEB_MAX_SEARCH_LIMIT", 50)
