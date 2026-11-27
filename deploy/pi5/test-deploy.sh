#!/usr/bin/env bash
#
# Tests for the pi5 deployment (ticket #8). Never touches pi5, the tailnet or
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
check "refuses to start without TAILNET_IP (never binds 0.0.0.0)" \
  fails env -u TAILNET_IP docker compose -f "$PI/compose.yml" config -q 2> /dev/null

# ── fakes for gtt-up.sh and install.sh ─────────────────────────────────────
FAKE="$T/fake"; BIN="$T/bin"; mkdir -p "$FAKE" "$BIN"
fake() { printf '#!/usr/bin/env bash\nFAKE=%q\n%s\n' "$FAKE" "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

# tailscale: prints the IP in $FAKE/ip (none if the file is missing).
fake tailscale '[[ "$*" == "ip -4" ]] && cat "$FAKE/ip" 2>/dev/null; true'
# docker: logs each call with the TAILNET_IP it saw. The running image is in
# $FAKE/running; a pull moves it to $FAKE/remote unless $FAKE/pull_fails exists.
fake docker 'printf "%s|TAILNET_IP=%s\n" "$*" "${TAILNET_IP:-}" >> "$FAKE/docker.log"
case "$*" in
  "compose pull"*) [[ -e "$FAKE/pull_fails" ]] && exit 1; cp "$FAKE/remote" "$FAKE/pulled" ;;
  "compose up"*)   [[ -e "$FAKE/pulled" ]] && mv "$FAKE/pulled" "$FAKE/running"; true ;;
  "inspect --format {{.Image}}"*) cat "$FAKE/running" 2>/dev/null || exit 1 ;;
  "inspect --format"*Health*) cat "$FAKE/health" 2>/dev/null; true ;;
  "compose version"*) echo "Docker Compose version v2.30.0" ;;
  "port"*) echo "3002/tcp -> $(cat "$FAKE/ip"):3002" ;;
  *) true ;;
esac'
fake sleep 'true'
fake systemctl 'printf "%s\n" "$*" >> "$FAKE/systemctl.log"
[[ "$*" == "show --property=RuntimeWatchdogUSec"* ]] && echo "RuntimeWatchdogUSec=15s"
[[ "$*" == "is-active"* ]] && exit 3; true'

reset_fakes() { rm -f "$FAKE"/*; echo 100.64.0.5 > "$FAKE/ip"; echo sha256:old > "$FAKE/running"; echo sha256:new > "$FAKE/remote"; }
up() { PATH="$BIN:$PATH" TAILNET_WAIT_S="${WAIT:-0}" bash "$PI/gtt-up.sh" > "$FAKE/out.log" 2>&1; }
ran() { grep -q "^$1" "$FAKE/docker.log"; }
ran_before() { local a b; a=$(grep -n "^$1" "$FAKE/docker.log" | head -1 | cut -d: -f1); b=$(grep -n "^$2" "$FAKE/docker.log" | head -1 | cut -d: -f1); [[ -n $a && -n $b && $a -lt $b ]]; }

# ── gtt-up.sh: one deploy pass ─────────────────────────────────────────────
echo "gtt-up.sh"
reset_fakes
check "a new image deploys" up
check "pulls, then brings the service up" ran_before "compose pull" "compose up -d"
check "compose sees the Pi's tailnet IP" grep -q "^compose up -d.*TAILNET_IP=100.64.0.5$" "$FAKE/docker.log"
check "the new image is running" test "$(cat "$FAKE/running")" = "sha256:new"
check "logs the deploy, old and new image" grep -q "sha256:old.*sha256:new\|sha256:new.*sha256:old" "$FAKE/out.log"
check "prunes only dangling images" grep -qx "image prune -f|.*" "$FAKE/docker.log"

reset_fakes; echo sha256:new > "$FAKE/running"
up
check "same image: says nothing was deployed" fails grep -q "deployed" "$FAKE/out.log"

reset_fakes; rm "$FAKE/ip"
check "no tailnet IP: fails" fails up
check "no tailnet IP: never starts the service" fails grep -qs "^compose up" "$FAKE/docker.log"

reset_fakes; touch "$FAKE/pull_fails"
check "registry unreachable: reports failure" fails up
check "registry unreachable: still brings up the image it has" ran "compose up -d"
check "registry unreachable: keeps the old image" test "$(cat "$FAKE/running")" = "sha256:old"

reset_fakes; echo unhealthy > "$FAKE/health"
up
check "hung (unhealthy) container is restarted" ran "compose restart scraper"
reset_fakes; echo healthy > "$FAKE/health"
up
check "healthy container is left alone" fails grep -q "^compose restart" "$FAKE/docker.log"

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
check "update unit runs gtt-up.sh under the same lock" \
  test "$(grep ^ExecStart= "$U/gtt-web-scraper.service")" = "$(grep ^ExecStart= "$U/gtt-web-scraper-update.service")"
check "timer pulls on a schedule" grep -q "^OnUnitActiveSec=" "$U/gtt-web-scraper-update.timer"
check "no template placeholders left" fails grep -rq "@DIR@" "$U"
check "hardware watchdog configured" \
  grep -qx "RuntimeWatchdogSec=15s" "$T/etc/systemd/system.conf.d/gtt-watchdog.conf"
check "systemd re-executed so the watchdog takes effect" grep -qx "daemon-reexec" "$FAKE/systemctl.log"
check "boot unit and timer enabled and started" \
  grep -qx "enable --now gtt-web-scraper.service gtt-web-scraper-update.timer" "$FAKE/systemctl.log"

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

ready_pi; rm "$FAKE/ip"
check "refuses when Tailscale has no IP" fails install_pi

echo
if (( FAILS )); then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
