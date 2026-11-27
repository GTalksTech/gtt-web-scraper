#!/usr/bin/env bash
#
# Checks the scraper on pi5 from this Mac, over Tailscale: it answers on the
# tailnet, a real page comes back as markdown, the MCP endpoint Claude Code
# connects to offers its fetch tool, and (given pi5's home-LAN
# address) it does NOT answer on the LAN.
#
#   deploy/pi5/verify-from-mac.sh [LAN_IP]
#
# PI_HOST (default pi5) and PI_PORT (default 3002) can be overridden in the environment.

set -euo pipefail

HOST="${PI_HOST:-pi5}"
PORT="${PI_PORT:-3002}"
LAN_IP="${1:-}"
URL="https://docs.python.org/3/library/sqlite3.html"

ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=1; }
FAILED=0

echo "Checking http://$HOST:$PORT over Tailscale"

if health=$(curl -fsS --max-time 10 "http://$HOST:$PORT/health"); then
  ok "health: $health"
else
  bad "no answer from http://$HOST:$PORT/health (is Tailscale connected here? is the service up on pi5?)"
fi

body=$(curl -sS --max-time 90 "http://$HOST:$PORT/v1/scrape" \
  -H 'content-type: application/json' -d "{\"url\": \"$URL\"}" || true)
if jq -e '.success == true and (.data.markdown | length) > 1000' <<< "$body" > /dev/null 2>&1; then
  ok "scrape returned $(jq -r '.data.markdown | length' <<< "$body") chars of markdown" \
     "(fetcher: $(jq -r .data.fetcher <<< "$body"), verdict: $(jq -r .data.verdict <<< "$body"))"
  # sed reads all of jq's output; `head` would close the pipe early and, under
  # pipefail, end the script with SIGPIPE before the remaining checks.
  jq -r '.data.markdown' <<< "$body" | sed -n '1,5s/^/          /p'
else
  bad "scrape of $URL did not return markdown: $(head -c 400 <<< "$body")"
fi

# The MCP endpoint is stateless, so one JSON-RPC call lists its tools.
tools=$(curl -sS --max-time 10 "http://$HOST:$PORT/mcp" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}' || true)
if jq -e '[.result.tools[].name] | index("fetch")' <<< "$tools" > /dev/null 2>&1; then
  ok "MCP at http://$HOST:$PORT/mcp offers: $(jq -r '[.result.tools[].name] | join(", ")' <<< "$tools")"
else
  bad "MCP at http://$HOST:$PORT/mcp did not list a fetch tool: $(head -c 400 <<< "$tools")"
fi

if [[ -n "$LAN_IP" ]]; then
  if curl -fsS --max-time 5 "http://$LAN_IP:$PORT/health" > /dev/null 2>&1; then
    bad "it ALSO answers on the LAN at $LAN_IP:$PORT; it must be tailnet only"
  else
    ok "no answer on the LAN at $LAN_IP:$PORT (tailnet only)"
  fi
else
  echo "  (pass pi5's LAN IP as an argument to also check it isn't reachable there)"
fi

echo
if (( FAILED )); then echo "Something's wrong; see deploy/pi5/README.md, Troubleshooting."; exit 1; fi
echo "pi5 is serving the scraper over Tailscale."
