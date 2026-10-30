"""Wall/binary variants over the cap (3.4 MB) and under it (1.2 MB)."""
import gzip
import random

import httpx
import pytest

from .conftest import EXTRA
from .test_fetch_ladder import make

MB = 1024 * 1024
WALL = ("<div class='cmp'><p>We value your privacy. We and our partners use cookies to personalise content.</p>"
        "<p>Accept all to continue.</p></div>")
CHAL = "<p>Checking your browser before accessing the site.</p>"
LINK = "<div class='f'><a href='/x'>Footer link to a section page</a></div>"
CSSHID = "<div style='display:none'>hidden section words lorem ipsum dolor sit amet</div>"
FILL = "<p>Plain filler paragraph words lorem ipsum dolor sit amet.</p>"
LIST_ROW = "<tr><td><a href='/c'>Country name</a></td><td>16</td><td>Notes on the statute of the year</td></tr>"


def pad_to(pre, unit, post, size):
    return pre + unit * max(0, (size - len(pre) - len(post)) // len(unit)) + post


def fixtures(size):
    rnd = random.Random(7)
    raw = bytes(rnd.getrandbits(8) for _ in range(size))
    real_head = ("<html><head><title>Harbour</title></head><body><h1>Harbour</h1>"
                 + "<p>Real article words about the harbour vote today.</p>" * 400)
    utf16 = pad_to("<html><head><title>Liste</title></head><body><h1>Liste</h1><table>", LIST_ROW,
                   "</table></body></html>", size // 2).encode("utf-16-le")
    return {
        # honesty: must NOT be a cheap success
        "wall_links_first": (pad_to("<html><head><title>Daily News</title></head><body>", LINK, WALL + "</body></html>", size).encode(), "text/html"),
        "chal_links_first_neutral_title": (pad_to("<html><head><title>news.example</title></head><body>", LINK, CHAL + "</body></html>", size).encode(), "text/html"),
        "chal_links_first_jm_title": (pad_to("<html><head><title>Just a moment...</title></head><body>", LINK, CHAL + "</body></html>", size).encode(), "text/html"),
        "wall_csshidden_first": (pad_to("<html><head><title>Daily News</title></head><body>", CSSHID, WALL + "</body></html>", size).encode(), "text/html"),
        "wall_csshidden_after": (pad_to("<html><head><title>Daily News</title></head><body>" + WALL, CSSHID, "</body></html>", size).encode(), "text/html"),
        "wall_nonlink_fill_after": (pad_to("<html><head><title>Daily News</title></head><body>" + WALL, FILL, "</body></html>", size).encode(), "text/html"),
        "js_noscript_links": (pad_to("<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app.</noscript>", LINK, "</body></html>", size).encode(), "text/html"),
        "mixed_html_then_random": (real_head.encode() + raw[: size - len(real_head)], "text/html"),
        "mixed_html_then_gzip": (real_head.encode() + gzip.compress(raw, 1)[: size - len(real_head)], "text/html"),
        "mixed_html_then_random_utf8": (real_head.encode() + raw[: size - len(real_head)], "text/html; charset=utf-8"),
        # false-fallback / false-422: real pages that SHOULD be a cheap success
        "legit_consent_heading": (pad_to("<html><head><title>Age of consent by country</title></head><body><h1>Age of consent by country</h1><table>", LIST_ROW, "</table></body></html>", size).encode(), "text/html"),
        "legit_captcha_early": (pad_to("<html><head><title>Big FAQ</title></head><body><h1>Big FAQ</h1><p>Why do I see a CAPTCHA when I log in?</p><table>", LIST_ROW, "</table></body></html>", size).encode(), "text/html"),
        "legit_utf16le_nobom_hdr": (utf16, "text/html; charset=utf-16le"),
        "legit_utf16le_nobom_nohdr": (utf16, "text/html"),
    }


SIZES = {"over": int(3.4 * MB), "under": int(1.2 * MB)}
HONEST = ["wall_links_first", "chal_links_first_neutral_title", "chal_links_first_jm_title"]
BINARY = ["mixed_html_then_random", "mixed_html_then_gzip", "mixed_html_then_random_utf8"]
LEGIT = ["legit_consent_heading", "legit_captcha_early", "legit_utf16le_nobom_hdr", "legit_utf16le_nobom_nohdr"]


def run(ctx, key, body, ctype, budget=40.0):
    """budget: total and extraction timeout (s). Generous by default: these tests check which engine answered, and
    a multi-megabyte extraction under coverage on a busy runner must not fall back to Firecrawl for lack of time."""
    up, furl, _ = ctx
    EXTRA[key] = (body, ctype, 200)
    n0 = len(up.scrapes)
    (srv, aurl), _ = make(furl, max_bytes=6 * MB, extract_max_html=3 * MB, total_timeout=budget,
                          extract_timeout=max(20.0, budget - 5))
    try:
        r = httpx.post(aurl + "/v2/scrape", json={"url": furl + f"/p/{key}", "timeout": budget * 1000},
                       timeout=budget + 30)
        j = r.json()
        d = j.get("data") or {}
        fw = (d.get("metadata") or {}).get("fastweb") or {}
        return r.status_code, fw, d.get("markdown") or "", len(up.scrapes) > n0, j.get("error") or ""
    finally:
        srv.should_exit = True


def cheap(res):
    return res[0] == 200 and res[1].get("engine") in ("plain", "impersonate")


@pytest.mark.parametrize("name", HONEST)
def test_wall_after_padding_not_cheap_over_cap(ctx, name):
    body, ctype = fixtures(SIZES["over"])[name]
    res = run(ctx, "wall-" + name, body, ctype)
    assert not cheap(res), (res[0], res[1], res[2][:200])


@pytest.mark.parametrize("where", list(SIZES))
@pytest.mark.parametrize("name", BINARY)
def test_mixed_binary_is_422(ctx, name, where):
    body, ctype = fixtures(SIZES[where])[name]
    res = run(ctx, f"r2{name}{where}", body, ctype)
    assert res[0] == 422 and "binary" in res[4], (res[0], res[4], res[2][:100])
    assert not res[3]


@pytest.mark.slow
@pytest.mark.xdist_group("bigpages")  # the multi-megabyte extractions run on one worker, never against each other
@pytest.mark.parametrize("where", list(SIZES))
@pytest.mark.parametrize("name", LEGIT)
def test_legit_big_pages_cheap(ctx, name, where):
    body, ctype = fixtures(SIZES[where])[name]
    res = run(ctx, f"r2{name}{where}", body, ctype)
    assert cheap(res) and not res[3], (res[0], res[1], res[4], res[2][:200])
    assert "Country name" in res[2] and len(res[2]) > 100_000


def test_title_only_marker_over_cap(ctx):
    body = pad_to("<html><head><title>Just a moment...</title></head><body>", LINK, "</body></html>", SIZES["over"])
    res = run(ctx, "r2titleonly", body.encode(), "text/html")
    assert not cheap(res), (res[0], res[1], res[2][:200])


def test_big_path_strips_form(ctx):
    body = pad_to("<html><head><title>Big list</title></head><body><form><p>FORMJUNK field label</p></form><table>",
                  LIST_ROW, "</table></body></html>", SIZES["over"])
    res = run(ctx, "r2form", body.encode(), "text/html")
    assert cheap(res) and res[1].get("lane") == "big", (res[0], res[1])
    assert "FORMJUNK" not in res[2]


def test_replacement_char_soup_is_binary(ctx):
    rnd = random.Random(3)
    # ~35 % lone high bytes (U+FFFD each) among letters: over the 25 % rule, far under 50 %, no C0 at all
    soup = bytes(rnd.randrange(0x80, 0xC0) if rnd.random() < 0.35 else rnd.randrange(97, 123)
                 for _ in range(SIZES["under"]))
    res = run(ctx, "r2ffd", b"<html><body>" + soup, "text/html; charset=utf-8")
    assert res[0] == 422 and "binary" in res[4], (res[0], res[4])


@pytest.mark.parametrize("n, tail", [(8150, "\x1a"), (8191, "\x1a"), (8191, "�")])
def test_short_body_middle_window_does_not_wrap(n, tail):
    """under 8 K chars the middle window must not wrap to the last few chars and call one stray
    control or replacement char 'binary'."""
    from fastweb_firecrawl.heuristics import is_binary
    text = "<p>" + "a" * (n - 3 - len(tail)) + tail
    assert len(text) == n
    assert not is_binary(b"<p>", text)
