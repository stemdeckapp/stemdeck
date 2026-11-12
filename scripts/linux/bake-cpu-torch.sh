#!/usr/bin/env bash
#
# Swap torch, torchaudio and torchvision in a Python install for the CPU-only
# wheels, then prove the result with scripts/check_torch_bundle.py.
#
# make-portable.sh runs this for both Linux variants, and the torch-bundle CI
# job runs it against a fresh venv, so CI tests this file and not a copy of it.
#
# Usage: bake-cpu-torch.sh <python>
#
# The versions come from packaging/torch-versions.json. TORCH_VERSION in the
# environment overrides the torch line; the torchvision for it is looked up in
# the same file, so an override cannot leave torchvision on another line.
#
# torchvision has to be named here. It arrives earlier as a dependency of the
# project install (audio-separator -> onnx2torch -> torchvision), and PyPI's
# default Linux torchvision is the cu124 build. --no-deps touches only what is
# named, so leaving it out shipped CPU torch beside a CUDA torchvision whose
# compiled ops cannot load: "operator torchvision::nms does not exist" (#778).

set -euo pipefail

PYTHON="${1:?usage: bake-cpu-torch.sh <python>}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSIONS="${REPO_ROOT}/packaging/torch-versions.json"
CPU_INDEX="https://download.pytorch.org/whl/cpu"

# Read with the target interpreter itself: it is the one Python this script is
# guaranteed to have.
TORCH_VERSION="${TORCH_VERSION:-$("$PYTHON" -I -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["cpuTorch"])' "$VERSIONS")}"
TORCHVISION_VERSION="$("$PYTHON" -I -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["torchvisionForTorch"].get(sys.argv[2], ""))' \
  "$VERSIONS" "$TORCH_VERSION")"
if [[ -z "$TORCHVISION_VERSION" ]]; then
  echo "ERROR: no torchvision for torch ${TORCH_VERSION} in ${VERSIONS}. Add the pair there." >&2
  exit 1
fi

# pip strips the local '+cpu' version when resolving, so the project install
# pulls the default wheels even if a CPU wheel was requested; --force-reinstall
# --no-deps replaces just these three (proven on Windows).
echo "==> Baking CPU-only torch ${TORCH_VERSION} with torchvision ${TORCHVISION_VERSION}"
"$PYTHON" -m pip install \
  "torch==${TORCH_VERSION}+cpu" \
  "torchaudio==${TORCH_VERSION}+cpu" \
  "torchvision==${TORCHVISION_VERSION}+cpu" \
  --index-url "$CPU_INDEX" \
  --force-reinstall --no-deps

echo "==> Checking the torch bundle"
"$PYTHON" "${REPO_ROOT}/scripts/check_torch_bundle.py" --versions "$VERSIONS" --expect-cpu
