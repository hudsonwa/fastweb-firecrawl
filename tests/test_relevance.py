"""Relevance guard: fixtures shaped on real off-topic engine answers (results leaked across queries)."""
import asyncio

import httpx

from fastweb_firecrawl.search import Searcher, guard_results, normalize_url, query_terms

BING_CRISPR = [  # "CRISPR Cas9 review nature" -> another query's dress pages (msockid = bing session leak)
    {"url": "https://www.forevernew.com.au/dresses?msockid=1a2b", "title": "Dresses | Shop Women's Dresses Online | Forever New",
     "content": "Shop the latest women's dresses online at Forever New.", "engines": ["bing"]},
    {"url": "https://www.forevernew.com.au/dresses/midi-dresses?msockid=1a2b", "title": "Midi Dresses | Forever New",
     "content": "Discover midi dresses for every occasion.", "engines": ["bing"]},
    {"url": "https://www.forevernew.com.au/dresses/formal-dresses", "title": "Formal Dresses & Gowns | Forever New",
     "content": "Formal dresses, evening gowns and more.", "engines": ["bing"]},
    {"url": "https://www.nature.com/articles/nbt.3437", "title": "CRISPR–Cas9 structures and mechanisms | Nature",
     "content": "A review of CRISPR-Cas9 genome editing.", "engines": ["bing"]},
]
GOOD_CRISPR = [
    {"url": "https://www.nature.com/articles/s41392-023-01309-7", "title": "CRISPR/Cas9 therapeutics: progress and prospects",
     "content": "This review summarises CRISPR/Cas9 gene editing ...", "engines": ["yahoo"]},
    {"url": "https://pubmed.ncbi.nlm.nih.gov/24906146/", "title": "Development and applications of CRISPR-Cas9 for genome engineering",
     "content": "Recent advances ...", "engines": ["yahoo"]},
    {"url": "https://nature.com/articles/s41392-023-01309-7/?utm_source=x", "title": "CRISPR/Cas9 therapeutics (dup)",
     "content": "", "engines": ["yahoo"]},
    {"url": "https://en.wikipedia.org/wiki/CRISPR_gene_editing", "title": "CRISPR gene editing - Wikipedia",
     "content": "CRISPR gene editing is a genetic engineering technique ...", "engines": ["yahoo"]},
]
BING_WEATHER = [
    {"url": "https://www.forevernew.com.au/?msockid=9f", "title": "Forever New | Women's Clothing", "content": "New arrivals",
     "engines": ["bing"]},
    {"url": "https://www.myer.com.au/c/women/dresses", "title": "Women's Dresses | MYER", "content": "Shop dresses at Myer",
     "engines": ["bing"]},
    {"url": "https://www.theiconic.com.au/womens-clothing-dresses/", "title": "Dresses | THE ICONIC", "content": "Free delivery",
     "engines": ["bing"]},
]
GOOD_WEATHER = [
    {"url": "http://www.bom.gov.au/wa/forecasts/denver.shtml", "title": "Denver Forecast", "content": "Bureau of Meteorology",
     "engines": ["yahoo"]},
    {"url": "https://www.weatherzone.com.au/wa/denver/denver", "title": "Denver Weather - Weatherzone",
     "content": "Denver 7 day forecasts", "engines": ["yahoo"]},
    {"url": "https://www.willyweather.com.au/wa/denver/denver.html", "title": "Denver Weather Forecast | WillyWeather",
     "content": "", "engines": ["yahoo"]},
]
GOOD = {  # yahoo-shaped good answers for the bench pack: none may be dropped
    "python asyncio gather documentation": ("https://docs.python.org/3/library/asyncio-task.html", "Coroutines and Tasks — Python 3 documentation"),
    "tokio spawn blocking docs": ("https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html", "spawn_blocking in tokio::task - Rust"),
    "javascript fetch api abort controller": ("https://developer.mozilla.org/en-US/docs/Web/API/AbortController", "AbortController - Web APIs | MDN"),
    "latest world news today": ("https://www.reuters.com/world/", "World News | Latest Top Stories | Reuters"),
    "OpenAI news this week": ("https://openai.com/news/", "Newsroom | OpenAI"),
    "Denver weather forecast": ("http://www.bom.gov.au/wa/forecasts/denver.shtml", "Denver Forecast"),
    "Transdenver train timetable": ("https://www.transdenver.wa.gov.au/timetables", "Timetables - Transdenver"),
    "WA government fuel watch prices Denver": ("https://www.fuelwatch.wa.gov.au/", "FuelWatch - Find the cheapest fuel"),
    "iPhone 17 Pro price Australia": ("https://www.apple.com/au/shop/buy-iphone/iphone-17-pro", "Buy iPhone 17 Pro - Apple (AU)"),
    "RTX 5090 price": ("https://www.nvidia.com/en-au/geforce/graphics-cards/50-series/rtx-5090/", "GeForce RTX 5090 Graphics Cards | NVIDIA"),
    "attention is all you need paper": ("https://arxiv.org/abs/1706.03762", "[1706.03762] Attention Is All You Need"),
    "CRISPR Cas9 review nature": ("https://www.nature.com/articles/s41392-023-01309-7", "CRISPR/Cas9 therapeutics"),
    "github cpython repository": ("https://github.com/python/cpython", "python/cpython: The Python programming language"),
    "searxng documentation": ("https://docs.searxng.org/", "Welcome to SearXNG — SearXNG Documentation"),
}


def mk(*answers):
    answers = list(answers)
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        res = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(200, json={"results": res})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Searcher(client, "http://searx", engines="yahoo", fallback_engines="brave,privacywall", hedge=5), calls


def test_query_terms():
    assert query_terms("RTX 5090 price") == ["rtx", "5090", "price"]
    assert query_terms("the a of is") == []
    assert query_terms("東京 天気") == []          # non-Latin: guard skipped
    assert "site" not in query_terms("asyncio site:docs.python.org")


def test_normalize_url():
    a = normalize_url("https://WWW.Nature.com/articles/x/?msockid=1&utm_source=a&id=3")
    assert a == normalize_url("http://nature.com/articles/x?id=3")


def test_poisoned_answer_drops_and_fallback_answers():
    s, calls = mk(BING_CRISPR, GOOD_CRISPR)
    out = asyncio.run(s.search("CRISPR Cas9 review nature", 5))
    assert out.source == "fallback" and calls[1]["engines"] == "brave,privacywall"
    urls = [x["url"] for x in out.items]
    assert len(urls) == 3 and urls[0].startswith("https://www.nature.com/")   # nature.com dup removed
    assert any("poisoned" in g for g in out.guard)


def test_weather_poisoned_never_cached():
    import fakeredis.aioredis

    from fastweb_firecrawl.search import RedisCache
    cache = RedisCache(client=fakeredis.aioredis.FakeRedis())
    s, _ = mk(BING_WEATHER)
    s.cache = cache
    out = asyncio.run(s.search("Denver weather forecast", 5))
    assert out.items is None
    assert asyncio.run(cache.redis.keys("*")) == []


def test_minority_offtopic_dropped_only():
    # single engine (the yahoo-only primary): 3 on-topic + 1 off-topic -> the answer stays, the junk result goes
    res = GOOD_WEATHER + [dict(BING_WEATHER[1], engines=["yahoo"])]
    items, info = guard_results("Denver weather forecast", res)
    assert [x["url"] for x in items] == [x["url"] for x in GOOD_WEATHER] and "off=0.25" in info


def test_per_engine_poisoning():
    res = BING_CRISPR[:3] + GOOD_CRISPR[:2] + GOOD_CRISPR[3:] + [dict(BING_CRISPR[3], engines=["bing"])]
    items, info = guard_results("CRISPR Cas9 review nature", res)
    # bing is 3/4 off-topic -> all its results go, even its on-topic nature.com hit; yahoo's stay
    assert all("yahoo" in x["engines"] for x in items) and len(items) == 3 and "bing" in info


def test_no_false_drops_on_pack():
    for q, (url, title) in GOOD.items():
        items, info = guard_results(q, [{"url": url, "title": title, "content": "", "engines": ["yahoo"]}])
        assert len(items) == 1, (q, info)


def test_stopword_and_non_latin_skip():
    for q in ("the of and", "東京 天気"):
        items, info = guard_results(q, BING_WEATHER)
        assert len(items) == 3 and "skip" in info


def test_guard_off_keeps_everything():
    s, _ = mk(BING_CRISPR)
    s.relevance = False
    out = asyncio.run(s.search("CRISPR Cas9 review nature", 5))
    assert out.source == "primary" and len(out.items) == 4
