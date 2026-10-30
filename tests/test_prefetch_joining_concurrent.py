"""Prefetch promotion with several prefetches in flight and the PF_MIN_EXTRACT check once the pf
lane is held."""
import threading
import time

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


def _multi(fake, n_results, target, prefetch_n=5, lane_factory=None):
    """n_results prefetches in flight (0.5 s pages); an unrelated on-demand page waits 4 s on the network; 0.2 s
    after the search an on-demand request joins the prefetch of `target`."""
    names = [f"art{i}" for i in range(1, n_results + 1)]
    for nm in names:
        fake.delay[nm] = 0.5
    fake.delay["art-slow"] = 6.0
    kw = {"lane_factory": lane_factory} if lane_factory else {}
    aurl, _, _ = make(fake, prefetch_n=prefetch_n, prefetch_timeout=6.0, total_timeout=8.0, **kw)
    fake.results = urls(fake, *names)
    slow = threading.Thread(target=sdk_scrape, args=(aurl, urls(fake, "art-slow")[0]))
    search(aurl)
    time.sleep(0.1)
    slow.start()
    time.sleep(0.1)
    r, dt = timed(sdk_scrape, aurl, urls(fake, target)[0])
    slow.join()
    return aurl, r, dt, fake.hits.count(target)


# ---------- the joined flight is the one promoted, whichever prefetch set its key last ----------
@pytest.mark.slow
@pytest.mark.parametrize("n_results,target", [(2, "art1"), (2, "art2"), (3, "art1"), (3, "art2")])
def test_joiner_not_delayed_with_several_prefetches_in_flight(fake, n_results, target):
    _, r, dt, n = _multi(fake, n_results, target)
    assert r.status_code == 200 and dt < 3.5 and n == 1, (dt, n)  # bad case waits for art-slow (6 s)


@pytest.mark.slow
def test_unjoined_prefetch_not_promoted_when_another_is_joined(fake):
    pools = Pools()
    aurl, r, dt, n = _multi(fake, 2, "art2", lane_factory=pools)
    p = settle(aurl, 2)
    assert r.status_code == 200 and n == 1
    assert pools.used.count("pf") == 1 and p["gave_up"] == 1, (pools.used, p)  # art1 never extracts beside art-slow


# ---------- a prefetch that waited in the pf lane queue gives up instead of being killed ----------
@pytest.mark.slow
def test_prefetch_gives_up_after_pf_lane_queue_wait(fake):
    for nm in ("art1", "art2"):
        fake.delay[nm] = 0.3
    pools = Pools(slow={"pf"}, delay=5.3)  # art2 gets the lane at ~5.6 s: < 1 s left of its 6 s
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1", "art2")
    search(aurl)
    p = settle(aurl, 2)
    assert p["done"] == 1 and p["gave_up"] == 1 and p["failed"] == 0 and p["failed_by"] == {}, p
    assert pools.used.count("pf") == 1, pools.used
