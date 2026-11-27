# Running gtt-web-scraper on pi5

How it works:

- **Build.** Every merge to `main` runs the tests, then builds an arm64 image and publishes it to GHCR as `ghcr.io/gtalkstech/gtt-web-scraper:main` and `:sha-<7 chars>` (`.github/workflows/image.yml`).
- **Deploy.** On pi5, `gtt-web-scraper-update.timer` runs `gtt-up.sh` every 10 minutes: pull `:main`, and if it changed, replace the container. The Pi pulls; nothing pushes into the home network. `gtt-web-scraper.service` runs the same script at boot, once Docker and Tailscale are up.
- **Tailnet only.** The port is published on pi5's Tailscale IP only (`TAILNET_IP`, set by `gtt-up.sh`), so the LAN and the internet can't reach it. Without that IP the service doesn't start at all.
- **Page cache.** Fetched pages are cached for 24 hours in SQLite at `/app/data` inside the container, which is the named volume `gtt-web-scraper_cache`, so the cache survives restarts and image updates. The same SQLite file holds per-site memory (which Fetcher last worked on each site, and sites on a cool-down), so emptying the volume forgets that too. To empty it: `ssh pi5 'cd ~/gtt-web-scraper && TAILNET_IP=127.0.0.1 docker compose down && docker volume rm gtt-web-scraper_cache'`, then deploy now.
- **Hardening.** The container gets 2 of 4 cores, 2 GB of 16 (no extra swap), a process cap, and is the kernel's first choice to kill under memory pressure, so tailscaled (exit node, subnet router) always has room. It restarts if it exits; the timer restarts it if its healthcheck says it hung; systemd pets the hardware watchdog so a hung Pi reboots itself. Container logs rotate at 3 × 10 MB.
- **Not yet:** running the Canary Set against a new image before it takes traffic, and rolling back when it does worse (spec #1, Deploy). Today a new `:main` replaces the old one directly.

The files in this folder live on pi5 in `~/gtt-web-scraper/`, next to the `.env` file the setup wizard writes there.

## First install (Garrett, once)

Do these in order. Steps 1 and 2 need GitHub; the rest happen on this Mac and pi5.

1. **Merge to `main`.** Merge `build/spec-1` (with this work) into `main`. In the repo's Actions tab, check the `image` workflow goes green: both `test` and `image` jobs.

2. **Make the image public**, so pi5 can pull it without a token. It holds no secrets (the keys stay in pi5's `.env`).
   Open <https://github.com/users/GTalksTech/packages/container/gtt-web-scraper/settings>, then Danger Zone → Change visibility → Public.
   Check from this Mac that an anonymous pull works (the empty config dir means no saved login is used): `DOCKER_CONFIG=$(mktemp -d) docker pull --platform linux/arm64 ghcr.io/gtalkstech/gtt-web-scraper:main`.

3. **Run the setup wizard** if you haven't: `scripts/setup/wizard.sh` (ticket #2). It turns on Tailscale SSH on pi5 and writes `~/gtt-web-scraper/.env` there. Keep the defaults: port 3002, env file `~/gtt-web-scraper/.env`.
   If `ssh pi5` says `Host key verification failed`, that's because Tailscale SSH presents a new host key. Once you're satisfied it's your Pi, remove the old entry (`ssh-keygen -R pi5`) and connect once to accept the new one.

4. **Docker on pi5.** Check: `ssh pi5 docker compose version`. If it's missing, install Docker Engine with the Compose plugin following Docker's Debian instructions (Raspberry Pi OS 64-bit is Debian): <https://docs.docker.com/engine/install/debian/>.

5. **Copy the deploy files to pi5.** From the repo root on this Mac (this never touches `.env`):

   ```sh
   COPYFILE_DISABLE=1 tar -C deploy/pi5 -cf - . | ssh pi5 'mkdir -p ~/gtt-web-scraper && tar -C ~/gtt-web-scraper -xf -'
   ```

6. **Install:**

   ```sh
   ssh -t pi5 'sudo ~/gtt-web-scraper/install.sh'
   ```

   It checks the Pi first and stops with the fix if something's missing. The two you might hit, each needing a reboot and then re-running this step:
   - memory cgroup off: append ` cgroup_enable=memory cgroup_memory=1 cgroup_enable=cpuset` to the single line in `/boot/firmware/cmdline.txt`;
   - no watchdog device: add `dtparam=watchdog=on` to `/boot/firmware/config.txt`.

   It ends by printing the published port, which must be pi5's `100.x.y.z:3002` and nothing else.

7. **Check the watchdog and limits on pi5:**

   ```sh
   ssh pi5 'systemctl show -p RuntimeWatchdogUSec; cat /sys/class/watchdog/watchdog0/state; docker stats --no-stream gtt-web-scraper'
   ```

   Expect `RuntimeWatchdogUSec=15s`, `active`, and a memory LIMIT of `2GiB`.

8. **Fetch a page from this Mac over Tailscale**, passing pi5's home-LAN IP so it also proves the LAN can't reach it:

   ```sh
   deploy/pi5/verify-from-mac.sh 192.168.x.y
   ```

   It should print the health answer, the start of the markdown for Python's sqlite3 docs, the MCP `fetch` tool, and "no answer on the LAN".

9. **Add the MCP server to Claude Code** on the MacBook and on GM-Desktop: see [Using it from Claude Code](#using-it-from-claude-code-mcp).

## Using it from Claude Code (MCP)

The service offers an MCP `fetch` tool at `http://pi5:3002/mcp`, on the same port as the scrape API, so it's tailnet only too. It takes a URL, an optional `max_age_seconds` (0 forces a live fetch) and an optional `fetcher` to force (`plain` or `paid`). It returns the markdown with the title, the Fetcher used, the Verdict and the confidence, or an explicit failure listing every Fetcher tried.

Add it once on each machine, with Tailscale connected. The command is the same on the MacBook (Terminal) and on GM-Desktop (PowerShell):

```sh
claude mcp add --transport http --scope user gtt-scraper http://pi5:3002/mcp
```

`--scope user` makes it available in every project on that machine. Then check with `claude mcp list`: it should show `gtt-scraper` as connected. In a session, `/mcp` lists it with its `fetch` tool, which Claude sees as `mcp__gtt-scraper__fetch`.

- **`pi5` doesn't resolve** (MagicDNS off on that machine): use the Pi's tailnet IP. Get it with `tailscale ip -4 pi5`, then `claude mcp remove gtt-scraper --scope user` and add it again with `http://100.x.y.z:3002/mcp`.
- **Failed to connect:** check the service answers from that machine with `curl http://pi5:3002/health` (on Windows, `curl.exe`). If it doesn't, Tailscale isn't connected there or the service is down; see Troubleshooting. If it does, check the MCP endpoint itself (`deploy/pi5/verify-from-mac.sh` does this on the Mac). On GM-Desktop, in PowerShell:

  ```powershell
  (Invoke-RestMethod -Method Post -Uri http://pi5:3002/mcp -ContentType 'application/json' -Headers @{Accept = 'application/json, text/event-stream'} -Body '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}').result.tools.name
  ```

  It should print `fetch`.
- **Long pages cut short:** Claude Code caps an MCP tool's output (25,000 tokens by default). To allow more, set `MAX_MCP_OUTPUT_TOKENS` (for example `50000`) in the environment Claude Code starts from.
- A deploy on the Pi doesn't break connected sessions: the MCP endpoint keeps no sessions between calls.

## Day to day

- **What got deployed:** `ssh pi5 journalctl -u gtt-web-scraper-update -u gtt-web-scraper -n 50`. A deploy logs `deployed sha256:… (was sha256:…)`.
- **Deploy now** instead of waiting for the timer: `ssh pi5 sudo systemctl start gtt-web-scraper-update`.
- **Pin or roll back** to an earlier build: add `IMAGE_TAG=sha-<7 chars>` (from the commit) to `~/gtt-web-scraper/.env` on pi5, then deploy now. Remove the line to follow `main` again.
- **Changed a file in this folder** (compose.yml, units, scripts)? Those don't ride along with the image. Repeat steps 5 and 6; re-installing applies them without downtime.
- **Stop it** (the timer would otherwise start it again within 10 minutes): `ssh pi5 sudo systemctl disable --now gtt-web-scraper-update.timer gtt-web-scraper`. Re-run step 6 to bring it back.
- **Port:** 3002 is fixed in `compose.yml`. If you gave the wizard a different port, change it there too (and in the Tailscale policy).
- **Service logs:** `ssh pi5 docker logs --tail 100 gtt-web-scraper`.

## Troubleshooting

- `verify-from-mac.sh` gets no answer: `ssh pi5 systemctl status gtt-web-scraper gtt-web-scraper-update.timer` and `ssh pi5 docker ps -a`. A unit failing with "no Tailscale IPv4" means tailscaled wasn't connected; it retries on the next timer run.
- `pulling from GHCR failed` in the journal: the package isn't public (step 2), or pi5 has no internet. The image already on the Pi keeps running.
- Right after a reboot the boot unit may fail once or twice ("cannot assign requested address") while Tailscale brings up its IP; it retries every 30 s on its own.
- The Tailscale policy from the wizard only lets `tag:ci` reach port 3002 on pi5; your own devices reach it through the `autogroup:member` rule.
