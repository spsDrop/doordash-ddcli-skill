#!/usr/bin/env bash
# token-fetch.sh — obtain a fresh DD_CLI_ACCESS_TOKEN for the dd-cli.
#
# Two paths, chosen automatically:
#   A) KEYCHAIN (default when a browser is available — macOS, or Linux with a
#      desktop / DISPLAY): `dd-cli export-token` runs the normal browser
#      sign-in, prints the token to stdout (nothing is written to the keychain
#      by export-token itself — `dd-cli login` is what persists it there), and
#      we capture that printed token.
#   B) CDP (headless fallback — no browser): drive a Chrome/Chromium already
#      running with --remote-debugging-port (default :9222) through the OAuth
#      flow and capture the token from the callback. This is the "hydra"
#      headless pattern: a persistent signed-in Chrome is the identity source.
#
# Usage:
#   bash token-fetch.sh
#
# Environment:
#   DD_TOKEN_MODE      auto (default) | keychain | cdp   — force a path
#   CDP_PORT           CDP port (default 9222)
#   TOKEN_OUT          where to write the token (default: a new shell file
#                      ~/.config/ddcli/token.env that you source, e.g.
#                      `set -a; . ~/.config/ddcli/token.env; set +a`).
#                      If unset and ~/.bashrc contains a DD_CLI_ACCESS_TOKEN
#                      export, it is updated in place (single-line, backup
#                      first) so existing bashrc setups keep working.
#
# Security: the token is printed to this script's stderr ONLY, never to
# stdout (stdout is reserved for the token on the keychain path so it can be
# captured with `$(bash token-fetch.sh)`). Do not paste it into chat.
set -uo pipefail

PORT="${CDP_PORT:-9222}"
MODE="${DD_TOKEN_MODE:-auto}"
OUT="${TOKEN_OUT:-}"

log() { echo "[token-fetch] $*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

browser_available() {
  [ "$(uname -s)" = "Darwin" ] && return 0
  [ -n "${DISPLAY:-}" ] && return 0
  [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  return 1
}

cdp_up() {
  if have curl; then
    curl -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1
  elif have wget; then
    wget -qO- "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1
  else
    return 1
  fi
}

choose_mode() {
  if [ "$MODE" != "auto" ]; then echo "$MODE"; return; fi
  if browser_available; then echo keychain; return; fi
  if cdp_up; then echo cdp; return; fi
  echo none
}

# --- Path A: native keychain / browser (export-token) ----------------------
do_keychain() {
  log "keychain mode: running 'dd-cli export-token' (a browser sign-in will open)"
  # export-token prints the token on its own line to stdout; everything else
  # goes to stderr. Capture only the JWT-shaped line.
  local raw tok
  raw="$(dd-cli export-token 2>/dev/null)" || {
    log "export-token exited non-zero (not signed in? run 'dd-cli login' first)"
    return 1
  }
  tok="$(printf '%s\n' "$raw" | grep -Eo 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' | tail -1)"
  [ -n "$tok" ] || { log "no token in export-token output"; return 1; }
  emit "$tok"
}

# --- Path B: headless CDP fallback -----------------------------------------
do_cdp() {
  log "cdp mode: driving Chrome on 127.0.0.1:${PORT} (must be signed into DoorDash)"
  local raw tok
  # Start the token flow in the background, steer the signed-in Chrome to the
  # authorize URL, and wait for the callback to mint the token.
  (
    exec dd-cli export-token -v
  ) > "$TMP_RAW" 2>&1 &
  local pid=$!
  local auth=""
  for _ in $(seq 1 25); do
    auth="$(grep -o 'https://identity.doordash.com/authorize[^ ]*' "$TMP_RAW" | head -1)"
    [ -n "$auth" ] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if [ -z "$auth" ]; then
    log "no authorize URL appeared (Chrome on :${PORT} not signed in?)"
    kill "$pid" 2>/dev/null
    return 1
  fi
  log "navigating CDP Chrome to authorize URL"
  navigate_cdp "$auth"
  tok=""
  for _ in $(seq 1 40); do
    tok="$(grep -Eo 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' "$TMP_RAW" | tail -1)"
    [ -n "$tok" ] && break
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
  done
  kill "$pid" 2>/dev/null
  [ -n "$tok" ] || { log "no token from CDP callback"; return 1; }
  emit "$tok"
}

# Drive a CDP Chrome to a URL. If a node helper (cdp.cdp.js) is available use
# it; otherwise fall back to plain HTTP /json/new (Chrome >= 111 headless
# needs --remote-allow-origins=*; the /json/new fallback is best-effort).
navigate_cdp() {
  local url="$1"
  local helper="${CDP_NAV:-$HOME/.hermes/skills/devops/hydra-headless-browser/scripts/cdp.cdp.js}"
  if [ -f "$helper" ] && have node; then
    node "$helper" nav "$url" && return 0
  fi
  # Best-effort fallback: open a new tab via CDP's HTTP endpoint.
  if have curl; then
    curl -fsS "http://127.0.0.1:${PORT}/json/new?${url}" >/dev/null 2>&1 || true
  fi
}

# --- Emit: write the token where the caller wants it -----------------------
TMP_RAW="$(mktemp)"
trap 'rm -f "$TMP_RAW"' EXIT

emit() {
  local tok="$1"
  # Always write to the configured output (or default location).
  local dest="$OUT"
  if [ -z "$dest" ]; then
    if [ -f "$HOME/.bashrc" ] && grep -q '^export DD_CLI_ACCESS_TOKEN=' "$HOME/.bashrc"; then
      # Preserve existing bashrc setups: swap the single token line in place.
      local ts bak
      ts="$(date +%Y%m%d%H%M%S)"; bak="${HOME}/.bashrc.bak-ddtoken-${ts}"
      cp -f "$HOME/.bashrc" "$bak"
      python3 - "$tok" "$HOME/.bashrc" "$bak" <<'PY'
import re, sys
tok, path, bak = sys.argv[1], sys.argv[2], sys.argv[3]
b = open(path).read()
n = len(re.findall(r'^export DD_CLI_ACCESS_TOKEN=.*$', b, re.M))
if n != 1:
    print(f"[token-fetch] ABORT: expected 1 token line in bashrc, found {n}", file=sys.stderr)
    sys.exit(2)
open(path, "w").write(re.sub(r'^export DD_CLI_ACCESS_TOKEN=.*$',
                             'export DD_CLI_ACCESS_TOKEN="' + tok + '"',
                             b, flags=re.M))
PY
      local rc=$?
      [ "$rc" -eq 0 ] || { log "bashrc swap failed (rc=$rc)"; return "$rc"; }
      log "token written to ~/.bashrc (backup: ${bak##*/})"
      return 0
    fi
    dest="$HOME/.config/ddcli/token.env"
  fi
  mkdir -p "$(dirname "$dest")"
  printf 'export DD_CLI_ACCESS_TOKEN="%s"\n' "$tok" > "$dest"
  chmod 600 "$dest"
  log "token written to $dest"
  # Also print it (stderr, so it's visible but not captured by stdout).
  echo "DD_CLI_ACCESS_TOKEN=$tok" >&2
}

# --- Main ------------------------------------------------------------------
MODE="$(choose_mode)"
case "$MODE" in
  keychain) have dd-cli || { log "dd-cli not on PATH — run: bash $(dirname "$0")/install-dd-cli.sh"; exit 1; }
             do_keychain ;;
  cdp)      have dd-cli || { log "dd-cli not on PATH — run: bash $(dirname "$0")/install-dd-cli.sh"; exit 1; }
             do_cdp ;;
  none)
    log "no token path available: no browser (DISPLAY/macOS) and no CDP Chrome on :${PORT}."
    log "Start a signed-in Chrome:  google-chrome --remote-debugging-port=${PORT} --user-data-dir=/tmp/ddchrome"
    log "or set DD_TOKEN_MODE=keychain on a machine with a browser."
    exit 1 ;;
esac
