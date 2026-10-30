"""Pure page heuristics: is a plain result thin, walled, binary or an app shell, and small helpers.

Everything here is a pure function of its arguments, so it is unit-tested directly (tests/test_heuristics.py and
friends). The thresholds are explained in docs/design.md.
"""
import json
import re
import ssl
import urllib.parse

import httpx

from .extract import LARGE_HTML, SHELL_MARKERS

JS_MARKERS = re.compile(r"enable javascript|javascript is (disabled|required)|you need to enable javascript", re.I)
# IAB/TCF and CMP consent text: short pages carrying it are consent walls.
CONSENT_MARKERS = re.compile(r"partners store and/or access|manage (your )?(options|preferences|choices)|"
                             r"accept all|reject all|we value your privacy|consent to our and our", re.I)
CHALLENGE_MARKERS = re.compile(r"just a moment|checking your browser|cf-chl|captcha|are you a robot|"
                               r"access denied|perimeterx|datadome|akamai|incapsula", re.I)
# Sign-in walls: a short page whose text is a sign-up / log-in prompt (x.com without a session).
LOGIN_MARKERS = re.compile(r"by (continuing|signing up|signing in|logging in)\b[^.]{0,60}\bagree|"
                           r"(sign|log) ?in to (continue|view|see|read)|you must (be )?(logged|signed) in|"
                           r"\bjoin (today|now)\b|create (an |your )?account to (continue|view|see|read)", re.I)
MIN_LOGIN_TEXT = 600      # text below this carrying LOGIN_MARKERS is a sign-in wall
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "text/csv", "text/x-rst",
              "application/xml", "text/xml",  # RSS feeds and sitemaps
              "application/javascript", "text/javascript", "application/x-javascript", "application/ecmascript")
ASTRAL = re.compile(rb"[\xf0-\xf4]")    # UTF-8 lead bytes of chars above U+FFFF -> 4 bytes per char in a str
WIDE = re.compile(rb"[\x80-\x9f\xc4-\xef]")  # UTF-8 leads above U+00FF, or cp1252 curly quotes -> 2 bytes per char
BINARY_MAGIC = (b"\x1f\x8b", b"\x89PNG", b"%PDF", b"PK\x03\x04", b"GIF8", b"\xff\xd8\xff")
NOT_TEXT = re.compile(r"[\x00-\x08\x0b\x0e-\x1a\x1c-\x1f]")  # C0 controls minus \t \n \f \r ESC
MIN_HTML_TEXT = 100       # extracted text below this on an HTML page -> fallback (example.com has 156)
# App-shell evidence for the suspect-SPA signal: a JS app whose plain HTML is a small static summary.
APP_ROOT = re.compile(r"""<div[^<>]+id=["']?(?:root|app|__next|__nuxt|svelte|q-app)["'\s>]|__NEXT_DATA__|ng-version|"""
                      r"""data-reactroot|data-server-rendered|window\.__NUXT__|self\.__next_f|/_next/static/|"""
                      r"""/_nuxt/""", re.I)
MODULE_SCRIPT = re.compile(r"""<script[^<>]+type=["']?module""", re.I)
SUSPECT_TEXT = 2500       # main-only text below this (chars) on an app shell -> try the browser


def is_thin(html: str, text: str) -> bool:
    """Plain result too poor to trust: JS shell / consent / challenge with little text, or big HTML with tiny text."""
    n = len(text.strip())
    if n < 500 and SHELL_MARKERS.search(html):
        return True
    if n < 2000 and JS_MARKERS.search(html):  # JS shell padded with menu text (new BOM site)
        return True
    if n < 2000 and CHALLENGE_MARKERS.search(text):  # same honesty check as Firecrawl's result
        return True
    if n < 3000 and CONSENT_MARKERS.search(text):  # TCF consent text that trafilatura kept as "content"
        return True
    if n < MIN_LOGIN_TEXT and LOGIN_MARKERS.search(text):  # a sign-in wall, not the page
        return True
    return len(html) > 50_000 and n < 300


# A markdown link (text and target), matched linearly: the text excludes "[" so an unclosed "[[[[" line is not
# rescanned from every bracket.
MD_LINK = re.compile(r"\[[^\[\]\n]*\]\((?:[^()]|\([^()]*\))*\)")
LETTERS = re.compile(r"[^\W\d_]")
SUSPECT_LINKY_TEXT = 8000  # an app shell whose text is under this and LINKY_SHARE link text is chrome only
LINKY_SHARE = 0.8


def mostly_links(md: str) -> bool:
    """At least LINKY_SHARE of the letters sit inside markdown links: menus and footer, no content of its own."""
    total = len(LETTERS.findall(md))
    own = len(LETTERS.findall(MD_LINK.sub(" ", md)))
    return total > 0 and own <= (1 - LINKY_SHARE) * total


def suspect_spa(html: str, text: str) -> bool:
    """Small visible text on an app shell: a root div / framework marker, or a module bundle page whose markup is much
    bigger than its text. Plain served it as ok; the browser may find the real content. An app shell whose text is
    nearly all links (header menus and a big footer around an empty product grid, ikea.com category pages) is
    suspect up to SUSPECT_LINKY_TEXT."""
    n = len(text.strip())
    if n >= SUSPECT_TEXT:
        return n < SUSPECT_LINKY_TEXT and bool(APP_ROOT.search(html) or MODULE_SCRIPT.search(html)) \
            and mostly_links(text)
    if APP_ROOT.search(html):
        return True
    return bool(MODULE_SCRIPT.search(html)) and len(html) > 30 * max(n, 1)


def is_blocked(status: int, body: str) -> bool:
    if status in (401, 403, 429):
        return True
    return status == 503 and bool(CHALLENGE_MARKERS.search(body))


def choose_lane(is_pdf: bool, markup_len: int) -> str:
    """By markup left after lighten(): a script-heavy homepage is a small page."""
    if is_pdf:
        return "pdf"
    return "big" if markup_len > LARGE_HTML else "small"


def decoded_size(data: bytes) -> int:
    """Upper bound of the memory a decoded body takes as a str (1, 2 or 4 bytes per char)."""
    kind = 4 if ASTRAL.search(data) else 2 if WIDE.search(data) else 1
    return len(data) * kind


C0 = re.compile("[\x00-\x08\x0b\x0e-\x1a\x1c-\x1f]")
FFFD_RUN = re.compile("\ufffd{3,}")


def binary_message(text: str, mime: str) -> str:
    """422 wording: U+FFFD in word-long runs with no control bytes is text in a mislabelled charset (a cp1251 or GBK
    page served as UTF-8), not binary; scattered U+FFFD soup stays "binary"."""
    sample = text[:200_000]
    bad = sample.count("\ufffd")
    if (bad > 0.05 * len(sample) and len(C0.findall(sample)) < 0.002 * len(sample)
            and sum(map(len, FFFD_RUN.findall(sample))) > 0.6 * bad):
        return f"Unsupported content: undecodable text (charset mislabelled?) served as {mime}"
    return f"Unsupported content: binary body served as {mime}"


def is_binary(head: bytes, text: str) -> bool:
    """A body that is not text whatever its declared type: a known binary magic, any NUL in a body under 512 chars,
    or in any 8 K window over the whole text (a clean HTML prefix over a file dump, binary between sampled
    windows) more than 2 % NUL/C0 controls (not tab/newline/FF/CR/ESC) or more than 25 % U+FFFD
    (gzip without Content-Encoding, a mislabelled image). Mislabelled cp1252/latin-1 text and ANSI
    logs stay text."""
    if head.startswith(BINARY_MAGIC):
        return True
    if len(text) < 512:
        return "\x00" in text
    starts = [*range(0, max(1, len(text) - 8192), 8192), max(0, len(text) - 8192)]  # last window is full
    for sample in (text[i:i + 8192] for i in starts):  # linear: one pass of the regex over the text
        if len(NOT_TEXT.findall(sample)) > 0.02 * len(sample) or sample.count("\ufffd") > 0.25 * len(sample):
            return True
    return False


def is_tls_error(e: BaseException | None) -> bool:
    """A TLS handshake failure (Firecrawl's browser may still get the page), not a dead port."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLError) or re.search(r"\bSSL|TLS|certificate", str(e)):
            return True
        e = e.__cause__ or e.__context__
    return False


def is_refused(e: BaseException | None) -> bool:
    """A connection refused somewhere in the exception chain."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ConnectionRefusedError) or "refused" in str(e).lower():
            return True
        e = e.__cause__ or e.__context__
    return False



def drop_added_query(requested: str, final: str) -> str:
    """final, minus query parameters a redirect back to the same page added (a bot challenge's js_challenge,
    jsc_token, solution, ...): they are session tokens, not part of the page's address agents should cite."""
    try:
        a, b = httpx.URL(requested), httpx.URL(final)
        if (a.scheme, a.host, a.port, a.path) != (b.scheme, b.host, b.port, b.path):
            return final
        keep = [(k, v) for k, v in b.params.multi_items() if k in a.params]
        return str(b.copy_with(query=urllib.parse.urlencode(keep).encode() or None))
    except Exception:  # noqa: BLE001 - metadata must never fail on an odd URL
        return final


PATH_PARAMS = re.compile(r";[^/]*")


def safe_url(url: str) -> str:
    """scheme://host/path only: query strings and ;matrix parameters (e.g. jsessionid) can carry tokens. A token
    written as a plain path segment (some webhook URLs) is still logged."""
    try:
        u = httpx.URL(url)
        return f"{u.scheme}://{u.netloc.decode('ascii')}{PATH_PARAMS.sub('', u.path)}"
    except Exception:  # noqa: BLE001 - logging must never fail on an odd URL
        return "<invalid>"


def text_markdown(txt: str, ctype: str, cap: int) -> str:
    if "json" in ctype:
        try:
            txt = "```json\n" + json.dumps(json.loads(txt), indent=2, ensure_ascii=False)[:cap] + "\n```"
        except ValueError:
            txt = "```json\n" + txt[:cap] + "\n```"
    return txt[:cap]
