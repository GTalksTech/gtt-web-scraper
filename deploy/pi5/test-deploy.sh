#!/usr/bin/env bash
#
# Tests for the pi5 deployment (tickets #8, #9). Never touches pi5, the tailnet or
# GitHub: docker, tailscale and systemctl are faked, except that the real
# `docker compose config` checks the compose file (it needs no daemon).
#
#   deploy/pi5/test-deploy.sh
#
# Asserts on what pi5 would end up with: where the port is published, the
# limits, which docker commands a deploy runs, and what the installer writes.

# The fakes are single-quoted on purpose: they expand when they run.
# shellcheck disable=SC2016

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

FAILS=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }
fails() { ! "$@"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }

# A copy of the deploy dir as it sits on pi5 (~/gtt-web-scraper), with the
# wizard's env file next to it.
PI="$T/home/garrett/gtt-web-scraper"
mkdir -p "$PI" "$T/etc/systemd/system" "$T/dev" "$T/cgroup"
cp -R "$HERE/." "$PI/"
printf "FIRECRAWL_API_KEY='fc-test'\n" > "$PI/.env"

# ── compose file ───────────────────────────────────────────────────────────
echo "compose file"
compose_json() { (cd "$PI" && TAILNET_IP=100.64.0.5 docker compose -f compose.yml config --format json); }
C="$(compose_json)"
q() { jq -r "$1" <<< "$C"; }

check "publishes port 3002 on the tailnet IP only" \
  test "$(q '[.services.scraper.ports[] | "\(.host_ip):\(.published):\(.target)"] | join(",")')" = "100.64.0.5:3002:3002"
check "pulls the GHCR image built from main" \
  test "$(q .services.scraper.image)" = "ghcr.io/gtalkstech/gtt-web-scraper:main"
check "IMAGE_TAG pins another version" \
  test "$(cd "$PI" && TAILNET_IP=100.64.0.5 IMAGE_TAG=sha-abc1234 docker compose -f compose.yml config --format json | jq -r .services.scraper.image)" \
       = "ghcr.io/gtalkstech/gtt-web-scraper:sha-abc1234"
check "never builds on the Pi" test "$(q '.services.scraper.build // "none"')" = "none"
check "restarts unless stopped" test "$(q .services.scraper.restart)" = "unless-stopped"
mem=$(q .services.scraper.mem_limit)
check "memory capped at 2 GiB or less (of 16)" test "$mem" -gt 0 -a "$mem" -le $((2 * 1024 * 1024 * 1024))
check "no swap beyond the memory cap" test "$(q .services.scraper.memswap_limit)" = "$mem"
check "CPU capped at 2 of the Pi's 4 cores" \
  python3 -c "import sys; c=float(sys.argv[1]); sys.exit(not 0 < c <= 2)" "$(q .services.scraper.cpus)"
check "process count capped" test "$(q '.services.scraper.pids_limit // 0')" -gt 0
check "OOM killer picks the scraper before tailscaled" test "$(q '.services.scraper.oom_score_adj // 0')" -gt 0
check "logs rotate (json-file, size-limited)" \
  test "$(q '.services.scraper.logging | "\(.driver) \(.options["max-size"]) \(.options["max-file"])"')" = "json-file 10m 3"
check "has a healthcheck" test "$(q '.services.scraper.healthcheck.test | length')" -gt 0
check "passes the wizard's env file to the service" test "$(q .services.scraper.environment.FIRECRAWL_API_KEY)" = "fc-test"
check "page cache dir /app/data is a named volume (survives restarts and image updates)" \
  test "$(q '[.services.scraper.volumes[]? | select(.target == "/app/data") | "\(.type):\(.source)"] | join(",")')" = "volume:cache"
check "  declared as a project volume, not an external or host path" \
  test "$(q '.volumes.cache.name // "missing"')" = "gtt-web-scraper_cache"
check "image makes /app/data writable by the service user" \
  grep -q "chown scraper:scraper /app/data" "$HERE/../../Dockerfile"
check "refuses to start without TAILNET_IP (never binds 0.0.0.0)" \
  fails env -u TAILNET_IP docker compose -f "$PI/compose.yml" config -q 2> /dev/null

echo "compose file: the candidate (a new image on trial)"
check "not started by a plain deploy" test "$(q '.services.candidate // "absent"')" = "absent"
C="$(cd "$PI" && TAILNET_IP=100.64.0.5 docker compose -f compose.yml --profile candidate config --format json)"
check "runs the locally tagged candidate, never pulled" \
  test "$(q '"\(.services.candidate.image) \(.services.candidate.pull_policy)"')" = "gtt-web-scraper:candidate never"
check "publishes no port: it never takes traffic" test "$(q '.services.candidate.ports // [] | length')" = 0
check "has no volume: never touches the live cache" test "$(q '.services.candidate.volumes // [] | length')" = 0
check "on the live service's network, so it can reach http://scraper:3002" \
  test "$(q '.services.candidate.networks | keys | join(",")')" = "default"
check "memory capped at 1 GiB, so live + candidate leave tailscaled room" \
  test "$(q .services.candidate.mem_limit)" -le $((1024 * 1024 * 1024))
check "  no swap beyond it" test "$(q .services.candidate.memswap_limit)" = "$(q .services.candidate.mem_limit)"
check "CPU capped at 1 core" test "$(q .services.candidate.cpus)" = "1"
check "OOM killer picks the candidate first" \
  test "$(q .services.candidate.oom_score_adj)" -gt "$(q .services.scraper.oom_score_adj)"
check "gets the wizard's env file too" test "$(q .services.candidate.environment.FIRECRAWL_API_KEY)" = "fc-test"

# ── fakes for gtt-up.sh and install.sh ─────────────────────────────────────
FAKE="$T/fake"; BIN="$T/bin"; mkdir -p "$FAKE" "$BIN"
fake() { printf '#!/usr/bin/env bash\nFAKE=%q\n%s\n' "$FAKE" "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

# tailscale: prints the IP in $FAKE/ip (none if the file is missing).
fake tailscale '[[ "$*" == "ip -4" ]] && cat "$FAKE/ip" 2>/dev/null; true'
# docker: logs each call with the TAILNET_IP it saw. The live container runs the
# image in $FAKE/running; the local :main tag points at $FAKE/tagged, which a
# pull sets to $FAKE/remote unless $FAKE/pull_fails exists. The candidate's
# Canary run exits with $FAKE/canary_rc (default 0, promote; 3 is kept out).
fake docker 'printf "%s|TAILNET_IP=%s\n" "$*" "${TAILNET_IP:-}" >> "$FAKE/docker.log"
MAIN=ghcr.io/gtalkstech/gtt-web-scraper:main
case "$*" in
  "compose config --images scraper") echo "$MAIN" ;;
  "compose pull"*) [[ -e "$FAKE/pull_fails" ]] && exit 1; cp "$FAKE/remote" "$FAKE/tagged" ;;
  "compose --profile candidate up"*) [[ -e "$FAKE/candidate_fails" ]] && exit 1; true ;;
  "compose --profile candidate"*) true ;;
  "compose up"*)   [[ -e "$FAKE/up_fails" ]] && exit 1; [[ -e "$FAKE/tagged" ]] && cp "$FAKE/tagged" "$FAKE/running"; true ;;
  "image inspect --format {{.Id}} $MAIN") cat "$FAKE/tagged" 2>/dev/null || exit 1 ;;
  "tag "*" $MAIN") echo "$2" > "$FAKE/tagged" ;;
  "exec gtt-web-scraper-candidate python -m scraper.health deploy"*)
    rc=$(cat "$FAKE/canary_rc" 2>/dev/null || echo 0)
    [[ $rc == 3 ]] && echo "{\"fingerprint\": \"deploy_rejected\", \"kind\": \"deploy_rejected\"}"
    exit "$rc" ;;
  "exec gtt-web-scraper python -m scraper.health canary")
    [[ -e "$FAKE/live_down" ]] && exit 1
    [[ -e "$FAKE/canary_incident" ]] && echo "{\"fingerprint\": \"canary_failed:lwn.net\", \"kind\": \"canary_failed\"}"
    true ;;
  "inspect --format {{.Image}}"*) cat "$FAKE/running" 2>/dev/null || exit 1 ;;
  "inspect --format"*Health*) cat "$FAKE/health" 2>/dev/null; true ;;
  "compose version"*) echo "Docker Compose version v2.30.0" ;;
  "port"*) echo "3002/tcp -> $(cat "$FAKE/ip"):3002" ;;
  *) true ;;
esac'
fake sleep 'true'
fake systemctl 'printf "%s\n" "$*" >> "$FAKE/systemctl.log"
[[ "$*" == "show --property=RuntimeWatchdogUSec"* ]] && echo "RuntimeWatchdogUSec=15s"
[[ "$*" == "is-active"* ]] && exit 3
[[ "$*" == "enable --now"* && -e "$FAKE/start_fails" ]] && exit 1; true'

reset_fakes() {
  rm -f "$FAKE"/* "$PI/incidents.jsonl" "$PI/.rejected-images"
  echo 100.64.0.5 > "$FAKE/ip"; echo sha256:old > "$FAKE/running"; echo sha256:old > "$FAKE/tagged"
  echo sha256:new > "$FAKE/remote"
}
up() { PATH="$BIN:$PATH" TAILNET_WAIT_S="${WAIT:-0}" bash "$PI/gtt-up.sh" > "$FAKE/out.log" 2>&1; }
canary() { PATH="$BIN:$PATH" bash "$PI/gtt-canary.sh" > "$FAKE/out.log" 2>&1; }
incidents() { if [[ -f "$PI/incidents.jsonl" ]]; then wc -l < "$PI/incidents.jsonl" | tr -d ' '; else echo 0; fi; }
valid_jsonl() { jq -e . "$PI/incidents.jsonl" > /dev/null; }
ran() { grep -q "^$1" "$FAKE/docker.log"; }
ran_before() { local a b; a=$(grep -n "^$1" "$FAKE/docker.log" | head -1 | cut -d: -f1); b=$(grep -n "^$2" "$FAKE/docker.log" | head -1 | cut -d: -f1); [[ -n $a && -n $b && $a -lt $b ]]; }
# $2 ran at some point after the first time $1 ran.
ran_after() { local a b; a=$(grep -n "^$1" "$FAKE/docker.log" | head -1 | cut -d: -f1); b=$(grep -n "^$2" "$FAKE/docker.log" | tail -1 | cut -d: -f1); [[ -n $a && -n $b && $a -lt $b ]]; }

# ── gtt-up.sh: one deploy pass ─────────────────────────────────────────────
echo "gtt-up.sh"
reset_fakes
check "a new image deploys" up
check "pulls, then brings the service up" ran_before "compose pull" "compose up -d"
check "compose sees the Pi's tailnet IP" grep -q "^compose up -d.*TAILNET_IP=100.64.0.5$" "$FAKE/docker.log"
check "the new image is running" test "$(cat "$FAKE/running")" = "sha256:new"
check "logs the deploy, old and new image" grep -q "sha256:old.*sha256:new\|sha256:new.*sha256:old" "$FAKE/out.log"
check "prunes only this project's dangling images" \
  grep -q "^image prune -f --filter label=org.opencontainers.image.source=https://github.com/GTalksTech/gtt-web-scraper|" "$FAKE/docker.log"

reset_fakes; echo sha256:new > "$FAKE/running"
up
check "same image: says nothing was deployed" fails grep -q "deployed" "$FAKE/out.log"

reset_fakes; rm "$FAKE/ip"
check "no tailnet IP: fails" fails up
check "no tailnet IP: never starts the service" fails grep -qs "^compose up" "$FAKE/docker.log"

reset_fakes; touch "$FAKE/pull_fails"
check "registry unreachable: still succeeds (the service is up)" up
check "registry unreachable: warns in the journal" grep -q "warning: pulling from GHCR failed" "$FAKE/out.log"
check "registry unreachable: still brings up the image it has" ran "compose up -d"
check "registry unreachable: keeps the old image" test "$(cat "$FAKE/running")" = "sha256:old"

reset_fakes; touch "$FAKE/up_fails"
check "service fails to start (e.g. IP not on tailscale0 yet): fails so systemd retries" fails up

reset_fakes; echo unhealthy > "$FAKE/health"
up
check "hung (unhealthy) container is restarted" ran "compose restart scraper"
reset_fakes; echo healthy > "$FAKE/health"
up
check "healthy container is left alone" fails grep -q "^compose restart" "$FAKE/docker.log"

# ── gtt-up.sh: a new image must pass the Canary Set first ──────────────────
echo "gtt-up.sh: deploys gated by the Canary Set"
reset_fakes
up
check "a new image runs as the candidate before it takes traffic" \
  ran_before "compose --profile candidate up -d --no-deps candidate" "compose up -d"
check "  the candidate is the newly pulled image" ran "tag sha256:new gtt-web-scraper:candidate"
check "  the Canary Set runs in it against the live service and itself" \
  ran "exec gtt-web-scraper-candidate python -m scraper.health deploy --live http://scraper:3002 --candidate http://127.0.0.1:3002 --candidate-image sha256:new|"
check "  and the candidate is removed afterwards" \
  ran_after "exec gtt-web-scraper-candidate" "compose --profile candidate rm -sf candidate"
check "scores at least as well: promoted" test "$(cat "$FAKE/running")" = "sha256:new"
check "  with no Incident" test "$(incidents)" = 0

reset_fakes; echo 3 > "$FAKE/canary_rc"
check "scores worse: the service stays up" up
check "  the old image keeps serving" test "$(cat "$FAKE/running")" = "sha256:old"
check "  the local tag points back at the old image" test "$(cat "$FAKE/tagged")" = "sha256:old"
check "  the runner's Incident is in the outbox" grep -q '"fingerprint": "deploy_rejected"' "$PI/incidents.jsonl"
check "  says so in the journal" grep -q "kept out sha256:new" "$FAKE/out.log"
check "  the kept-out image stays tagged (no re-download every 10 minutes)" ran "tag sha256:new gtt-web-scraper:kept-out"
: > "$FAKE/docker.log"
up
check "the same kept-out image isn't run against the Canary Set again" fails ran "exec gtt-web-scraper-candidate"
check "  nor reported again" test "$(incidents)" = 1
check "  and the old image still serves" test "$(cat "$FAKE/running")" = "sha256:old"
echo sha256:newer > "$FAKE/remote"; rm "$FAKE/canary_rc"
up
check "a later image is checked again, and promoted when it passes" test "$(cat "$FAKE/running")" = "sha256:newer"

reset_fakes; touch "$FAKE/candidate_fails"
up
check "candidate won't start: kept out" test "$(cat "$FAKE/running")" = "sha256:old"
check "  with a valid Incident saying so" valid_jsonl
check "  naming the image" grep -q '"candidateImage": "sha256:new".*"fingerprint": "deploy_rejected"' "$PI/incidents.jsonl"
check "  and why" grep -q "wouldn't start" "$PI/incidents.jsonl"

reset_fakes; echo 1 > "$FAKE/canary_rc"
up
check "Canary run crashes in the candidate: kept out" test "$(cat "$FAKE/running")" = "sha256:old"
check "  with a valid Incident saying so" valid_jsonl
check "  and why" grep -q "couldn't run in it (exit 1)" "$PI/incidents.jsonl"

reset_fakes; rm "$FAKE/running" "$FAKE/tagged"
up
check "first install (nothing live to compare with): the image just starts" test "$(cat "$FAKE/running")" = "sha256:new"
check "  without a candidate" fails ran "compose --profile candidate up"

reset_fakes; echo 3 > "$FAKE/canary_rc"
PATH="$BIN:$PATH" TAILNET_WAIT_S=0 GTT_DEPLOY_UNGATED=1 bash "$PI/gtt-up.sh" > "$FAKE/out.log" 2>&1
check "GTT_DEPLOY_UNGATED=1 deploys without the Canary Set" test "$(cat "$FAKE/running")" = "sha256:new"
check "  without a candidate" fails ran "compose --profile candidate up"

# ── gtt-canary.sh: the daily health run ────────────────────────────────────
echo "gtt-canary.sh"
reset_fakes
check "a healthy run succeeds" canary
check "  runs the health runner in the live container" ran "exec gtt-web-scraper python -m scraper.health canary|"
check "  and writes no Incident" test "$(incidents)" = 0
reset_fakes; touch "$FAKE/canary_incident"
canary
check "the runner's Incidents go to the outbox" grep -q '"fingerprint": "canary_failed:lwn.net"' "$PI/incidents.jsonl"
canary
check "  appended, never overwritten" test "$(incidents)" = 2
reset_fakes; touch "$FAKE/live_down"
check "live container not answering: fails" fails canary
check "  with a valid Incident" valid_jsonl
check "  that the service isn't answering" \
  test "$(jq -r '"\(.fingerprint) \(.effect)"' "$PI/incidents.jsonl")" \
       = "canary_widespread the service isn't answering (the Canary Set couldn't run)"

# ── install.sh ─────────────────────────────────────────────────────────────
echo "install.sh"
install_pi() {
  PATH="$BIN:$PATH" GTT_ETC="$T/etc" GTT_DEV="$T/dev" GTT_CGROUP="$T/cgroup" \
    bash "$PI/install.sh" > "$FAKE/install.log" 2>&1
}
ready_pi() { reset_fakes; echo "cpuset cpu io memory pids" > "$T/cgroup/cgroup.controllers"; touch "$T/dev/watchdog0"; }

ready_pi
check "installs on a ready Pi" install_pi
U="$T/etc/systemd/system"
check "boot unit runs gtt-up.sh from the deploy dir" \
  grep -q "^ExecStart=.*flock .* $PI/gtt-up.sh$" "$U/gtt-web-scraper.service"
check "boot unit waits for docker and tailscaled" \
  grep -q "^After=.*docker.service.*tailscaled.service" "$U/gtt-web-scraper.service"
check "boot unit retries until the service is up" grep -qx "Restart=on-failure" "$U/gtt-web-scraper.service"
check "boot unit can stop the service without TAILNET_IP set" \
  grep -qx "ExecStop=/usr/bin/env TAILNET_IP=127.0.0.1 /usr/bin/docker compose down" "$U/gtt-web-scraper.service"
check "update unit runs gtt-up.sh under the same lock" \
  test "$(grep ^ExecStart= "$U/gtt-web-scraper.service")" = "$(grep ^ExecStart= "$U/gtt-web-scraper-update.service")"
check "timer pulls on a schedule" grep -q "^OnUnitActiveSec=" "$U/gtt-web-scraper-update.timer"
check "Canary unit runs gtt-canary.sh under the deploy lock (never mid-deploy)" \
  grep -q "^ExecStart=.*flock /run/lock/gtt-web-scraper.lock $PI/gtt-canary.sh$" "$U/gtt-canary.service"
check "Canary timer runs daily, catching up after downtime" \
  grep -q "^OnCalendar=\*-\*-\* " "$U/gtt-canary.timer"
check "  persistent" grep -qx "Persistent=true" "$U/gtt-canary.timer"
check "deploy units allow time for two Canary runs" \
  grep -qx "TimeoutStartSec=45min" "$U/gtt-web-scraper-update.service"
check "gtt-canary.sh made executable" test -x "$PI/gtt-canary.sh"
check "no template placeholders left" fails grep -rq "@DIR@" "$U"
check "hardware watchdog configured" \
  grep -qx "RuntimeWatchdogSec=15s" "$T/etc/systemd/system.conf.d/gtt-watchdog.conf"
check "systemd re-executed so the watchdog takes effect" grep -qx "daemon-reexec" "$FAKE/systemctl.log"
check "boot unit and timers enabled and started" \
  grep -qx "enable --now gtt-web-scraper.service gtt-web-scraper-update.timer gtt-canary.timer" "$FAKE/systemctl.log"

ready_pi; rm "$PI/.env"
check "refuses without the wizard's env file" fails install_pi
check "  and says to run the wizard" grep -qi "wizard" "$FAKE/install.log"
printf "FIRECRAWL_API_KEY='fc-test'\n" > "$PI/.env"

ready_pi; echo "cpuset cpu io pids" > "$T/cgroup/cgroup.controllers"
check "refuses when memory limits can't be enforced" fails install_pi
check "  and says how to turn the memory cgroup on" grep -q "cgroup_enable=memory" "$FAKE/install.log"

ready_pi; rm "$T/dev/watchdog0"
check "refuses without a hardware watchdog device" fails install_pi
check "  and says how to enable it" grep -q "dtparam=watchdog=on" "$FAKE/install.log"

ready_pi; touch "$FAKE/start_fails"
check "first deploy failing stops the install" fails install_pi
check "  and points at the journal" grep -q "journalctl -u gtt-web-scraper" "$FAKE/install.log"
check "  and at the GHCR visibility step" grep -q "public on GHCR" "$FAKE/install.log"

ready_pi; rm "$FAKE/ip"
check "refuses when Tailscale has no IP" fails install_pi

echo
if (( FAILS )); then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
