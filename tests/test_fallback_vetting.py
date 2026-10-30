"""Firecrawl answers are vetted on the content the caller wants (not the raw format list), blank content never counts
as available, unproduced formats are reported, and Firecrawl's final URL gets the guard whatever key carries it."""
from fastweb_firecrawl.cache import available_formats, project

from .test_api import scrape


def _fc_doc(**data):
    return {"success": True, "data": {**data, "metadata": {"fastweb": {"engine": "firecrawl"}}}}


def test_links_only_fallback_with_blank_markdown_is_refused(ctx):
    """["links"] wants the default markdown: blank markdown plus unrequested markup is no answer (and is not stored
    under the shared cache key for later markdown callers)."""
    up, furl, aurl = ctx
    up.mode = "blank_md_html"
    r = scrape(aurl, furl + "/p/shell", formats=["links"])
    assert r.status_code == 422 and "no content" in r.json()["error"]


def test_html_request_is_served_from_firecrawl_markup(ctx):
    up, furl, aurl = ctx
    up.mode = "blank_md_html"
    r = scrape(aurl, furl + "/p/shell", formats=["html"])
    assert r.status_code == 200 and r.json()["data"]["html"] == "<p>only markup</p>"
    assert "markdown" not in r.json()["data"]


def test_blank_content_is_not_available_from_the_cache():
    doc = _fc_doc(markdown="", html="<p>x</p>")
    assert available_formats(doc) == {"html"}
    assert project(doc, ["markdown"]) is None
    assert project(doc, ["links"]) is None  # links means the default markdown: a MISS, not an empty HIT


def test_unproduced_formats_are_reported():
    doc = _fc_doc(markdown="# x")
    out = project(doc, ["markdown", "links", "screenshot"])
    assert out is not None
    assert out["data"]["metadata"]["fastweb"]["ignored_formats"] == ["links", "screenshot"]
    assert "ignored_formats" not in doc["data"]["metadata"]["fastweb"]  # the stored document is not changed
    assert "ignored_formats" not in project(doc, ["markdown"])["data"]["metadata"]["fastweb"]


def test_unproduced_formats_reported_over_http(ctx):
    _, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/article", formats=["markdown", "links", "screenshot"])
    data = r.json()["data"]
    assert r.status_code == 200 and data["metadata"]["fastweb"]["ignored_formats"] == ["screenshot"]
    assert isinstance(data["links"], list) and "html" not in data  # links come from the stored markup


def test_links_format_is_produced_from_markup():
    page = ("<html><head><base href='/docs/v2/'></head><body><a href='a.html'>a</a> <a href='#top'>top</a> "
            "<a href='https://other.example/x#frag'>x</a> <a href='mailto:me@example.com'>m</a> "
            "<a href='a.html'>again</a></body></html>")
    doc = _fc_doc(markdown="# x", html=page)
    doc["data"]["metadata"]["url"] = "https://site.example/start/page.html"
    out = project(doc, ["markdown", "links"])
    assert out is not None and "ignored_formats" not in out["data"]["metadata"]["fastweb"]
    assert out["data"]["links"] == ["https://site.example/docs/v2/a.html",
                                    "https://site.example/docs/v2/", "https://other.example/x"]


def test_private_source_url_is_refused_when_url_is_missing(ctx):
    up, furl, aurl = ctx
    up.mode = "private_source"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 422 and "private" in r.json()["error"] and "secret" not in r.text


def test_any_guard_refusal_of_the_final_url_rejects_the_answer(ctx):
    up, furl, aurl = ctx
    up.mode = "bad_scheme_final"
    r = scrape(aurl, furl + "/p/shell")
    assert r.status_code == 422 and "refused URL" in r.json()["error"]


def test_firecrawl_internal_metadata_is_dropped(ctx):
    _, furl, aurl = ctx
    r = scrape(aurl, furl + "/p/shell")
    meta = r.json()["data"]["metadata"]
    assert r.status_code == 200 and meta["fastweb"]["engine"] == "firecrawl" and meta["title"] == "Rendered"
    assert not {"scrapeId", "creditsUsed", "proxyUsed", "concurrencyLimited", "someFutureRunKey"} & meta.keys()
    assert meta["og:title"] == "Rendered OG"  # page <meta> tags are page metadata


def test_firecrawl_links_and_markdown_follow_fastweb_rules(ctx):
    _, furl, aurl = ctx
    data = scrape(aurl, furl + "/p/shell", formats=["markdown", "links"]).json()["data"]
    assert data["links"] == ["https://site.example/a"]  # http(s) only, like fastweb's own links
    assert "javascript:" not in data["markdown"] and "[a script](#)" in data["markdown"]


def test_firecrawl_metadata_url_values_hold_safe_schemes():
    from fastweb_firecrawl.upstream import page_metadata
    meta = {"ogUrl": "javascript:alert(1)", "ogImage": " JaVaScRiPt:x", "og:image": "data:image/svg+xml,x",
            "favicon": "https://x.example/f.ico", "twitter:image": "/rel.png", "title": "javascript: the good parts",
            "url": "https://x.example/"}
    assert page_metadata(meta) == {"favicon": "https://x.example/f.ico", "twitter:image": "/rel.png",
                                   "title": "javascript: the good parts", "url": "https://x.example/"}
