#!/usr/bin/env bash
#
# One deploy pass on pi5: pull the newest image from GHCR and run it, published
# on the Pi's Tailscale IP only. systemd runs it at boot (gtt-web-scraper.service)
# and every 10 minutes (gtt-web-scraper-update.timer), always under one lock.
# The Pi pulls; nothing ever pushes into the home network.
#
# Safe to run by hand: sudo ~/gtt-web-scraper/gtt-up.sh
#
# Exits non-zero if the tailnet IP never appears, or if the pull failed (the
# image already here is still brought up, so the service stays available).
#
# Not yet: running the Canary Set against a new image before it takes traffic
# (spec #1, deploy). Today a new image replaces the old one directly.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CONTAINER=gtt-web-scraper
WAIT_S="${TAILNET_WAIT_S:-120}"   # at boot, tailscaled may still be connecting

log() { printf '%s\n' "$*"; }

tailnet_ip() { tailscale ip -4 2>/dev/null | head -n1; }
image_id() { docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true; }

deadline=$((SECONDS + WAIT_S))
until ip="$(tailnet_ip)" && [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; do
  if (( SECONDS >= deadline )); then
    log "error: no Tailscale IPv4 after ${WAIT_S}s; not starting (it must never bind elsewhere)"
    exit 1
  fi
  sleep 2
done
export TAILNET_IP="$ip"

before="$(image_id)"
pull_ok=1
if ! docker compose pull --quiet; then
  log "warning: pulling from GHCR failed; running the image already on the Pi"
  pull_ok=0
fi
docker compose up -d --remove-orphans
after="$(image_id)"
if [[ "$after" != "$before" ]]; then
  log "deployed $after (was ${before:-nothing}) on $TAILNET_IP:3002"
fi

# Docker restarts a container that exits, not one that hangs. A hang shows up
# as a failing healthcheck, so restart it here.
health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$health" == unhealthy ]]; then
  log "container unhealthy: restarting it"
  docker compose restart scraper
fi

# Replaced images become dangling; tagged ones (a pinned IMAGE_TAG) are kept.
docker image prune -f > /dev/null || true

(( pull_ok )) || exit 1
