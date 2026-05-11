#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOURCE_SVG="${SOURCE_SVG:-${REPO_ROOT}/imgs/stemdeck-svg-assets/stemdeck-icon.svg}"
ICON_DIR="${REPO_ROOT}/desktop/src-tauri/icons"
WORK_DIR="${TMPDIR:-/tmp}/stemdeck-iconset"
RENDER_DIR="${WORK_DIR}/render"
ICONSET_DIR="${WORK_DIR}/icon.iconset"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: make-iconset.sh must run on macOS" >&2
  exit 1
fi

for cmd in qlmanage sips iconutil; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found on PATH: $cmd" >&2
    exit 1
  fi
done

if [[ ! -f "$SOURCE_SVG" ]]; then
  echo "ERROR: source SVG not found: $SOURCE_SVG" >&2
  exit 1
fi

rm -rf "$WORK_DIR"
mkdir -p "$RENDER_DIR" "$ICONSET_DIR" "$ICON_DIR"

qlmanage -t -s 1024 -o "$RENDER_DIR" "$SOURCE_SVG" >/dev/null
RENDERED_PNG="$(find "$RENDER_DIR" -maxdepth 1 -type f -name '*.png' | head -1)"
if [[ -z "$RENDERED_PNG" || ! -f "$RENDERED_PNG" ]]; then
  echo "ERROR: qlmanage did not render a PNG from $SOURCE_SVG" >&2
  exit 1
fi

cp "$RENDERED_PNG" "$ICON_DIR/icon.png"

for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$ICON_DIR/icon.png" \
    --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
done

sips -z 32 32 "$ICON_DIR/icon.png" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
sips -z 64 64 "$ICON_DIR/icon.png" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
sips -z 256 256 "$ICON_DIR/icon.png" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 512 512 "$ICON_DIR/icon.png" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
cp "$ICON_DIR/icon.png" "$ICONSET_DIR/icon_512x512@2x.png"

iconutil -c icns "$ICONSET_DIR" -o "$ICON_DIR/icon.icns"

echo "==> Icon PNG: $ICON_DIR/icon.png"
echo "==> Icon ICNS: $ICON_DIR/icon.icns"
