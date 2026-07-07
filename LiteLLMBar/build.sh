#!/usr/bin/env bash
# Build LiteLLMBar.app into LiteLLMBar/dist/ and (re)install to /Applications.
# SMAppService login items need a stable path, hence the install step.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release

APP=dist/LiteLLMBar.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/LiteLLMBar "$APP/Contents/MacOS/"
cp Info.plist "$APP/Contents/"
codesign --force --sign - "$APP"

echo "Built $APP"
if [ "${1:-}" = "--install" ]; then
  rm -rf /Applications/LiteLLMBar.app
  cp -R "$APP" /Applications/
  echo "Installed to /Applications/LiteLLMBar.app"
fi
