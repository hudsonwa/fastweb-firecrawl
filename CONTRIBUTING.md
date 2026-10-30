# Contributing

Thanks for your interest. Bug reports, measurements from your network, and focused pull requests are all welcome.

## Set-up

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install --no-deps -e .     # the fastweb-firecrawl package itself, editable
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
```

The default suite (about 1,400 tests, about 30 s; `-m ""` adds the slow ones, about 2 min) needs no internet
access: it starts local fake origins, a fake Firecrawl, a fake SearXNG and a fake Chrome DevTools server
(`tests/fake_cdp.py`) on 127.0.0.1.

Opt-in suites, skipped by default:

- `FASTWEB_TEST_CDP=http://127.0.0.1:PORT FASTWEB_TEST_BIND=0.0.0.0 pytest -m browser`: against a real
  `chromedp/headless-shell` that publishes its DevTools port.
- `tests/egress_stack/stack.sh up`, then `eval "$(tests/egress_stack/stack.sh env)"` and
  `pytest tests/test_egress_proxy.py -k real_chrome`: a throwaway Docker stack with the same network topology
  as `docker-compose.yml`, which checks that every request vector of a real Chrome (workers, iframes, WebSocket,
  WebRTC, forms, beacons, ...) goes through the egress proxy. `stack.sh down` removes it.

## Guidelines

- **Behaviour first.** Every change to the tier ladder, extraction heuristics or security checks needs a test that
  fails without it. Prefer synthetic fixtures; do not commit copies of third-party web pages.
- **Honesty over coverage.** fastweb-firecrawl would rather return a clear error than a consent wall or an empty "success".
  Keep it that way.
- **502 means "busy, retry"** (the one status firecrawl-py retries): use it only for fastweb-firecrawl's own capacity, never
  for a page that failed. Keep the Firecrawl response shapes intact: clients depend on them.
- **Bounded everything.** New queues, caches and concurrency need limits and a test for the limit.
- **Comments explain why**, in neutral terms, next to the code they justify.
- Lint with `ruff check .` and type-check with `mypy`; lines are at most 120 characters. The code is not
  auto-formatted: do not run `ruff format` (or another formatter) over existing files, as it rewrites most of them
  and buries the real change in the diff.
- `pytest` runs in parallel and skips tests marked `slow` (several seconds of real timing each); run `pytest -m ""`
  before a pull request, as CI does. Mark a new test `slow` when it takes over about 2 s.
- For benchmarks, use `bench/bench.py` and say where and when you measured. Pace requests (the default 2.5 s gap)
  and respect the sites you measure against.

## Pull requests

Describe the behaviour change and how you tested it (the pull request template has a short checklist). CI runs
ruff, mypy, pip-audit, the whole suite with coverage (`pytest -m "" --cov=fastweb_firecrawl --cov-fail-under=90`, slow tests
included), `docker compose config` for both profiles, a Docker build, a boot smoke test, an egress-proxy start check and, as a non-blocking job,
`scripts/smoke-firecrawl.sh`.

Dependabot proposes weekly updates for the Python requirements, the `Dockerfile` base image, the digest-pinned images
in `docker-compose.yml` and the GitHub Actions.

## Layout

- `fastweb_firecrawl/`: the package. `app.py` is the HTTP layer, `pipeline.py` the tier ladder; `docs/design.md` explains
  both.
- `tests/`: the test suite (`tests/egress_stack/` is the opt-in real-Chrome harness).
- `requirements.txt`: the exact versions the Docker image and CI install; `pyproject.toml` declares compatible ranges.
- `examples/`: runnable SDK, curl and compose examples.

Participation is covered by the [code of conduct](CODE_OF_CONDUCT.md).
