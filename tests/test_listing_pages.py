"""Main-only extraction keeps listing/index items (linked titles trafilatura drops) while articles stay main-only.
Fixtures are synthetic pages shaped like real listing, article and product pages."""
import re

import pytest

from fastweb_firecrawl import extract
from fastweb_firecrawl.extract import extract_job

MENU = ("<div class='menu'><ul>" + "".join(
    f"<li><a href='/sec{i}/'>Navigation section number {i}</a><ul>"
    + "".join(f"<li><a href='/sec{i}/p{j}'>Sub page {i}.{j} title</a></li>" for j in range(3)) + "</ul></li>"
    for i in range(4)) + "</ul></div>")
FOOTER = ("<footer><ul>" + "".join(f"<li><a href='/f{i}'>Footer link {i}</a></li>" for i in range(10))
          + "</ul><p>Copyright Example Organisation. All rights reserved.</p></footer>")
INTRO = ("<p>The board is responsible for formulating policy. It sets the target rate, which is the market interest "
         "rate on overnight funds. Decisions are explained in a media release announcing the decision at 2.30 pm "
         "after each board meeting. Before 2007, releases were issued only when the target was changed.</p>")
DATES = ["29 September 2026", "11 August 2026", "16 June 2026", "5 May 2026", "17 March 2026", "3 February 2026"]
URL = "https://bank.example/decisions/"


def decisions(dates=DATES):
    items = "".join(f'<li><a href="/media-releases/2026/mr-{i}.html">{d}</a></li>' for i, d in enumerate(dates))
    return ("<html><head><title>Policy decisions</title></head><body>" + MENU
            + "<div id='content'><section><h1>Policy Decisions - 2026</h1>" + INTRO
            + f"<ul class='list-articles'>{items}</ul></section><aside><h2>Related</h2><ul>"
            + "".join(f"<li><a href='/r{i}'>Related publication {i}</a></li>" for i in range(5))
            + "</ul></aside></div>" + FOOTER + "</body></html>")


def article(rail=True):
    paras = "".join(f"<p>Paragraph {i}: the harbour council voted on the ferry terminal plan after a long debate "
                    f"about funding, tides and the timetable for construction work along the eastern wharf.</p>"
                    for i in range(25))
    rail_html = ("<div class='rail'><h2>Most popular</h2><ul>" + "".join(
        f"<li><a href='/news/story-{i}'>Another popular story headline number {i}</a></li>" for i in range(8))
        + "</ul></div>") if rail else ""
    return ("<html><head><title>Ferry terminal approved</title></head><body>" + MENU
            + f"<main><article><h1>Ferry terminal approved</h1>{paras}</article>{rail_html}</main>"
            + FOOTER + "</body></html>")


def without_listing(monkeypatch, html, url):
    with monkeypatch.context() as m:
        m.setattr(extract, "is_listing", lambda *a: False)
        return extract_job(html, url, True)[0]


def test_listing_keeps_linked_items(monkeypatch):
    html = decisions()
    assert "29 September 2026" not in without_listing(monkeypatch, html, URL)  # the gap the fallback closes
    md = extract_job(html, URL, True)[0]
    for d in DATES:
        assert d in md, d


def test_listing_survives_one_item_fewer():
    md = extract_job(decisions(DATES[1:]), URL, True)[0]
    for d in DATES[1:]:
        assert d in md, d


def test_article_with_link_rail_stays_main_only(monkeypatch):
    html = article()
    md = extract_job(html, "https://news.example/ferry", True)[0]
    assert md == without_listing(monkeypatch, html, "https://news.example/ferry")
    assert "Another popular story" not in md and "Paragraph 24" in md


def test_url_slug_does_not_count_as_title():
    """A link URL's slug must not make a dropped title look present."""
    text = extract.listing_text("Read [more](https://x.example/pentagon-fires-general-it-says-leaked-classified-information)")
    assert "pentagon" not in text and "more" in text


def test_no_listing_scan_when_body_too_big_for_whole_page(monkeypatch):
    """The whole-page fallback can't happen over WHOLE_PAGE_MAX_HTML, so the scan is skipped."""
    monkeypatch.setattr(extract, "WHOLE_PAGE_MAX_HTML", 100)
    monkeypatch.setattr(extract, "is_listing", lambda *a: pytest.fail("scanned"))
    assert extract_job(decisions(), URL, True)[0]


def test_listing_items_link_to_their_pages():
    md = extract_job(decisions(), URL, True)[0]
    assert len(re.findall(r"\]\((?:https://bank\.example)?/media-releases/2026/", md)) >= len(DATES)
