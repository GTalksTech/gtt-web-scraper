#!/usr/bin/env bash
#
# Sends the Incidents waiting in incidents.jsonl to GitHub, each as one
# repository dispatch event of type `incident`, where the incident workflow
# turns it into an issue (ticket #10). The Pi only calls out; nothing calls in.
#
# Run at the end of every deploy pass (gtt-up.sh, every 10 minutes) and every
# health run (gtt-canary.sh), so an Incident that couldn't be sent (no
# internet, GitHub down) is tried again within 10 minutes. Sent ones move to
# incidents-sent.jsonl; a line that isn't an Incident at all is set aside in
# incidents-unsendable.jsonl instead of blocking the rest.
#
# Uses GITHUB_DISPATCH_TOKEN from the wizard's .env: a fine-grained token for
# this repo only. It's passed to curl in a header file, never on the command line.
#
# By hand: sudo ~/gtt-web-scraper/gtt-report.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

OUTBOX="$PWD/incidents.jsonl"
SENDING="$PWD/.incidents-sending"   # the batch being sent (survives a crash)
SENT="$PWD/incidents-sent.jsonl"
UNSENDABLE="$PWD/incidents-unsendable.jsonl"
REPO="${GTT_REPO:-GTalksTech/gtt-web-scraper}"
API="${GTT_GITHUB_API:-https://api.github.com}"
MAX_PAYLOAD=60000   # GitHub refuses a client_payload over 65,535 characters

log() { printf '%s\n' "$*"; }

[[ -s "$OUTBOX" || -s "$SENDING" ]] || exit 0

# The wizard writes KEY='value'; accept KEY=value and KEY="value" too.
token="$(sed -n 's/^GITHUB_DISPATCH_TOKEN=//p' .env 2> /dev/null | tail -n1)"
token="${token#[\'\"]}"; token="${token%[\'\"]}"
if [[ -z "$token" ]]; then
  log "no GITHUB_DISPATCH_TOKEN in .env: Incidents wait in $OUTBOX (run the setup wizard)"
  exit 0
fi

# Take the outbox as one batch; anything written meanwhile starts a new outbox.
if [[ -s "$OUTBOX" ]]; then
  cat "$OUTBOX" >> "$SENDING"
  rm -f "$OUTBOX"
fi
keep="$(mktemp)"
trap 'rm -f "$keep"' EXIT

send() {  # $1: the request body. Prints the HTTP status (000 if GitHub wasn't reached).
  curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 30 \
    -X POST "$API/repos/$REPO/dispatches" \
    -H @<(printf 'Authorization: Bearer %s\n' "$token") \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    -H 'Content-Type: application/json' \
    --data-binary @- <<< "$1" 2> /dev/null || true
}

sent=0 offline=0
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -n "${line//[[:space:]]/}" ]] || continue
  if (( offline )); then echo "$line" >> "$keep"; continue; fi
  if ! payload="$(jq -ce 'select(type == "object" and (.fingerprint | type) == "string"
                               and (.kind | type) == "string" and (.effect | type) == "string")
                          | {event_type: "incident", client_payload: .}' <<< "$line" 2> /dev/null)"; then
    echo "$line" >> "$UNSENDABLE"
    log "set aside a line that isn't an Incident (see $UNSENDABLE)"
    continue
  fi
  if (( ${#payload} > MAX_PAYLOAD )); then
    payload="$(jq -c --argjson n $((MAX_PAYLOAD / 2)) \
      '.client_payload.evidence |= {cutShort: "the full evidence is in incidents-sent.jsonl on pi5", start: (tojson | .[0:$n])}' \
      <<< "$payload")"
  fi
  fingerprint="$(jq -r .client_payload.fingerprint <<< "$payload")"
  code="$(send "$payload")"
  case "$code" in
    204)
      echo "$line" >> "$SENT"; sent=$((sent + 1))
      log "sent Incident $fingerprint to GitHub" ;;
    401 | 403 | 404)
      echo "$line" >> "$keep"; offline=1
      log "error: GitHub refused the dispatch token (HTTP $code). It may have expired:" \
          "make a new one with the setup wizard. Incidents wait in $OUTBOX" ;;
    422)
      echo "$line" >> "$UNSENDABLE"
      log "error: GitHub rejected Incident $fingerprint as invalid (HTTP 422); set aside in $UNSENDABLE" ;;
    000 | 5?? | 429)
      echo "$line" >> "$keep"; offline=1
      log "warning: couldn't reach GitHub (HTTP $code); Incidents wait in $OUTBOX for the next run" ;;
    *)
      echo "$line" >> "$keep"
      log "warning: GitHub didn't take Incident $fingerprint (HTTP $code); it waits in $OUTBOX" ;;
  esac
done < "$SENDING"

# Unsent ones go back to the front of the outbox, before anything new.
if [[ -s "$keep" ]]; then
  [[ -e "$OUTBOX" ]] && cat "$OUTBOX" >> "$keep"
  cp "$keep" "$OUTBOX"
fi
rm -f "$SENDING"
(( sent == 0 )) || log "sent $sent Incident(s)"
[[ ! -s "$OUTBOX" ]]
