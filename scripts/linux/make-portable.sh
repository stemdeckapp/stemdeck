#!/usr/bin/env bash
#
# Build a portable Linux StemDeck package: a single .tar.gz containing the
# Tauri binary plus a self-contained Python runtime (torch + demucs), so the
# user extracts and runs ./StemDeck with no toolchain.
#
# This is the Linux analog of scripts/windows/make-portable.ps1. Like the macOS
# runtime pack (scripts/macos/make-runtime-pack.sh) it bundles a full
# python-build-standalone install — a plain `venv` will not work because the
# desktop shell checks for the stdlib under python/lib/ (python_stdlib_present
# in desktop/src-tauri/src/main.rs).
#
# Phase 1 ships the CPU-only variant. FFmpeg is NOT bundled in the tarball (so we
# don't redistribute it); instead the desktop shell downloads a static build on
# first launch into the user data dir, falling back to a system `ffmpeg` on PATH
# when one exists (see ensure_ffmpeg / download_linux_ffmpeg).
#
# Layout produced (so find_repo_root matches its backend/app + python branch):
#   StemDeck-Linux-x64/
#     StemDeck                 # Tauri ELF binary
#     cpu-only                 # marker read by is_cpu_only_package
#     README-LINUX.txt
#     THIRD_PARTY_NOTICES.txt
#     backend/{app,static,pyproject.toml,uv.lock}
#     python/{bin/python,lib/pythonX.Y/...}   # full PBS install

set -euo pipefail

PACKAGE_NAME="${PACKAGE_NAME:-StemDeck-Linux-x64}"
PACKAGE_VERSION="${PACKAGE_VERSION:-}"
OUTPUT_ROOT="${OUTPUT_ROOT:-dist}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
TORCH_VERSION="${TORCH_VERSION:-2.6.0}"
SKIP_TAURI_BUILD="${SKIP_TAURI_BUILD:-0}"
# CPU_ONLY=1 (default): force the CPU-only torch wheel and mark the package so the
# desktop shell skips GPU detection. CPU_ONLY=0: keep the project's default torch,
# which on Linux x86_64 is the CUDA build (NVIDIA variant) — the shell then detects
# the GPU and uses CUDA at runtime.
CPU_ONLY="${CPU_ONLY:-1}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE="${REPO_ROOT}/${OUTPUT_ROOT}/${PACKAGE_NAME}"
ARCHIVE_PATH="${REPO_ROOT}/${OUTPUT_ROOT}/${PACKAGE_NAME}.tar.gz"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
PYTHON_DIR="${STAGE}/python"
BACKEND_DIR="${STAGE}/backend"
TARGET_BIN="${REPO_ROOT}/desktop/src-tauri/target/release/stemdeck"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: this packaging script must run on Linux." >&2
  exit 1
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found on PATH: $1" >&2
    exit 1
  fi
}

require_command uv
require_command cargo
require_command node
require_command npm
require_command tar
require_command sha256sum

# python-build-standalone (PBS) Python for x86_64 Linux. Unlike a venv, the
# full install carries its own stdlib under lib/, which the desktop shell needs.
echo "==> Installing python-build-standalone ${PYTHON_VERSION}"
uv python install "cpython-${PYTHON_VERSION}-linux-x86_64-gnu"
PBS_PYTHON="$(uv python find "cpython-${PYTHON_VERSION}-linux-x86_64-gnu")"
PBS_BASE_PREFIX="$("$PBS_PYTHON" -c 'import sys; print(sys.base_prefix)')"
if [[ ! -d "${PBS_BASE_PREFIX}/lib" ]]; then
  echo "ERROR: PBS base prefix has no lib/ dir: ${PBS_BASE_PREFIX}" >&2
  exit 1
fi

echo "==> Cleaning stage"
rm -rf "$STAGE" "$ARCHIVE_PATH" "$CHECKSUM_PATH"
mkdir -p "$STAGE" "$BACKEND_DIR" "$PYTHON_DIR"

# Copy the entire PBS install into python/ (-a preserves symlinks/permissions).
echo "==> Bundling Python runtime from ${PBS_BASE_PREFIX}"
cp -a "$PBS_BASE_PREFIX/." "$PYTHON_DIR/"
# PBS ships an EXTERNALLY-MANAGED marker that blocks installs into the copy.
find "$PYTHON_DIR/lib" -name "EXTERNALLY-MANAGED" -delete 2>/dev/null || true

BUNDLED_PYTHON="${PYTHON_DIR}/bin/python"

echo "==> Installing StemDeck into bundled Python"
# --system is required because python/ is a full PBS install, not a venv.
uv pip install --system --python "$BUNDLED_PYTHON" pip setuptools wheel
# Version is git-derived (hatch-vcs / setuptools-scm). Pin it so the install
# does not depend on git tags in the build checkout (#169).
if [[ -n "$PACKAGE_VERSION" ]]; then
  export SETUPTOOLS_SCM_PRETEND_VERSION="${PACKAGE_VERSION#v}"
fi
uv pip install --system --python "$BUNDLED_PYTHON" "$REPO_ROOT"

# Always bake the small CPU-only torch wheel — for BOTH variants. On Linux the
# default PyPI torch wheel bundles the full CUDA runtime (~2.5 GB), which makes
# the packaged tarball exceed GitHub's 2 GiB per-asset release limit. So we
# mirror what the Windows NVIDIA package actually does: ship CPU torch, and let
# the desktop shell download the matching CUDA wheel at first run on GPU
# machines (install_cuda_torch, gated cfg(not(macos)) so it covers Linux). The
# NVIDIA variant differs only by omitting the cpu-only marker below.
#
# pip strips the local '+cpu' version when resolving, so the project install
# pulls the CUDA wheel even if a CPU wheel was requested; --force-reinstall
# --no-deps replaces just the torch/torchaudio wheels (proven on Windows).
echo "==> Baking CPU-only torch (NVIDIA variant downloads CUDA at first run)"
"$BUNDLED_PYTHON" -m pip install \
  "torch==${TORCH_VERSION}+cpu" "torchaudio==${TORCH_VERSION}+cpu" \
  --index-url https://download.pytorch.org/whl/cpu \
  --force-reinstall --no-deps

# The project install above pulled the default Linux torch, which is the CUDA
# build, dragging in nvidia-* CUDA runtime packages (cuDNN, cuBLAS, NCCL, ...) and
# triton -- together ~2.5 GB. The CPU torch swap used --no-deps, so those packages
# are now orphaned but still installed, bloating the tarball past GitHub's 2 GiB
# asset limit. Remove them: CPU torch does not use them, and the NVIDIA variant
# re-downloads CUDA at first run anyway.
echo "==> Removing orphaned CUDA runtime packages"
orphans=$("$BUNDLED_PYTHON" -m pip list --format=freeze 2>/dev/null \
  | sed -n 's/^\(nvidia-[^=]*\)==.*/\1/p')
orphans="$orphans triton"
echo "    removing:$orphans"
"$BUNDLED_PYTHON" -m pip uninstall -y $orphans 2>/dev/null || true

echo "==> Verifying imports"
"$BUNDLED_PYTHON" -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile; print('torch', torch.__version__, 'cuda', torch.version.cuda)"

echo "==> Staging backend"
cp -R "$REPO_ROOT/app" "$BACKEND_DIR/app"
cp -R "$REPO_ROOT/static" "$BACKEND_DIR/static"
cp "$REPO_ROOT/pyproject.toml" "$BACKEND_DIR/pyproject.toml"
cp "$REPO_ROOT/uv.lock" "$BACKEND_DIR/uv.lock"
RESOLVED_VERSION="${PACKAGE_VERSION#v}"
printf '{ "version": "%s" }\n' "$RESOLVED_VERSION" > "$BACKEND_DIR/static/version.json"

cp "$REPO_ROOT/packaging/linux/README-LINUX.txt" "$STAGE/README-LINUX.txt"
cp "$REPO_ROOT/packaging/linux/THIRD_PARTY_NOTICES.txt" "$STAGE/THIRD_PARTY_NOTICES.txt"

# CPU-only marker: read by is_cpu_only_package so the shell skips GPU detection.
# Omitted for the NVIDIA variant so the shell detects the GPU and uses CUDA.
if [[ "$CPU_ONLY" == "1" ]]; then
  touch "$STAGE/cpu-only"
fi

echo "==> Stripping build-time artifacts from bundled Python"
find "$PYTHON_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PYTHON_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null || true
TORCH_LIB="${PYTHON_DIR}/lib/python${PYTHON_VERSION}/site-packages/torch"
for rel in include test share/cmake; do
  rm -rf "${TORCH_LIB:?}/${rel}" 2>/dev/null || true
done
# Static link archives are only needed to build C++ extensions, never to run.
find "$TORCH_LIB" -name "*.a" -type f -delete 2>/dev/null || true

echo "==> Building Tauri desktop binary"
if [[ "$SKIP_TAURI_BUILD" != "1" ]]; then
  pushd "$REPO_ROOT/desktop" >/dev/null
  if [[ -f package-lock.json ]]; then
    npm ci --include=dev
  else
    npm install --include=dev
  fi
  CI=true node node_modules/@tauri-apps/cli/tauri.js build
  popd >/dev/null
fi

if [[ ! -f "$TARGET_BIN" ]]; then
  echo "ERROR: Tauri binary not found at ${TARGET_BIN}" >&2
  exit 1
fi
cp "$TARGET_BIN" "$STAGE/StemDeck"
chmod +x "$STAGE/StemDeck"

echo "==> Creating archive"
tar -czf "$ARCHIVE_PATH" -C "${REPO_ROOT}/${OUTPUT_ROOT}" "$PACKAGE_NAME"
( cd "${REPO_ROOT}/${OUTPUT_ROOT}" && sha256sum "${PACKAGE_NAME}.tar.gz" > "${PACKAGE_NAME}.tar.gz.sha256" )

echo "==> Done"
if [[ "$CPU_ONLY" == "1" ]]; then
  echo "Variant : CPU-only"
else
  echo "Variant : NVIDIA/CUDA"
fi
echo "Stage   : ${STAGE}"
echo "Archive : ${ARCHIVE_PATH}"
echo "Checksum: ${CHECKSUM_PATH}"
