"""Exceptions and error helpers shared by the scrape tiers and search."""

# fastweb's own capacity is full (every browser tab, or the search queue wait ran out): answered 502 with Retry-After
# because 502 is the one status firecrawl-py's v1 and v2 clients retry with backoff. Every other failure is a status
# the SDK does not retry (see docs/design.md, "Status codes").
BUSY_STATUS = 502


def describe(e: BaseException) -> str:
    """A log- and caller-safe description of an exception: its class name, plus the status code of an HTTP error.

    Never str(e): httpx and URL-parsing errors quote the full request URL, which can carry a search query or a
    page's query string."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"{type(e).__name__}: {status}" if isinstance(status, int) else type(e).__name__


class Busy(Exception):
    """The in-flight byte budget stayed full: a later tier (the browser or Firecrawl) renders the page instead."""


class Fail(Exception):
    """A final answer for the caller: HTTP `status` with `error` as the message.

    target_side: the target itself refused (not our timeout, budget or a Firecrawl outage); such refusals count
    towards marking a host DEAD. transient: says nothing lasting about the page (a net::ERR_* or a truncated budget),
    so it is not remembered. retry_after: seconds for a Retry-After header (a capacity answer worth retrying).
    """

    def __init__(self, status: int, error: str, target_side: bool = False, transient: bool = False,
                 retry_after: int | None = None):
        super().__init__(error)
        self.status, self.error, self.target_side, self.transient = status, error, target_side, transient
        self.retry_after = retry_after
