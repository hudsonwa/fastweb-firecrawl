"""Search tuning: leak signals (msockid, site:, >= 4-term queries), query classes, relevance coverage."""
import asyncio

import httpx

from fastweb_firecrawl.search import Searcher, guard_results, normalize_url, on_topic, query_terms

from .test_relevance import GOOD_WEATHER


def y(url, title, content=""):
    return {"url": url, "title": title, "content": content, "engines": ["yahoo"]}


def urls(items):
    return [x["url"] for x in items]


# ---------- leak signals ----------

def test_msockid_results_dropped_even_when_on_topic():
    leak = y("https://www.davidjones.com/christmas?msockid=3f2", "Christmas 2026 | David Jones", "Denver weather")
    items, info = guard_results("Denver weather forecast", GOOD_WEATHER + [leak])
    assert leak["url"] not in urls(items) and len(items) == 3 and "leak=1" in info


def test_msockid_counts_toward_engine_poisoning():
    res = [dict(GOOD_WEATHER[0], engines=["bing"]),
           {"url": "https://www.myer.com.au/denver?msockid=1", "title": "Myer Denver weather", "engines": ["bing"]},
           {"url": "https://www.iconic.com.au/denver?msockid=1", "title": "Denver forecast delivery", "engines": ["bing"]},
           ] + [dict(r, engines=["yahoo"]) for r in GOOD_WEATHER[1:]] + [y("https://bom.gov.au/x", "Denver weather radar")]
    items, info = guard_results("Denver weather forecast", res)
    assert "poisoned_engines=bing" in info and all(x["engines"] == ["yahoo"] for x in items) and len(items) == 3


def test_site_query_drops_other_hosts():
    res = [y("https://docs.python.org/3/library/asyncio.html", "asyncio — Asynchronous I/O"),
           y("https://www.python.org/about/asyncio", "Python asyncio overview"),
           y("https://docs.python.org/3/library/asyncio-task.html", "Coroutines and Tasks"),
           y("https://www.nine.com.au/latest", "Latest news asyncio")]
    items, info = guard_results("asyncio site:python.org", res)
    assert urls(items) == urls(res[:3])


def test_site_query_all_offsite_is_filtered():
    # a site: mismatch filters (-> [] -> fallback) but no longer marks the answer poisoned
    res = [y(f"https://news{i}.example.com/asyncio", "asyncio latest") for i in range(4)]
    items, info = guard_results("asyncio site:docs.python.org", res)
    assert items == [] and "poisoned" not in info


def test_four_terms_need_two_matches():
    res = GOOD_WEATHER[:2] + [y("https://www.bom.gov.au/wa/", "Denver weather forecast warnings"),
                              y("https://shop.example/latest", "Latest arrivals | Shop online")]
    items, _ = guard_results("Denver weather forecast latest", res)   # the shop matches only the generic "latest"
    assert "https://shop.example/latest" not in urls(items) and len(items) == 3
    assert "poisoned" not in guard_results("Denver weather forecast latest", res[:1] + res[3:] * 2)[1]  # weak != miss


def test_four_terms_one_distinctive_match_enough():
    # the canonical page matches only "acmepay" of 4 terms
    assert on_topic(query_terms("ACMEPAY Pty Ltd Springfield what is it"), y("https://www.acmepay.example/", "ACMEPAY - Gifting"))
    assert not on_topic(query_terms("latest stable Python release notes"), y("https://www.nine.com.au/latest", "Latest News"))


def test_site_query_without_terms_keeps_on_site():
    res = [y(f"https://docs.example.org/{p}", "Example Docs") for p in ("", "docs/", "docs/x/")]
    assert len(guard_results("site:docs.example.org", res)[0]) == 3


def test_three_terms_one_match_still_enough():
    assert on_topic(query_terms("Denver weather forecast"), y("https://bom.gov.au/", "Forecast"))


# ---------- query classes ----------

def test_accent_folding_both_sides():
    assert on_topic(query_terms("beyonce tour"), y("https://x.com/a", "Beyoncé — Cowboy Carter"))
    assert on_topic(query_terms("Beyoncé"), y("https://x.com/beyonce", "Beyonce news"))
    assert query_terms("Pokémon") == ["pokemon"]


def test_hyphen_compound_variants():
    assert on_topic(query_terms("wifi router"), y("https://x.com/", "Best Wi-Fi 7 Routers"))
    assert on_topic(query_terms("XSCAP8 Sentosa"), y("https://x.com/", "X-Scap8 attraction"))
    assert on_topic(query_terms("e-bike"), y("https://x.com/", "Best ebikes 2026"))


def test_mixed_script_query_native_results_on_topic():
    res = [y(f"https://tenki.jp/{i}", "東京の天気 - 日本気象協会") for i in range(4)]
    items, info = guard_results("東京 weather", res)
    assert len(items) == 4
    items, _ = guard_results("Москва news", [y(f"https://ria.ru/{i}", "Новости Москвы") for i in range(3)])
    assert len(items) == 3


def test_mixed_script_latin_junk_still_dropped():
    res = [y(f"https://tenki.jp/{i}", "東京の天気") for i in range(3)] + [y("https://myer.com.au/", "Myer dresses")]
    items, _ = guard_results("東京 weather", res)
    assert len(items) == 3


def test_tiny_answer_filtered_not_poisoned():
    res = [y("https://waivpay.com/", "WAIVPAY | Gifting"), y("https://abr.gov.au/x", "ABN lookup")]
    items, info = guard_results("WAIVPAY Claremont", res)
    assert urls(items) == ["https://waivpay.com/"] and "poisoned" not in info


# ---------- coverage ----------

def test_stem_prefix_and_compound_match():
    assert on_topic(["forecasts"], y("https://x/", "Forecast"))
    assert on_topic(["forecast"], y("https://x/", "7 day forecasts"))
    assert on_topic(["transdenver"], y("https://x/", "Transdenvereasy timetables"))


def test_url_host_and_snippet_are_read():
    assert on_topic(["transdenver"], y("https://www.transdenver.wa.gov.au/", "Timetables"))
    assert on_topic(["timetable"], y("https://x.example/", "Home", "train timetable for Denver"))
    assert on_topic(["timetable"], y("https://x.example/timetable/123", "Home"))


def test_poison_threshold_boundary():
    good = [y(f"https://bom.gov.au/{i}", "Denver forecast") for i in range(2)]
    bad = [y(f"https://myer.com.au/{i}", "Dresses") for i in range(2)]
    assert guard_results("Denver weather forecast", good + bad)[0] == []        # 2/4 off -> poisoned
    assert len(guard_results("Denver weather forecast", good + good[:1] + bad[:1])[0]) == 3   # 1/4 -> kept
    assert len(guard_results("Denver weather forecast", good[:1] + good[:1] + bad[:1])[0]) == 2  # 1/3 -> kept


def test_operator_values_and_excluded_terms_stripped():
    assert query_terms("asyncio site:docs.python.org") == ["asyncio"]
    assert query_terms("pasta recipe -pinterest") == ["pasta", "recipe"]
    assert query_terms("report filetype:pdf") == ["report"]


def test_dedupe_keeps_distinct_query_ids():
    assert normalize_url("https://youtube.com/watch?v=a") != normalize_url("https://youtube.com/watch?v=b")


def test_nfkc_full_width():
    assert query_terms("ＲＴＸ ５０９０") == ["rtx", "5090"]


def test_result_kept_when_any_engine_clean():
    bad = [{"url": f"https://myer.com.au/{i}", "title": "Dresses", "engines": ["bing"]} for i in range(3)]
    shared = {"url": "https://bom.gov.au/denver", "title": "Denver forecast", "engines": ["bing", "yahoo"]}
    res = bad + [shared] + [dict(r) for r in GOOD_WEATHER]
    items, info = guard_results("Denver weather forecast", res)
    assert "poisoned_engines=bing" in info and shared["url"] in urls(items)


# ---------- timing defaults ----------

def test_hedge_default_above_yahoo_p50():
    s = Searcher(httpx.AsyncClient(), "http://s")
    assert s.hedge >= 1.1


def test_slow_primary_under_hedge_not_hedged():
    calls = []

    async def handler(request):
        calls.append(request.url.params.get("engines"))
        await asyncio.sleep(0.95)
        return httpx.Response(200, json={"results": GOOD_WEATHER})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await Searcher(c, "http://s", engines="yahoo", fallback_engines="privacywall").search(
                "Denver weather forecast", 5)
    out = asyncio.run(go())
    assert out.source == "primary" and out.attempts == 1 and calls == ["yahoo"]


def test_hyphenated_query_matches_joined_doc():
    # "wi"/"fi" alone are 2-char exact tokens; only the joined "wifi" matches
    assert on_topic(query_terms("wi-fi"), y("https://x.example/", "Best wifi routers"))


def test_substring_compound_match():
    assert on_topic(["denver"], y("https://x.example/", "Transdenver"))


def test_engine_poison_boundary_half():
    bad = [{"url": f"https://myer.com.au/{i}", "title": "Dresses", "engines": ["bing"]} for i in range(2)]
    bing_ok = [{"url": f"https://bom.gov.au/b{i}", "title": "Denver forecast", "engines": ["bing"]} for i in range(2)]
    good = [y(f"https://bom.gov.au/{i}", "Denver forecast") for i in range(4)]
    items, info = guard_results("Denver weather forecast", bad + bing_ok + good)   # bing 2/4 -> poisoned
    assert "poisoned_engines=bing" in info and urls(items) == urls(good)


def test_generic_rule_only_from_four_terms_and_years_generic():
    myer = y("https://www.myer.com.au/denver", "Myer Denver | Shop online")
    assert on_topic(query_terms("Denver weather forecast"), myer)            # 3 terms: one match is enough
    assert not on_topic(query_terms("Python stable release notes 2026"), y("https://x.example/", "Christmas 2026 gifts"))
