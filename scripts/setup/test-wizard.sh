#!/usr/bin/env bash
#
# Tests for scripts/setup/wizard.sh. Runs the real wizard end to end with every
# outside tool faked (tailscale, ssh, gh, curl, claude, open, pbcopy), so it
# never touches a real account, the tailnet, pi5, hermes or GitHub.
#
#   scripts/setup/test-wizard.sh
#
# Asserts on what Garrett would end up with: what lands in pi5's env file, which
# repo secrets get set, what's left on this Mac, and that a re-run keeps it all.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WIZARD="$HERE/wizard.sh"

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
FAKE="$T/fake"; BIN="$T/bin"; TMP="$T/tmp"
mkdir -p "$FAKE/secrets" "$BIN" "$TMP"

FAILS=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }

# ── fakes ──────────────────────────────────────────────────────────────────
cat > "$FAKE/status.json" <<'EOF'
{
  "BackendState": "Running",
  "Self": {"HostName": "MacBook-Pro", "UserID": 1, "TailscaleIPs": ["100.64.0.1"]},
  "User": {"1": {"LoginName": "garrett@example.com"}},
  "Peer": {
    "a": {"HostName": "pi5", "TailscaleIPs": ["100.64.0.5"], "Tags": ["tag:pi"], "sshHostKeys": ["k"]},
    "b": {"HostName": "hermes", "TailscaleIPs": ["100.64.0.6"], "sshHostKeys": ["k"]},
    "c": {"HostName": "GM-DESKTOP", "TailscaleIPs": ["100.64.0.2"]}
  }
}
EOF

fake() { printf '#!/usr/bin/env bash\nFAKE=%q\n%s\n' "$FAKE" "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

fake tailscale 'cat "$FAKE/status.json"'

# ssh: emulates pi5's filesystem with one file, and logs each remote command.
fake ssh 'cmd="${@: -1}"; printf "%s\n" "$cmd" >> "$FAKE/ssh.log"
case "$cmd" in
  *"cat > "*) cat > "$FAKE/remote.env" ;;
  "if [ -e "*)
    [[ -e "$FAKE/unreadable" ]] && { echo "Permission denied" >&2; exit 1; }
    cat "$FAKE/remote.env" 2>/dev/null || true ;;
  *)          true ;;
esac'

# curl: 401 for any key containing BAD, 204 for dispatches, else 200. Logs its
# command line so the test can prove keys never appear on it.
fake curl 'printf "%s\n" "$*" >> "$FAKE/curl.log"; hdrs=""; url=""
while (( $# )); do
  case "$1" in -H) [[ "$2" == @* ]] && hdrs+=$(cat "${2#@}"); shift 2 ;;
               -o|-w|-X|--max-time|--data) shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
if [[ "$hdrs" == *BAD* ]]; then printf 401
elif [[ "$url" == */dispatches ]]; then printf 204
else printf 200; fi'

fake gh 'case "$1 $2" in
  "auth status") exit 0 ;;
  "api repos/GTalksTech/gtt-web-scraper") echo true ;;
  "secret set") cat > "$FAKE/secrets/$3"; echo "$GH_REPO" > "$FAKE/gh_repo" ;;
  "secret list") ls "$FAKE/secrets" ;;
  *) exit 1 ;;
esac'

fake claude 'if [[ "$1" == setup-token ]]; then echo "token: sk-ant-oat01-printed"; exit 0; fi
printf "%s\n" "$*" >> "$FAKE/claude.log"
# A bad token gets an error that still exits 0 and contains "ok" (as in "token").
[[ "${CLAUDE_CODE_OAUTH_TOKEN:-}" == *BAD* ]] && { echo "Invalid OAuth token"; exit 0; }
[[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] && { echo ok; exit 0; }  # falls back to the Mac login
echo ok'

fake open 'true'; fake xdg-open 'true'; fake pbcopy 'cat > /dev/null'

run_wizard() { # feeds stdin lines to the wizard; returns its exit code
  PATH="$BIN:$PATH" TMPDIR="$TMP" SETUP_FILE="$FAKE/setup.env" \
    bash "$WIZARD" > "$FAKE/out.log" 2>&1
}

remote_has() { grep -qx "$1='$2'" "$FAKE/remote.env"; }
secret_is()  { [[ "$(cat "$FAKE/secrets/$1" 2>/dev/null)" == "$2" ]]; }
no_secret_in() { ! grep -rqE 'good|BAD' "$@" 2>/dev/null; }

# ── run 1: first-time setup, with some mistakes along the way ─────────────
echo "run 1: first-time setup"
{
  echo ""                                   # banner
  echo "NOPE"; echo ""                      # wrong desktop name, then default
  echo "pi"; echo "garrett"; echo ""; echo ""; echo ""  # users, port, path, continue
  echo "y"; echo ""                         # policy saved
  echo ""; echo ""                          # ssh enabled, continue
  echo "fc-BAD"; echo "fc-good"; echo ""    # Firecrawl: bad key re-asked
  echo "tf-good"; echo ""
  echo "exa-good"; echo ""
  echo "tvly-good"; echo ""
  echo "ghp_classic"; echo "github_pat_good"; echo ""  # classic token refused
  echo ""; echo ""; echo "sk-ant-oat01-BAD"             # run setup-token; empty, bad
  echo "sk-ant-oat01-good"; echo ""                     # then the good token
  echo "tskey-wrong"; echo "tskey-auth-good"; echo ""   # bad prefix re-asked
} | run_wizard && rc=0 || rc=$?

check "wizard finishes"                      [ "$rc" -eq 0 ]
check "Firecrawl key on pi5 (bad one dropped)" remote_has FIRECRAWL_API_KEY fc-good
check "TinyFish key on pi5"                  remote_has TINYFISH_API_KEY tf-good
check "Exa key on pi5"                       remote_has EXA_API_KEY exa-good
check "Tavily key on pi5"                    remote_has TAVILY_API_KEY tvly-good
check "dispatch token on pi5"                remote_has GITHUB_DISPATCH_TOKEN github_pat_good
check "no bad value reached pi5"             bash -c "! grep -qE 'BAD|ghp_' '$FAKE/remote.env'"
check "pi5 env written with umask 077"       grep -q 'umask 077' "$FAKE/ssh.log"
check "Claude token is a repo secret"        secret_is CLAUDE_CODE_OAUTH_TOKEN sk-ant-oat01-good
check "Tailscale key is a repo secret"       secret_is TS_AUTHKEY tskey-auth-good
check "secrets target GTalksTech repo"       grep -qx GTalksTech/gtt-web-scraper "$FAKE/gh_repo"
check "no other repo secrets"                [ "$(ls "$FAKE/secrets" | wc -l | tr -d ' ')" -eq 2 ]
check "answers saved locally"                grep -qx "PI5_SSH_USER='pi'" "$FAKE/setup.env"
check "default desktop name used"            grep -qx "DESKTOP_HOST='GM-DESKTOP'" "$FAKE/setup.env"
check "local answers hold no secret"         no_secret_in "$FAKE/setup.env"
check "keys never on curl's command line"    no_secret_in "$FAKE/curl.log"
check "token never on claude's command line" no_secret_in "$FAKE/claude.log"
check "no temp files left behind"            [ -z "$(ls -A "$TMP")" ]
check "policy snippet has CI port rule"      grep -q '"tag:ci"\], "dst": \["gtt-pi5"\], "ip": \["tcp:3002"\]' "$FAKE/out.log"
check "policy SSH rule uses pi5's tag"       grep -q '"dst": \["tag:pi"\], "users": \["pi"\]' "$FAKE/out.log"
check "policy SSH rule for untagged hermes"  grep -q '"dst": \["autogroup:self"\], "users": \["garrett"\]' "$FAKE/out.log"
check "SSH limited to the two computers"     grep -q '"src": \["gtt-mac", "gtt-desktop"\], "dst": \["gtt-pi5", "gtt-hermes"\], "ip": \["tcp:22"\]' "$FAKE/out.log"

# ── run 2: re-run keeps everything with just Enter (and "n" on secrets) ───
echo "run 2: re-run"
cp "$FAKE/remote.env" "$T/remote.before"
{
  echo ""                                   # banner
  echo ""; echo ""; echo ""; echo ""; echo ""; echo ""  # keep all answers
  echo "y"; echo ""
  echo ""; echo ""
  for _ in 1 2 3 4 5; do echo ""; echo ""; done          # keep each key
  echo "n"; echo ""                         # keep Claude secret
  echo "n"; echo ""                         # keep Tailscale secret
} | run_wizard && rc=0 || rc=$?

check "re-run finishes"                      [ "$rc" -eq 0 ]
check "re-run leaves pi5's env unchanged"    cmp -s "$T/remote.before" "$FAKE/remote.env"
check "re-run keeps Claude secret"           secret_is CLAUDE_CODE_OAUTH_TOKEN sk-ant-oat01-good
check "re-run keeps Tailscale secret"        secret_is TS_AUTHKEY tskey-auth-good
check "re-run keeps answers"                 grep -qx "HERMES_SSH_USER='garrett'" "$FAKE/setup.env"

# ── run 2b: pi5's env file exists but can't be read ───────────────────────
echo "run 2b: unreadable env file on pi5"
touch "$FAKE/unreadable"
{
  echo ""; for _ in 1 2 3 4 5 6; do echo ""; done; echo "y"; echo ""
  echo ""; echo ""                          # ssh stage; the read check fails, input ends
} | run_wizard && rc=0 || rc=$?
rm -f "$FAKE/unreadable"
check "unreadable env file stops the wizard" [ "$rc" -ne 0 ]
check "unreadable env file is left intact"   cmp -s "$T/remote.before" "$FAKE/remote.env"

# ── run 3: input ends early, the wizard stops instead of hanging ──────────
echo "run 3: input ends early"
printf '\n' | run_wizard && rc=0 || rc=$?
check "stops with an error at end of input"  [ "$rc" -ne 0 ]

# ── the repo itself ────────────────────────────────────────────────────────
echo "repo"
check ".env.example is tracked-safe (not ignored)" bash -c "! git -C '$ROOT' check-ignore -q .env.example"
check ".env.setup is ignored"                git -C "$ROOT" check-ignore -q .env.setup
check ".env is ignored"                      git -C "$ROOT" check-ignore -q .env
check ".env.example holds only placeholders" bash -c "! grep -E '^[A-Z_]+=' '$ROOT/.env.example' | grep -v 'replace-me'"

if (( FAILS )); then echo "$FAILS failed (wizard output: see below)"; tail -n 40 "$FAKE/out.log"; exit 1; fi
echo "all passed"
