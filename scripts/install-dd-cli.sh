#!/usr/bin/env bash
# install-dd-cli.sh — download & install the official DoorDash dd-cli binary.
#
# Downloads the release tarball for this platform from
# github.com/doordash-oss/doordash-cli, verifies it against the published
# .sha256 sidecar, and installs to ~/.local/bin/dd-cli via the bundled
# install.sh. Works on macOS (Apple Silicon) and glibc Linux (x86_64).
#
#   bash install-dd-cli.sh [version]
#
#   version  Optional dd-cli version (default: auto — reads the local
#            ~/.local/bin/dd-cli version if present, else the latest release).
#
# Uses curl when available, else wget. Honors $DD_CLI_VERSION to override.
set -euo pipefail

REPO="https://github.com/doordash-oss/doordash-cli"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1) Detect platform (must match a published asset name) ---------------
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64|Darwin-arm)  PLAT="darwin-arm64" ;;
  Linux-x86_64|Linux-amd64) PLAT="linux-amd64" ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m). dd-cli publishes only darwin-arm64 and linux-amd64." >&2; exit 1 ;;
esac

# --- 2) Determine version --------------------------------------------------
VER="${1:-${DD_CLI_VERSION:-}}"
if [ -z "$VER" ] && command -v dd-cli >/dev/null 2>&1; then
  # Reuse the locally installed version if one is on PATH.
  VER="$(dd-cli --version 2>/dev/null | sed -n 's/.*version \([0-9.]*\).*/\1/p')"
fi
if [ -z "$VER" ]; then
  # Fall back to the latest GitHub release tag (strip leading v if present).
  VER="$(curl -fsSL "${REPO}/releases/latest" 2>/dev/null \
         | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  VER="${VER#v}"
fi
if [ -z "$VER" ]; then
  echo "Could not determine a dd-cli version. Pass one explicitly: bash install-dd-cli.sh 0.2.5" >&2
  exit 1
fi

ASSET="dd-cli-v${VER}-${PLAT}.tar.gz"
DIRNAME="dd-cli-v${VER}-${PLAT}"
URL="${REPO}/releases/download/v${VER}/${ASSET}"
URL_SHA="${URL}.sha256"
echo "==> dd-cli v${VER} for ${PLAT}"
echo "    ${URL}"

# --- 3) Download (curl preferred, wget fallback) --------------------------
dl() { # dl <url> <out>
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    echo "Need curl or wget to download." >&2; exit 1
  fi
}
dl "$URL"      "$TMP/$ASSET"
# The .sha256 sidecar is a small text file; fetch it separately so a missing
# sidecar is a hard error rather than a silent skip.
dl "$URL_SHA"  "$TMP/$ASSET.sha256"

# --- 4) Verify SHA256 ------------------------------------------------------
PUB="$(awk '{print $1}' "$TMP/$ASSET.sha256" | tr 'A-F' 'a-f')"
ACT="$(
  if command -v shasum >/dev/null 2>&1;   then shasum -a 256 "$TMP/$ASSET" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$TMP/$ASSET" | awk '{print $1}'
  else
    echo "need shasum or sha256sum to verify" >&2; exit 1
  fi
)"
if [ "$PUB" != "$ACT" ]; then
  echo "SHA256 MISMATCH — expected ${PUB}, got ${ACT}. Aborting." >&2
  exit 1
fi
echo "==> SHA256 OK"

# --- 5) Extract and install ------------------------------------------------
tar -xzf "$TMP/$ASSET" -C "$TMP"
bash "$TMP/$DIRNAME/install.sh"

# --- 6) Verify + PATH hint -------------------------------------------------
BIN="${HOME}/.local/bin/dd-cli"
if [ -x "$BIN" ]; then
  echo "==> Installed: $($BIN --version 2>&1 || echo 'version check pending')"
else
  echo "==> install.sh finished but $BIN was not found; check the output above." >&2
fi
case ":$PATH:" in
  *":${HOME}/.local/bin:"*) : ;;
  *) echo "==> Add to PATH:  export PATH=\"\$HOME/.local/bin:\$PATH\""
     echo "    (or: source ~/.bashrc / ~/.profile)" ;;
esac
