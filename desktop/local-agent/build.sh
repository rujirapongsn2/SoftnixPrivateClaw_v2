#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
PYINSTALLER="${SOFTNIX_PYINSTALLER:-/private/tmp/softnix-agent-portable-venv/bin/pyinstaller}"
BUILD_DIR="${SOFTNIX_AGENT_BUILD_DIR:-/private/tmp/softnix-agent-build-portable}"
APP="$BUILD_DIR/Softnix Local Agent.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" dist/local-agent
"$PYINSTALLER" --noconfirm --onefile --name softnix-local-agent --paths sbot \
  --distpath "$BUILD_DIR/binary" --workpath "$BUILD_DIR/pyinstaller" --specpath "$BUILD_DIR" sbot/softnix_local_agent.py
cp "$BUILD_DIR/binary/softnix-local-agent" "$APP/Contents/MacOS/"
cp desktop/local-agent/Info.plist "$APP/Contents/Info.plist"
xcrun swiftc desktop/local-agent/main.swift -target "$(uname -m)-apple-macos12.0" -o "$APP/Contents/MacOS/SoftnixLocalAgent" -framework Cocoa
codesign --force --deep --sign "${SOFTNIX_SIGN_IDENTITY:--}" "$APP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "dist/local-agent/softnix-local-agent-macos-$(uname -m).zip"
shasum -a 256 "dist/local-agent/softnix-local-agent-macos-$(uname -m).zip"
