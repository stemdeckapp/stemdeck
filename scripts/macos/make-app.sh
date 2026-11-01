#!/usr/bin/env bash
set -euo pipefail

ARCH="${ARCH:-arm64}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Default the version to the current git tag so local builds match the tag with
# no VERSION passed; falls back to a dev label outside a tagged checkout. (#169)
VERSION="${VERSION:-$(git -C "$REPO_ROOT" describe --tags --always 2>/dev/null || echo 0.0.0-dev)}"
VERSION="${VERSION#v}"
BUILD_DIR="${REPO_ROOT}/.build"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: make-app.sh must run on macOS" >&2
  exit 1
fi

if [[ "$ARCH" != "arm64" && "$ARCH" != "x64" ]]; then
  echo "ERROR: ARCH must be arm64 or x64, got '${ARCH}'" >&2
  exit 1
fi

for cmd in node npm cargo; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found on PATH: $cmd" >&2
    exit 1
  fi
done

mkdir -p "$BUILD_DIR"

echo "==> Stamping version ${VERSION}"
sed -i '' "s/^version = \".*\"/version = \"${VERSION}\"/" "$REPO_ROOT/desktop/src-tauri/Cargo.toml"
sed -i '' "s/\"version\": \".*\"/\"version\": \"${VERSION}\"/" "$REPO_ROOT/desktop/src-tauri/tauri.conf.json"
sed -i '' "s/\"version\": \".*\"/\"version\": \"${VERSION}\"/" "$REPO_ROOT/desktop/package.json"

cd "$REPO_ROOT/desktop"

# Tauri CLI reads CI env var as a boolean flag; Woodpecker sets CI=woodpecker
# which fails the boolean parser. Force a valid value.
export CI=true

npm ci
# Tauri signs and notarizes on its own when it sees these variables, but this
# script adds files to Contents/Resources after the build, which would break
# that seal (#857). Hide them from Tauri and sign once at the end instead.
TAURI_ENV=(env -u APPLE_SIGNING_IDENTITY -u APPLE_CERTIFICATE -u APPLE_CERTIFICATE_PASSWORD
  -u APPLE_API_ISSUER -u APPLE_API_KEY -u APPLE_API_KEY_PATH
  -u APPLE_ID -u APPLE_PASSWORD -u APPLE_TEAM_ID)
if [[ "$ARCH" == "arm64" ]]; then
  "${TAURI_ENV[@]}" npx tauri build --bundles app --target aarch64-apple-darwin
  TARGET_DIR="$REPO_ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release"
else
  "${TAURI_ENV[@]}" npx tauri build --bundles app --target x86_64-apple-darwin
  TARGET_DIR="$REPO_ROOT/desktop/src-tauri/target/x86_64-apple-darwin/release"
fi

APP_DIR="${TARGET_DIR}/bundle/macos/StemDeck.app"
if [[ ! -d "$APP_DIR" ]]; then
  APP_DIR="$(find "$REPO_ROOT/desktop/src-tauri/target" -path '*/bundle/macos/StemDeck.app' -type d | head -1)"
fi
if [[ -z "$APP_DIR" || ! -d "$APP_DIR" ]]; then
  echo "ERROR: could not find built StemDeck.app" >&2
  exit 1
fi

RESOURCES="${APP_DIR}/Contents/Resources"
mkdir -p "$RESOURCES"

if [[ -f "$BUILD_DIR/runtime-manifest-${ARCH}.json" ]]; then
  cp "$BUILD_DIR/runtime-manifest-${ARCH}.json" "$RESOURCES/runtime-manifest.json"
else
  cp "$REPO_ROOT/desktop/ui/runtime-manifest.json" "$RESOURCES/runtime-manifest.json"
fi

if [[ -f "$REPO_ROOT/packaging/macos/THIRD_PARTY_NOTICES.txt" ]]; then
  cp "$REPO_ROOT/packaging/macos/THIRD_PARTY_NOTICES.txt" "$RESOURCES/THIRD_PARTY_NOTICES.txt"
fi
# The AGPL asks for a copy of the license with every copy of the program.
cp "$REPO_ROOT/LICENSE" "$RESOURCES/LICENSE.txt"

# Signing must be the last thing that touches the bundle: any file changed
# after this invalidates the seal and Gatekeeper reports the app as damaged.
# shellcheck source=scripts/macos/notarize.sh
source "$REPO_ROOT/scripts/macos/notarize.sh"
ENTITLEMENTS="$REPO_ROOT/desktop/src-tauri/Entitlements.plist"
MAIN_EXE="${APP_DIR}/Contents/MacOS/stemdeck"
if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  echo "==> Signing with Developer ID (hardened runtime)"
  for target in "$MAIN_EXE" "$APP_DIR"; do
    codesign --force --options runtime --timestamp \
      --entitlements "$ENTITLEMENTS" \
      --sign "$APPLE_SIGNING_IDENTITY" "$target"
  done
else
  echo "NOTICE: APPLE_SIGNING_IDENTITY is not set. Sealing ad hoc: this build is for"
  echo "        local use only, is not notarized, and other Macs will reject it."
  for target in "$MAIN_EXE" "$APP_DIR"; do
    codesign --force --sign - "$target"
  done
fi
codesign --verify --deep --strict --verbose=2 "$APP_DIR"

if notarize_credentials_set; then
  NOTARY_DIR="$(mktemp -d -t stemdeck-notary-app)"
  NOTARY_ZIP="${NOTARY_DIR}/StemDeck-${ARCH}.zip"
  ditto -c -k --keepParent "$APP_DIR" "$NOTARY_ZIP"
  notarize_submit "$NOTARY_ZIP"
  rm -f "$NOTARY_ZIP"
  rmdir "$NOTARY_DIR"
  staple_and_validate "$APP_DIR"
elif [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  echo "NOTICE: APPLE_API_ISSUER, APPLE_API_KEY or APPLE_API_KEY_PATH is not set;"
  echo "        the app is signed but not notarized."
fi

echo "$APP_DIR" > "$BUILD_DIR/app-path-${ARCH}.txt"
echo "==> App ready: $APP_DIR"
