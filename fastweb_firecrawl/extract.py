"""HTML decoding and extraction: trafilatura main content, or whole-page markdown for hubs/homepages."""
import codecs
import re
from html import escape
from typing import Any
from urllib.parse import urljoin

import trafilatura
from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser, LexborNode

from .links import absolutize_links, neutralise_html

__all__ = ["absolutize_links"]  # re-exported: upstream and the tests import it from here

META_CHARSET = re.compile(rb"""<meta[^<>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-:.]+)", re.I)
BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))
# Removed before whole-page conversion. Header/footer stay: site names and section labels live there.
STRIP = ("script", "style", "noscript", "template", "svg", "canvas", "iframe", "nav", "aside", "button", "dialog",
         "select", "[hidden]", "[aria-hidden=true]", '[style*="display:none" i]', '[style*="display: none" i]',
         '[style*="visibility:hidden" i]', '[style*="visibility: hidden" i]')
# (inline-hidden padding only; class/other CSS hiding is not chased)
WHOLE_PAGE_HOSTS = ("github.com",)  # repo pages: file list + README sit outside trafilatura's main block
MIN_BODY = 3000           # visible text below this: main content is trusted
MAIN_RATIO = 0.15         # main content smaller than this share of the body text -> whole page
HUB_LINK_FRAC = 0.5       # share of body text inside <a> that marks a link hub
HUB_MAIN_RATIO = 0.3
# Card-style hubs (bbc.com/news, nytimes.com): every story is a block-level <a> card that trafilatura drops, so its
# markdown keeps the summaries but no links and the ratio tests above never fire. Signal: main kept almost no links
# (<= CARD_MAX_MAIN_LINKS) while the page is mostly link text and carries CARD_MIN_HEADLINES distinct headline-like
# links. Articles keep their inline links, or are mostly non-link text, so they never qualify.
CARD_LINK_FRAC = 0.45
CARD_MAX_MAIN_LINKS = 2
CARD_MIN_HEADLINES = 8
CARD_HEADLINE_CHARS = 25  # and at least CARD_HEADLINE_WORDS words: "World", "Sign in", "More" are not headlines
CARD_HEADLINE_WORDS = 4
# Listing/index pages: trafilatura keeps item blurbs but drops the linked titles (aljazeera.com/where/iran,
# abc.net.au/news/justin, rba.gov.au decision list). Signal: >= LIST_MIN_ITEMS sibling items of one list
# (same-tag children of one parent, each with a link) whose link texts main lacks, and the list sits next to kept
# text (it or its parent holds a main paragraph). Header/footer menus and rails of other stories ("Most popular",
# "More in") sit away from the article text, so they never anchor; main at LIST_MAX_MAIN or more is an article.
LIST_RATIO = 0.4
LIST_MAX_MAIN = 8000
LIST_MIN_ITEMS = 4
LIST_LINK_CHARS = 4       # shorter link texts ("›", "Go") are not titles; dates ("5 May 2026") are
LIST_ITEM_UP = 6          # levels from a link up to its list item
LIST_ANCHOR_UP = 1        # levels from the list up to text main kept
LIST_PARA = 40            # main paragraphs this long anchor a list; main with none falls back to LIST_SHORT_PARA
LIST_SHORT_PARA = 15
WHOLE_PAGE_MAX_HTML = 1_500_000  # slimmed <body> markup; markdownify on more costs hundreds of MiB
MAX_MD_CHARS = 500_000
LARGE_HTML = 500_000      # bytes; bigger pages are lightened before parsing and use the big lane


class TooLarge(Exception):
    """The page is too large for this extraction path."""
# non-link letters/digits (about 1 000 chars) below this + a wall marker -> consent/challenge page, never whole page
WALL_TEXT = 850
SHELL_MARKERS = re.compile(r"enable javascript|javascript is (disabled|required)|you need to enable javascript|"
                           r"accept all cookies|we value your privacy|consent|"
                           r"just a moment|checking your browser|cf-chl|captcha|are you a robot|access denied", re.I)
# Markers only a wall carries (not "consent"/"captcha"/"access denied", which real pages mention): a page whose own
# non-link text is under WALL_LONG and carries one is a wall even with 1-2 K chars of TCF/challenge text.
HARD_MARKERS = re.compile(r"just a moment|checking your browser|cf-chl|are you a robot|enable javascript|"
                          r"javascript is (disabled|required)|we value your privacy|partners store and/or access|"
                          r"consent to our and our", re.I)
WALL_LONG = 2200          # letters/digits (about 2 650 chars): tcf(8) is 1 947, forum.proxmox.com 2 518
NOSCRIPT_OWN = 250        # a marker only in <noscript> counts when own text is under this: real retail pages carry
                          # "Javascript is required" there with 1-2.5 K of text (Bunnings). Trade-off:
                          # a <noscript>-only JS shell with 250+ letters of menu text passes as cheap; real shells
                          # measured (BOM) have about 200-280 chars and is_thin()'s JS rule sends them on.
NONWORD = re.compile(r"\W+")  # own text in letters/digits: link separators (" · ", " | ") are not text
# C0 (not tab/LF/FF/CR/ESC), PUA, and U+FFFD next to another non-ASCII char (UTF-16 soup: trafilatura drops PUA).
# A lone U+FFFD between ASCII letters is a mislabelled cp1252/latin-2 accent, left to is_binary.
JUNK = re.compile("[\x00-\x08\x0b\x0e-\x1a\x1c-\x1f\ue000-\uf8ff]|\ufffd(?=[^\x00-\x7f\ufffd])|"
                  "(?<=[^\x00-\x7f\ufffd])\ufffd")


# Openers of blocks lighten() drops; each block ends at its own closer, found with a forward search (linear: a
# lazy .*? over the page was quadratic on unclosed tags and froze the event loop).
HEAVY_OPEN = re.compile(r"<(script|style|svg)\b|<!--", re.I)
HEAVY_CLOSE = {"script": re.compile(r"</script\s*>", re.I), "style": re.compile(r"</style\s*>", re.I),
               "svg": re.compile(r"</svg\s*>", re.I)}


LONG_SPACE = re.compile(r"[ \t\r\n]{65,}")  # linear; markdownify's own whitespace regexes are quadratic on long runs


def squeeze_space(html: str) -> str:
    """Long whitespace runs (a 50k-space <pre>) -> one newline or space: keeps markdownify linear."""
    return LONG_SPACE.sub(lambda m: "\n" if "\n" in m.group() else " ", html)


def lighten(html: str) -> str:
    """Drop script/style/svg/comments (most of a big homepage's bytes) before parsing; extraction ignores them.
    Linear: stops at the first block without a closer and keeps the rest as is."""
    out, pos = [], 0
    while True:
        m = HEAVY_OPEN.search(html, pos)
        if not m:
            break
        if m.group(1):
            c = HEAVY_CLOSE[m.group(1).lower()].search(html, m.end())
            end = c.end() if c else -1
        else:
            end = html.find("-->", m.end())
            end = end + 3 if end >= 0 else -1
        if end < 0:
            break
        out.append(html[pos:m.start()])
        out.append(" ")
        pos = end
    out.append(html[pos:])
    return "".join(out)


def _codec(name: str | bytes) -> str | None:
    try:
        return codecs.lookup(name.decode("ascii", "ignore") if isinstance(name, bytes) else name).name
    except LookupError:
        return None


HIGH = bytes(range(0x80, 0x100))
ASCII_LETTERS = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def is_sparse_cp1252(sample: bytes) -> bool:
    """WHATWG's windows-1252 default for undeclared Western pages: French, Portuguese or a pound sign
    were sniffed as cp1250/cp932 mojibake. Only when cp1252 decodes cleanly and non-ASCII bytes are sparse among
    Latin letters; Cyrillic, Greek or CJK legacy pages are dense and still go to charset_normalizer."""
    try:
        sample.decode("cp1252")
    except UnicodeDecodeError:
        return False
    high = len(sample) - len(sample.translate(None, HIGH))
    letters = len(sample) - len(sample.translate(None, ASCII_LETTERS))
    return high <= 0.25 * letters


def is_utf16le(sample: bytes) -> bool:
    """NUL-interleaved ASCII with no BOM or label: every odd byte NUL, even bytes mostly not."""
    odd, even = sample[1::2], sample[0::2]
    return len(odd) >= 64 and odd.count(0) > 0.95 * len(odd) and even.count(0) < 0.05 * len(even)


def decode_html(data: bytes, ctype: str) -> str:
    """Charset order: BOM -> HTTP header -> <meta> in the first 4 KB -> strict UTF-8 -> sparse cp1252 ->
    charset_normalizer."""
    for bom, bom_enc in BOMS:
        if data.startswith(bom):
            return data.decode(bom_enc, errors="replace")
    m = HEADER_CHARSET.search(ctype or "")
    enc: str | None = _codec(m.group(1)) if m else None
    if not enc:
        mb = META_CHARSET.search(data[:4096])
        enc = _codec(mb.group(1)) if mb else None
        if enc and enc.startswith("utf-16"):
            enc = "utf-8"  # WHATWG: a <meta> that could be read at all is ASCII-compatible
    if not enc and "html" in (ctype or "") and is_utf16le(data[:4096]):  # HTML only: UTF-16 XML stays 422
        enc = "utf-16-le"
    if enc:
        # Pages labelled latin-1/ascii are cp1252 in practice (WHATWG); cp1252 is a superset for printable text.
        if enc in ("latin-1", "iso8859-1", "ascii"):
            enc = "cp1252"
        return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if is_sparse_cp1252(data[:200_000]):
        return data.decode("cp1252", errors="replace")
    from charset_normalizer import from_bytes
    best = from_bytes(data[:200_000]).best()
    return data.decode(best.encoding if best else "utf-8", errors="replace")


def head_meta(tree: LexborHTMLParser) -> tuple[str, str | None]:
    """(title, description) from <head>; trafilatura.extract_metadata costs ~1 s on a 3.7 MB page."""
    def attr(sel: str) -> str | None:
        n = tree.css_first(sel)
        return ((n.attributes.get("content") or "").strip() or None) if n else None
    t = tree.css_first("title")
    title = attr('meta[property="og:title"]') or (t.text(strip=True) if t else "")
    return title, attr('meta[name="description"]') or attr('meta[property="og:description"]')


def junk_share(text: str) -> float:
    """Share of C0 controls, Private Use chars and soup U+FFFD: binary that survived extraction."""
    return len(JUNK.findall(text)) / len(text) if text else 0.0


def is_hard_wall(own_len: int, text: str, title: str, shell: str = "") -> bool:
    return own_len < WALL_LONG and bool(HARD_MARKERS.search(text) or HARD_MARKERS.search(title or "")
                                        or (own_len < NOSCRIPT_OWN and HARD_MARKERS.search(shell)))


def own_len(body: LexborNode) -> int:
    """Letters/digits of the visible text outside links."""
    return max(0, len(NONWORD.sub("", body.text(separator=" ", strip=True)))  # no per-letter list
               - sum(len(NONWORD.sub("", a.text(separator=" ", strip=True))) for a in body.css("a")))


BLOCKS = {"div", "section", "form", "dialog", "aside", "footer", "header", "main", "article", "body"}


def outside_marker_block(body: LexborNode, own: int) -> int:
    """Own letters outside the block that carries the visible hard marker (a cookie notice on a short forum
    thread is a minority, a wall's marker block is the whole page)."""
    best, seen = own, 0
    for node in body.traverse(include_text=True):
        if node.tag == "-text" and HARD_MARKERS.search(node.text_content or ""):
            blk = node.parent
            while blk is not None and blk.tag not in BLOCKS:
                blk = blk.parent
            if blk is None:
                return 0
            best = min(best, max(0, own - own_len(blk)))
            seen += 1
            if best < WALL_TEXT or seen >= 8:
                break
    return best


def is_hard_page(body: LexborNode, own: int, text: str, title: str, shell: str) -> bool:
    """is_hard_wall, but a visible marker in a minority block (XenForo's "We value your privacy" notice) is not."""
    return is_hard_wall(own, text, title, shell) and (bool(HARD_MARKERS.search(title or "")) or own < NOSCRIPT_OWN
                                                      or outside_marker_block(body, own) < WALL_TEXT)


STRIP_BASE = STRIP[:-4]
HIDDEN = ", ".join(STRIP[-4:])


def page_stats(tree: LexborHTMLParser, title: str = "") -> tuple[int, float, bool, bool]:
    """Strip boilerplate in place; return (visible body text length, share of it inside links, wall marker hit,
    hard wall). Non-link text is counted in letters/digits: separators between 18 K padded links are not text.
    <noscript> text counts for the markers only (a JS shell's "enable JavaScript" lives there)."""
    shell = " ".join(n.text(separator=" ", strip=True) for n in tree.css("noscript"))[:2000]
    for sel in STRIP:
        for n in tree.css(sel):
            n.decompose()
    body = tree.body or tree.root
    if body is None:
        return 0, 0.0, False, False
    text = body.text(separator=" ", strip=True)
    anchors = body.css("a")
    links = sum(len(a.text(separator=" ", strip=True)) for a in anchors)
    own = own_len(body)
    wall = own < WALL_TEXT and bool(SHELL_MARKERS.search(text) or (own < NOSCRIPT_OWN and SHELL_MARKERS.search(shell)))
    hard = is_hard_page(body, own, text, title, shell)
    return len(text), (links / len(text) if text else 0.0), wall or hard, hard


def slim_body(tree: LexborHTMLParser) -> str:
    """<body> markup with every attribute but href dropped (apnews.com: 1.1 MB -> 480 KB)."""
    body = tree.body or tree.root
    for n in body.traverse():
        if not n.is_element_node:  # comments have no attributes
            continue
        attrs = n.attrs
        for k in [k for k in n.attributes if k != "href"]:
            del attrs[k]
    return body.html or ""


def whole_page_markdown(body_html: str) -> str:
    # autolinks=False: <a href="ask">ask</a> becomes [ask](ask), which absolutize_links resolves; an autolink
    # (<ask>) would keep the relative target.
    md = markdownify(squeeze_space(body_html), heading_style="ATX", strip=["img"], bullets="-", autolinks=False)
    md = "\n".join(line.rstrip(" \t") for line in md.split("\n"))  # linear (a [ \t]+\n regex is quadratic)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


MD_LINK_URL = re.compile(r"\]\((?:[^()]|\([^()]*\))*\)")  # disjoint alternatives: linear on unclosed "]("
LAYOUT_TABLE = re.compile(r"<(/?)(?:table|tbody|thead|tfoot|center)>")
LAYOUT_ROW = re.compile(r"<(/?)tr>")


def base_url(tree: LexborHTMLParser, url: str) -> str:
    """The page URL, or its <base href> resolved against it (the base relative links resolve against)."""
    base = tree.css_first("base[href]")
    href = (base.attributes.get("href") or "").strip() if base is not None else ""
    return urljoin(url, href) if url and href else url


def page_links(html: str, url: str) -> list[str]:
    """Firecrawl's `links` format: every http(s) a[href] on the page, resolved against the page (or <base href>),
    fragments dropped, deduplicated in page order."""
    tree = LexborHTMLParser(html)
    base = base_url(tree, url)
    seen: dict[str, None] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        try:
            full = urljoin(base, href).split("#", 1)[0]
        except ValueError:
            continue
        if full.startswith(("http://", "https://")):
            seen.setdefault(full, None)
    return list(seen)


# Inline tags trafilatura leaves in its markdown: Wikipedia's citation <sup>, and the <font color> spans of
# terminal-output <pre> blocks (FastAPI docs). <sup>/<sub> are only unwrapped outside code fences (an HTML tutorial's
# code shows them on purpose); <font> is obsolete markup and unwrapped everywhere. <span> is left alone: real HTML
# examples in code blocks use it too often to tell apart.
SUP_SUB_TAG = re.compile(r"</?su[pb]>")
FONT_TAG = re.compile(r"<font\b[^<>]*>|</font>")
FENCE_LINE = re.compile(r"[ ]{0,3}(```|~~~)")


def fence_blocks(md: str) -> list[tuple[bool, str]]:
    """md split into (in_code_fence, text) runs by a linear line scan (a lazy ^```.*?^``` regex was quadratic on
    many unclosed fences). A fence closes on a line starting with the same marker; an unclosed one runs to the
    end, as in CommonMark."""
    runs: list[tuple[bool, list[str]]] = [(False, [])]
    marker = ""
    for line in md.split("\n"):
        m = FENCE_LINE.match(line)
        if not marker and m:
            marker = m.group(1)
            runs.append((True, [line]))
        elif marker:
            runs[-1][1].append(line)
            if m and m.group(1) == marker and not line.strip().strip(marker[0]):
                marker = ""
                runs.append((False, []))
        else:
            runs[-1][1].append(line)
    return [(code, "\n".join(lines)) for code, lines in runs if lines]


def strip_inline_tags(md: str) -> str:
    if "<" not in md:
        return md
    md = FONT_TAG.sub("", md)
    if not SUP_SUB_TAG.search(md):
        return md
    return "\n".join(text if code else SUP_SUB_TAG.sub("", text) for code, text in fence_blocks(md))


def tidy_blank_lines(md: str) -> str:
    """Outside code fences: whitespace-only lines become empty and runs of blank lines collapse to one
    (linkedin.com profiles carry dozens of whitespace-only lines)."""
    out = []
    for code, text in fence_blocks(md):
        if not code:
            text = "\n".join(ln if ln.strip() else "" for ln in text.split("\n"))
            text = re.sub(r"\n{3,}", "\n\n", text)
        out.append(text)
    return "\n".join(out)


SOUP_MIN_LINES = 40      # trafilatura output this long with no links and mostly
SOUP_SHORT = 20          # lines of at most SOUP_SHORT chars ...
SOUP_SHARE = 0.7         # ... in this share is a table layout flattened one cell per line


def is_token_soup(md: str) -> bool:
    lines = [ln for ln in md.split("\n") if ln.strip()]
    return (len(lines) >= SOUP_MIN_LINES and "](" not in md
            and sum(len(ln.strip()) <= SOUP_SHORT for ln in lines) >= SOUP_SHARE * len(lines))


def flat_page_markdown(tree: LexborHTMLParser) -> str:
    """General whole-page markdown renderer (card hubs, pages trafilatura finds nothing in, table-layout listings
    such as Hacker News).
    Header-less tables are layout: rows become paragraphs and cells inline text, so links survive in reading order
    instead of one token per line. Pages with <th> keep their tables."""
    for sel in STRIP:
        for node in tree.css(sel):
            node.decompose()
    body_html = slim_body(tree)
    if len(body_html) > WHOLE_PAGE_MAX_HTML:
        return ""
    if tree.css_first("th") is None:
        body_html = LAYOUT_TABLE.sub(r"<\1div>", body_html)
        body_html = LAYOUT_ROW.sub(r"<\1p>", body_html)
        body_html = body_html.replace("<td>", "<span> ").replace("</td>", " </span>")
    return whole_page_markdown(body_html)


def norm_text(t: str) -> str:
    return re.sub(r"\W+", " ", t).lower().strip()


def listing_text(md: str) -> str:
    """Normalized main text without link URLs: a URL slug must not make a dropped title look present."""
    return norm_text(MD_LINK_URL.sub(" ", md))


def _has_item_siblings(n: LexborNode, memo: dict[tuple[Any, Any], bool]) -> bool:
    """True when n's parent has at least LIST_MIN_ITEMS children with n's tag that each contain a link."""
    parent = n.parent
    key = (parent.mem_id, n.tag)
    if key not in memo:
        count, child = 0, parent.child
        while child is not None and count < LIST_MIN_ITEMS:
            if child.tag == n.tag and child.css_first("a") is not None:
                count += 1
            child = child.next
        memo[key] = count >= LIST_MIN_ITEMS
    return memo[key]


def is_listing(main_len: int, body_len: int, tree: LexborHTMLParser, md: str) -> bool:
    """True when the extracted markdown looks like one item of a listing page rather than the page itself.

    A link is "dropped" when its text is missing from the markdown. Links are grouped by their list item (an
    ancestor with at least LIST_MIN_ITEMS linked siblings of the same tag). The page is a listing when one group has
    at least LIST_MIN_ITEMS dropped items and the group sits next to (within LIST_ANCHOR_UP levels of) a paragraph
    that the markdown did keep. Only checked when the main text is a small share of the body.
    """
    if not body_len or main_len >= LIST_RATIO * body_len or main_len >= LIST_MAX_MAIN:
        return False
    clean = MD_LINK_URL.sub(" ", md)
    have = norm_text(clean)
    blocks = [norm_text(b) for b in re.split(r"\n\s*\n", clean)]
    paras = [b[:80] for b in blocks if len(b) >= LIST_PARA] or [b[:80] for b in blocks if len(b) >= LIST_SHORT_PARA]
    if not paras:
        return False
    memo: dict[tuple[Any, Any], bool] = {}  # (parent, tag) -> has_item_siblings
    groups: dict[int, list[Any]] = {}  # list node id -> [list node, {item id: every link in it dropped so far}]
    for a in (tree.body or tree.root).css("a"):
        link_text = norm_text(a.text(separator=" ", strip=True))
        if len(link_text) < LIST_LINK_CHARS:
            continue
        item, node = None, a
        for _ in range(LIST_ITEM_UP):
            node = node.parent
            if node is None or node.parent is None or node.tag in ("body", "html"):
                break
            if _has_item_siblings(node, memo):
                item = node
                break
        if item is None:
            continue
        group = groups.setdefault(item.parent.mem_id, [item.parent, {}])
        group[1][item.mem_id] = group[1].get(item.mem_id, True) and link_text not in have
    for anchor, items in groups.values():
        if sum(items.values()) < LIST_MIN_ITEMS:
            continue
        for _ in range(LIST_ANCHOR_UP + 1):
            if anchor is None:
                break
            txt = norm_text(anchor.text(separator=" ", strip=True))
            if any(p in txt for p in paras):
                return True
            anchor = anchor.parent
    return False


def wants_whole_page(url: str, main_only: bool, main_len: int, body_len: int, link_frac: float) -> bool:
    if not main_only:
        return True
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()
    if host in WHOLE_PAGE_HOSTS:
        return True
    if body_len > MIN_BODY and main_len < MAIN_RATIO * body_len:
        return True
    return body_len > MIN_BODY / 2 and link_frac > HUB_LINK_FRAC and main_len < HUB_MAIN_RATIO * body_len


def is_card_hub(md: str, tree: LexborHTMLParser, body_len: int, link_frac: float) -> bool:
    """True for a hub page whose story cards trafilatura reduced to linkless summaries (see CARD_LINK_FRAC)."""
    if body_len <= MIN_BODY / 2 or link_frac < CARD_LINK_FRAC or md.count("](") > CARD_MAX_MAIN_LINKS:
        return False
    seen: set[str] = set()
    for a in (tree.body or tree.root).css("a[href]"):
        text = a.text(separator=" ", strip=True)
        if len(text) >= CARD_HEADLINE_CHARS and len(text.split()) >= CARD_HEADLINE_WORDS:
            seen.add(a.attributes.get("href") or "")
            if len(seen) >= CARD_MIN_HEADLINES:
                return True
    return False


# Sign-in walls that pad the prompt with some public text (linkedin.com/in/...: the "Sign in to view <name>'s full
# profile" modal repeated over a teaser). The same short prompt line AUTH_REPEAT times or more, in the markdown or as
# a sign-in modal's heading, is a wall: only the prompt is kept, so is_thin() sends the page on.
AUTH_PROMPT = re.compile(r"\b(?:sign|log) ?in to (?:view|see|continue|read)\b|\bjoin (?:now )?to (?:view|see)\b", re.I)
AUTH_LINE = 200
AUTH_REPEAT = 2


def _repeated_prompt(lines: Any) -> str:
    counts: dict[str, int] = {}
    for line in lines:
        line = line.strip(" \t#*-_>")
        if len(line) <= AUTH_LINE and AUTH_PROMPT.search(line):
            counts[line] = counts.get(line, 0) + 1
            if counts[line] >= AUTH_REPEAT:
                return line
    return ""


def auth_prompt(md: str) -> str:
    """The sign-in prompt line when the markdown repeats one (a wall), else ""."""
    return _repeated_prompt(md.split("\n")) if AUTH_PROMPT.search(md) else ""


def modal_auth_prompt(tree: LexborHTMLParser) -> str:
    """The same for sign-in modals (dialog headings), which extraction drops: linkedin.com/in/... pages carry the
    "Sign in to view ...'s full profile" modal four times over a public teaser."""
    heads = tree.css("dialog h1, dialog h2, dialog h3, [role=dialog] h1, [role=dialog] h2, [role=dialog] h3, "
                     "[aria-modal=true] h1, [aria-modal=true] h2, [aria-modal=true] h3")
    return _repeated_prompt(h.text(separator=" ", strip=True) for h in heads)


# Overlay-link cards (theguardian.com): the story link is an empty <a href aria-label> laid over the card, next to
# (not around) its headline, so every extraction path keeps the headline without its URL. The headline (h1-h4 or
# role=heading, OVERLAY_UP levels up from the link at most, not already linked) gets the link wrapped around its
# text and the empty overlay goes.
OVERLAY_UP = 3
HEADLINE_SEL = "h1, h2, h3, h4, [role=heading]"


def link_overlay_cards(tree: LexborHTMLParser) -> bool:
    """In place; True when a card was rewritten."""
    changed = False
    for a in (tree.body or tree.root).css("a[href]"):
        href = a.attributes.get("href") or ""
        if not href or href == "#" or a.text(strip=True) or a.css_first("img, svg, picture") is not None:
            continue
        node = a.parent
        for _ in range(OVERLAY_UP):
            if node is None or node.tag in ("body", "html"):
                break
            heads = node.css(HEADLINE_SEL)
            if heads:
                head = heads[0]
                text = " ".join(head.text(separator=" ").split())
                if len(heads) == 1 and text and head.css_first("a") is None:
                    tag = head.tag if head.tag in ("h1", "h2", "h3", "h4") else "h3"
                    linked = LexborHTMLParser(f'<{tag}><a href="{escape(href, quote=True)}">{escape(text)}</a>'
                                              f"</{tag}>").css_first(tag)
                    if linked is not None:
                        head.replace_with(linked)
                        a.decompose()
                        changed = True
                break
            node = node.parent
    return changed


def prepare_html(html: str) -> str:
    """Page HTML as extraction should see it: unsafe link schemes neutralised (fastweb_firecrawl.links) and overlay-link
    cards linked. Serialised again only when something changed."""
    tree = LexborHTMLParser(html)
    changed = neutralise_html(tree)
    changed = link_overlay_cards(tree) or changed
    return (tree.html or html) if changed else html


def finish_markdown(md: str, wall: bool, base: str) -> str:
    """Inline tags unwrapped, blank lines tidied, a repeated sign-in prompt cut to its short text (so is_thin()
    sends it on), links resolved. A wall keeps its short text as is (is_thin() reads its length); unsafe link
    targets are neutralised anyway."""
    md = tidy_blank_lines(strip_inline_tags(md))
    prompt = "" if wall else auth_prompt(md)
    if prompt:
        md, wall = prompt, True
    return absolutize_links(md, "" if wall else base)


def extract_html(html: str, url: str, main_only: bool) -> tuple[str, str, str | None]:
    md, title, desc, _ = extract_job(html, url, main_only)
    return md, title, desc


def extract_job(html: str, url: str, main_only: bool, max_html: int = 0) -> tuple[str, str, str | None, int]:
    """Runs in an extraction worker process. (markdown, title, description, text length without the injected
    heading). max_html: pages over 500 KB are lightened first; still above max_html -> TooLarge."""
    if len(html) > LARGE_HTML:
        html = lighten(html)
    if max_html and len(html) > max_html:
        raise TooLarge
    html = prepare_html(squeeze_space(html))
    md = trafilatura.extract(html, output_format="markdown", include_links=True,
                             include_tables=True, favor_recall=not main_only) or ""
    tree = LexborHTMLParser(html)
    base = base_url(tree, url)
    title, desc = head_meta(tree)
    h1 = tree.css_first("h1")
    heading = (h1.text(separator=" ", strip=True) if h1 else "") or title
    # Consent walls and challenge pages keep their tiny main text so is_thin() sends them on.
    wall = len(md) < 500 and bool(SHELL_MARKERS.search(md))
    hard = False
    if not wall:
        had_hidden = tree.css_first(HIDDEN) is not None
        prompt = modal_auth_prompt(tree)  # before page_stats strips the dialogs
        body_len, link_frac, wall, hard = page_stats(tree, title)
        if hard or prompt:  # wall padded with links or hidden text: keep its short own text so is_thin() sends it on
            md = prompt or md[:400] or (tree.body.text(separator=" ", strip=True)[:400] if tree.body else "") or title
            wall = hard = True
        main_len = len(md.strip())
        if not wall and had_hidden:  # inline-hidden tabs (product specs) stay in whole-page/listing
            tree = LexborHTMLParser(html)
            for sel in STRIP_BASE:
                for node in tree.css(sel):
                    node.decompose()
            body = tree.body or tree.root
            text = body.text(separator=" ", strip=True) if body is not None else ""
            body_len = len(text)
            link_chars = sum(len(a.text(separator=" ", strip=True)) for a in body.css("a"))
            link_frac = link_chars / body_len if body_len else 0.0
        whole = not wall and wants_whole_page(url, main_only, main_len, body_len, link_frac)
        if not whole and not wall and is_card_hub(md, tree, body_len, link_frac):
            # flat_page_markdown: also handles table-layout hubs (Hacker News) without one-cell-per-line soup
            page = flat_page_markdown(LexborHTMLParser(html))
            if len(page) > len(md):
                md = page
        if whole or (not wall and main_len < LIST_RATIO * body_len):
            body_html = slim_body(tree)  # attributes only: is_listing reads tags, href-free link text
            # Markup over WHOLE_PAGE_MAX_HTML can't fall back, so the listing scan is skipped there.
            if len(body_html) <= WHOLE_PAGE_MAX_HTML and (whole or is_listing(main_len, body_len, tree, md)):
                page = whole_page_markdown(body_html)
                if len(page) > len(md):
                    md = page
    if not wall and (not md or is_token_soup(md)):  # no main block, or table cells one per line (Hacker News)
        md = flat_page_markdown(LexborHTMLParser(html)) or md
    if not md:
        md = trafilatura.html2txt(html) or ""
    md = finish_markdown(md, wall or hard, base)
    n = len(md.strip())
    if md and heading and _plain(heading) not in _plain(md[:5000]):
        md = f"# {heading}\n\n{md}"  # trafilatura often drops the page <h1>; Firecrawl keeps it
    return md[:MAX_MD_CHARS], title, desc, n


def _plain(text: str) -> str:
    """Text for the heading check: markdown links reduced to their text, without backticks or permalink glyphs."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return " ".join(text.replace("`", " ").replace("\u00b6", " ").lower().split())


BIG_BLOCKS = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")


def big_text_job(html: str, url: str) -> tuple[str, str, str | None, int]:
    """Runs in the big lane for markup over max_html: selectolax(lexbor)-only text, no trafilatura/markdownify,
    far cheaper than extract_job (deeply nested markup
    is still superlinear in lexbor; the lane's hard timeout bounds it). Same return shape as extract_job."""
    tree = LexborHTMLParser(html)
    title, desc = head_meta(tree)
    h1 = tree.css_first("h1")
    heading = (h1.text(separator=" ", strip=True) if h1 else "") or title
    shell = " ".join(x.text(separator=" ", strip=True) for x in tree.css("noscript"))[:2000]
    for sel in (*STRIP, "form"):  # same boilerplate as page_stats; lexbor parses an unclosed <script> as script
        for node in tree.css(sel):
            node.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""
    # Wall rule of page_stats, on the whole visible text and <title> (the regex is linear; CMP markup often sits
    # after the site chrome): a consent/challenge/JS shell padded with links past the cap returns only its short own
    # text, so is_thin() sends it on. Non-link text is measured in letters/digits outside links:
    # separators between 100k links would otherwise count as text. A real page that merely says "consent" falls
    # through to the full text.
    if body is not None and (SHELL_MARKERS.search(text) or SHELL_MARKERS.search(title) or HARD_MARKERS.search(text)
                             or SHELL_MARKERS.search(shell)):
        own = own_len(body)
        if ((own < WALL_TEXT and (SHELL_MARKERS.search(text) or SHELL_MARKERS.search(title)))
                or (own < NOSCRIPT_OWN and SHELL_MARKERS.search(shell)) or is_hard_wall(own, text, title, shell)):
            for a in body.css("a"):  # the wall's own words, not the link padding before it
                a.decompose()
            short = (body.text(separator="\n", strip=True) or shell or title)[:400]
            return short, title, desc, len(short)
    md = BIG_BLOCKS.sub("\n\n", LONG_SPACE.sub(" ", text)).strip()
    n = len(md)
    if md and heading and heading.lower() not in md[:5000].lower():
        md = f"# {heading}\n\n{md}"
    elif md and heading and md.startswith(heading):
        md = f"# {md}"
    return md[:MAX_MD_CHARS], title, desc, n


def pdf_job(data: bytes, cap: int) -> tuple[str, str, int]:
    """Runs in the pdf worker lane: (markdown capped at cap chars, title, pages)."""
    import io

    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    text = "\n\n".join((p.extract_text() or "").strip() for p in r.pages)
    title = (r.metadata.title if r.metadata and r.metadata.title else "") or ""
    return text[:cap], title, len(r.pages)
