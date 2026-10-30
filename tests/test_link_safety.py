"""No unsafe URL scheme (javascript:, vbscript:, data:) survives as a link target in any markdown fastweb returns:
every markdown link form, every scheme obfuscation a browser or CommonMark renderer undoes, and every output path
(HTML extraction main-content and whole-page, wall pages, big-lane text, text documents, the Firecrawl fallback's
markdown). Fixtures are synthetic."""
import html as htmllib
import re

import pytest

from fastweb_firecrawl.extract import big_text_job, extract_job
from fastweb_firecrawl.links import absolutize_links, is_unsafe, neutralise_html
from fastweb_firecrawl.pipeline import document
from fastweb_firecrawl.upstream import clean_content

PAGE = "https://docs.example/a/b/c.html"
WORDS = ("The harbour council voted on the ferry terminal plan after a long debate about funding, tides and the "
         "timetable for construction work along the eastern wharf. ")

# Scheme spellings that all mean javascript: (or another unsafe scheme) to a browser or a markdown renderer.
SCHEMES = {
    "plain": "javascript:alert(1)",
    "mixed case": "JaVaScRiPt:alert(1)",
    "leading space": " javascript:alert(1)",
    "leading tab+newline": "\t\njavascript:alert(1)",
    "inner tab": "java\tscript:alert(1)",
    "inner newline": "java\nscript:alert(1)",
    "colon entity": "javascript&#58;alert(1)",
    "named colon entity": "javascript&colon;alert(1)",
    "hex letter entity": "&#x6A;avascript:alert(1)",
    "padded decimal entity": "&#0000106;avascript:alert(1)",
    "tab entity": "java&#x09;script:alert(1)",
    "control prefix": "\x01javascript:alert(1)",
    "nested parens": "javascript:alert((1))",
    "vbscript": "vbscript:msgbox(1)",
    "data html": "data:text/html;base64,PHNjcmlwdD4=",
    "data image": "DATA:image/svg+xml,x",
}
# Markdown link forms that a CommonMark renderer turns into a link; {t} is the target.
MD_FORMS = {
    "inline": "see [click]({t}) now",
    "image": "![pic]({t})",
    "inline leading spaces": "[click](   {t})",
    "inline line ending": "[click](\n  {t})",
    "inline title": '[click]({t} "title")',
    "angle": "[click](<{t}>)",
    "reference": "[click][r]\n\n[r]: {t}\n",
    "reference indented": "[click][r]\n\n   [r]:\n  {t}\n",
    "reference angle": "[click][r]\n\n[r]: <{t}>\n",
    "reference in blockquote": "> [r]: {t}\n\n[click][r]\n",
    "reference in list item": "- [r]: {t}\n\n[click][r]\n",
    "reference in nested containers": "1. > * [r]: {t}\n\n[click][r]\n",
    "autolink": "<{t}>",
    "raw html": '<a href="{t}">click</a>',
    "raw html single quotes": "<a href='{t}'>click</a>",
    "raw img src": '<img src="{t}">',
    "raw html slash before attribute": '<a/href="{t}">click</a>',
    "raw html backtick quotes": "<a href=`{t}`>click</a>",
}
MD_ONLY = {"escaped colon": "javascript\\:alert(1)"}  # backslash escapes only exist in markdown


def norm(md: str) -> str:
    """How a renderer reads the text: entities and backslash escapes decoded, whitespace/controls gone."""
    s = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", htmllib.unescape(md))
    return "".join(c for c in s if c > " ").lower()


def assert_safe(md: str):
    flat = norm(md)
    for bad in ("javascript:", "vbscript:", "data:"):
        assert bad not in flat, (bad, md)


def md_cases():
    for form, tpl in MD_FORMS.items():
        for name, scheme in {**SCHEMES, **MD_ONLY}.items():
            # Not a link at all in markdown, so nothing to neutralise (the HTML-level pass handles these spellings
            # in page markup, test_html_level_unsafe_attributes): whitespace inside a destination ends it; an
            # autolink takes no whitespace, controls or entities in its scheme; raw HTML has no backslash escapes.
            inner_ws = any(c in scheme.lstrip(" \t\n") for c in " \t\n")
            raw = form.startswith("raw")
            if (inner_ws and not raw) or (name in MD_ONLY and raw) or (
                    form == "autolink" and (scheme[0] <= " " or "&" in scheme or name in MD_ONLY)) or (
                    "\n" in scheme and form in ("angle", "reference angle", "inline line ending",
                                                 "reference indented")):
                continue
            yield pytest.param(tpl.format(t=scheme), id=f"{form}-{name}")


@pytest.mark.parametrize("md", list(md_cases()))
@pytest.mark.parametrize("base", [PAGE, ""], ids=["base", "nobase"])
def test_markdown_pass_neutralises_every_form(md, base):
    assert_safe(absolutize_links(md, base))


def path_cases():
    """The other output paths end in the same absolutize_links call: one case per link form and one per scheme
    spelling is enough to show each path runs it."""
    seen_forms, seen_schemes = set(), set()
    for case in md_cases():
        form, name = case.id.split("-", 1)
        if form not in seen_forms or name not in seen_schemes:
            seen_forms.add(form)
            seen_schemes.add(name)
            yield case


@pytest.mark.parametrize("md", list(path_cases()))
def test_firecrawl_fallback_markdown_is_neutralised(md):
    data = {"markdown": "# Rendered\n\n" + md}
    clean_content(data, ["markdown"])
    assert_safe(data["markdown"])


@pytest.mark.parametrize("md", list(path_cases()))
def test_every_served_markdown_is_neutralised(md):
    # document() builds every fastweb answer (text documents, PDFs, big-lane pages, cache hits)
    assert_safe(document(["markdown"], md, "", {}, {}, 500_000)["data"]["markdown"])


def test_neutralised_targets_leave_no_debris():
    assert absolutize_links("[x](javascript:alert((1)))", "") == "[x](#)"
    assert absolutize_links("[x](<javascript:a b>)", "") == "[x](#)"
    assert absolutize_links("[x](  javascript:a) y", PAGE) == "[x](#) y"
    assert absolutize_links("[x](< javascript:x>)", PAGE) == "[x](#)"  # not https://docs.example/a/b/< javascript..


def test_safe_targets_are_kept_or_resolved():
    md = ('[a](https://x.example/y) [m](mailto:me@x.example) [t](tel:+100) [p](Python_(lang)) [r](d.html "t") '
          "[s](  /f) [b](<rel path>) <https://x.example/auto> [c][r]\n\n[r]: ../e\n")
    out = absolutize_links(md, PAGE)
    for want in ("(https://x.example/y)", "(mailto:me@x.example)", "(tel:+100)", "(https://docs.example/a/b/Python_(lang))",
                 '(https://docs.example/a/b/d.html "t")', "(  https://docs.example/f)",
                 "(<https://docs.example/a/b/rel path>)", "<https://x.example/auto>", "[r]: https://docs.example/a/e"):
        assert want in out, want
    assert absolutize_links(md, "") == md


@pytest.mark.parametrize("scheme", list(SCHEMES.values()), ids=list(SCHEMES))
def test_html_level_unsafe_attributes(scheme):
    from selectolax.lexbor import LexborHTMLParser
    attr = htmllib.escape(scheme, quote=True).replace("&amp;", "&")  # keep the entity spellings as entities
    tree = LexborHTMLParser(f'<p><a href="{attr}">x</a><img src="{attr}"><form action="{attr}"></form></p>')
    assert neutralise_html(tree)
    assert tree.css_first("a").attributes["href"] == "#"
    assert "src" not in tree.css_first("img").attributes and "action" not in tree.css_first("form").attributes


def test_html_level_trims_href_whitespace():
    from selectolax.lexbor import LexborHTMLParser
    tree = LexborHTMLParser('<a href="  d.html\n">x</a><a href="https://x.example/">y</a>')
    assert neutralise_html(tree)
    assert [a.attributes["href"] for a in tree.css("a")] == ["d.html", "https://x.example/"]


def article(href: str) -> str:
    attr = htmllib.escape(href, quote=True).replace("&amp;", "&")
    paras = "".join(f"<p>{WORDS * 2}{'See <a href=' + chr(34) + attr + chr(34) + '>dee</a>.' if i == 3 else ''}</p>"
                    for i in range(12))
    return f"<html><head><title>Ferry</title></head><body><main><article><h1>Ferry</h1>{paras}</article></main></body></html>"


@pytest.mark.parametrize("scheme", list(SCHEMES.values()), ids=list(SCHEMES))
@pytest.mark.parametrize("main_only", [True, False], ids=["trafilatura", "whole-page"])
def test_extracted_markdown_is_safe(scheme, main_only):
    md = extract_job(article(scheme), PAGE, main_only)[0]
    assert "dee" in md
    assert_safe(md)


def test_leading_space_relative_href_resolves():
    md = extract_job(article(" d.html"), PAGE, True)[0]
    assert "[dee](https://docs.example/a/b/d.html)" in md


@pytest.mark.parametrize("scheme", list(SCHEMES.values()), ids=list(SCHEMES))
def test_wall_page_markdown_is_safe(scheme):
    text_form = scheme if not any(c in htmllib.unescape(scheme).strip() for c in "\t\n") else "javascript:x"
    attr = htmllib.escape(scheme, quote=True).replace("&amp;", "&")
    wall = (f"<html><head><title>Just a moment...</title></head><body><p>Checking your browser. "
            f"<a href=\"{attr}\">continue</a> [go]({htmllib.escape(text_form)})</p></body></html>")
    assert_safe(extract_job(wall, PAGE, True)[0])


def test_page_text_written_as_markdown_is_safe():
    # visible page text that is itself markdown link syntax becomes a link once served as markdown
    text = "".join(f"<p>{WORDS} [x]({s})</p>" for s in SCHEMES.values()
                   if not any(c in htmllib.unescape(s).strip() for c in "\t\n"))  # inner whitespace: no link
    page = f"<html><head><title>T</title></head><body>{text * 3}</body></html>"
    for md in (extract_job(page, PAGE, True)[0], document(["markdown"], big_text_job(page, PAGE)[0], "", {}, {},
                                                          500_000)["data"]["markdown"]):
        assert_safe(md)


def test_is_unsafe():
    assert not is_unsafe("https://x.example/") and not is_unsafe("/rel:ative") and not is_unsafe("a/b:c")
    assert not is_unsafe("d.html") and not is_unsafe("#frag") and not is_unsafe("MAILTO:a@b")
    assert is_unsafe("ftp://x.example/") and is_unsafe("file:///etc/passwd")
