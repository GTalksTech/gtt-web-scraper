# Single-page fetches act as a browser and do not consult robots.txt

Every Caller request is a targeted read of one page chosen by an agent working for a person (Hermes's radar and research jobs read tens of hand-picked pages a day), not a crawl that discovers pages by following links. robots.txt exists to govern crawlers, so single-page fetches present as a normal browser and skip it, matching how Firecrawl itself treats single scrapes (it checks robots.txt only for crawls). Politeness is enforced instead by per-site rate limits and cool-downs, which also protect the home connection.

## Consequences

- If a crawl or bulk-discovery feature is ever added, it must obey robots.txt and identify itself. This ADR covers single-page fetches only.
- Fetchers may imitate a real browser's network signature. That is deliberate, not an oversight to "fix".
