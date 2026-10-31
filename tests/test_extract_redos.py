"""Every regex-driven markdown/HTML scan stays linear on adversarial input (ReDoS). Each input is a few hundred KB
that made a quadratic version take tens of seconds; the bound is generous (BOUND seconds) so a loaded CI machine
or a parallel test run never trips it, while a quadratic regression still does by a wide margin."""
import time

import pytest

from fastweb_firecrawl import extract, heuristics, links

BOUND = 5.0
N = 100_000  # repetitions of each adversarial unit: 300-800 KB of input


def timed(fn, *args):
    t = time.perf_counter()
    fn(*args)
    return time.perf_counter() - t


CASES = {
    # regression: ^```.*?^``` with re.S was quadratic on many unclosed fence openers (750 KB took 94 s)
    "unclosed code fences": (extract.strip_inline_tags, "```x <sup>1</sup>\n" * (N // 2)),
    "fence openers, no tags": (extract.tidy_blank_lines, "```x\n \n" * N),
    "inline link openers": (lambda s: links.absolutize_links(s, "https://e.example/"), "](a" * N),
    "inline nested parens": (lambda s: links.absolutize_links(s, "https://e.example/"), "](" + "(" * N),
    "inline angle openers": (lambda s: links.absolutize_links(s, ""), "](<a" * N),
    "inline leading spaces": (lambda s: links.absolutize_links(s, ""), ("](" + " " * 20) * N),
    "entity-padded schemes": (lambda s: links.absolutize_links(s, ""), "[a](j&#0000;" * N),
    "reference definitions": (lambda s: links.absolutize_links(s, ""), "[" + "a" * 900 + "]:\n" * N),
    "container-prefixed definitions": (lambda s: links.absolutize_links(s, ""), ("> - 1. " * 20 + "[a\n") * (N // 100)),
    "autolink openers": (lambda s: links.absolutize_links(s, ""), "<ab:" * N),
    "raw html attributes": (lambda s: links.absolutize_links(s, ""), " href=" * N),
    "slash attributes, open backticks": (lambda s: links.absolutize_links(s, ""), "</href=`" * N),
    "link-url strip": (extract.listing_text, "](a" * N),
    "long spaces": (extract.squeeze_space, (" " * 64 + "x") * (N // 10)),
    "unclosed heavy blocks": (extract.lighten, "<script>" * N),
    "font tags": (extract.FONT_TAG.findall, "<font " * N),
    "blank line runs": (extract.BIG_BLOCKS.findall, "\n \t" * N),
    "junk scan": (extract.junk_share, "�é" * N),
    "auth prompt lines": (extract.auth_prompt, "".join(f"sign in to view {i}\n" for i in range(N // 2)) + "y" * N),
    "meta charset": (extract.META_CHARSET.search, b"<meta a" * N),
    "app-root marker": (heuristics.APP_ROOT.search, "<div a" * N),
    "module script marker": (heuristics.MODULE_SCRIPT.search, "<script a" * N),
    "shell markers": (extract.SHELL_MARKERS.search, "enable javascrip " * N),
    "link-share brackets": (heuristics.mostly_links, "[" * N + "](" * N),
    "link-share unclosed targets": (heuristics.mostly_links, "[a](b" * N),
    "fence and blank-line tidy": (extract.tidy_blank_lines, "~~~\n\n\n```\n \n" * N),
}


@pytest.mark.parametrize("name", CASES)
def test_scan_is_linear(name):
    fn, data = CASES[name]
    assert timed(fn, data) < BOUND, name


def test_unclosed_fences_through_extract_job():
    # End to end: a page of <p>```x</p> (220 KB once took 8.7 s). Linear now: well under a second unloaded; the
    # bound only catches a return to quadratic.
    page = "<html><body><main>" + "<p>```x &lt;sup&gt;</p>" * 12_000 + "</main></body></html>"
    assert timed(extract.extract_job, page, "https://e.example/", True) < 4 * BOUND


def test_fences_are_still_respected():
    md = "a<sup>1</sup>\n```\n<sup>kept</sup>\n```\nb<sub>2</sub>\n~~~\n<sup>kept too</sup>\n```\nstill code\n~~~\n"
    out = extract.strip_inline_tags(md)
    assert "a1\n" in out and "b2\n" in out and "<sup>kept</sup>" in out and "<sup>kept too</sup>" in out
    assert extract.strip_inline_tags("x\n```\n<sup>unclosed</sup>") == "x\n```\n<sup>unclosed</sup>"


def test_ref_def_container_prefix_is_linear():
    md = ">   " * 16 + "x\n"
    t = time.monotonic()
    links.absolutize_links(md * 2000 + "> - [a]: javascript:x\n", "")
    assert time.monotonic() - t < 0.5
    assert links.absolutize_links("> - [a]: javascript:x", "") == "> - [a]: #"
