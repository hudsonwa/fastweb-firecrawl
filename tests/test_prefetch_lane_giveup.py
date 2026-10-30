"""A prefetched PDF that queued for the pf lane with too little time left gives up;
its worker is not killed."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from .test_prefetch import make, search, settle, urls
from .test_prefetch_joining_concurrent import fake


class Seq:
    """pf lane: job k sleeps delays[k] before running (first job holds the lane; later ones model a slow extraction)."""

    def __init__(self, delays):
        self.delays, self.used = list(delays), []

    def __call__(self, name):
        pools = self

        class Pool(ThreadPoolExecutor):
            def submit(self, fn, *a, **kw):
                k = len([u for u in pools.used if u[0] == name])
                pools.used.append((name, fn.__name__))
                d = pools.delays[k] if name == "pf" and k < len(pools.delays) else 0

                def run(*a, **kw):
                    time.sleep(d)
                    return fn(*a, **kw)
                return super().submit(run, *a, **kw)
        return Pool(1)


@pytest.mark.slow
def test_pdf_prefetch_after_lane_queue_not_killed(fake):
    fake.delay["art1"] = 0.1
    fake.delay["bigpdf"] = 0.3
    pools = Seq([4.1, 2.3])  # art1 holds the pf lane until ~4.2 s; the PDF then needs 2.3 s (4 MB PDF at nice 10)
    aurl, _, _ = make(fake, prefetch_timeout=6.0, total_timeout=8.0, lane_factory=pools)
    fake.results = urls(fake, "art1", "bigpdf")
    search(aurl)
    p = settle(aurl, 2, timeout=12)
    print(p, pools.used)
    assert p["failed"] == 0 and p["gave_up"] == 1, p  # killed -> 422 "worker killed" counted failed
