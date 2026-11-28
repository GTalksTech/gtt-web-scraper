# Running gtt-web-scraper on pi5

How it works:

- **Build.** Every merge to `main` runs the tests, then builds an arm64 image and publishes it to GHCR as `ghcr.io/gtalkstech/gtt-web-scraper:main` and `:sha-<7 chars>` (`.github/workflows/image.yml`).
- **Deploy.** On pi5, `gtt-web-scraper-update.timer` runs `gtt-up.sh` every 10 minutes: pull `:main`, and if it changed, check it against the Canary Set before it takes traffic (below). The Pi pulls; nothing pushes into the home network. `gtt-web-scraper.service` runs the same script at boot, once Docker and Tailscale are up.
- **Deploys gated by the Canary Set.** A new image first runs as `gtt-web-scraper-candidate` next to the live service: no published port, no volume, 1 core and 1.5 GB. From inside it, the Canary Set (`config/canary.yaml`) is run against the live service and against the candidate. Canary pages and searches the candidate failed but the live service passed are asked once more after 30 seconds, so one blip on a real site doesn't decide it. Only the checks both versions made are compared (an older live image may not run every search), and the candidate replaces the live service only if it passes at least as many of them. The image that went live is remembered in `.live-image`, so after a reboot (which removes the container) that image starts again first and anything newer is still checked. Otherwise the live image keeps serving, a `deploy_rejected` Incident goes to the outbox, and the image is listed in `.rejected-images` (and tagged `gtt-web-scraper:kept-out`), so it isn't tried or downloaded again; the next image on `:main` is checked afresh. On the very first install nothing is live to compare with, so the image just starts.
- **Daily Canary Set and health.** `gtt-canary.timer` runs `gtt-canary.sh` once a day (around 06:00, or at the next boot if the Pi was off then), under the same lock as deploys. It runs the Canary Set against the live service and reads the rolling 24-hour success rates of each Fetcher, Search Provider and Site Adapter (`http://pi5:3002/health/fetchers`, `/health/providers`, `/health/adapters`). The Canary searches are asked of every Search Provider in turn (one with no key set is skipped). Canary pages are fetched with the plain download, a Site Adapter or the Chrome Fetcher (each page says which), never the Paid Fetcher, so a run never spends Firecrawl credits.
- **Incidents.** Every Incident found (a Canary page or search without its expected result, a Fetcher, Search Provider or Site Adapter whose success rate fell below the floor in `config/scraper.yaml`, a quota running out, a kept-out deploy) is appended as one JSON line to `~/gtt-web-scraper/incidents.jsonl`: its kind, the effect a Caller sees, evidence, and a fingerprint that stays the same for the same problem. At the end of every deploy pass and health run, `gtt-report.sh` sends each one to GitHub as a repository dispatch event (type `incident`, using `GITHUB_DISPATCH_TOKEN` from `.env`) and moves it to `incidents-sent.jsonl`. One it couldn't send (no internet, GitHub down) stays in `incidents.jsonl` and is tried again on the next pass, within 10 minutes.
- **Incident issues and the fixing agent.** On GitHub, `.github/workflows/incident.yml` opens an issue assigned to GTalksTech (so it emails you) titled `🚨 gtt-web-scraper: <effect>`, with three lines: what's broken, what the agent is doing, whether you need to act. The same fingerprint again joins the open issue as a comment (later repeats edit that comment, so no email each time); one back within a week of being fixed reopens its issue. Claude Code starts on it in the same run. It gets 2 attempts per Incident and about 5 runs a day across the repo; past either, when it says no code change can fix it, or when its fix hasn't landed 2 days later, the issue becomes `🙋 gtt-web-scraper needs you: …`. Closing it as fixed sends `✅ fixed: …`. Nothing else emails you, apart from GitHub's own note when an issue is closed or reopened. The logic is in `src/scraper/incident_issues/`.
- **Auto-merge.** Every pull request to `main` runs the test suite (`.github/workflows/tests.yml`), and branch protection on `main` requires it to pass. When it passes on the fixing agent's pull request (opened by `claude[bot]` from a branch of this repo, never a fork's), `.github/workflows/automerge.yml` merges it, starts the image build and sends the Incident's ✅. Nothing else merges itself. A fix that removes a Canary Set entry or a Source List domain, relabels or removes a Content Gate corpus entry, loosens a Content Gate or health threshold, or changes the workflows or the auto-merge code waits for you: the pull request is labelled `ready-for-human` and its Incident turns 🙋, listing what it weakens. Merge or close it by hand. The rules are in `src/scraper/automerge/`; why it's our own workflow and not GitHub's auto-merge is ADR 0004.
- **Monthly summary.** On the 1st, `.github/workflows/monthly-summary.yml` asks pi5 for last month's usage (`http://pi5:3002/health/usage?month=YYYY-MM`) and rewrites the pinned `📊 gtt-web-scraper monthly summary` issue: page and search requests from Callers, the share of pages each Fetcher, the cache or a Site Adapter served, the Paid Fetcher's Firecrawl calls, and the Incidents opened that month. The Canary Set's own requests are counted apart. It only edits that issue, so it never emails you. The service keeps 62 days of request records for it.
- **Reachability check.** pi5 can't report its own death, so `.github/workflows/reachability.yml` joins the tailnet every 2 hours with the CI key (`TS_AUTHKEY`, `tag:ci`) and calls `http://pi5:3002/health`. If pi5 doesn't answer, or the key is refused (they expire after 90 days), it raises an Incident that goes straight to you as 🙋, and closes it with a ✅ once a check gets through. Each run also sweeps the Incident issues: a fix that hasn't landed 2 days after the agent's last run becomes 🙋, and an issue closed as fixed whose ✅ didn't go out (a close made with the workflow token starts no workflow) gets it.
- **Tailnet only.** The port is published on pi5's Tailscale IP only (`TAILNET_IP`, set by `gtt-up.sh`), so the LAN and the internet can't reach it. Without that IP the service doesn't start at all.
- **Page cache.** Fetched pages are cached for 24 hours in SQLite at `/app/data` inside the container, which is the named volume `gtt-web-scraper_cache`, so the cache survives restarts and image updates. The same SQLite file holds per-site memory (which Fetcher last worked on each site, and sites on a cool-down), so emptying the volume forgets that too. To empty it: `ssh pi5 'cd ~/gtt-web-scraper && TAILNET_IP=127.0.0.1 docker compose down && docker volume rm gtt-web-scraper_cache'`, then deploy now.
- **Chrome.** The image carries Google Chrome (the ARM64 build) and a virtual display (Xvfb), which the Chrome Fetcher drives through Patchright in a visible window, for pages the plain download rejects. Its profile (cookies, history) lives in the cache volume at `/app/data/chrome-profile`, so it survives restarts and image updates. At most 2 pages are open at once, and Chrome is restarted every 50 pages (`chrome:` in `config/scraper.yaml`). If Chrome won't start, the Chrome Fetcher reports itself unavailable and requests go on to the Paid Fetcher.
- **SearXNG.** The last Search Provider, after DuckDuckGo (ADR 0005), is our own SearXNG in the `gtt-web-scraper-searxng` container. It asks Google, Bing, DuckDuckGo and Brave at once over the home connection, so search still answers when every free-tier Provider has failed, and it needs no key. Only the scraper and the candidate reach it, at `http://searxng:8080` on the compose network: it publishes no port, so the tailnet and the LAN can't. Its settings are `searxng/settings.yml` in this folder, mounted read-only, and its image version is pinned in `compose.yml` (and the root `docker-compose.yml`); a deploy pass starts it with the scraper. It gets half a core and 512 MB, keeps no state and its logs rotate at 2 × 10 MB. When every upstream search service behind it fails, search's error says which (for example `google (Suspended: CAPTCHA)`). Upgrading it: change the image tag in both compose files to a newer one from <https://hub.docker.com/r/searxng/searxng/tags>, then copy the files over (steps 5 and 6 below). **When the change adding SearXNG merges**, copy this folder to pi5 (steps 5 and 6) straight away: the new image already lists `searxng` in its Provider order, so until `compose.yml` on the Pi starts the container, SearXNG counts as unreachable and the daily Canary Set raises an Incident for it. A deploy pass also starts SearXNG before the candidate, so the candidate's Canary searches reach it.
- **Hardening.** The container gets 2 of 4 cores, 3 GB of 16 (no extra swap; Chrome for the Chrome Fetcher runs inside it, with 512 MB of shared memory counted in that), a process cap, and is the kernel's first choice to kill under memory pressure, so tailscaled (exit node, subnet router) always has room. It restarts if it exits; the timer restarts it if its healthcheck says it hung; systemd pets the hardware watchdog so a hung Pi reboots itself. Container logs rotate at 3 × 10 MB.

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

   Expect `RuntimeWatchdogUSec=15s`, `active`, and a memory LIMIT of `3GiB`.

8. **Fetch a page from this Mac over Tailscale**, passing pi5's home-LAN IP so it also proves the LAN can't reach it:

   ```sh
   deploy/pi5/verify-from-mac.sh 192.168.x.y
   ```

   It should print the health answer, the start of the markdown for Python's sqlite3 docs, the MCP `fetch` tool, and "no answer on the LAN".

9. **Let the fixing agent open pull requests.** Install the Claude GitHub app on the repo: <https://github.com/apps/claude>, then Configure → Only select repositories → `gtt-web-scraper`. The agent's pull requests are then by `claude[bot]`, so their checks run (ones made with the workflow's own token wouldn't). Then, in the repo's Actions tab, run the `reachability` workflow once by hand: it should go green and open no issue.

10. **Protect `main` and pin the monthly summary.** Re-run `scripts/setup/wizard.sh` (Enter keeps every saved answer, and `n` keeps each secret). Its last stage makes `main` require the `tests` check and opens, locks and pins the summary issue. Check: `gh api repos/GTalksTech/gtt-web-scraper/branches/main/protection --jq '.required_status_checks.checks'` shows `tests`, and the issue is pinned at the top of the Issues tab. Then run the `monthly-summary` workflow once by hand (Actions tab → Run workflow, month set to this one, e.g. `2026-12`): the issue gets this month so far, and you get no email.

11. **Add the MCP server to Claude Code** on the MacBook and on GM-Desktop: see [Using it from Claude Code](#using-it-from-claude-code-mcp).

## Using it from Claude Code (MCP)

The service offers an MCP `fetch` tool at `http://pi5:3002/mcp`, on the same port as the scrape API, so it's tailnet only too. It takes a URL, an optional `max_age_seconds` (0 forces a live fetch) and an optional `fetcher` to force (`plain` or `paid`). It returns the markdown with the title, the Fetcher used, the Verdict and the confidence, or an explicit failure listing every Fetcher tried.

A `search` tool sits next to it. It takes a query and an optional `limit` (default 10) and returns title, URL, snippet, Published date (YYYY-MM-DD or `unknown`) and the Search Provider used for each result. Results are shaped by the Source List in `config/source-list.yaml`: trusted primary sources come first and blocked content farms are dropped. Providers are tried in the order set in `config/scraper.yaml` (TinyFish, Exa, Tavily, DuckDuckGo, then our own SearXNG), results are cached for 24 hours, and search never spends Firecrawl credits. When every Provider fails, it returns an explicit failure naming each Provider's error. The same search answers Firecrawl-style at `/v1/search` and `/v2/search`.

Add it once on each machine, with Tailscale connected. The command is the same on the MacBook (Terminal) and on GM-Desktop (PowerShell):

```sh
claude mcp add --transport http --scope user gtt-scraper http://pi5:3002/mcp
```

`--scope user` makes it available in every project on that machine. Then check with `claude mcp list`: it should show `gtt-scraper` as connected. In a session, `/mcp` lists it with its `fetch` and `search` tools, which Claude sees as `mcp__gtt-scraper__fetch` and `mcp__gtt-scraper__search`.

- **`pi5` doesn't resolve** (MagicDNS off on that machine): use the Pi's tailnet IP. Get it with `tailscale ip -4 pi5`, then `claude mcp remove gtt-scraper --scope user` and add it again with `http://100.x.y.z:3002/mcp`.
- **Failed to connect:** check the service answers from that machine with `curl http://pi5:3002/health` (on Windows, `curl.exe`). If it doesn't, Tailscale isn't connected there or the service is down; see Troubleshooting. If it does, check the MCP endpoint itself (`deploy/pi5/verify-from-mac.sh` does this on the Mac). On GM-Desktop, in PowerShell:

  ```powershell
  (Invoke-RestMethod -Method Post -Uri http://pi5:3002/mcp -ContentType 'application/json' -Headers @{Accept = 'application/json, text/event-stream'} -Body '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}').result.tools.name
  ```

  It should print `fetch`.
- **Long pages cut short:** Claude Code caps an MCP tool's output (25,000 tokens by default). To allow more, set `MAX_MCP_OUTPUT_TOKENS` (for example `50000`) in the environment Claude Code starts from.
- A deploy on the Pi doesn't break connected sessions: the MCP endpoint keeps no sessions between calls.

## Day to day

- **What got deployed:** `ssh pi5 journalctl -u gtt-web-scraper-update -u gtt-web-scraper -n 50`. A deploy logs the Canary scores of both versions, then `deployed sha256:… (was sha256:…)` or `kept out sha256:…`.
- **Run the Canary Set now:** `ssh pi5 sudo systemctl start gtt-canary`, then `ssh pi5 journalctl -u gtt-canary -n 40` for the score and each failing page.
- **Incidents so far:** `ssh pi5 cat ~/gtt-web-scraper/incidents-sent.jsonl`; any not sent yet are in `incidents.jsonl`, and `journalctl -u gtt-web-scraper-update` says why. Send them now: `ssh pi5 sudo ~/gtt-web-scraper/gtt-report.sh`. Needs `curl` and `jq` on pi5 (`install.sh` checks). Success rates: `curl http://pi5:3002/health/fetchers` (also `/health/providers` and `/health/adapters`).
- **Deploy a kept-out image anyway** (you've checked the Canary failures are the sites' fault, not the image's): `ssh pi5 'cd ~/gtt-web-scraper && sudo rm -f .rejected-images && sudo GTT_DEPLOY_UNGATED=1 ./gtt-up.sh'`. The same variable skips the check for a rollback you pinned with `IMAGE_TAG`.
- **Deploy now** instead of waiting for the timer: `ssh pi5 sudo systemctl start gtt-web-scraper-update`.
- **Pin or roll back** to an earlier build: add `IMAGE_TAG=sha-<7 chars>` (from the commit) to `~/gtt-web-scraper/.env` on pi5, then deploy now. Remove the line to follow `main` again.
- **SearXNG logs:** `ssh pi5 docker logs --tail 100 gtt-web-scraper-searxng`. An upstream search service (SearXNG calls them engines) that blocks the Pi shows there as suspended; one that stays blocked for days can be dropped from `searxng/settings.yml`. A line saying `X-Forwarded-For nor X-Real-IP header is set` on each search is expected (there is no proxy in front of it).
- **Changed a file in this folder** (compose.yml, SearXNG's settings, units, scripts)? Those don't ride along with the image. Repeat steps 5 and 6; re-installing applies them without downtime.
- **Stop it** (the timer would otherwise start it again within 10 minutes): `ssh pi5 sudo systemctl disable --now gtt-web-scraper-update.timer gtt-web-scraper`. Re-run step 6 to bring it back.
- **Port:** 3002 is fixed in `compose.yml`. If you gave the wizard a different port, change it there too (and in the Tailscale policy).
- **Service logs:** `ssh pi5 docker logs --tail 100 gtt-web-scraper`.

## Troubleshooting

- `verify-from-mac.sh` gets no answer: `ssh pi5 systemctl status gtt-web-scraper gtt-web-scraper-update.timer` and `ssh pi5 docker ps -a`. A unit failing with "no Tailscale IPv4" means tailscaled wasn't connected; it retries on the next timer run.
- `pulling from GHCR failed` in the journal: the package isn't public (step 2), or pi5 has no internet. The image already on the Pi keeps running.
- Right after a reboot the boot unit may fail once or twice ("cannot assign requested address") while Tailscale brings up its IP; it retries every 30 s on its own.
- The Tailscale policy from the wizard only lets `tag:ci` reach port 3002 on pi5; your own devices reach it through the `autogroup:member` rule.
