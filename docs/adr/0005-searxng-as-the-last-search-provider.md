# SearXNG is the last Search Provider

The parent spec (#1) put "SearXNG as Search Providers" out of scope, because a SearXNG instance scrapes other search services from the Home connection, which risks those services flagging Garrett's home address. Garrett reversed that on 2026-11-27 (ticket #20). The free-tier Providers (TinyFish, Exa, Tavily) can all run out or fail at once, and DuckDuckGo alone, the only keyless one, rate-limits easily; then search fails outright. A self-hosted SearXNG asks several upstream search services at once and keeps answering when one of them blocks it, at no cost and with no key. At this service's volume (tens of searches a day, plus the Canary Set's few) the risk to the Home connection is low, and SearXNG is only asked once every other Provider has failed.

So a SearXNG container runs on pi5 next to the scraper, and `searxng` is last in `search.providers`, after DuckDuckGo. This line of the spec's Out of Scope no longer holds for SearXNG; Brave as a keyed Provider, Gemini grounding and Firecrawl for search stay out.

## Consequences

- It is a container on the compose network with no published port: only the scraper and the candidate reach it (`http://searxng:8080`). It keeps no state, so it has no volume.
- Its image is pinned and its settings are committed (`deploy/pi5/searxng/settings.yml`): JSON output on, four upstream search services (Google, Bing, DuckDuckGo, Brave), the limiter off because the scraper is its only client. A SearXNG upgrade is a reviewed change to the image line, not something a pull brings in.
- It takes half a core and 512 MB, so the live service, the candidate and SearXNG together stay within 5 of the Pi's 16 GB.
- Its `secret_key` is committed. It signs image-proxy links and preference cookies, neither of which is used here, so it guards nothing.
- Like every Provider it has a success rate and is asked the Canary Set's searches, so upstream services that stop answering raise an Incident. When every one behind it fails, it reports which, instead of an empty answer.
- The Canary Set's searches now also reach Google, Bing, DuckDuckGo and Brave through SearXNG each day and on each deploy. If one starts blocking the Home connection for long, drop it from the settings rather than add more.
