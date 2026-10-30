"""robust site: parsing; a site: mismatch filters results but never poisons the whole answer."""
import asyncio

import httpx
import pytest

from fastweb_firecrawl import search
from fastweb_firecrawl.search import Outcome, guard_results, site_hosts, site_match

from .test_search_relevance_signals import urls, y


@pytest.mark.parametrize("q,hosts", [
    ("(site:reddit.com OR site:news.ycombinator.com) self hosting", ["reddit.com", "news.ycombinator.com"]),
    ("site:https://www.reddit.com/r/selfhosted nas", ["reddit.com"]),
    ("site:.gov.au grants", ["gov.au"]),
    ("site:*.gov.au grants", ["gov.au"]),
    ('"vaccine schedule site:health.gov.au"', ["health.gov.au"]),
    ("site:intranet-host:8092 report", ["intranet-host"]),
    ("[site:example.org], x", ["example.org"]),
    ("SITE:Docs.Python.org asyncio", ["docs.python.org"]),
])
def test_site_hosts_spellings(q, hosts):
    assert site_hosts(q) == hosts


@pytest.mark.parametrize("host,site,ok", [
    ("docs.python.org", "python.org", True),
    ("evilpython.org", "python.org", False),
    ("python.org.evil.com", "python.org", False),
    ("en.m.wikipedia.org", "en.wikipedia.org", True),
    ("de.wikipedia.org", "en.wikipedia.org", False),
    ("m.youtube.com", "youtube.com", True),
    ("youtu.be", "youtube.com", True),
    ("www.youtube.com", "youtu.be", True),
    ("notyoutu.be", "youtube.com", False),
    ("mobile.twitter.com", "twitter.com", True),
    ("www.health.gov.au", "gov.au", True),
    ("intranet-host", "intranet-host", True),
])
def test_site_match_label_boundary(host, site, ok):
    assert site_match(host, [site]) is ok


PROBES = [
    ("(site:reddit.com OR site:news.ycombinator.com) self hosting", "https://www.reddit.com/r/selfhosted/a", "Self hosting"),
    ("site:https://www.reddit.com/r/selfhosted nas", "https://old.reddit.com/r/selfhosted/b", "nas"),
    ("site:.gov.au grants", "https://business.gov.au/grants", "grants"),
    ("site:*.gov.au grants", "https://www.grants.gov.au/x", "grants"),
    ('"vaccine schedule site:health.gov.au"', "https://www.health.gov.au/v", "vaccine schedule"),
    ("site:intranet-host:8092 report", "http://intranet-host:8092/report.html", "report"),
    ("site:en.wikipedia.org Denver", "https://en.m.wikipedia.org/wiki/Denver", "Denver"),
    ("site:youtube.com lofi", "https://youtu.be/abc", "lofi"),
]


@pytest.mark.parametrize("q,url,title", PROBES)
def test_probe_spellings_keep_on_site(q, url, title):
    res = [y(url + f"?i={i}", f"{title} {i}") for i in range(4)]
    items, info = guard_results(q, res)
    assert len(items) == 4 and "poisoned" not in info


def test_lookalike_host_rejected():
    res = [y("https://docs.python.org/3/library/asyncio.html", "asyncio"),
           y("https://evilpython.org/asyncio", "asyncio tutorial")]
    assert urls(guard_results("asyncio site:python.org", res)[0]) == urls(res[:1])


def test_bare_site_query_drops_offsite():
    res = [y(f"https://docs.example.org/p{i}", f"Page {i}") for i in range(3)]
    res.append(y("https://evil.example.com/", "Something"))
    items, _ = guard_results("site:docs.example.org", res)
    assert urls(items) == urls(res[:3])


def test_all_offsite_filtered_not_poisoned():
    res = [y(f"https://news{i}.example.com/asyncio", "asyncio latest") for i in range(4)]
    items, info = guard_results("asyncio site:docs.python.org", res)
    assert items == [] and "poisoned" not in info and "offsite=4" in info


def test_offsite_mixed_engine_not_poisoned():
    # one engine answers half off-site: on-site results of that engine survive
    res = [dict(y("https://docs.python.org/a", "asyncio a"), engines=["bing"]),
           dict(y("https://other.example/a", "asyncio x"), engines=["bing"]),
           dict(y("https://other.example/b", "asyncio y"), engines=["bing"]),
           dict(y("https://docs.python.org/b", "asyncio b"), engines=["bing"])]
    items, info = guard_results("asyncio site:python.org", res)
    assert urls(items) == ["https://docs.python.org/a", "https://docs.python.org/b"] and "poisoned" not in info


def test_or_sites_offsite_not_leak():
    res = [y("https://reddit.com/r/x", "nas"), y("https://other.example/", "nas")]
    items, info = guard_results("site:reddit.com OR site:news.ycombinator.com nas", res)
    assert urls(items) == urls(res[:1]) and "leak=" not in info and "offsite=1" in info


def test_single_site_offsite_counts_as_leak():
    res = [y("https://reddit.com/r/x", "nas"), y("https://other.example/", "nas")]
    assert "leak=1" in guard_results("site:reddit.com nas", res)[1]


def test_msockid_still_poisons():
    res = [y(f"https://docs.python.org/{i}?msockid=1", "asyncio") for i in range(4)]
    assert "poisoned" in guard_results("asyncio site:python.org", res)[1]


def test_negated_site_not_a_filter():
    res = [y("https://docs.python.org/a", "asyncio")]
    assert site_hosts("asyncio -site:pinterest.com") == [] and len(guard_results("asyncio -site:pinterest.com", res)[0]) == 1


def test_offsite_fallthrough_uses_fallback():
    async def handler(request):
        eng = request.url.params.get("engines")
        host = "other.example" if eng == "yahoo" else "docs.python.org"
        return httpx.Response(200, json={"results": [y(f"https://{host}/{i}", "asyncio") for i in range(3)]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await search.Searcher(c, "http://s", engines="yahoo", fallback_engines="privacywall").search(
                "asyncio site:python.org", 5)
    out = asyncio.run(go())
    assert out.source == "fallback" and len(out.items) == 3


# ---------- header values and Settings default ----------

@pytest.mark.parametrize("source,cache,expect", [
    ("hedge", "miss", "source=hedge;attempts=2"),
    ("fallback", "miss", "source=fallback;attempts=2"),
    ("", "hit", "source=hit;attempts=0"),
])
def test_search_header_values(ctx, monkeypatch, source, cache, expect):
    _, _, aurl = ctx

    async def fake(self, q, limit):
        return Outcome([{"url": "https://a.example/", "title": "A", "content": "a"}], cache,
                       0 if cache == "hit" else 2, source=source)
    monkeypatch.setattr(search.Searcher, "search", fake)
    r = httpx.post(aurl + "/v2/search", json={"query": "q " + expect, "limit": 3})
    assert r.status_code == 200 and r.headers["X-Fastweb-Search"] == expect
    assert r.headers["X-Fastweb-Cache"] == cache.upper()


def test_settings_hedge_default(monkeypatch):
    monkeypatch.delenv("FASTWEB_SEARCH_HEDGE", raising=False)
    from fastweb_firecrawl.settings import Settings
    assert Settings().search_hedge == 1.1
