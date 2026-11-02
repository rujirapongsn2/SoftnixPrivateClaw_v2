#!/usr/bin/env bash
# Install the pinned GridCraft CLI used by the `spreadsheet` tool (claw/tools/spreadsheet.py).
# Idempotent; verifies the SHA-256 published with the release. Installs:
#   <dest>/linux-<arch>/gridcraft-cli  (mounted into the claw-sandbox container)
#   <dest>/macos/gridcraft-cli         (macOS hosts: native fallback when the sandbox is off)
# Usage: scripts/install-gridcraft.sh [dest]   (default ~/.softnix/claw/vendor/gridcraft/0.14.2)
set -euo pipefail

VERSION="0.14.2"
BASE="https://github.com/storytold/gridcraft/releases/download/v${VERSION}"
DEST="${1:-${CLAW_SPREADSHEET_VENDOR_DIR:-$HOME/.softnix/claw/vendor/gridcraft/$VERSION}}"
DEST="${DEST/#\~/$HOME}"

# From the release's SHA256SUMS.txt (v0.14.2). Bump together with GRIDCRAFT_VERSION.
sha_for() {
  case "$1" in
    gridcraft-${VERSION}-linux-aarch64.tar.gz) echo bc2cfbbc93483ed8c487f55007f8b88c12bd7806f12098446af850d02c92e307 ;;
    gridcraft-${VERSION}-linux-x86_64.tar.gz) echo 9c8153b7630adfbdef8021aa9b982e1ef187b6cf27cd2233280cfaf0ba6a1842 ;;
    gridcraft-cli-${VERSION}-macos-universal.zip) echo 79ce97d3cc1746a1d420ba59c90bcd3d01a27b00009b8627a0dc90cca4be5941 ;;
    *) return 1 ;;
  esac
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

fetch() {  # fetch <asset> <dir>
  local asset="$1" dir="$2" want got
  want="$(sha_for "$asset")"
  if [ ! -f "$dir/$asset" ] || [ "$(sha256 "$dir/$asset")" != "$want" ]; then
    echo "downloading $asset"
    curl -fsSL --connect-timeout 15 --max-time 600 -o "$dir/$asset.part" "$BASE/$asset"
    mv "$dir/$asset.part" "$dir/$asset"
  fi
  got="$(sha256 "$dir/$asset")"
  if [ "$got" != "$want" ]; then
    echo "checksum mismatch for $asset: got $got want $want" >&2
    rm -f "$dir/$asset"
    exit 1
  fi
}

case "$(uname -m)" in
  arm64|aarch64) ARCH=aarch64 ;;
  x86_64|amd64) ARCH=x86_64 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$DEST/dl" "$DEST/linux-$ARCH"
LINUX="gridcraft-${VERSION}-linux-${ARCH}.tar.gz"
fetch "$LINUX" "$DEST/dl"
tar -xzf "$DEST/dl/$LINUX" -C "$DEST/dl" "gridcraft-${VERSION}-linux-${ARCH}/bin/gridcraft-cli"
install -m 0755 "$DEST/dl/gridcraft-${VERSION}-linux-${ARCH}/bin/gridcraft-cli" "$DEST/linux-$ARCH/gridcraft-cli"

if [ "$(uname -s)" = "Darwin" ]; then
  MAC="gridcraft-cli-${VERSION}-macos-universal.zip"
  fetch "$MAC" "$DEST/dl"
  mkdir -p "$DEST/macos"
  unzip -oq "$DEST/dl/$MAC" -d "$DEST/dl/mac"
  install -m 0755 "$DEST/dl/mac/gridcraft-cli-${VERSION}-macos-universal/gridcraft-cli" "$DEST/macos/gridcraft-cli"
  xattr -dr com.apple.quarantine "$DEST/macos" 2>/dev/null || true
  "$DEST/macos/gridcraft-cli" version
fi

echo "GridCraft $VERSION installed in $DEST"
