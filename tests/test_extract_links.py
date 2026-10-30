"""Relative links in extracted markdown resolve against the page URL (or its <base href>) on every extraction path,
and table-layout listings keep their links. Fixtures are synthetic."""
from fastweb_firecrawl.extract import absolutize_links, extract_job, is_token_soup

PAGE = "https://docs.example/a/b/c.html"
WORDS = ("The harbour council voted on the ferry terminal plan after a long debate about funding, tides and the "
         "timetable for construction work along the eastern wharf. ")
LINKS = "See <a href='d.html'>dee</a>, <a href='#x'>ex</a>, <a href='../e'>ee</a> and <a href='/f'>ef</a>. "


def article(head=""):
    paras = "".join(f"<p>{WORDS * 2}{LINKS if i == 3 else ''}</p>" for i in range(12))
    return (f"<html><head><title>Ferry</title>{head}</head><body><main><article><h1>Ferry</h1>{paras}"
            "</article></main></body></html>")


def test_main_content_links_resolve_against_the_page():
    md = extract_job(article(), PAGE, True)[0]
    for target in ("https://docs.example/a/b/d.html", "https://docs.example/a/b/c.html#x",
                   "https://docs.example/a/e", "https://docs.example/f"):
        assert f"]({target})" in md, target


def test_base_href_is_respected():
    md = extract_job(article("<base href='https://cdn.example/root/'>"), PAGE, True)[0]
    assert "](https://cdn.example/root/d.html)" in md and "](https://cdn.example/f)" in md


def test_whole_page_links_resolve():
    hub = ("<html><head><title>Hub</title></head><body><h1>Hub</h1><ul>"
           + "".join(f"<li><a href='sec/{i}'>Section page number {i}</a></li>" for i in range(60))
           + "</ul></body></html>")
    md = extract_job(hub, PAGE, False)[0]
    assert "](https://docs.example/a/b/sec/7)" in md and "](sec/7)" not in md


def test_absolute_and_odd_targets_are_left_alone():
    md = "[a](https://x.example/y) [m](mailto:me@x.example) ![i](img/p.png \"t\") [b](<rel path>)"
    out = absolutize_links(md, PAGE)
    assert "(https://x.example/y)" in out and "(mailto:me@x.example)" in out
    assert "(https://docs.example/a/b/img/p.png \"t\")" in out
    assert absolutize_links(md, "") == md


def hn_page(rows=30):
    items = "".join(
        f'<tr class="athing"><td class="title"><span class="rank">{i}.</span></td><td><a href="vote?id={i}">'
        f'</a></td><td class="title"><span class="titleline"><a href="https://site{i}.example/post">Story title '
        f'number {i}</a> (<a href="from?site=site{i}.example">site{i}.example</a>)</span></td></tr>'
        f'<tr><td colspan="2"></td><td class="subtext">{100 + i} points by <a href="user?id=u{i}">u{i}</a> '
        f'<a href="item?id={i}">{i} hours ago</a> | <a href="hide?id={i}">hide</a> | '
        f'<a href="item?id={i}">{i * 3}&nbsp;comments</a></td></tr>'
        for i in range(1, rows + 1))
    return ('<html><head><title>Listing News</title></head><body><center><table><tr><td><table><tr><td>'
            '<a href="/"><b>Listing News</b></a> <a href="newest">new</a> | <a href="front">past</a></td></tr>'
            f'</table></td></tr><tr><td><table>{items}</table></td></tr></table></center></body></html>')


def test_table_layout_listing_keeps_links_and_rows():
    md = extract_job(hn_page(), "https://news.example/", True)[0]
    assert "[Story title number 7](https://site7.example/post)" in md
    assert "[21\xa0comments](https://news.example/item?id=7)" in md
    line = next(ln for ln in md.split("\n") if "Story title number 7" in ln)
    assert line.startswith("7.") and "site7.example" in line  # one row per line, not one cell per line
    assert "|  |" not in md  # layout tables are not rendered as markdown tables


def test_token_soup_needs_many_short_link_free_lines():
    assert is_token_soup("\n".join(["1.", "(", "x.example", ")", "101 points"] * 10))
    assert not is_token_soup("\n".join(["1.", "[t](u)"] * 30))
    assert not is_token_soup("\n".join([WORDS] * 50))
    assert not is_token_soup("a\nb\nc")


def test_whole_page_autolink_form_resolves():
    # href == text: markdownify's autolink form (<ask>) would keep the relative target
    hub = ("<html><head><title>Hub</title></head><body><p><a href='ask'>ask</a> | <a href='show'>show</a></p><ul>"
           + "".join(f"<li><a href='sec/{i}'>Section page number {i}</a></li>" for i in range(60))
           + "</ul></body></html>")
    md = extract_job(hub, PAGE, False)[0]
    assert "[ask](https://docs.example/a/b/ask)" in md and "<ask>" not in md and "<show>" not in md


def test_script_and_data_targets_are_neutralised():
    md = ("[x](javascript:alert(1)) [y](JaVaScRiPt:void(0)) ![d](data:image/png;base64,AA) [v](vbscript:x) "
          "[w](https://w.example/Python_(lang)) [m](mailto:me@x.example) [t](tel:+100)")
    for base in (PAGE, ""):
        out = absolutize_links(md, base)
        assert "[x](#)" in out and "[y](#)" in out and "![d](#)" in out and "[v](#)" in out
        assert "](https://w.example/Python_(lang))" in out and "(mailto:me@x.example)" in out and "(tel:+100)" in out
        assert "javascript" not in out.lower() and "data:" not in out


def test_extracted_markdown_has_no_script_links():
    page = article().replace("<a href='d.html'>dee</a>", "<a href='javascript:steal()'>dee</a>")
    for main_only in (True, False):  # trafilatura's and markdownify's paths
        md = extract_job(page, PAGE, main_only)[0]
        assert "javascript:" not in md and "dee" in md


def test_inline_sup_and_font_tags_are_unwrapped():
    from fastweb_firecrawl.extract import strip_inline_tags
    md = ('Born 1956<sup>[\\[1\\]](https://w.example/#c1)</sup>, H<sub>2</sub>O.\n\n```\n$ <font color="#4E9A06">run'
          '</font> dev\n<sup>kept in code</sup>\n```\n')
    out = strip_inline_tags(md)
    assert "Born 1956[\\[1\\]](https://w.example/#c1), H2O." in out
    assert "$ run dev" in out and "<sup>kept in code</sup>" in out
