"""A good 200 body whose lightened markup is over extract_max_html gets lexbor-only text of the whole page
(output capped at 500k chars) instead of being handed to Firecrawl."""
from .conftest import ARTICLE, EXTRA
from .test_api import scrape
from .test_fetch_ladder import CONSENT_WALL, make

ROWS = "<tr><td>cell</td><td>value</td></tr>"
MB = 1024 * 1024


def page(size):
    head = ("<html><head><title>Big List</title></head><body><h1>Big List</h1><article>" + ARTICLE
            + "<p>Harbour council phrase marker.</p></article><table>")
    tail = "</table></body></html>"
    return (head + ROWS * ((size - len(head) - len(tail)) // len(ROWS)) + tail).encode()


def run(ctx, name, size, cap):
    up, furl, _ = ctx
    EXTRA[name] = (page(size), "text/html", 200)
    (srv, aurl), _ = make(furl, max_bytes=6 * MB, extract_max_html=cap, total_timeout=10.0)
    try:
        r = scrape(aurl, furl + f"/p/{name}")
        return r.status_code, r.json()["data"], up
    finally:
        srv.should_exit = True


def test_just_over_cap_extracts_prefix(ctx):
    code, d, up = run(ctx, "over", 3 * MB + 30_000, 3 * MB)
    fw = d["metadata"]["fastweb"]
    assert code == 200 and fw["engine"] == "plain" and fw["text_only_large"] is True
    assert "Harbour council phrase marker" in d["markdown"]
    assert not up.scrapes


def test_at_body_cap_extracts_prefix(ctx):
    code, d, up = run(ctx, "atcap", 6 * MB - 100, 3 * MB)
    assert code == 200 and d["metadata"]["fastweb"]["text_only_large"] is True
    assert "Harbour council phrase marker" in d["markdown"] and not up.scrapes


def test_big_text_job_keeps_heading_and_drops_nav():
    from fastweb_firecrawl.extract import big_text_job
    md, title, _, n = big_text_job("<html><head><title>T</title></head><body><nav>Menu Home</nav><h1>Big List</h1>"
                                   "<p>Harbour vote</p><script>x</script></body></html>", "https://x.example/")
    assert md.startswith("# Big List") and "Harbour vote" in md and "Menu" not in md and title == "T"


def test_big_consent_wall_still_thin(ctx):
    """Honesty rules apply to the big path too: a wall padded past the cap is not a cheap-tier answer."""
    up, furl, _ = ctx
    pad = "<div hidden>" + "<i></i>" * (3 * MB // 7 + 10) + "</div>"
    EXTRA["bigwall"] = (CONSENT_WALL.replace("</body>", pad + "</body>").encode(), "text/html", 200)
    (srv, aurl), _ = make(furl, max_bytes=6 * MB, extract_max_html=3 * MB, total_timeout=10.0)
    try:
        scrape(aurl, furl + "/p/bigwall")
        assert up.scrapes  # handed on, not served as content
    finally:
        srv.should_exit = True
