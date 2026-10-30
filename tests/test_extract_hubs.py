"""Card-style news hubs (story cards as block-level links, as on bbc.com/news; headline links next to summary
paragraphs, as on nytimes.com) come back as readable markdown with one link per story, while articles are left to
trafilatura. Fixtures are synthetic: they copy the page structure, not any site's text."""
import re

from fastweb_firecrawl.extract import extract_job

NAV = "".join(f"<li><a href='/{s.lower()}'>{s}</a></li>" for s in ("Home", "World", "Business", "Sport", "Travel"))
SUMMARY = "Officials in the harbour town said the work would continue through the winter months despite delays."


def card_hub(n=16):
    """bbc.com/news-like: each story is <a><h3>title</h3><p>summary</p><span>age</span></a>, nothing else."""
    cards = "".join(
        f"<div><a href='/news/articles/story{i}'><h3>Harbour council approves ferry terminal plan number {i}</h3>"
        f"<p>{SUMMARY}</p><span>{i} hrs ago</span><span>World</span></a></div>"
        for i in range(n))
    return (f"<html><head><title>News</title></head><body><header><nav><ul>{NAV}</ul></nav></header>"
            f"<main><h1>News</h1><section>{cards}</section></main></body></html>")


def headline_hub(n=30):
    """nytimes.com-like: a headline link above a short separate summary in each story block (about half the page
    text is link text, as measured on the real page)."""
    blocks = "".join(
        f"<section><div><a href='/2026/10/30/world/story-{i}.html'><div><p>Inside the long fight over the "
        f"eastern wharf, part {i}</p></div></a></div><p>Work goes on despite delays.</p><p>{i} min read</p></section>"
        for i in range(n))
    return (f"<html><head><title>The Daily</title></head><body><nav><ul>{NAV}</ul></nav><main>{blocks}</main>"
            "</body></html>")


def article_with_rail():
    """An article whose page also carries a rail of related headlines: must stay the article."""
    paras = "".join(f"<p>{SUMMARY} The vote was close, and the minority report runs to {i} pages.</p>"
                    for i in range(25))
    rail = "".join(f"<li><a href='/news/related{i}'>Another story about the wharf and its ferry, part {i}</a></li>"
                   for i in range(10))
    return (f"<html><head><title>Ferry vote</title></head><body><nav><ul>{NAV}</ul></nav><main><article>"
            f"<h1>Ferry vote</h1>{paras}</article><aside><h2>Related</h2><ul>{rail}</ul></aside></main>"
            "</body></html>")


def story_links(md):
    return set(re.findall(r"\]\((https://news\.example/[^)]+(?:story|related)[^)]*)\)", md))


def test_card_hub_keeps_every_story_link_on_its_own_lines():
    md = extract_job(card_hub(), "https://news.example/news", True)[0]
    assert len(story_links(md)) == 16
    assert "https://news.example/news/articles/story7" in md
    assert md.count("\n") > 30  # one block per card, not one line of run-together text


def test_headline_hub_keeps_headlines_with_their_links():
    md = extract_job(headline_hub(), "https://news.example/", True)[0]
    assert "[Inside the long fight over the eastern wharf, part 5](https://news.example/2026/10/30/world/story-5.html)" \
        in md
    assert len(story_links(md)) == 30


def test_article_with_related_rail_stays_main_content():
    md = extract_job(article_with_rail(), "https://news.example/news/ferry", True)[0]
    assert "minority report runs to 24 pages" in md
    assert not story_links(md) and "Another story about the wharf" not in md


def overlay_hub(n=20):
    """theguardian.com-like: each card holds an empty full-card <a href aria-label> beside (not around) its
    headline, and a kicker + headline + summary; no link wraps any text."""
    cards = "".join(
        f"<li><div class='card'><a href='/world/2026/oct/30/story{i}' aria-label='Harbour story {i}'></a>"
        f"<div><h3><span>Harbour</span> <span>Council approves ferry terminal plan number {i}</span></h3>"
        f"<p>{SUMMARY}</p></div></div></li>"
        for i in range(n))
    return (f"<html><head><title>News</title></head><body><header><nav><ul>{NAV}</ul></nav></header>"
            f"<main><h1>International</h1><section><ul>{cards}</ul></section></main></body></html>")


def test_overlay_link_cards_keep_their_links():
    for main_only in (True, False):
        md = extract_job(overlay_hub(), "https://news.example/international", main_only)[0]
        assert "[Harbour Council approves ferry terminal plan number 7](https://news.example/world/2026/oct/30/story7)" \
            in md, main_only
        assert len(story_links(md)) == 20


def test_overlay_rewrite_leaves_ordinary_pages_alone():
    # an empty icon link next to an already linked headline, and an article: unchanged
    page = ("<html><head><title>T</title></head><body><main><article><h1>Ferry vote</h1>"
            "<div><a href='/share' aria-label='Share'></a><h2><a href='/x'>Linked headline here</a></h2></div>"
            + f"<p>{SUMMARY}</p>" * 20 + "</article></main></body></html>")
    md = extract_job(page, "https://news.example/a", True)[0]
    assert "/share" not in md and "Ferry vote" in md
