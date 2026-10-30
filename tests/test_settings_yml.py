"""The committed searxng/settings.yml must be secret-free; compose must feed SEARXNG_SECRET from .env."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_settings_committable_and_engines():
    cfg = yaml.safe_load((ROOT / "searxng/settings.yml").read_text())
    assert cfg["server"]["secret_key"] == "placeholder-overridden-by-SEARXNG_SECRET"
    eng = {e["name"]: e for e in cfg["engines"]}
    assert eng["yahoo"].get("disabled") is False
    assert eng["brave"]["disabled"] is True       # fallback via explicit engines= only
    assert eng["bing"]["disabled"] is True        # leaks other queries' results
    for name in ("google", "qwant", "google cse", "duckduckgo", "wikidata", "startpage"):
        assert eng[name]["disabled"] is True
    assert cfg["outgoing"]["request_timeout"] <= 1.0
    assert eng["yahoo"]["timeout"] == 1.5                                   # slow yahoo answers instead of timing out
    assert all(e.get("timeout", 0) <= cfg["outgoing"]["max_request_timeout"] for e in cfg["engines"])


def test_compose_passes_secret_from_env():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["searxng"]["environment"]
    assert env["SEARXNG_SECRET"].startswith("${SEARXNG_SECRET:?")
    fw = compose["services"]["fastweb"]["environment"]
    assert fw["FASTWEB_REDIS_URL"] == "redis://fastweb-redis:6379/0"
    fr = compose["services"]["fastweb-redis"]
    assert "allkeys-lru" in fr["command"] and fr["mem_limit"] == "256M" and fr["networks"] == ["backend"]


def test_compose_engine_defaults():
    fw = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["fastweb"]["environment"]
    assert fw["SEARXNG_ENGINES"] == "${SEARXNG_ENGINES:-yahoo}"
    assert fw["FASTWEB_SEARCH_FALLBACK_ENGINES"] == "${FASTWEB_SEARCH_FALLBACK_ENGINES:-privacywall,brave}"
    assert fw["FASTWEB_SEARCH_HEDGE"] == "${FASTWEB_SEARCH_HEDGE:-1.1}"
    assert fw["FASTWEB_SEARCH_RELEVANCE"] == "${FASTWEB_SEARCH_RELEVANCE:-1}"
