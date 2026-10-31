"""Link-target safety for markdown output: unsafe URL schemes (javascript:, data:, vbscript:, ...) never reach a
client as a link, in the page HTML before conversion or in the markdown after it.

Two layers:

* ``neutralise_html`` works on the parsed page before trafilatura/markdownify run: every ``href``/``src``-style
  attribute is read the way a browser reads it (the parser has already decoded entities; leading/trailing C0
  controls and spaces are stripped and tabs/newlines removed, WHATWG URL parsing) and an unsafe one is replaced.
* ``absolutize_links`` is a markdown pass over every link form a CommonMark renderer turns into a link: inline
  ``[t](dest)`` / ``![t](dest)`` (angle-bracket or balanced-paren destination, optional leading whitespace and one
  line ending), reference definitions ``[label]: dest``, autolinks ``<scheme:...>`` and raw HTML attributes
  (``<a href=...>``). Entities and backslash escapes are decoded before the scheme test. It runs on every markdown
  fastweb returns, including wall pages and the Firecrawl fallback's markdown.

Every scan here is linear in the input: regexes have disjoint alternatives and each inline destination is consumed
once (tests/test_extract_redos.py times adversarial inputs).
"""
import html as htmllib
import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
STRIP_CHARS = "".join(map(chr, range(0x21)))  # C0 controls and space: stripped at both ends of a URL (WHATWG)
TAB_NL = str.maketrans("", "", "\t\n\r")      # removed anywhere in a URL (WHATWG)
MD_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")  # CommonMark backslash escapes (ASCII punctuation)
URL_ATTRS = ("href", "src", "action", "formaction", "xlink:href", "poster", "background", "cite", "data")


def url_scheme(raw: str, markdown: bool = False) -> str:
    """The scheme a browser would see for this attribute value or markdown destination ('' when relative)."""
    s = raw
    if markdown:
        s = MD_ESCAPE.sub(r"\1", s)
    s = htmllib.unescape(s).translate(TAB_NL).strip(STRIP_CHARS)
    m = SCHEME.match(s)
    return m.group()[:-1].lower() if m else ""


def is_unsafe(raw: str, markdown: bool = False) -> bool:
    scheme = url_scheme(raw, markdown)
    return bool(scheme) and scheme not in SAFE_SCHEMES


def neutralise_html(tree: LexborHTMLParser) -> bool:
    """In place: unsafe URL attributes -> "#" (href) or removed (others); hrefs with surrounding whitespace are
    trimmed (browsers ignore it; markdown would not). True when anything changed."""
    changed = False
    sel = ", ".join(f"[{a.replace(':', chr(92) + ':')}]" for a in URL_ATTRS)
    for node in tree.css(sel):
        attrs = node.attrs
        for name in URL_ATTRS:
            value = node.attributes.get(name)
            if value is None:
                continue
            if is_unsafe(value):
                if name == "href":
                    attrs[name] = "#"
                else:
                    del attrs[name]
                changed = True
            elif name == "href" and value != value.strip(STRIP_CHARS):
                attrs[name] = value.strip(STRIP_CHARS)
                changed = True
    return changed


INLINE_OPEN = re.compile(r"\]\(")
# Whitespace (and, defensively, other C0 controls) before an inline destination: CommonMark allows spaces and one
# line ending; browsers strip controls from an href.
LEAD_SPACE = re.compile(r"[\x00-\x09\x0b-\x1f ]*(?:\n)?[\x00-\x09\x0b-\x1f ]*")
ANGLE_DEST = re.compile(r"<([^<>\n]*)>")
# A bare destination: no whitespace or controls, parentheses balanced (up to three levels deep). The alternatives
# are disjoint, so a failed nested group backtracks only within itself.
_P = r"[^()\x00-\x20]"
BARE_DEST = re.compile(rf"(?:{_P}|\((?:{_P}|\((?:{_P}|\({_P}*\))*\))*\))+")
# A reference definition, also inside block quotes and list items (CommonMark makes those document-global too).
# atomic: the spaces around markers could otherwise be split many ways and a failed match backtracks for seconds
_CONTAINER = r"(?>(?:[ ]{0,3}(?:>|[-+*]|\d{1,9}[.)])[ \t]?){0,16})"
REF_DEF = re.compile(r"^(" + _CONTAINER + r"[ ]{0,3}\[(?:[^\[\]\\\n]|\\.){1,999}\]:[ \t]*(?:\r?\n)?[ \t]*)"
                     r"(<[^<>\n]*>|\S+)", re.M)
AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20]*)>")
# A URL attribute inside raw HTML that a renderer may pass through: name=value with any quoting.
_ATTR_NAMES = "|".join(re.escape(a) for a in URL_ATTRS)
# HTML tokenisers also split attributes on "/" (<a/href=...>); lenient renderers accept backtick quoting.
HTML_ATTR = re.compile(rf"""(?i)([\s/](?:{_ATTR_NAMES})\s*=\s*)("[^"]*"|'[^']*'|`[^`]*`|[^\s"'=<>`]+)""")


def _resolve(target: str, base: str) -> str | None:
    """New target for a markdown destination: "#" when unsafe, absolute when base is set, None to keep it."""
    if is_unsafe(target, markdown=True):
        return "#"
    if not base or not target:
        return None
    try:
        return urljoin(base, target)
    except ValueError:  # malformed target (bad IPv6 literal): leave as is
        return None


def _inline(md: str, base: str) -> str:
    out, pos, n = [], 0, len(md)
    i = 0
    while True:
        m = INLINE_OPEN.search(md, i)
        if not m:
            break
        lead = LEAD_SPACE.match(md, m.end())
        d = lead.end() if lead else m.end()
        if d < n and md[d] == "<":
            a = ANGLE_DEST.match(md, d)
            if not a:
                i = m.end()
                continue
            start, end, target, full_end = d + 1, a.end() - 1, a.group(1), a.end()
        else:
            b = BARE_DEST.match(md, d)
            if not b:
                i = m.end()
                continue
            start, end, target = d, b.end(), b.group()
            full_end = end
        new = _resolve(target, base)
        if new == "#":  # the whole destination (whitespace, angle brackets, nested parens) becomes "#"
            out += [md[pos:m.end()], "#"]
            pos = full_end
        elif new is not None:
            out += [md[pos:start], new]
            pos = end
        i = full_end  # each destination is consumed once: linear
    out.append(md[pos:])
    return "".join(out)


def _ref_def(md: str, base: str) -> str:
    def fix(m: re.Match[str]) -> str:
        dest = m.group(2)
        angle = dest.startswith("<")
        new = _resolve(dest[1:-1] if angle else dest, base)
        if new is None:
            return m.group()
        return m.group(1) + (f"<{new}>" if angle and new != "#" else new)
    return REF_DEF.sub(fix, md)


def _autolink(m: re.Match[str]) -> str:
    return "#" if is_unsafe(m.group(1)) else m.group()


def _html_attr(m: re.Match[str]) -> str:
    value = m.group(2)
    if value[:1] in "\"'`":
        value = value[1:-1]
    return f'{m.group(1)}"#"' if is_unsafe(value) else m.group()


def absolutize_links(md: str, base: str) -> str:
    """Resolve relative markdown link and image targets against base (trafilatura is called without url=: given
    one, it resolves every relative href against the site root instead of the page), and replace every link
    target whose scheme is not in SAFE_SCHEMES with "#" (all link forms, see the module docstring). With no base,
    only the neutralising is done."""
    if "](" in md:
        md = _inline(md, base)
    if "]:" in md:
        md = _ref_def(md, base)
    if "<" in md:
        md = AUTOLINK.sub(_autolink, md)
        md = HTML_ATTR.sub(_html_attr, md)
    return md
