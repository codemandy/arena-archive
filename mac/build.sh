#!/bin/zsh
# Builds "CHANNEL.app" with the Command Line Tools (no Xcode needed).
#
#   ./mac/build.sh            build into build.noindex/
#   ./mac/build.sh --install  build and copy to ~/Applications
set -euo pipefail

ROOT=${0:A:h:h}
# .noindex keeps Spotlight (and the Apps launcher) from listing the build copy.
BUILD="$ROOT/build.noindex"
APP="$BUILD/CHANNEL.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos13.0" \
  -o "$APP/Contents/MacOS/ArenaArchive" "$ROOT/mac/ArenaArchive.swift" "$ROOT/mac/MenuBar.swift"

cp "$ROOT/server.py" "$ROOT/style.css" "$ROOT/arena_archive.py" "$APP/Contents/Resources/"

"$APP/Contents/MacOS/ArenaArchive" --make-icon "$BUILD/AppIcon.iconset"
iconutil -c icns -o "$APP/Contents/Resources/AppIcon.icns" "$BUILD/AppIcon.iconset"
rm -rf "$BUILD/AppIcon.iconset"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>CHANNEL</string>
  <key>CFBundleDisplayName</key><string>CHANNEL</string>
  <key>CFBundleIdentifier</key><string>studio.oxoy.arena-archive</string>
  <key>CFBundleExecutable</key><string>ArenaArchive</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 1)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# Ad-hoc signature: free, and enough for an app you build yourself.
codesign --force --sign - "$APP"

echo "Built $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/CHANNEL.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "Installed to ~/Applications/CHANNEL.app"
fi
