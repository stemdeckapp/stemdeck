#!/usr/bin/env bash
set -euo pipefail

ARCH="${ARCH:-arm64}"
VERSION="${VERSION:-LOCAL_DEV_TEST}"
VERSION="${VERSION#v}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
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

npm ci
if [[ "$ARCH" == "arm64" ]]; then
  npx tauri build --bundles app --target aarch64-apple-darwin
  TARGET_DIR="$REPO_ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release"
else
  npx tauri build --bundles app --target x86_64-apple-darwin
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

if [[ -f "$BUILD_DIR/runtime-manifest.json" ]]; then
  cp "$BUILD_DIR/runtime-manifest.json" "$RESOURCES/runtime-manifest.json"
else
  cp "$REPO_ROOT/desktop/ui/runtime-manifest.json" "$RESOURCES/runtime-manifest.json"
fi

if [[ -f "$REPO_ROOT/packaging/macos/THIRD_PARTY_NOTICES.txt" ]]; then
  cp "$REPO_ROOT/packaging/macos/THIRD_PARTY_NOTICES.txt" "$RESOURCES/THIRD_PARTY_NOTICES.txt"
fi

echo "$APP_DIR" > "$BUILD_DIR/app-path.txt"
echo "==> App ready: $APP_DIR"
