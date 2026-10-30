"""Walls under the extract cap (link-padded, CSS-hidden padding, <noscript> shells), binary that the
sampled windows miss (content-level junk check on the markdown), and TCF/challenge walls with 1-2 K of own text."""
import base64
import random

import pytest

from .test_extract_over_cap_walls import CHAL, LINK, LIST_ROW, SIZES, cheap, fixtures, pad_to, run

MB = 1024 * 1024
ART = "<p>Real article words about the harbour vote today and what the council decided.</p>"
UNDER = SIZES["under"]


def tcf(n):
    return ("<div class='cmp'><h2>We value your privacy</h2>" + ("<p>We and our 842 partners store and/or access "
            "information on a device, such as cookies, and process personal data, such as unique identifiers and "
            "standard information sent by a device for personalised advertising and content measurement, audience "
            "research and services development.</p>") * n
            + "<p>You may click to consent to our and our partners' processing as described above.</p></div>")


def wall_bodies(size):
    rnd = random.Random(11)
    head = "<html><head><title>Harbour</title></head><body><h1>Harbour</h1>" + ART * 250
    third = size // 3
    raw = bytes(rnd.getrandbits(8) for _ in range(third))
    tailart = pad_to("", ART, "</body></html>", size - len(head) - third)
    news = "<html><head><title>Daily News</title></head><body>"
    return {
        "bin_q1_utf8": (head.encode() + raw + tailart.encode(), "text/html; charset=utf-8"),
        "bin_q1_nocs": (head.encode() + raw + tailart.encode(), "text/html"),
        "tcf2k_links_first": (pad_to(news, LINK, tcf(4) + "</body></html>", size).encode(), "text/html"),
        "tcf1k_links_first": (pad_to(news, LINK, tcf(2) + "</body></html>", size).encode(), "text/html"),
        "chal_links_long": (pad_to("<html><head><title>news.example</title></head><body>", LINK,
                                   "<p>Checking your browser before accessing the site. This process is automatic. "
                                   "Your browser will redirect to your requested content shortly. Please allow up "
                                   "to 5 seconds.</p>" * 6 + "</body></html>", size).encode(), "text/html"),
    }


def body_of(name, size=UNDER):
    if name in wall_bodies(1000):
        return wall_bodies(size)[name]
    return fixtures(size)[name]


# walls must not be a cheap success under the cap (over the cap too for the row-split shapes)
WALLS_UNDER = ["chal_links_first_jm_title", "chal_links_first_neutral_title", "wall_links_first",
               "wall_csshidden_first", "wall_csshidden_after", "js_noscript_links",
               "tcf2k_links_first", "tcf1k_links_first", "chal_links_long"]


@pytest.mark.slow
@pytest.mark.parametrize("name", WALLS_UNDER)
def test_wall_under_cap_not_cheap(ctx, name):
    body, ctype = body_of(name)
    res = run(ctx, f"i43{name}", body, ctype)
    assert not cheap(res), (res[0], res[1], res[2][:200])


@pytest.mark.parametrize("name", ["tcf2k_links_first", "tcf1k_links_first", "chal_links_long"])
def test_long_wall_over_cap_not_cheap(ctx, name):
    body, ctype = wall_bodies(SIZES["over"])[name]
    res = run(ctx, f"i43o{name}", body, ctype)
    assert not cheap(res), (res[0], res[1], res[2][:200])


# binary says binary, wherever it sits
@pytest.mark.parametrize("name", ["mixed_html_then_random", "mixed_html_then_gzip", "mixed_html_then_random_utf8",
                                  "bin_q1_utf8", "bin_q1_nocs"])
@pytest.mark.parametrize("where", list(SIZES))
def test_binary_says_binary(ctx, name, where):
    body, ctype = body_of(name, SIZES[where])
    res = run(ctx, f"i43b{name}{where}", body, ctype)
    assert res[0] == 422 and "binary" in res[4], (res[0], res[4], res[2][:100])
    assert not res[3]


def _mixed(where_bin, size=UNDER):
    """Clean HTML in all windows but one region: 'middle' (tail clean) or 'tail' (middle clean)."""
    rnd = random.Random(5)
    raw = bytes(rnd.getrandbits(8) for _ in range(20_000))
    clean = ART * (size // len(ART) // 2)
    head = "<html><head><title>Harbour</title></head><body><h1>Harbour</h1>"
    if where_bin == "middle":
        return (head + clean).encode() + raw + (clean + "</body></html>").encode()
    return (head + clean + clean).encode() + raw + ("<p>end</p>" * 2000 + "</body></html>").encode()


@pytest.mark.parametrize("where_bin", ["middle", "tail"])
def test_binary_in_one_window_only(ctx, where_bin):
    res = run(ctx, f"i43w{where_bin}", _mixed(where_bin), "text/html")
    assert res[0] == 422 and "binary" in res[4], (res[0], res[4], res[2][:100])


def test_bin_c0_10_html():
    """10 % C0 controls among letters in text/html: the 2 % C0 rule fires on the HTML path."""
    from fastweb_firecrawl.heuristics import is_binary
    rnd = random.Random(10)
    text = "<html><body><p>" + "".join(chr(rnd.choice([1, 2, 3, 4, 5, 6, 7, 8, 0x0e, 0x10]))
                                       if rnd.random() < 0.1 else rnd.choice("abcdefgh ")
                                       for _ in range(50_000)) + "</p></body></html>"
    assert is_binary(text[:64].encode(), text)


def test_all_nul_html(ctx):
    res = run(ctx, "i43nul", b"\x00" * 200_000, "text/html")
    assert res[0] == 422 and "binary" in res[4], (res[0], res[4])


def test_junk_share_helper():
    from fastweb_firecrawl.extract import junk_share
    assert junk_share("plain words " * 100) == 0
    assert junk_share("ab\x01\ue001" * 100) > 0.4
    assert junk_share("Ünïcödé 中文 русский ✓ \t\n" * 50) == 0


# precision: real pages carrying marker words stay a cheap success
@pytest.mark.slow
def test_wall_thresh_10x_big_real_page(ctx):
    """Big real page: 1 000-10 000 chars of non-link text, the rest links, plus a marker -> still cheap."""
    pre = ("<html><head><title>Index</title></head><body><h1>Index of articles</h1>"
           + "<p>This index lists every article in the archive, sorted by year and section.</p>" * 60)
    body = pad_to(pre, "<li><a href='/a'>Article title here</a></li>", "<footer>Manage cookie consent</footer>"
                  "</body></html>", UNDER)
    res = run(ctx, "i43thresh", body.encode(), "text/html")
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])


@pytest.mark.slow
@pytest.mark.xdist_group("bigpages")
@pytest.mark.parametrize("name", ["legit_consent_heading", "legit_captcha_early"])
def test_legit_under_cap_cheap(ctx, name):
    body, ctype = fixtures(UNDER)[name]
    res = run(ctx, f"i43l{name}", body, ctype)
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_legit_footer_consent_and_datauri(ctx):
    rnd = random.Random(11)
    png = base64.b64encode(bytes(rnd.getrandbits(8) for _ in range(300_000))).decode()
    for i, body in enumerate([
            pad_to("<html><head><title>Harbour</title></head><body><h1>Harbour</h1>", ART,
                   "<footer><a href='/p'>Privacy</a> <span>Manage cookie consent</span> <span>Access denied? Contact "
                   "us</span></footer></body></html>", UNDER),
            pad_to("<html><head><title>Harbour</title></head><body><h1>Harbour</h1>", ART,
                   f"<img src='data:image/png;base64,{png}'><script>var f='{png}';</script></body></html>", UNDER)]):
        res = run(ctx, f"i43legit{i}", body.encode(), "text/html")
        assert cheap(res), (i, res[0], res[1], res[4], res[2][:200])


def test_short_real_article_with_cookie_banner_cheap(ctx):
    """A short real article (about 4 K chars) with a CMP banner stays cheap: the wall's text is a minority."""
    body = ("<html><head><title>Harbour</title></head><body><h1>Harbour vote</h1><article>" + ART * 50
            + "</article><div class='cmp'><p>We value your privacy. Accept all.</p></div></body></html>")
    res = run(ctx, "i43short", body.encode(), "text/html")
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])


# --- legacy text, retailer pages, UTF-16 junk ---------------------------------------------------------------------------------------------
FR = ("<p>Le conseil municipal a décidé mercredi d'accélérer les travaux du port. Les élus ont débattu des coûts, "
      "des délais et de l'impact écologique sur la baie ; à la fin, la majorité a voté pour.</p>")


def test_junk_share_ignores_replacement_char():
    """U+FFFD is a mislabelled legacy page's accents, not binary (is_binary's 25 % rule owns it)."""
    from fastweb_firecrawl.extract import junk_share
    assert junk_share("caf\ufffd cr\ufffdme p\ufffd\ufffdli\ufffd " * 100) == 0
    assert junk_share("\u35ce\ufffd\u915f\u2928" * 100) > 0.2


def test_mislabelled_cp1252_french_is_text(ctx):
    """cp1252 French served as charset=utf-8 (about 3-5 % U+FFFD in the markdown) stays a cheap 200."""
    page = ("<html><head><title>Port</title></head><body><h1>Le port</h1><article>" + FR * 60
            + "</article></body></html>")
    res = run(ctx, "i43a_fr", page.encode("cp1252"), "text/html; charset=utf-8")
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])


def bunnings(own_paras, links):
    """a retailer page: 1-2.5 K non-link chars, 60-75 % link text, a <noscript> JS note."""
    return ("<html><head><title>Hardware store</title></head><body><noscript>Javascript is required for this page"
            "</noscript><h1>Christmas trees and plants</h1>"
            + "<p>Shop real and artificial Christmas trees, wreaths and garlands for the festive season.</p>" * own_paras
            + "<ul>" + "<li><a href='/c'>Outdoor living and garden furniture</a></li>" * links + "</ul></body></html>")


@pytest.mark.parametrize("own_paras,links", [(13, 100), (20, 150), (27, 200)])
def test_noscript_note_on_real_page_cheap(ctx, own_paras, links):
    res = run(ctx, f"i43a_bun{own_paras}", bunnings(own_paras, links).encode(), "text/html; charset=utf-8")
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])


def u16_head_then_random(size):
    """a UTF-16 head over random bytes; only the markdown junk check sees it."""
    rnd = random.Random(11)
    head = ("<html><head><title>Liste</title></head><body><h1>Liste</h1><p>" + "Zeile mit Text. " * 200
            + "</p>").encode("utf-16-le")
    return head + bytes(rnd.getrandbits(8) for _ in range(size - len(head)))


@pytest.mark.slow
@pytest.mark.parametrize("where", ["under", "over"])
def test_u16_head_then_random_binary(ctx, where):
    res = run(ctx, f"i43a_u16{where}", u16_head_then_random(SIZES[where]), "text/html")
    assert res[0] == 422 and "binary" in res[4], (res[0], res[4], res[2][:100])


NEWS = "<html><head><title>Daily News</title></head><body>"
HARD_UNDER = {
    "chal_dot_sep": pad_to(NEWS, "<a href='/x'>Footer link to a section page</a> · ", CHAL + "</body></html>", UNDER),
    "chal_pipe_sep": pad_to(NEWS, "<a href='/x'>Section</a> | ", CHAL + "</body></html>", UNDER),
    "hidden_display_space": pad_to(NEWS, "<div style='display: none'>hidden section words lorem ipsum</div>",
                                   CHAL + "</body></html>", UNDER),
    "hidden_visibility": pad_to(NEWS, "<div style='visibility:hidden'>hidden section words lorem ipsum</div>",
                                CHAL + "</body></html>", UNDER),
    "tcf8": pad_to(NEWS, LINK, tcf(8) + "</body></html>", UNDER),
    "title_only": pad_to("<html><head><title>Just a moment...</title></head><body>", LINK,
                         "<p>One moment please while we load the page for you.</p></body></html>", UNDER),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(HARD_UNDER))
def test_more_walls_under_cap_not_cheap(ctx, name):
    res = run(ctx, f"i43a_{name}", HARD_UNDER[name].encode(), "text/html")
    assert not cheap(res), (res[0], res[1], res[2][:200])


TCF_ONLY = ("<div><h2>Your choices</h2>" + "<p>We and our 842 partners store and/or access information on a device, "
            "such as cookies, and process personal data for personalised advertising.</p>" * 4 + "</div>")


@pytest.mark.parametrize("name", ["js_noscript_links", "tcf8", "tcf_only"])
def test_more_walls_over_cap_not_cheap(ctx, name):
    """the big path reads <noscript> and enters the wall rule on HARD_MARKERS too."""
    over = SIZES["over"]
    body = {"js_noscript_links": lambda: fixtures(over)["js_noscript_links"][0],
            "tcf8": lambda: pad_to(NEWS, LINK, tcf(8) + "</body></html>", over).encode(),
            "tcf_only": lambda: pad_to(NEWS, LINK, TCF_ONLY + "</body></html>", over).encode()}[name]()
    res = run(ctx, f"i43a_o{name}", body, "text/html")
    assert not cheap(res), (res[0], res[1], res[2][:200])


# --- forum notices, wall thresholds ---------------------------------------------------------------------------------------------------
XF_NOTICE = ("<div class='u-bottomFixer'><div class='notices notices--bottom_fixer'><div class='notice-content'>"
             "<div class='cookies-main'><h2>We value your privacy</h2><p>We use essential cookies to make this site "
             "work, and optional cookies to enhance your experience.</p><a href='/help/cookies'>Learn more</a>"
             "</div></div></div></div>")
XF_NAV = "".join(f"<li><a href='/forums/{i}/'>Forum section number {i}</a></li>" for i in range(60))
SIMILAR = "".join(f"<li><a href='/threads/t.{i}/'>Kernel upgrade breaks boot on node {i}</a></li>" for i in range(30))
POST = ("<p>Reply {}: after upgrading to the new kernel the host hangs at boot right after the initramfs loads, and the "
        "console shows nothing more. Rolling back to the old kernel works fine every time.</p>")


def xenforo(main):
    return ("<html><head><title>Hang on boot with kernel | Proxmox Support Forum</title></head><body>"
            "<header><nav><ul>" + XF_NAV + "</ul></nav></header><div class='p-body'><div class='p-body-main'>"
            + main + "<div class='block-similar'><h3>Similar threads</h3><ul>" + SIMILAR + "</ul></div></div></div>"
            + XF_NOTICE + "</body></html>")


def test_xenforo_cookie_notice_short_thread_cheap(ctx):
    """XenForo 2.3's visible "We value your privacy" notice on a short thread (1.4 K letters) is not a wall."""
    body = xenforo("<h1>Hang on boot with kernel 7.0.14-26</h1><article class='message'>" + POST * 9 + "</article>")
    res = run(ctx, "i43b_xfthread", body.encode(), "text/html; charset=utf-8")
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])
    assert "initramfs" in res[2]


def test_xenforo_cookie_notice_listing_cheap(ctx):
    """a XenForo forum listing (thread titles as links, short snippets) with the notice stays cheap."""
    rows = "".join(f"<div class='structItem'><div class='structItem-title'><a href='/threads/{i}/'>Cannot use PCIe "
                   f"passthrough on host number {i}</a></div><div class='structItem-minor'>Started by user{i} "
                   f"yesterday, replies {i}</div></div>" for i in range(40))
    body = xenforo("<h1>Proxmox VE: Installation and configuration</h1><div class='structItemContainer'>" + rows
                   + "</div>")
    res = run(ctx, "i43b_xflist", body.encode(), "text/html; charset=utf-8")
    assert cheap(res), (res[0], res[1], res[4], res[2][:200])


def stats(html, title=""):
    from selectolax.lexbor import LexborHTMLParser

    from fastweb_firecrawl.extract import page_stats
    return page_stats(LexborHTMLParser(html), title)


def test_wall_long_upper_end():
    """a TCF wall block with about 2 400 own letters is past WALL_LONG (pins it from above)."""
    from selectolax.lexbor import LexborHTMLParser

    from fastweb_firecrawl.extract import WALL_LONG, own_len
    html = "<html><body>" + tcf(10) + "</body></html>"
    own = own_len(LexborHTMLParser(html).body)
    assert WALL_LONG < own < 2600, own
    assert stats(html)[3] is False
    assert stats("<html><body>" + tcf(9) + "</body></html>")[3] is True


def test_wall_text_boundary():
    """the soft wall rule sits at about 850 own letters (1 000 chars): 800 is a wall, 900 is not."""
    def page(letters):
        return "<html><body><p>Manage consent</p><p>" + "abcdefghi " * ((letters - 13) // 9) + "</p></body></html>"
    assert stats(page(800))[2] is True
    assert stats(page(900))[2] is False


def test_junk_share_fffd_after_non_ascii():
    """U+FFFD right after a non-ASCII char counts even when ASCII follows (the look-behind half)."""
    from fastweb_firecrawl.extract import junk_share
    assert junk_share("㗎� " * 100) > 0.2


def test_mislabelled_charset_message(ctx):
    """mostly-undecodable text says so, not "binary body"."""
    ru = ("<p>Городской совет в среду решил ускорить работы в порту, обсудив стоимость, сроки и влияние на "
          "залив.</p>") * 80
    page = "<html><head><title>Порт</title></head><body><h1>Порт</h1><article>" + ru + "</article></body></html>"
    res = run(ctx, "i43b_ru1251", page.encode("cp1251"), "text/html; charset=utf-8")
    assert res[0] == 422 and "undecodable text (charset mislabelled?)" in res[4], (res[0], res[4])


# --- CMP rows, hidden spec tabs -------------------------------------------------------------------------------------------------------
TCFP = ("<p>We and our 842 partners store and/or access information on a device, such as cookies, and process personal "
        "data, such as unique identifiers and standard information sent by a device for personalised advertising.</p>")


def test_tcf_title_in_own_row_div_not_cheap(ctx):
    """the CMP title sits in its own row div, the TCF text (its own marker) in a sibling row: still a wall."""
    body = pad_to(NEWS, LINK, "<div class='msg'><div class='row'><h2>We value your privacy</h2></div><div class='row'>"
                  + TCFP * 8 + "</div></div></body></html>", 50_000)
    res = run(ctx, "i43c_tcfrow", body.encode(), "text/html")
    assert not cheap(res), (res[0], res[1], res[2][:200])


def product_page():
    """A retailer product page: a short overview, then a Specs tab in a display:none pane, plus menus."""
    menu = "<div class='menu'><ul>" + "".join(f"<li><a href='/c{i}'>Category {i} deals</a></li>" for i in range(30)) + "</ul></div>"
    specs = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in [
        ("Chipset", "Example X550"), ("Memory Standard", "DDR4 3200 / 3600 / 4000"), ("Form Factor", "ATX"),
        ("Socket", "Example AM4"), ("Memory Slots", "4 x 288 pin"), ("Max Memory", "128 GB"),
        ("PCI Express x16", "2 slots"), ("SATA 6Gb/s", "6 ports"), ("M.2", "2 slots"), ("LAN", "2.5 Gigabit"),
    ] * 20)
    reviews = "".join(f"<div class='review'><p>Review {i}: solid board, easy BIOS updates and stable memory "
                      f"overclocking in my build after a week of daily use.</p></div>" for i in range(6))
    return ("<html><head><title>Example X550 ATX Motherboard</title></head><body>" + menu
            + "<main><h1>Example X550 ATX Motherboard</h1><p>An ATX board for mainstream desktop builds with "
            "two M.2 slots and 2.5 Gigabit networking.</p><div class='tabs'><div class='tab-pane' "
            f"style='display:none'><h2>Specifications</h2><table>{specs}</table></div>{reviews}</div></main>"
            "</body></html>")


def test_hidden_tab_spec_table_kept(ctx):
    """A Specs tab in a display:none pane: the listing/whole-page fallback keeps it."""
    res = run(ctx, "i43c_product", product_page().encode(), "text/html; charset=utf-8")
    assert cheap(res), (res[0], res[1], res[2][:200])
    assert "Memory Standard" in res[2] and "Chipset" in res[2], len(res[2])
