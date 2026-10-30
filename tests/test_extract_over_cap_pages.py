"""Honesty of the over-cap (big lane) path on 3.2-6 MB hostile fixtures."""
import gzip
import os
import random

import pytest

from .conftest import EXTRA
from .test_api import scrape
from .test_fetch_ladder import CHALLENGE_200, CONSENT_WALL, make

MB = 1024 * 1024


def go(ctx, name, body, ctype="text/html", status=200):
    up, furl, _ = ctx
    EXTRA[name] = (body, ctype, status)
    n0 = len(up.scrapes)
    (srv, aurl), _ = make(furl, max_bytes=6 * MB, extract_max_html=3 * MB, total_timeout=10.0)
    try:
        r = scrape(aurl, furl + f"/p/{name}")
        j = r.json()
        d = j.get("data") or {}
        fw = (d.get("metadata") or {}).get("fastweb") or {}
        return r.status_code, fw, d.get("markdown") or "", len(up.scrapes) > n0, j
    finally:
        srv.should_exit = True


def cheap_success(res):
    code, fw, md, fc, _ = res
    return code == 200 and fw.get("engine") in ("plain", "impersonate")


def wall_padded(base, pad, size):
    unit = pad
    return base.replace("</body>", unit * ((size - len(base)) // len(unit)) + "</body>").encode()


LINKS = "<div class='f'><a href='/x'>Footer link to a section page</a></div>"
HIDDEN = "<div hidden>hidden section words lorem ipsum dolor sit amet</div>"
ARIA = "<div aria-hidden='true'>hidden section words lorem ipsum dolor sit amet</div>"
DIALOG = "<dialog>dialog words lorem ipsum dolor sit amet consectetur</dialog>"


@pytest.mark.parametrize("pad", [LINKS, HIDDEN, ARIA, DIALOG], ids=["links", "hidden", "aria", "dialog"])
def test_consent_wall_padded(ctx, pad):
    res = go(ctx, "cw" + str(abs(hash(pad)) % 1000), wall_padded(CONSENT_WALL, pad, int(3.4 * MB)))
    assert not cheap_success(res), (res[0], res[1], res[2][:200])


@pytest.mark.parametrize("pad", [LINKS, HIDDEN], ids=["links", "hidden"])
def test_challenge_padded(ctx, pad):
    res = go(ctx, "ch" + str(abs(hash(pad)) % 1000), wall_padded(CHALLENGE_200, pad, int(3.4 * MB)))
    assert not cheap_success(res), (res[0], res[1], res[2][:200])


def test_js_shell_unclosed_script(ctx):
    body = ("<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app."
            "</noscript><div id='root'></div><script>window.__S=\"" + "abcdefgh " * (int(3.5 * MB) // 9)).encode()
    res = go(ctx, "jsunclosed", body)
    assert not cheap_success(res), (res[0], res[1], res[2][:200])


def test_js_shell_unclosed_style(ctx):
    body = ("<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app."
            "</noscript><div id='root'></div><style>" + ".a{color:red} " * (int(3.5 * MB) // 15)).encode()
    res = go(ctx, "jsstyle", body)
    assert not cheap_success(res), (res[0], res[1], res[2][:200])


def test_js_shell_hidden_json(ctx):
    """Next-like shell where the state blob sits in a hidden div (not a <script>)."""
    body = ("<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app."
            "</noscript><div id='root'></div><div hidden id='state'>" + '{"k":"v","n":12345},' * (int(3.5 * MB) // 20)
            + "</div></body></html>").encode()
    res = go(ctx, "jshidden", body)
    assert not cheap_success(res), (res[0], res[1], res[2][:200])


@pytest.mark.parametrize("kind", ["random", "gzip", "png", "random_utf8hdr"])
def test_binary_over_cap_as_html(ctx, kind):
    rnd = random.Random(7)
    raw = bytes(rnd.getrandbits(8) for _ in range(int(3.6 * MB)))
    body = {"random": raw, "random_utf8hdr": raw, "gzip": gzip.compress(raw, 1),
            "png": b"\x89PNG\r\n\x1a\n" + raw}[kind]
    ctype = "text/html; charset=utf-8" if kind == "random_utf8hdr" else "text/html"
    res = go(ctx, "bin" + kind, body, ctype)
    assert not cheap_success(res), (res[0], res[1], repr(res[2][:120]))


def test_binary_under_cap_as_html_baseline(ctx):
    """Baseline (pre-existing path): the same binary under the cap."""
    rnd = random.Random(7)
    raw = bytes(rnd.getrandbits(8) for _ in range(int(1.0 * MB)))
    res = go(ctx, "binsmall", raw)
    assert not cheap_success(res), (res[0], res[1], repr(res[2][:120]))


def test_output_bounded(ctx):
    body = ("<html><head><title>Huge</title></head><body>" + "<p>word " * (int(5.9 * MB) // 8) + "</body></html>").encode()
    code, fw, md, fc, _ = go(ctx, "hugeout", body)
    assert code == 200 and fw.get("text_only_large") and len(md) <= 500_000 + 200, len(md)


def test_small_lane_style_404_over_cap(ctx):
    body = ("<html><body><h1>Not found</h1>" + "<p>Sorry, nothing here. </p>" * (int(3.4 * MB) // 30)).encode()
    code, *_ = go(ctx, "big404", body, status=404)
    assert code == 422


def test_big_page_runs_big_text_job_in_big_lane(ctx):
    """the over-cap page goes to big_text_job in the big lane (tasks_per_child=1 returns its memory)."""
    from concurrent.futures import ThreadPoolExecutor
    up, furl, _ = ctx
    calls = []

    class Rec(ThreadPoolExecutor):
        def __init__(self, name):
            super().__init__(1)
            self.name = name

        def submit(self, fn, *a, **k):
            calls.append((self.name, fn.__name__))
            return super().submit(fn, *a, **k)

    body = ("<html><head><title>Huge</title></head><body>" + "<p>word " * (int(3.5 * MB) // 8)).encode()
    EXTRA["lanecheck"] = (body, "text/html", 200)
    (srv, aurl), _ = make(furl, max_bytes=6 * MB, extract_max_html=3 * MB, total_timeout=10.0, lane_factory=Rec)
    try:
        r = scrape(aurl, furl + "/p/lanecheck")
    finally:
        srv.should_exit = True
    assert r.status_code == 200 and ("big", "big_text_job") in calls and not any(
        n != "big" for n, f in calls if f == "big_text_job"), calls


def test_big_text_job_output_capped():
    """big_text_job itself caps markdown at MAX_MD_CHARS while n reports the full text."""
    from fastweb_firecrawl.extract import MAX_MD_CHARS, big_text_job
    md, title, desc, n = big_text_job("<html><body>" + "<p>word " * 200_000 + "</body></html>", "http://x/")
    assert len(md) <= MAX_MD_CHARS and n > MAX_MD_CHARS
