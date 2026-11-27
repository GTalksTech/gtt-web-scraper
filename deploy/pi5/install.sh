#!/usr/bin/env bash
#
# One-time install of gtt-web-scraper on pi5. Run on the Pi, from the deploy
# dir that holds the wizard's .env file (see deploy/pi5/README.md):
#
#   sudo ~/gtt-web-scraper/install.sh
#
# Checks the Pi can enforce the limits, then installs the boot unit, the
# 10-minute update timer and the hardware watchdog, and starts the service.
# Safe to re-run (that's also how changed units or compose.yml take effect).

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tests point these at a temp dir; on the Pi they are the real thing.
ETC="${GTT_ETC:-/etc}"
DEV="${GTT_DEV:-/dev}"
CGROUP="${GTT_CGROUP:-/sys/fs/cgroup}"

ok()  { printf '  ok  %s\n' "$*"; }
die() { printf '\n  stop: %s\n\n' "$*" >&2; exit 1; }

echo "Checking pi5 is ready"
[[ -n "${GTT_ETC:-}" || "$EUID" -eq 0 ]] || die "run it with sudo: sudo $DIR/install.sh"
# The path goes into systemd ExecStart lines unquoted.
[[ "$DIR" =~ ^[A-Za-z0-9/._-]+$ ]] || die "move the deploy dir to a path without spaces or symbols (now: $DIR)"

command -v docker > /dev/null || die "Docker isn't installed. See 'Install Docker' in deploy/pi5/README.md."
docker compose version > /dev/null 2>&1 || die "the Docker Compose plugin is missing: sudo apt-get install docker-compose-plugin"
ok "Docker and Compose installed"

command -v tailscale > /dev/null || die "Tailscale isn't installed on this machine"
ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
[[ -n "$ip" ]] || die "Tailscale has no IPv4 here. Is it connected? (tailscale status)"
ok "Tailscale up at $ip"

[[ -f "$DIR/.env" ]] || die "no $DIR/.env yet. Run the setup wizard from your Mac first (scripts/setup/wizard.sh), with the env file path $DIR/.env."
ok "found the wizard's env file"

for controller in memory cpu pids; do
  grep -qw "$controller" "$CGROUP/cgroup.controllers" 2>/dev/null \
    || die "the kernel's $controller cgroup is off, so Docker can't enforce the scraper's limits.
  Add ' cgroup_enable=memory cgroup_memory=1 cgroup_enable=cpuset' to the end of the single
  line in /boot/firmware/cmdline.txt, reboot, and run this again."
done
ok "memory, CPU and process limits can be enforced"

[[ -e "$DEV/watchdog0" || -e "$DEV/watchdog" ]] \
  || die "no hardware watchdog device. Add 'dtparam=watchdog=on' to /boot/firmware/config.txt, reboot, and run this again."
if systemctl is-active --quiet watchdog 2>/dev/null; then
  die "the 'watchdog' package's daemon already holds the watchdog. Remove it (sudo apt-get remove watchdog) so systemd can, then run this again."
fi
ok "hardware watchdog present"

echo "Installing"
chmod 0755 "$DIR/gtt-up.sh"
mkdir -p "$ETC/systemd/system" "$ETC/systemd/system.conf.d"
for unit in gtt-web-scraper.service gtt-web-scraper-update.service gtt-web-scraper-update.timer; do
  sed "s|@DIR@|$DIR|g" "$DIR/systemd/$unit" > "$ETC/systemd/system/$unit"
  chmod 0644 "$ETC/systemd/system/$unit"
done
ok "systemd units installed"
install -m 0644 "$DIR/systemd/gtt-watchdog.conf" "$ETC/systemd/system.conf.d/gtt-watchdog.conf"
# Re-exec (not just reload) so systemd picks up the watchdog setting now.
systemctl daemon-reexec
ok "hardware watchdog on ($(systemctl show --property=RuntimeWatchdogUSec | cut -d= -f2))"

echo "Starting (the first pull of the image can take a few minutes)"
systemctl enable --now gtt-web-scraper.service gtt-web-scraper-update.timer \
  || die "the first deploy failed; systemd keeps retrying every 30 s. See why with:
  journalctl -u gtt-web-scraper -n 30
  Most likely the image isn't public on GHCR yet (deploy/pi5/README.md, step 2)."
# enable --now does nothing for a service already active: run one deploy pass
# so a re-install applies a changed compose.yml straight away, without downtime.
systemctl start gtt-web-scraper-update.service
ok "service and update timer enabled"

echo
echo "Published ports (should be $ip:3002 only):"
docker port gtt-web-scraper | sed 's/^/  /'
echo
echo "Next, from your Mac: deploy/pi5/verify-from-mac.sh"
