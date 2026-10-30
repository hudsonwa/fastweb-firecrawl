"""Configuration edge cases: compose passes unset .env keys as empty strings, and a pip-installed fastweb left on the
compose service names warns at startup instead of failing silently."""
import asyncio
import json

from fastweb_firecrawl.app import warn_unresolvable
from fastweb_firecrawl.settings import DEFAULT_UA, Settings


def test_empty_values_mean_the_code_default(monkeypatch):
    for k in ("FASTWEB_SEARCH_CONCURRENCY", "FASTWEB_SEARCH_HEDGE", "FASTWEB_IMPERSONATE", "FASTWEB_USER_AGENT"):
        monkeypatch.setenv(k, "")
    s = Settings()
    assert s.user_agent == DEFAULT_UA
    assert s.search_concurrency == 6 and s.search_hedge == 1.1 and s.impersonate is True


def test_set_values_still_win(monkeypatch):
    monkeypatch.setenv("FASTWEB_SEARCH_CONCURRENCY", "2")
    monkeypatch.setenv("FASTWEB_IMPERSONATE", "0")
    monkeypatch.setenv("FASTWEB_USER_AGENT", "test-agent")
    s = Settings()
    assert s.search_concurrency == 2 and s.impersonate is False and s.user_agent == "test-agent"


def test_unresolvable_backend_hosts_are_logged(capsys):
    s = Settings(searxng_url="http://searxng.invalid:8080", redis_url="redis://127.0.0.1:6379/0", upstream="",
                 browser=False)
    asyncio.run(warn_unresolvable(s))
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["variable"] for x in lines] == ["SEARXNG_URL"]
    assert lines[0]["warning"] == "startup_config" and lines[0]["host"] == "searxng.invalid"


def test_url_without_host_is_logged(capsys):
    s = Settings(searxng_url="not a url", redis_url="", upstream="", browser=False)
    asyncio.run(warn_unresolvable(s))
    assert "no host in URL" in capsys.readouterr().out
