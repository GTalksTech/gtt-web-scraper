# Callers reach the service through a Firecrawl-compatible API

Existing Callers (Hermes first) already speak Firecrawl's scrape and search API, so the service answers the subset of that API they use, and switching a Caller over or back is a one-line change of its server address. A native MCP interface is offered alongside for Claude Code, carrying the extra detail (Fetcher used, Verdict, confidence). Only the slice of Firecrawl's API that Callers actually use is matched, not the whole surface.
