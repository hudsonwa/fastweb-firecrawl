# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. You can expect an acknowledgement within a week.
Please include a reproduction (a URL pattern, redirect chain or page that gets past a guard is ideal).

## Supported versions

Only the latest release receives fixes.

## Threat model

fastweb-firecrawl fetches URLs chosen by its callers, follows redirects, and renders pages in Chrome. It is designed to run on
a trusted host for trusted callers (for example AI agents you operate). The README's "Security model" section
describes the guards. In scope:

- reaching a private, loopback, link-local or otherwise internal address (directly, by redirect, by DNS rebinding,
  or from JavaScript, workers, iframes, WebSocket or WebRTC inside the rendered page);
- escaping the browser container's network boundary (`browsernet` + `browser-egress`), or the optional Firecrawl
  profile's (`firecrawlnet` + `firecrawl-egress`);
- denial of service that exceeds the documented bounds (memory budget, queue sizes, timeouts);
- leaking query strings or search queries into logs.

Out of scope: returned content is the target page's. `markdown`, `html`, `rawHtml` and `links` are page-controlled
(a `<base href>` can point links at internal hosts; fastweb-firecrawl never fetches them, and a later scrape of one goes
through the guards). fastweb-firecrawl replaces link targets whose scheme is not `http`, `https`, `mailto` or `tel` with `#`
in every markdown answer (inline, image, reference-definition (also in block quotes and list items), autolink and
raw-HTML `href`/`src` forms with any quoting, after decoding entities and escapes; fastweb-firecrawl's own and the Firecrawl
fallback's markdown) and keeps `links` to `http(s)`. That is defence in depth, not a sanitiser: other raw HTML in
markdown (tags, event-handler attributes) is passed through, and `html`/`rawHtml` are the page as fetched. Clients
that render any of them as HTML must sanitise it.
There is no authentication by design (ports are bound to 127.0.0.1); anyone who can reach the API can
use it to fetch public pages.

## Known residuals

- **The Firecrawl fallback is outside fastweb-firecrawl's own SSRF checks.** fastweb-firecrawl checks the URL it hands to Firecrawl and
  the URL Firecrawl ended on, but Firecrawl follows redirects, loads subresources and runs page JavaScript itself. In
  the compose `firecrawl` profile that is contained by the network: Firecrawl's containers sit on an internal
  network with only their own Redis, RabbitMQ and Postgres, fastweb-firecrawl (its client) and `firecrawl-egress`, which
  refuses private addresses like `browser-egress`. Each container bypasses the proxy only for itself (`localhost`), and
  `api` also for `playwright-service`, which it sends render jobs; so a page cannot send HTTP to Firecrawl's API,
  Redis, RabbitMQ or Postgres through it. A request `api` makes for a page can still reach `playwright-service` (a
  renderer whose own browser goes through the proxy), but not fastweb-firecrawl, its Redis, SearXNG, the browser, the Docker
  host or your LAN.
  `firecrawl-egress` is the enforcing guard there. `playwright-service` runs with `ALLOW_LOCAL_WEBHOOKS=true`, which
  turns off the image's own DNS-based pre-check: on the gateway-less network every lookup fails and that check treats
  a failed lookup as "internal", so it would refuse every page. Its Chrome still has no route off `firecrawlnet`
  except the proxy, which resolves each host itself and refuses private addresses. `playwright-service` lists
  `firecrawl-egress` in `NO_PROXY` so that Node's environment proxy does not proxy its in-process proxy's hop to
  the egress a second time; that adds no route. `scripts/smoke-firecrawl.sh` checks, through `api` and straight at
  `playwright-service`, that a JavaScript page renders over `http://` and `https://`, that the metadata address
  is refused as a private address, and that a real 302 to that address, from a redirector it runs in its own
  throwaway project and followed by playwright-service's Chrome, is refused at the proxy. Within
  `firecrawlnet` two residual paths remain, because this third-party image does not get the `browser` service's
  Chrome flags: page JavaScript can send WebRTC UDP to `firecrawlnet` peers (none listens on UDP), and Chromium
  never proxies loopback, so a page can reach the service's own `localhost:3000` (whose renders go back through the
  proxy).
- **The fastweb-firecrawl API is reachable from both sandboxed networks.** fastweb-firecrawl sits on `browsernet` and `firecrawlnet`
  because it drives the browser and calls Firecrawl, so a compromised Chrome or Firecrawl can send requests to its
  API like any client. The API has no privileged endpoints, every scrape goes through the same SSRF guard, and the
  container is read-only, capability-free and non-root.
  If you point `FIRECRAWL_UPSTREAM` at a Firecrawl deployed some other way, its network access is yours to contain.

- Chrome runs with `--no-sandbox` (an unprivileged container cannot provide its sandbox). A Chrome exploit would
  land in a non-root, read-only, capability-free container whose only network route is the egress proxy.
- JavaScript on a rendered page can learn the proxy's per-start token that marks proxy refusals, so a hostile page
  can make its own render look like a proxy failure. The worst outcome is that its own page is handed to the
  Firecrawl fallback.
- For a name that rebinds between lookups, the cheap tiers' error message can still distinguish "private address"
  from "connection refused".
