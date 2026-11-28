# gtt-web-scraper

A self-hosted service that turns a URL into clean markdown for Garrett's agents, trying free ways of getting the page before paid ones and refusing to pass off a block page or empty shell as real content.

## Language

### Getting a page

**Fetcher**:
One way of getting a page, such as a plain download, a real browser, or the paid fallback service.
_Avoid_: tier, method, rung, backend

**Engine**:
The browser software a browser-based Fetcher drives. An Engine lives inside a Fetcher; it is never a step of its own.
_Avoid_: using "engine" for any Fetcher

**Ladder**:
The ordered list of Fetchers, cheapest first, that a request climbs.
_Avoid_: chain, pipeline, fallback list

**Escalation**:
Moving a request to the next Fetcher on the Ladder because the current Fetcher's result was rejected.
_Avoid_: retry (a retry repeats the same Fetcher)

**Paid Fetcher**:
The last Fetcher on the Ladder, backed by the external paid service. When its quota is exhausted, it is unavailable and the request fails rather than falling further. It is never used for search.

**Site**:
A URL's host name. Rate limits, Cool-downs and Per-site memory are kept per Site. Not to be confused with a Site Adapter.

**Home connection**:
The internet connection the service itself fetches over (Garrett's home address). Fetchers that use it, such as the plain download, are spaced per Site and respect Cool-downs; the Paid Fetcher reaches sites from the paid service's network instead.

**Cool-down**:
A period after a Site blocked the Home connection (403, 429 or a challenge) during which Fetchers that use the Home connection skip that Site, so requests go straight to the Paid Fetcher.
_Avoid_: ban, backoff

**Per-site memory**:
The Fetcher that last succeeded on a Site, which the next request to that Site tries first. It is trusted only for a while after it first succeeded, then the Ladder is climbed from the bottom again.

**Site Adapter**:
A route that catches URLs from a specific site and gets the content through that site's official feed or API instead of the Ladder.
_Avoid_: plugin, special case, scraper rule

### Searching

**Search Provider**:
One external search service that answers a query with a ranked list of results. Providers are tried in order; a failing Provider passes the query to the next. A Provider may itself ask several **upstream search services** (our SearXNG asks Google, Bing, DuckDuckGo and Brave); those are neither Providers nor Engines.
_Avoid_: search engine, search backend

**Source List**:
The domains the service trusts (boosted in search results) and blocks (dropped from them). It shapes which sources Callers see; it does not judge whether any claim is true.
_Avoid_: allowlist, whitelist, reputation list

**Published date**:
When a page's content was published or last updated, according to the page's own metadata, attached to every search result and fetched page, or marked unknown.

### Judging a page

**Content Gate**:
The check every Fetcher's result passes through before it can be returned, which decides whether it is real page content or a block page, challenge, cookie wall, empty app shell, or disguised error.
_Avoid_: completeness check, validator, quality check

**Verdict**:
The Content Gate's decision on one result: **accepted** (real content), **suspect** (passed, but thin enough to doubt), or **rejected** (not real content; causes Escalation).

**Possibly partial**:
The confidence a Caller sees when the best result available was suspect, not accepted. A rejected result is never returned as content.

### Keeping it healthy

**Canary Set**:
A fixed list of trusted, content-rich URLs with known expected Verdicts, run daily and before every deploy, whose failures are how the system learns something broke.
_Avoid_: smoke test list, test URLs

**Incident**:
A detected degradation (a Canary failure, a Fetcher or Search Provider failing repeatedly, or quota running out) that the service reports with evidence so an agent can fix it.
_Avoid_: alert, error

### Who asks

**Caller**:
An agent or script that asks the service for a page or a search, such as a Claude Code session or Hermes.
_Avoid_: client, user, consumer
