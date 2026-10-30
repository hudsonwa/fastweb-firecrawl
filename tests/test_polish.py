

def test_sphinx_heading_with_permalink_not_duplicated():
    from fastweb_firecrawl.extract import _plain
    assert _plain("json — JSON encoder ¶") in _plain("# `json` — JSON encoder[¶](#module-json)\n\ntext")


def test_player_meta_with_unsafe_scheme_is_dropped():
    from fastweb_firecrawl.upstream import _unsafe_url_value
    assert _unsafe_url_value("twitter:player", "javascript:alert(1)")
    assert _unsafe_url_value("og:see_also", "javascript:alert(1)")


def test_searxng_non_object_json_is_not_timed_out():
    import httpx

    from fastweb_firecrawl.search import _timed_out
    assert not _timed_out(httpx.Response(200, json=[1, 2]))
