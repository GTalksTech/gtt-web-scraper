#!/usr/bin/env bash
#
# One health run on pi5: the Canary Set against the live service, plus its
# Fetchers' rolling success rates. Any Incident found is appended to
# incidents.jsonl (one JSON object per line) for the Incident pipeline; the
# run's account goes to the journal. Run daily by gtt-canary.timer, under the
# deploy lock so it never overlaps a deploy.
#
# On demand: sudo systemctl start gtt-canary   (then journalctl -u gtt-canary)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

OUTBOX="$PWD/incidents.jsonl"
EFFECT="the service isn't answering (the Canary Set couldn't run)"

if ! docker exec gtt-web-scraper python -m scraper.health canary >> "$OUTBOX"; then
  # The runner never ran, so it couldn't say so itself. Same fingerprint as the
  # runner's own "isn't answering" Incident: it's the same problem.
  printf '{"effect": "%s", "evidence": {"container": "gtt-web-scraper"}, "fingerprint": "canary_widespread", "kind": "canary_widespread"}\n' \
    "$EFFECT" >> "$OUTBOX"
  echo "Incident canary_widespread: $EFFECT"
  exit 1
fi
