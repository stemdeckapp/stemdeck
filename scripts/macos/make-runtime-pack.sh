#!/usr/bin/env bash
set -euo pipefail

ARCH="${ARCH:-arm64}"
VERSION="${VERSION:-LOCAL_DEV_TEST}"
VERSION="${VERSION#v}"
RELEASE_BASE_URL="${RELEASE_BASE_URL:-https://github.com/thcp/stemdeck/releases/download/v${VERSION}}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/.build"
STAGING="${BUILD_DIR}/runtime-staging"
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

for cmd in shasum tar "$PYTHON_BIN"; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found on PATH: $cmd" >&2
    exit 1
  fi
done

copy_python_runtime_libs() {
  local source_python="$1"
  local venv_dir="$2"
  local lib_dir="${venv_dir}/lib"
  mkdir -p "$lib_dir"

  "$source_python" - <<'PY' | while IFS= read -r path; do
import os
import sys
import sysconfig

names = [
    sysconfig.get_config_var("LDLIBRARY"),
    f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib",
]
dirs = [
    sysconfig.get_config_var("LIBDIR"),
    os.path.join(sys.base_prefix, "lib"),
    os.path.join(os.path.dirname(sys.executable), "..", "lib"),
]
seen = set()
for directory in dirs:
    if not directory:
        continue
    directory = os.path.abspath(directory)
    for name in names:
        if not name:
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path) and path not in seen:
            seen.add(path)
            print(path)
PY
    cp -f "$path" "$lib_dir/"
  done
}

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

echo "==> Creating Python runtime (${ARCH})"
echo "==> Python: $("$PYTHON_BIN" --version)"
echo "==> Python architecture: ${PYTHON_MACHINE}"
"$PYTHON_BIN" -m venv --copies --without-pip "$PYTHON_DIR"
copy_python_runtime_libs "$PYTHON_BIN" "$PYTHON_DIR"
"$PYTHON_DIR/bin/python" -m ensurepip --upgrade --default-pip
"$PYTHON_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$PYTHON_DIR/bin/python" -m pip install "$REPO_ROOT"

echo "==> Verifying runtime imports"
"$PYTHON_DIR/bin/python" - <<'PY'
import importlib
packages = [
    "fastapi", "uvicorn", "yt_dlp", "demucs", "torch", "torchaudio",
    "librosa", "pyloudnorm", "soundfile",
]
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

echo "==> Capturing dependency inventory"
mkdir -p "$RUNTIME_DIR/licenses"
"$PYTHON_DIR/bin/python" -m pip list --format=json > "$RUNTIME_DIR/licenses/pip-list.json"

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

ARCHIVE_NAME="StemDeck-runtime-macOS-${ARCH}.tar.zst"
ARCHIVE_PATH="${BUILD_DIR}/${ARCHIVE_NAME}"
if command -v zstd >/dev/null 2>&1; then
  tar --zstd -cf "$ARCHIVE_PATH" -C "$STAGING" runtime
else
  ARCHIVE_NAME="StemDeck-runtime-macOS-${ARCH}.tar.gz"
  ARCHIVE_PATH="${BUILD_DIR}/${ARCHIVE_NAME}"
  tar -czf "$ARCHIVE_PATH" -C "$STAGING" runtime
fi

SIZE="$(stat -f%z "$ARCHIVE_PATH")"
SHA256="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')"
RUNTIME_URL="${RELEASE_BASE_URL}/${ARCHIVE_NAME}"

cat > "${BUILD_DIR}/runtime-manifest.json" <<JSON
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
echo "Manifest: ${BUILD_DIR}/runtime-manifest.json"
