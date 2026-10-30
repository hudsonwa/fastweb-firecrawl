"""On-demand never waits on a paused prefetch, cheap give-ups, compose defaults,
and the prefetch byte budget is still never waited for."""
import re
import threading
import time
from pathlib import Path

import pytest

from .conftest import serve
from .test_prefetch import Fake, make, sdk_scrape, search, settle, stats, urls
from .test_prefetch_resources import Pools


@pytest.fixture
def fake():
    f = Fake()
    srv, url = serve(f.app())
    f.url = url
    yield f
    srv.should_exit = True


def timed(fn, *a):
    t = time.monotonic()
    r = fn(*a)
    return r, time.monotonic() - t


def slow_ondemand(aurl, fake):
    return threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, "art-slow")[0]))


# ---------- a joined prefetch flight is promoted ----------
@pytest.mark.slow
@pytest.mark.parametrize("slow_s", [5.0, 7.0])
def test_joiner_not_delayed_by_unrelated_slow_fetch(fake, slow_s):
    """An unrelated on-demand page waits on the network; an on-demand request joins the prefetch of art1. The
    prefetch stops yielding once joined: one upstream fetch, no wait."""
    fake.delay["art1"] = 0.5
    fake.delay["art-slow"] = slow_s
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0)
    fake.results = urls(fake, "art1")
    slow = slow_ondemand(aurl, fake)
    search(aurl)
    time.sleep(0.1)
    slow.start()
    time.sleep(0.1)
    r, dt = timed(sdk_scrape, aurl, urls(fake, "art1")[0])
    slow.join()
    # bad case: the joiner waits for the unrelated fetch (about slow_s); the bound leaves room for a loaded runner
    assert r.status_code == 200 and dt < 0.5 + 0.6 * slow_s and fake.hits.count("art1") == 1, (dt, fake.hits)
    assert r.headers["x-fastweb-cache"] == "JOIN-PREFETCH"


@pytest.mark.slow
def test_joiner_of_extracted_prefetch_not_held_back(fake):
    """The prefetch has extracted art1 when an on-demand request joins it: no hold-back before serialising."""
    fake.delay["art1"] = 0.3
    fake.delay["art-slow"] = 6.0
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=Pools(slow={"pf"}, delay=1.0))
    fake.results = urls(fake, "art1")
    search(aurl)
    time.sleep(0.6)  # prefetch is extracting (0.3 .. 1.3 s)
    slow = slow_ondemand(aurl, fake)
    slow.start()
    time.sleep(0.2)
    r, dt = timed(sdk_scrape, aurl, urls(fake, "art1")[0])
    slow.join()
    assert r.status_code == 200 and dt < 3.5, dt  # not held back until art-slow (6 s) ends


@pytest.mark.slow
def test_yielding_prefetch_releases_its_budget_bytes(fake):
    """A prefetch that yields holds no byte-budget bytes: an on-demand page needing them does not wait for it
    (no priority inversion; budget scaled to 5 KB for 2.2 KB pages)."""
    fake.delay["art1"] = 0.3
    fake.delay["art-slow"] = 6.0
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, inflight_bytes=5000)
    fake.results = urls(fake, "art1")
    slow = slow_ondemand(aurl, fake)
    search(aurl)
    time.sleep(0.1)
    slow.start()
    time.sleep(0.5)  # prefetch has fetched art1 and is yielding
    r, dt = timed(sdk_scrape, aurl, urls(fake, "art2")[0])
    slow.join()
    assert r.status_code == 200 and dt < 3.5, dt  # not waiting for budget until art-slow (6 s) ends
    assert r.json()["data"]["metadata"]["fastweb"]["engine"] == "plain"


# ---------- a late give-up is cheap and counted as gave_up ----------
@pytest.mark.slow
def test_prefetch_gives_up_without_extracting_when_too_little_time_is_left(fake):
    fake.delay["art1"] = 0.3
    fake.delay["art-slow"] = 4.5
    pools = Pools()
    aurl, rs, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1")
    slow = slow_ondemand(aurl, fake)
    search(aurl)
    time.sleep(0.1)
    slow.start()  # the prefetch fetches, then yields until ~4.6 s: < 3 s left of its 6 s
    slow.join()
    p = settle(aurl, 1)
    assert p["gave_up"] == 1 and p["failed"] == 0 and p["failed_by"] == {}, p
    assert "pf" not in pools.used


# ---------- compose defaults equal the code defaults ----------
def test_compose_prefetch_defaults_match_settings(monkeypatch):
    from fastweb_firecrawl.settings import Settings
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    found = dict(re.findall(r"(FASTWEB_PREFETCH_\w+): \$\{\1:-([^}]*)\}", compose))
    assert {"FASTWEB_PREFETCH_MAX_BYTES", "FASTWEB_PREFETCH_MAX_HTML"} <= set(found)
    for k in list(found):
        monkeypatch.delenv(k, raising=False)
    default = Settings()
    for k, v in found.items():
        monkeypatch.setenv(k, v)
    pinned = Settings()
    fields = [f for f in vars(default) if f.startswith("prefetch_")]
    assert {f: getattr(pinned, f) for f in fields} == {f: getattr(default, f) for f in fields}


# ---------- prefetch meets the full budget and does not wait ----------
@pytest.mark.slow
def test_prefetch_never_waits_for_byte_budget_held_by_another_prefetch(fake):
    """No on-demand page in flight (no busy skip): one prefetch holds the budget in a slow pf extraction, the
    other meets the full budget and fails at once instead of waiting."""
    pools = Pools(slow={"pf"}, delay=5.5)
    fake.delay["art2"] = 0.2
    aurl, _, _ = make(fake, inflight_bytes=100, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1", "art2")
    t0 = time.monotonic()
    search(aurl)
    while stats(aurl)["prefetch"]["failed"] < 1 and time.monotonic() - t0 < 6:
        time.sleep(0.05)
    p = stats(aurl)["prefetch"]
    assert p["failed"] == 1 and time.monotonic() - t0 < 3.5, p  # not after the 5.5 s extraction
    settle(aurl, 2)


# ---------- the app-level busy() counts one on-demand request ----------
@pytest.mark.slow
def test_one_on_demand_request_in_flight_makes_prefetch_skip(fake):
    fake.delay["art-slow"] = 3.5
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0)
    slow = slow_ondemand(aurl, fake)
    slow.start()
    time.sleep(0.2)
    fake.results = urls(fake, "art1")
    search(aurl)
    p = settle(aurl, 1)
    slow.join()
    assert p["skipped"] == {"busy": 1} and "art1" not in fake.hits, p
