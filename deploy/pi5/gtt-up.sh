#!/usr/bin/env bash
#
# One deploy pass on pi5: pull the newest image from GHCR and, if it passes the
# Canary Set at least as well as the live service, run it, published on the
# Pi's Tailscale IP only. systemd runs it at boot (gtt-web-scraper.service) and
# every 10 minutes (gtt-web-scraper-update.timer), always under one lock.
# The Pi pulls; nothing ever pushes into the home network.
#
# Safe to run by hand: sudo ~/gtt-web-scraper/gtt-up.sh
#
# A new image is first started as the candidate (compose.yml: no port, no
# volume) next to the live service, and the Canary Set is run against both from
# inside it. It's promoted only if it passes at least as many Canary pages.
# Otherwise the live image keeps serving, the new one is remembered as kept out
# (.rejected-images, so it isn't re-tried every 10 minutes) and an Incident is
# written to incidents.jsonl for the Incident pipeline. With nothing running yet
# (first install) there's nothing to compare against, and the image just starts.
# GTT_DEPLOY_UNGATED=1 skips the Canary Set (e.g. a rollback you chose).
#
# Exits non-zero if the service isn't up at the end (no tailnet IP, or Docker
# couldn't start it), so the boot unit retries. A failed pull only warns: the
# image already here keeps serving, and the next timer run tries again.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CONTAINER=gtt-web-scraper
CANDIDATE_TAG=gtt-web-scraper:candidate   # compose.yml's candidate image
REJECTED_TAG=gtt-web-scraper:kept-out     # the last image the Canary Set kept out
WAIT_S="${TAILNET_WAIT_S:-120}"   # at boot, tailscaled may still be connecting
OUTBOX="$PWD/incidents.jsonl"     # Incidents, one JSON object per line
REJECTED="$PWD/.rejected-images"  # image IDs the Canary Set kept out

log() { printf '%s\n' "$*"; }

tailnet_ip() { tailscale ip -4 2>/dev/null | head -n1; }
image_id() { docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true; }
tagged_id() { docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

# An Incident the runner couldn't write itself (the candidate never ran it).
deploy_incident() {
  printf '{"effect": "a new version was kept out: %s", "evidence": {"candidateImage": "%s"}, "fingerprint": "deploy_rejected", "kind": "deploy_rejected"}\n' \
    "$2" "$1" >> "$OUTBOX"
  log "Incident deploy_rejected: a new version was kept out: $2"
}

candidate() { docker compose --profile candidate "$@"; }

# Runs the Canary Set against the live service and image $1; true if $1 may
# take traffic. Incidents from the run go to the outbox.
passes_canary() {
  local new="$1" rc=0
  log "checking $new against the Canary Set before it takes traffic"
  candidate rm -sf candidate > /dev/null 2>&1 || true
  docker tag "$new" "$CANDIDATE_TAG"
  if ! candidate up -d --no-deps candidate; then
    deploy_incident "$new" "it wouldn't start"
    return 1
  fi
  docker exec gtt-web-scraper-candidate python -m scraper.health deploy \
    --live http://scraper:3002 --candidate http://127.0.0.1:3002 \
    --candidate-image "$new" >> "$OUTBOX" || rc=$?
  candidate rm -sf candidate > /dev/null 2>&1 || true
  docker image rm "$CANDIDATE_TAG" > /dev/null 2>&1 || true
  case "$rc" in
    0) return 0 ;;
    3) return 1 ;;  # the runner wrote the Incident
    *) deploy_incident "$new" "the Canary Set couldn't run in it (exit $rc)"; return 1 ;;
  esac
}

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
ref="$(docker compose config --images scraper)"
if ! docker compose pull --quiet; then
  log "warning: pulling from GHCR failed; running the image already on the Pi"
fi
pulled="$(tagged_id "$ref")"

if [[ -n "$before" && -n "$pulled" && "$pulled" != "$before" && -z "${GTT_DEPLOY_UNGATED:-}" ]]; then
  if grep -qxF "$pulled" "$REJECTED" 2> /dev/null; then
    keep=1  # already kept out once; no need to run the Canary Set again
  elif passes_canary "$pulled"; then
    keep=0
  else
    keep=1
    echo "$pulled" >> "$REJECTED"
  fi
  if (( keep )); then
    log "kept out $pulled: it did worse on the Canary Set; $before keeps serving"
    # Keep it tagged so the prune below leaves it: the next pull then finds it
    # here instead of downloading it again every 10 minutes.
    docker tag "$pulled" "$REJECTED_TAG"
    # Point the tag back at the live image, so compose leaves it running.
    docker tag "$before" "$ref"
  fi
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
# Only this project's images (CI labels them), never another project's on pi5.
docker image prune -f --filter label=org.opencontainers.image.source=https://github.com/GTalksTech/gtt-web-scraper > /dev/null || true
