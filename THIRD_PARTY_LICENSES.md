# Third-party licences

fastweb-firecrawl itself is MIT-licensed. It depends on the following components; none of them is copied into this
repository. All Python dependencies are under permissive licences compatible with MIT.

## Python runtime dependencies (`requirements.txt`, installed into the image)

| Package | Licence |
|---|---|
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| httpx (+ h2) | BSD-3-Clause (h2: MIT) |
| trafilatura (+ lxml, justext, htmldate, courlan) | Apache-2.0 (lxml BSD-3-Clause, justext BSD-2-Clause, htmldate/courlan Apache-2.0) |
| pypdf | BSD-3-Clause |
| selectolax | MIT |
| markdownify | MIT |
| redis (redis-py) | MIT |
| charset-normalizer | MIT |
| curl_cffi | MIT (bundles curl-impersonate, MIT, and BoringSSL, OpenSSL/ISC-style) |
| websockets | BSD-3-Clause |

## Development dependencies (`requirements-dev.txt`)

pytest (MIT), pytest-timeout (MIT), pytest-xdist (MIT), pytest-cov (MIT), coverage (Apache-2.0), firecrawl-py (MIT, used only to check SDK compatibility),
fakeredis (BSD-3-Clause), PyYAML (MIT), ruff (MIT), mypy (MIT), pip-audit (Apache-2.0). Packaging: hatchling (MIT).

## Container images (pulled at run time, not distributed with fastweb-firecrawl)

| Image | Licence | Note |
|---|---|---|
| `searxng/searxng` | AGPL-3.0-or-later | Run unmodified as a separate service; `searxng/settings.yml` is configuration. If you modify SearXNG and offer it to others over a network, the AGPL applies to your modifications. |
| `chromedp/headless-shell` | Chromium licences (BSD-3-Clause and others) | Run unmodified. |
| `redis:7.4` | RSALv2 / SSPLv1 (Redis 7.4 and later) | Used internally as a cache, which both licences allow. Valkey (BSD-3-Clause) is a drop-in alternative if you prefer an OSI licence. |
| `python:3.12-slim` | PSF and Debian package licences | Base of the fastweb-firecrawl image. |
| Firecrawl (`firecrawl`, `playwright-service`, `nuq-postgres`), optional profile | AGPL-3.0 (Firecrawl); PostgreSQL licence | Only pulled with `--profile firecrawl`; run unmodified. |
| `rabbitmq:3.13` (optional profile) | MPL-2.0 | Only pulled with `--profile firecrawl`. |

fastweb-firecrawl talks to SearXNG and Firecrawl over HTTP only and does not link against them.
