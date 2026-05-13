#!/usr/bin/env bash
set -euo pipefail

ARCH="${ARCH:-arm64}"
VERSION="${VERSION:-LOCAL_DEV_TEST}"
VERSION="${VERSION#v}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/.build"
DIST_DIR="${BUILD_DIR}/macos-dist"
DMG_STAGING="${BUILD_DIR}/dmg-staging-${ARCH}"
DMG_NAME="StemDeck-macOS-${ARCH}.dmg"
DMG_PATH="${DIST_DIR}/${DMG_NAME}"
DMG_RW_PATH="${DIST_DIR}/StemDeck-macOS-${ARCH}.rw.dmg"
RUNTIME_NAME="StemDeck-runtime-macOS-${ARCH}.tar.zst"
RUNTIME_PATH="${BUILD_DIR}/${RUNTIME_NAME}"
BACKGROUND_SRC="${REPO_ROOT}/packaging/macos/dmg-background.svg"
BACKGROUND_DIR_NAME=".background"
BACKGROUND_PNG_NAME="dmg-background.png"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: make-dmg.sh must run on macOS" >&2
  exit 1
fi

if [[ "$ARCH" != "arm64" && "$ARCH" != "x64" ]]; then
  echo "ERROR: ARCH must be arm64 or x64, got '${ARCH}'" >&2
  exit 1
fi

for cmd in ditto hdiutil qlmanage shasum; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found on PATH: $cmd" >&2
    exit 1
  fi
done

if [[ "$ARCH" == "arm64" ]]; then
  APP_DIR="${REPO_ROOT}/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/StemDeck.app"
else
  APP_DIR="${REPO_ROOT}/desktop/src-tauri/target/x86_64-apple-darwin/release/bundle/macos/StemDeck.app"
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "ERROR: built app not found: $APP_DIR" >&2
  echo "Run: ARCH=${ARCH} scripts/macos/make-app.sh" >&2
  exit 1
fi

if [[ ! -f "$RUNTIME_PATH" ]]; then
  echo "ERROR: runtime pack not found: $RUNTIME_PATH" >&2
  echo "Run: ARCH=${ARCH} VERSION=${VERSION} scripts/macos/make-runtime-pack.sh" >&2
  exit 1
fi

rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING" "$DIST_DIR"
mkdir -p "$DMG_STAGING/$BACKGROUND_DIR_NAME"

ditto "$APP_DIR" "$DMG_STAGING/StemDeck.app"
ln -s /Applications "$DMG_STAGING/Applications"

if [[ -f "$BACKGROUND_SRC" ]]; then
  qlmanage -t -s 1320 -o "$DMG_STAGING/$BACKGROUND_DIR_NAME" "$BACKGROUND_SRC" >/dev/null 2>&1
  mv "$DMG_STAGING/$BACKGROUND_DIR_NAME/dmg-background.svg.png" "$DMG_STAGING/$BACKGROUND_DIR_NAME/$BACKGROUND_PNG_NAME"
fi

if [[ -f "$REPO_ROOT/packaging/macos/README-macOS.txt" ]]; then
  cp "$REPO_ROOT/packaging/macos/README-macOS.txt" "$DMG_STAGING/README-macOS.txt"
fi

if [[ -f "$REPO_ROOT/packaging/macos/THIRD_PARTY_NOTICES.txt" ]]; then
  cp "$REPO_ROOT/packaging/macos/THIRD_PARTY_NOTICES.txt" "$DMG_STAGING/THIRD_PARTY_NOTICES.txt"
fi

rm -f "$DMG_PATH" "$DMG_RW_PATH"
hdiutil create \
  -volname "StemDeck" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDRW \
  "$DMG_RW_PATH"

MOUNT_DIR="$(mktemp -d /tmp/stemdeck-dmg.XXXXXX)"
cleanup_mount() {
  hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || true
  rmdir "$MOUNT_DIR" >/dev/null 2>&1 || true
}
trap cleanup_mount EXIT

hdiutil attach "$DMG_RW_PATH" -readwrite -noverify -nobrowse -mountpoint "$MOUNT_DIR" >/dev/null

if command -v SetFile >/dev/null 2>&1; then
  SetFile -a V "$MOUNT_DIR/$BACKGROUND_DIR_NAME" || true
  SetFile -a V "$MOUNT_DIR/README-macOS.txt" || true
  SetFile -a V "$MOUNT_DIR/THIRD_PARTY_NOTICES.txt" || true
fi

if [[ -f "$MOUNT_DIR/$BACKGROUND_DIR_NAME/$BACKGROUND_PNG_NAME" ]]; then
  osascript <<APPLESCRIPT || echo "warning: DMG window styling failed (cosmetic only, DMG is still valid)"
tell application "Finder"
  set dmgFolder to POSIX file "$MOUNT_DIR" as alias
  open dmgFolder
  set current view of container window of dmgFolder to icon view
  set toolbar visible of container window of dmgFolder to false
  set statusbar visible of container window of dmgFolder to false
  set bounds of container window of dmgFolder to {100, 100, 760, 500}
  set viewOptions to icon view options of container window of dmgFolder
  set arrangement of viewOptions to not arranged
  set icon size of viewOptions to 104
  set text size of viewOptions to 13
  set background picture of viewOptions to POSIX file "$MOUNT_DIR/$BACKGROUND_DIR_NAME/$BACKGROUND_PNG_NAME"
  set position of item "StemDeck.app" of dmgFolder to {205, 205}
  set position of item "Applications" of dmgFolder to {455, 205}
  close container window of dmgFolder
  open dmgFolder
  update dmgFolder without registering applications
  delay 1
  close container window of dmgFolder
end tell
APPLESCRIPT
fi

sync
hdiutil detach "$MOUNT_DIR" >/dev/null
rmdir "$MOUNT_DIR"
trap - EXIT

hdiutil convert "$DMG_RW_PATH" -format UDZO -imagekey zlib-level=9 -o "$DMG_PATH" >/dev/null
rm -f "$DMG_RW_PATH"

CHECKSUMS_PATH="${DIST_DIR}/SHA256SUMS-macOS-${ARCH}.txt"
{
  shasum -a 256 "$DMG_PATH"
  shasum -a 256 "$RUNTIME_PATH"
} | sed "s#${REPO_ROOT}/##" > "$CHECKSUMS_PATH"

echo "==> DMG ready: $DMG_PATH"
echo "==> Runtime pack: $RUNTIME_PATH"
echo "==> Checksums: $CHECKSUMS_PATH"
