from firecrawl import Firecrawl


def test_sdk_scrape_and_search(ctx):
    up, furl, aurl = ctx
    fc = Firecrawl(api_url=aurl, api_key="x")
    doc = fc.scrape(furl + "/p/article", formats=["markdown"])
    assert "harbour plan" in doc.markdown
    meta = doc.metadata.model_dump()
    assert meta["title"] == "Big Story" and meta["status_code"] == 200
    res = fc.search("harbour", limit=5)
    assert len(res.web) == 5 and res.web[0].url == "https://r0.example/" and res.web[0].title == "Result 0"


def test_sdk_404_raises(ctx):
    _, furl, aurl = ctx
    fc = Firecrawl(api_url=aurl, api_key="x")
    try:
        fc.scrape(furl + "/p/missing", formats=["markdown"])
    except Exception as e:
        assert "404" in str(e)
    else:
        raise AssertionError("expected error")


def test_sdk_scrape_call(ctx):
    _, furl, aurl = ctx
    fc = Firecrawl(api_url=aurl, api_key="x")
    doc = fc.scrape(furl + "/p/article", formats=["markdown", "html"], timeout=60000)
    assert "harbour plan" in doc.markdown and "<article>" in doc.html
    meta = doc.metadata.model_dump()
    assert meta["title"] == "Big Story"
    assert (meta.get("sourceURL") or meta.get("source_url")) == furl + "/p/article"


def test_sdk_raises_when_all_engines_empty(ctx):
    up, _, aurl = ctx
    up.mode = "search_empty"
    fc = Firecrawl(api_url=aurl, api_key="x")
    try:
        fc.search("harbour nothing", limit=5)
    except Exception as e:
        assert "Search backend failed" in str(e) or "503" in str(e)
    else:
        raise AssertionError("expected the SDK to raise on success:false")
