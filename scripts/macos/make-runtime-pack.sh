#!/usr/bin/env bash
set -euo pipefail

ARCH="${ARCH:-arm64}"
VERSION="${VERSION:-LOCAL_DEV_TEST}"
VERSION="${VERSION#v}"
RELEASE_BASE_URL="${RELEASE_BASE_URL:-https://github.com/stemdeckapp/stemdeck/releases/download/v${VERSION}}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/.build"
STAGING="${BUILD_DIR}/runtime-staging-${ARCH}"
RUNTIME_DIR="${STAGING}/runtime"
PYTHON_DIR="${RUNTIME_DIR}/python"
BACKEND_DIR="${RUNTIME_DIR}/backend"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: make-runtime-pack.sh must run on macOS" >&2
  exit 1
fi

if [[ "$ARCH" != "arm64" && "$ARCH" != "x64" ]]; then
  echo "ERROR: ARCH must be arm64 or x64, got '${ARCH}'" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

for cmd in ditto shasum tar "$PYTHON_BIN"; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found on PATH: $cmd" >&2
    exit 1
  fi
done

PYTHON_VERSION="$("$PYTHON_BIN" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
PYTHON_MACHINE="$("$PYTHON_BIN" - <<'PY'
import platform
print(platform.machine())
PY
)"
case "$PYTHON_VERSION" in
  3.10|3.11|3.12|3.13) ;;
  *)
    echo "ERROR: ${PYTHON_BIN} is Python ${PYTHON_VERSION}; Torch 2.6 runtime builds require Python 3.10-3.13." >&2
    echo "Set PYTHON_BIN=/path/to/python3.12 or PYTHON_BIN=/path/to/python3.11." >&2
    exit 1
    ;;
esac

HOST_ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" && "$PYTHON_MACHINE" != "arm64" ]]; then
  echo "ERROR: arm64 runtime requires an arm64 Python, got ${PYTHON_MACHINE}" >&2
  exit 1
fi
if [[ "$ARCH" == "x64" && "$PYTHON_MACHINE" != "x86_64" ]]; then
  echo "ERROR: x64 runtime requires an x86_64 Python, got ${PYTHON_MACHINE}" >&2
  exit 1
fi
if [[ "$ARCH" == "x64" && "$HOST_ARCH" == "arm64" ]]; then
  if ! arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
    echo "ERROR: x64 runtime on arm64 hosts requires Rosetta 2." >&2
    exit 1
  fi
fi

rm -rf "$STAGING"
mkdir -p "$PYTHON_DIR" "$BACKEND_DIR" "$BUILD_DIR"

echo "==> Bundling Python installation (${ARCH})"
echo "==> Python: $("$PYTHON_BIN" --version)"
echo "==> Python architecture: ${PYTHON_MACHINE}"

# Get the full PBS (python-build-standalone) installation root.
# This directory has the complete stdlib in lib/pythonX.Y/ — unlike a venv,
# which only creates site-packages/ and relies on the original base_prefix
# (a path that won't exist on user machines) for stdlib.
PYTHON_BASE_PREFIX="$("$PYTHON_BIN" - <<'PY'
import sys
print(sys.base_prefix)
PY
)"
echo "==> Python base prefix: ${PYTHON_BASE_PREFIX}"

if [[ ! -d "${PYTHON_BASE_PREFIX}/lib" ]]; then
  echo "ERROR: Python base prefix has no lib/ dir: ${PYTHON_BASE_PREFIX}" >&2
  echo "  Make sure PYTHON_BIN points to a python-build-standalone (UV) Python." >&2
  exit 1
fi

# Verify the stdlib is actually present in base_prefix before copying.
STDLIB_CHECK="$("$PYTHON_BIN" - <<'PY'
import sys, pathlib
ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
p = pathlib.Path(sys.base_prefix) / "lib" / ver / "encodings" / "__init__.py"
print("ok" if p.is_file() else f"missing:{p}")
PY
)"
if [[ "$STDLIB_CHECK" != "ok" ]]; then
  echo "ERROR: stdlib not found in base_prefix (${STDLIB_CHECK})" >&2
  echo "  base_prefix: ${PYTHON_BASE_PREFIX}" >&2
  exit 1
fi

# Copy the entire PBS Python installation into the runtime bundle.
# ditto preserves symlinks, HFS+ metadata, and extended attributes.
ditto "$PYTHON_BASE_PREFIX" "$PYTHON_DIR"

# PBS Python ships an EXTERNALLY-MANAGED marker that blocks uv from installing
# packages into it. Remove it so we can treat this copy as our own install.
find "$PYTHON_DIR/lib" -name "EXTERNALLY-MANAGED" -delete

echo "==> Installing packages into bundled Python"
# --system is required because $PYTHON_DIR is not a venv (it's a full Python install).
uv pip install --system --python "$PYTHON_DIR/bin/python" pip setuptools wheel
# The project version is git-derived (hatch-vcs). Pin it explicitly from $VERSION
# so the install doesn't depend on git tags being present in the build checkout (#169).
SETUPTOOLS_SCM_PRETEND_VERSION="${VERSION#v}" \
  uv pip install --system --python "$PYTHON_DIR/bin/python" "$REPO_ROOT"

echo "==> Verifying stdlib and imports"
PYTHON_DIR="$PYTHON_DIR" PYTHONHOME="$PYTHON_DIR" ARCH="$ARCH" "$PYTHON_DIR/bin/python" - <<'PY'
import importlib, os, pathlib, sys

ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
stdlib = pathlib.Path(os.environ["PYTHON_DIR"]) / "lib" / ver
if not (stdlib / "encodings" / "__init__.py").is_file():
    print(f"ERROR: encodings not found in {stdlib}", file=sys.stderr)
    sys.exit(1)
print(f"  stdlib OK at {stdlib}")

packages = [
    "fastapi", "uvicorn", "yt_dlp", "demucs", "torch", "torchaudio",
    "librosa", "pyloudnorm", "soundfile",
]
# audio_separator/onnxruntime (vocal split, #275) are excluded on Intel macOS
# (x64) -- the feature gates itself off there, matching pyproject.toml's
# platform marker. Missing here would have caught #407 before release.
if os.environ.get("ARCH") != "x64":
    packages += ["audio_separator", "onnxruntime"]
for package in packages:
    importlib.import_module(package)
    print(f"  OK {package}")
PY

echo "==> Staging backend"
cp -R "$REPO_ROOT/app" "$BACKEND_DIR/app"
cp -R "$REPO_ROOT/static" "$BACKEND_DIR/static"
cp "$REPO_ROOT/pyproject.toml" "$BACKEND_DIR/pyproject.toml"
cp "$REPO_ROOT/uv.lock" "$BACKEND_DIR/uv.lock"

cat > "$BACKEND_DIR/static/version.json" <<JSON
{
  "version": "${VERSION}",
  "arch": "${ARCH}"
}
JSON

# QuickJS, for YouTube's signature/n-challenge solver (#438). yt-dlp ships the
# solver script (the yt-dlp-ejs dependency) but needs a JavaScript engine to
# run it, and a packaged install has nothing on PATH.
#
# It rides inside backend/, the same place Windows and Linux put it, so all
# three packages agree and config.bundled_js_runtime() has one layout to find.
# Not the app's data directory: on macOS that lives in ~/Library/Application
# Support and is user-owned, so a binary there would have to be installed at
# first run rather than shipped.
#
# Pinned by version and SHA256, the same rule as the macOS FFmpeg download
# (#172).
# ARCH is this script's own value, validated at the top as arm64 or x64. It is
# not the asset naming, which uses x86_64: mapping between the two is the whole
# job of this case, and conflating them is what broke the first x64 build.
QJS_VERSION="v0.16.2"
case "$ARCH" in
  arm64) QJS_ASSET="qjs-darwin-arm64";  QJS_SHA256="f6200e9856c45578a5d42ac873a32f3f994b421e29df9f63b452d9c7145015fc" ;;
  x64)   QJS_ASSET="qjs-darwin-x86_64"; QJS_SHA256="4448991c0500dbe40c7b2f91ba39275995413aa4ee59db3b513b68350908a413" ;;
  *) echo "ERROR: no QuickJS mapping for ARCH '${ARCH}'" >&2; exit 1 ;;
esac
QJS_DIR="${BACKEND_DIR}/jsruntime"
mkdir -p "$QJS_DIR"
echo "==> Fetching QuickJS ${QJS_VERSION} (${QJS_ASSET})"
curl -fsSL --retry 3 -o "${QJS_DIR}/qjs"   "https://github.com/quickjs-ng/quickjs/releases/download/${QJS_VERSION}/${QJS_ASSET}"
echo "${QJS_SHA256}  ${QJS_DIR}/qjs" | shasum -a 256 -c - >/dev/null || {
    rm -f "${QJS_DIR}/qjs"
    echo "QuickJS checksum mismatch" >&2
    exit 1
}
chmod +x "${QJS_DIR}/qjs"

echo "==> Capturing dependency inventory"
mkdir -p "$RUNTIME_DIR/licenses"
uv pip list --system --python "$PYTHON_DIR/bin/python" --format=json > "$RUNTIME_DIR/licenses/pip-list.json"
# pip-list.json is names and versions, which is an inventory but not a
# notice. MIT, BSD and Apache-2.0 all require the copyright line and the
# license text itself to travel with a binary, so collect those too.
"$PYTHON_DIR/bin/python" "${REPO_ROOT}/scripts/collect_licenses.py" \
  "$PYTHON_DIR/lib/python${PYTHON_VERSION}/site-packages" \
  "$RUNTIME_DIR/licenses"

cat > "$RUNTIME_DIR/runtime-manifest.json" <<JSON
{
  "version": "${VERSION}",
  "arch": "${ARCH}",
  "createdBy": "scripts/macos/make-runtime-pack.sh"
}
JSON

echo "==> Stripping Python caches"
find "$PYTHON_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PYTHON_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# The ditto above preserves extended attributes on every copied file (#505).
# macOS tar then serializes those xattrs as AppleDouble "._name" members, and
# the Rust tar crate that unpacks this archive on the user's machine knows
# nothing about AppleDouble, so it writes them out as literal files. One of
# them lands in matplotlib's style directory, where the "*.mplstyle" glob picks
# it up and chokes on the binary header -- taking down every import of
# matplotlib.pyplot, and with it allin1_infer and automatic song sections.
# Strip the xattrs, delete any sidecars already on disk, and tell tar not to
# regenerate them.
echo "==> Stripping extended attributes and AppleDouble sidecars"
xattr -cr "$STAGING" 2>/dev/null || true
find "$STAGING" -name "._*" -delete

export COPYFILE_DISABLE=1
ARCHIVE_NAME="StemDeck-runtime-macOS-${ARCH}.tar.zst"
ARCHIVE_PATH="${BUILD_DIR}/${ARCHIVE_NAME}"
if command -v zstd >/dev/null 2>&1; then
  tar --zstd -cf "$ARCHIVE_PATH" -C "$STAGING" runtime
else
  ARCHIVE_NAME="StemDeck-runtime-macOS-${ARCH}.tar.gz"
  ARCHIVE_PATH="${BUILD_DIR}/${ARCHIVE_NAME}"
  tar -czf "$ARCHIVE_PATH" -C "$STAGING" runtime
fi

# The three guards above are all environment-dependent -- whether macOS tar
# emits AppleDouble members at all varies by OS version -- so verify the actual
# archive rather than trusting them. A pack that ships even one sidecar is a
# broken pack.
#
# `tar -tf` cannot do this audit: macOS tar folds "._name" members back into
# their sibling's metadata while listing, exactly as it does while creating, so
# it reports a clean archive whether or not one is clean. Stream the members
# through Python's tarfile instead, which has no AppleDouble handling at all.
echo "==> Verifying archive carries no AppleDouble entries"
if [[ "$ARCHIVE_PATH" == *.zst ]]; then
  DECOMPRESS=(zstd -dc "$ARCHIVE_PATH")
else
  DECOMPRESS=(gzip -dc "$ARCHIVE_PATH")
fi
APPLE_DOUBLE="$("${DECOMPRESS[@]}" | "$PYTHON_BIN" -c '
import posixpath, sys, tarfile

found = [
  member.name
  for member in tarfile.open(fileobj=sys.stdin.buffer, mode="r|")
  if posixpath.basename(member.name).startswith("._")
]
print("\n".join(found[:5]))
print(f"({len(found)} total)" if found else "", end="")
')"
if [[ -n "$APPLE_DOUBLE" ]]; then
  echo "ERROR: archive contains AppleDouble ._ entries (see #505)" >&2
  echo "$APPLE_DOUBLE" >&2
  exit 1
fi

SIZE="$(stat -f%z "$ARCHIVE_PATH")"
SHA256="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')"
RUNTIME_URL="${RELEASE_BASE_URL}/${ARCHIVE_NAME}"

cat > "${BUILD_DIR}/runtime-manifest-${ARCH}.json" <<JSON
{
  "version": "${VERSION}",
  "arch": "${ARCH}",
  "runtimeUrl": "${RUNTIME_URL}",
  "runtimeSha256": "${SHA256}",
  "runtimeSize": ${SIZE},
  "archiveName": "${ARCHIVE_NAME}"
}
JSON

echo "==> Runtime pack ready"
echo "Archive:  ${ARCHIVE_PATH}"
echo "Size:     ${SIZE}"
echo "SHA256:   ${SHA256}"
echo "Manifest: ${BUILD_DIR}/runtime-manifest-${ARCH}.json"
