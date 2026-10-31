"""fastweb: a fast, self-hosted search and scrape service that speaks Firecrawl's /v1 and /v2 API.

Entry point: :func:`fastweb_firecrawl.app.create_app` (an ASGI app factory). The scrape tier ladder lives in
:mod:`fastweb_firecrawl.pipeline`; see docs/design.md for how the tiers fit together.
"""
__version__ = "0.1.1"
