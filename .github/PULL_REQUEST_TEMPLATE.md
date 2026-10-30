## What changes

<!-- The behaviour change, in a sentence or two. -->

## How it was tested

<!-- Tests added or changed; for ladder or heuristic changes, a test that fails without the change. -->

## Checklist

- [ ] `ruff check .`, `mypy` and `pytest -m ""` (the whole suite, as CI runs it) pass locally
- [ ] 502 only for fastweb-firecrawl's own capacity (busy, retryable), and Firecrawl response shapes are unchanged
- [ ] New queues, caches or concurrency have a limit and a test for it
- [ ] CHANGELOG.md updated under "Unreleased" (user-visible changes only)
