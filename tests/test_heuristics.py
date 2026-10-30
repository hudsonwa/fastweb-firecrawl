from fastweb_firecrawl.heuristics import is_blocked, is_thin


def test_short_legit_page_not_thin():
    html = "<html><body><h1>Example Domain</h1><p>" + "a" * 100 + "</p></body></html>"
    assert not is_thin(html, "Example Domain " + "a" * 100)


def test_js_shell_is_thin():
    assert is_thin("<div id=root></div><noscript>Please enable JavaScript</noscript>", "")


def test_large_html_tiny_text_is_thin():
    assert is_thin("<script>" + "x" * 60000 + "</script>", "hi")


def test_long_text_with_marker_not_thin():
    assert not is_thin("enable javascript " + "<p>t</p>" * 10, "word " * 500)


def test_consent_page_thin():
    assert is_thin("<p>We value your privacy. Accept all cookies</p>", "We value your privacy. Accept all cookies")


def test_blocked():
    assert is_blocked(403, "<title>Just a moment...</title>")
    assert is_blocked(429, "")
    assert is_blocked(401, "")
    assert not is_blocked(200, "Just a moment")
    assert not is_blocked(503, "plain maintenance")
    assert is_blocked(503, "cf-chl")


def test_sign_in_wall_is_thin():
    text = ("Happening now. Join today. Sign up with Google. Create account. By signing up, you agree to the Terms of "
            "Service and Privacy Policy. Already have an account? Sign in")
    assert is_thin("<html><body>" + text + "</body></html>", text)


def test_long_article_with_sign_in_link_not_thin():
    text = "Sign in to continue reading comments. " + "A real article paragraph. " * 40
    assert not is_thin("<p>" + text + "</p>", text)


def test_link_only_app_shell_is_suspect():
    """ikea.com-category-like: an app shell whose plain text is header menus, an empty "Recommended for you" and a big
    footer, all links; the product grid is built by JavaScript."""
    from fastweb_firecrawl.heuristics import mostly_links, suspect_spa
    shell = '<html><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>'
    menu = "\n".join(f"- [Department number {i} and its range](https://shop.example/d/{i})" for i in range(60))
    md = f"# Products\n\n{menu}\n\n## Recommended for you\n\n{menu}"
    assert 2500 < len(md) < 8000 and mostly_links(md) and suspect_spa(shell, md)
    prose = "The harbour council voted on the ferry terminal plan after a long debate about funding. " * 40
    assert not mostly_links(prose + menu[:500]) and not suspect_spa(shell, prose + menu[:500])
    assert not suspect_spa("<html><body><p>static</p></body></html>", md)  # no app-shell evidence
    assert not suspect_spa(shell, md * 4)  # too much text to be an empty shell
