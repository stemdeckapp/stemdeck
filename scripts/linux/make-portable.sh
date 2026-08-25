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

# QuickJS, for YouTube's signature/n-challenge solver (#438). yt-dlp ships the
# solver script (the yt-dlp-ejs dependency) but needs a JavaScript engine to
# run it, and a portable install has nothing on PATH.
#
# quickjs-ng rather than deno: 2.6 MB against deno's ~110 MB, times two
# bundles, against a 2 GiB release asset cap this project has already hit once
# (#222, #225).
#
# It lives in backend/, not data/. The in-app updater replaces the executable
# and backend/ and leaves data/ alone, so a binary in data/ would only ever
# reach a fresh install. Here it arrives through an ordinary update, which is
# the whole point: shipping this must not cost existing users a reinstall.
#
# Pinned by version and SHA256, the same rule as the macOS FFmpeg download
# (#172). An unverified binary fetched at package time is a supply-chain hole
# whether or not it is small.
QJS_VERSION="v0.16.2"
QJS_SHA256="c5e1b16adfa36def7ac523d6ba54edc77ef66a4dfd65d73e6eae19025f9b7b0a"
QJS_URL="https://github.com/quickjs-ng/quickjs/releases/download/${QJS_VERSION}/qjs-linux-x86_64"
QJS_DIR="${BACKEND_DIR}/jsruntime"
mkdir -p "$QJS_DIR"
echo "==> Fetching QuickJS ${QJS_VERSION}"
curl -fsSL --retry 3 -o "${QJS_DIR}/qjs" "$QJS_URL"
echo "${QJS_SHA256}  ${QJS_DIR}/qjs" | sha256sum -c - >/dev/null || {
    rm -f "${QJS_DIR}/qjs"
    echo "QuickJS checksum mismatch" >&2
    exit 1
}
chmod +x "${QJS_DIR}/qjs"

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
#
# NOTE: unlike Windows, Linux CUDA torch wheels do NOT bundle the CUDA runtime --
# they dlopen it out of these nvidia-* packages at import time. install_cuda_torch
# in the desktop shell therefore runs a second, dependency-resolving pip pass on
# Linux to reinstate them; without it `import torch` dies with
# "libcublas.so.*[0-9] not found" and the backend never starts (#324).
echo "==> Removing orphaned CUDA runtime packages"
orphans=$("$BUNDLED_PYTHON" -m pip list --format=freeze 2>/dev/null \
  | sed -n 's/^\(nvidia-[^=]*\)==.*/\1/p')
orphans="$orphans triton"
echo "    removing:$orphans"
"$BUNDLED_PYTHON" -m pip uninstall -y $orphans 2>/dev/null || true

echo "==> Verifying imports"
# audio_separator/onnxruntime: vocal split (#275). Missing here would have
# caught #407 (librosa 1.0.0 dropping audioread, which audio-separator still
# needs) before release instead of after.
"$BUNDLED_PYTHON" -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile, audio_separator, onnxruntime; print('torch', torch.__version__, 'cuda', torch.version.cuda)"

echo "==> Staging backend"
cp -R "$REPO_ROOT/app" "$BACKEND_DIR/app"
cp -R "$REPO_ROOT/static" "$BACKEND_DIR/static"
cp "$REPO_ROOT/pyproject.toml" "$BACKEND_DIR/pyproject.toml"
cp "$REPO_ROOT/uv.lock" "$BACKEND_DIR/uv.lock"
RESOLVED_VERSION="${PACKAGE_VERSION#v}"
printf '{ "version": "%s" }\n' "$RESOLVED_VERSION" > "$BACKEND_DIR/static/version.json"

cp "$REPO_ROOT/packaging/linux/README-LINUX.txt" "$STAGE/README-LINUX.txt"
cp "$REPO_ROOT/packaging/linux/THIRD_PARTY_NOTICES.txt" "$STAGE/THIRD_PARTY_NOTICES.txt"

# Desktop-entry assets for the optional installer (#360). Carried inside the
# package so the installer needs no second download and no asset URL that could
# drift from the release it is installing. The Tauri icon is already square, so
# it doubles as the desktop icon with no separate artwork to keep in sync.
echo "==> Staging desktop-entry assets"
mkdir -p "$STAGE/packaging"
cp "$REPO_ROOT/desktop/src-tauri/icons/icon.png" "$STAGE/packaging/stemdeck.png"
cp "$REPO_ROOT/packaging/linux/stemdeck.desktop.in" "$STAGE/packaging/stemdeck.desktop.in"

# The optional installer (#361). It installs the package it sits in, so it needs
# no network access and cannot disagree with the build about which version it is
# installing: it reads that from backend/static/version.json above, and the
# variant from the cpu-only marker below.
cp "$REPO_ROOT/packaging/linux/install.sh" "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"

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

# The stdlib's own test suite and every package's bundled test/tests directory
# are never imported at runtime -- on Windows this accounted for ~2,000 files
# and 33 MB (#421). Mirrored here so both platforms ship the same shape.
rm -rf "${PYTHON_DIR}/lib/python${PYTHON_VERSION}/test" 2>/dev/null || true
for d in "${PYTHON_DIR}/lib/python${PYTHON_VERSION}/site-packages"/*/; do
  for name in test tests; do
    rm -rf "${d}${name}" 2>/dev/null || true
  done
done
# NOTE: do NOT strip .dist-info RECORD files. pip needs them to replace a
# package, and the NVIDIA variant pip-installs CUDA torch into this very tree on
# the user's first run (install_cuda_torch).

# Re-verify after the widened strip: the check above ran before it and would not
# catch a strip that removed something load-bearing (#407, #421).
"$BUNDLED_PYTHON" -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile, audio_separator, onnxruntime; print('Post-strip import check OK')"

# Runtime fingerprint, shipped inside python/ in every package (#421).
#
# The in-app updater's SAFETY GATE, not a download trigger: it replaces
# StemDeck + backend/ only and never touches python/, so an app-only update is
# safe exactly when the release needs the same Python dependencies already
# installed. Derived from uv.lock plus the interpreter's major.minor -- the same
# formula make-portable.ps1 uses, deliberately not the package version, which
# changes every release and would make every update look incompatible.
# tr -d '\r': hash the CONTENT with newlines normalised, not the bytes on
# disk, so a Windows checkout (CRLF) and a Linux one (LF) agree on the id for
# an identical lockfile. See the matching note in make-portable.ps1.
RUNTIME_ID="py${PYTHON_VERSION}-$(tr -d '\r' < "${REPO_ROOT}/uv.lock" | sha256sum | cut -c1-16)"
printf '{"runtimeId":"%s"}\n' "$RUNTIME_ID" > "${PYTHON_DIR}/runtime-version.json"
echo "==> Runtime id: ${RUNTIME_ID}"

# Importing above rewrote __pycache__ for everything it touched; on Windows that
# measured 1,912 files / 39 MB and cancelled out most of the strip. Drop it once
# here, after the last time this script runs the packaged interpreter.
find "$PYTHON_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PYTHON_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null || true
# A developer's local __pycache__ rides along in the cp -R above, and it is dead
# weight in the small update asset that exists precisely to stay small.
find "$BACKEND_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

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

# Slim "app layer" asset for the in-app updater (#421). The full archive above
# is untouched and stays the only thing a fresh install needs; this is an extra,
# much smaller asset used only by an already-installed app updating itself, so
# it never re-extracts the whole Python runtime for a release that only changed
# app code.
#
# StemDeck and backend/ are identical between the CPU and NVIDIA variants -- the
# only per-variant difference in the package is the `cpu-only` marker at the
# root, which the updater never touches -- so this is built once, from whichever
# invocation sets PUBLISH_UPDATER_ASSETS=1, and needs no variant suffix.
#
# tar rather than zip so the executable bit on StemDeck survives; a zip would
# land it without +x and the relaunch after an update would fail.
if [[ "${PUBLISH_UPDATER_ASSETS:-0}" == "1" ]]; then
  echo "==> Creating updater app-layer asset"
  UPDATER_APP_NAME="StemDeck-Linux-x64-app"
  ( cd "$STAGE" && tar -czf "${REPO_ROOT}/${OUTPUT_ROOT}/${UPDATER_APP_NAME}.tar.gz" StemDeck backend )
  ( cd "${REPO_ROOT}/${OUTPUT_ROOT}" \
      && sha256sum "${UPDATER_APP_NAME}.tar.gz" > "${UPDATER_APP_NAME}.tar.gz.sha256" )
  cp "${PYTHON_DIR}/runtime-version.json" \
     "${REPO_ROOT}/${OUTPUT_ROOT}/StemDeck-Linux-x64-runtime-version.json"
  echo "Updater app pack : ${REPO_ROOT}/${OUTPUT_ROOT}/${UPDATER_APP_NAME}.tar.gz"
fi

echo "==> Done"
if [[ "$CPU_ONLY" == "1" ]]; then
  echo "Variant : CPU-only"
else
  echo "Variant : NVIDIA/CUDA"
fi
echo "Stage   : ${STAGE}"
echo "Archive : ${ARCHIVE_PATH}"
echo "Checksum: ${CHECKSUM_PATH}"
