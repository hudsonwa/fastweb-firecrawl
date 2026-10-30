"""Once the pf lane is held, a prefetch gives up only when the time left is below an estimate from its
markup size (0.75 s + ~1 s per MB, floor 1.5 s), not the 3 s after-yield PF_MIN_EXTRACT."""
import threading
import time

import pytest

from fastweb_firecrawl.pipeline import PF_MIN_EXTRACT, pf_lane_min

from .test_prefetch import make, sdk_scrape, search, settle, urls
from .test_prefetch_joining_concurrent import fake
from .test_prefetch_lane_giveup import Seq
from .test_prefetch_resources import Pools

MB = 1024 * 1024


class FirstSlow(Pools):
    """Only the first pf extraction is slow: the second one waits for the lane, then runs at normal speed."""

    def __call__(self, name):
        pool, pools = super().__call__(name), self
        submit = pool.submit

        def once(fn, *a, **kw):
            r = submit(fn, *a, **kw)
            pools.slow.discard(name)
            return r
        pool.submit = once
        return pool


def test_one_mb_page_with_two_seconds_left_extracts():
    assert pf_lane_min(MB) < 2.0


def test_big_page_with_one_and_a_half_seconds_left_gives_up():
    assert pf_lane_min(int(3.5 * MB)) > 3.5  # ~1 s per MB, not a token slope


def test_floor_and_after_yield_threshold_kept():
    assert pf_lane_min(0) == 1.5 and PF_MIN_EXTRACT == 3.0


@pytest.mark.slow
def test_small_prefetch_extracts_with_two_seconds_left_after_lane_queue(fake):
    for nm in ("art1", "art2"):
        fake.delay[nm] = 0.3
    pools = FirstSlow(slow={"pf"}, delay=3.5)  # art2 gets the lane at ~3.8 s: ~2.2 s left of its 6 s
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1", "art2")
    search(aurl)
    p = settle(aurl, 2)
    assert p["done"] == 2 and p["gave_up"] == 0, p


@pytest.mark.slow
def test_big_prefetch_gives_up_after_lane_queue_when_size_needs_more_time(fake):
    # the markup size reaches the lane check (2.5 MB needs ~3.25 s; ~2.2 s are left)
    fake.delay["art1"] = fake.delay["artbig"] = 0.3
    fake.pages["artbig"] = "<html><body><article>" + "<p>The harbour story goes on and on.</p>" * 64_000 + "</article></body></html>"
    pools = FirstSlow(slow={"pf"}, delay=3.5)
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, max_bytes=4 * MB, lane_factory=pools)
    fake.results = urls(fake, "art1", "artbig")
    search(aurl)
    p = settle(aurl, 2, timeout=15)
    assert p["done"] == 1 and p["gave_up"] == 1 and pools.used.count("pf") == 1, (p, pools.used)


@pytest.mark.slow
def test_started_prefetch_extraction_not_killed_at_prefetch_deadline(fake):
    # a started nice-10 extraction gets left + 2 s, so only a real hang is killed
    fake.delay["art1"] = 0.3
    pools = Pools(slow={"pf"}, delay=3.5)  # ends ~0.8 s after the 3 s prefetch deadline
    aurl, _, _ = make(fake, prefetch_timeout=3.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1")
    search(aurl)
    p = settle(aurl, 1, timeout=10)
    assert p["done"] == 1 and p["failed"] == 0, p


@pytest.mark.slow
def test_joined_prefetch_extracts_after_lane_queue_with_little_time_left(fake):
    # a prefetch an on-demand request joined does not give up at the lane check
    fake.delay["art1"] = fake.delay["art2"] = 0.3
    pools = Seq([4.6])  # art2 gets the pf lane at ~4.9 s: ~1.1 s of its 6 s left, below the 1.5 s floor
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1", "art2")
    search(aurl)
    time.sleep(1.0)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=sdk_scrape(aurl, urls(fake, "art2")[0])))
    t.start()
    p = settle(aurl, 2, timeout=12)
    t.join()
    assert out["r"].status_code == 200 and p["gave_up"] == 0 and p["done"] == 2, p
