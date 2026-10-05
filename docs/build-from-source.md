# Build from source

<sub>[Back to the README](../README.md)</sub>

Every way to run StemDeck from this repository: the macOS app, a web server on any platform, Docker and Unraid.

### macOS Native App

Requires Rust, Node.js, and Python 3.12. Builds a self-contained `.app` that downloads its own runtime on first launch.

```sh
# First time only: add the cross-compilation targets
rustup target add aarch64-apple-darwin   # Apple Silicon
rustup target add x86_64-apple-darwin    # Intel

# Build Apple Silicon
ARCH=arm64 scripts/macos/make-runtime-pack.sh
ARCH=arm64 scripts/macos/make-app.sh
ARCH=arm64 scripts/macos/make-dmg.sh

# Build Intel (requires Rosetta 2 and an x86_64 Python)
ARCH=x64 scripts/macos/make-runtime-pack.sh
ARCH=x64 scripts/macos/make-app.sh
ARCH=x64 scripts/macos/make-dmg.sh
```

The `.app` lands at `desktop/src-tauri/target/<target>/release/bundle/macos/StemDeck.app`. The DMG lands at `.build/macos-dist/StemDeck-macOS-<arch>.dmg`.

To run a fresh build directly without the DMG:

```sh
open desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/StemDeck.app
```

If macOS blocks the app with a Gatekeeper prompt, run:

```sh
xattr -dr com.apple.quarantine desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/StemDeck.app
```

> **Note:** To test a clean first-launch during development, you can wipe previous app data first: `rm -rf ~/Library/Application\ Support/StemDeck`. Don't do this on a real install.

---

### Web Server (macOS / Linux / Windows with Python 3.12+)

#### Prerequisites

Python 3.12 or newer, `ffmpeg` on your PATH, and [uv](https://github.com/astral-sh/uv). Around 170 MB of free disk for the Demucs model, which downloads automatically on first run.

Optional, for song identification with an AcoustID key: an FFmpeg built with chromaprint (Debian and Ubuntu's `ffmpeg` is), or Chromaprint's `fpcalc` on your PATH (`brew install chromaprint`, `apt install libchromaprint-tools`, or your distro's `chromaprint` package). `./run.sh setup` installs it when your FFmpeg needs it. Without either, songs are identified by their tags only.

#### macOS / Linux (one-shot)

```sh
git clone https://github.com/stemdeckapp/stemdeck stemdeck && cd stemdeck
./run.sh setup     # installs ffmpeg + uv, runs uv sync
./run.sh start
```

Open <http://localhost:8000>.

`setup` uses Homebrew on macOS and `apt-get` on Debian/Ubuntu. For other Linux distros, install `ffmpeg` and [uv](https://github.com/astral-sh/uv) manually, then run `uv sync` followed by `./run.sh start`.

#### Windows (PowerShell)

Install prerequisites:
- [uv](https://docs.astral.sh/uv/getting-started/installation/): `winget install astral-sh.uv`
- [ffmpeg](https://ffmpeg.org/download.html): `winget install Gyan.FFmpeg` (or Chocolatey: `choco install ffmpeg`)

```powershell
git clone https://github.com/stemdeckapp/stemdeck stemdeck; cd stemdeck
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
```

Open <http://localhost:8000>.

> `run.sh` is macOS/Linux only. On Windows use the PowerShell commands above, or run inside WSL.

**NVIDIA GPU (CUDA):** install the CUDA-enabled torch build before starting:

```powershell
uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
$env:STEMDECK_DEMUCS_DEVICE = "cuda"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
```

---

#### Manual (any platform)

```sh
git clone https://github.com/stemdeckapp/stemdeck stemdeck && cd stemdeck
uv sync
uv run uvicorn app.main:app --reload --timeout-graceful-shutdown 5
```

> `--timeout-graceful-shutdown` bounds how long uvicorn waits for open
> connections when you stop it. StemDeck keeps a long-lived SSE stream open
> for the import queue while a browser tab is on the app, so without it
> Ctrl-C waits for that stream instead of exiting.

#### Docker

```sh
docker compose -f build/docker-compose.yml up --build
```

Stems land in `./jobs/` on the host. Demucs weights are cached in a named volume so they don't re-download on rebuild. Note: no GPU passthrough on macOS Docker.

A prebuilt image is published to GHCR. Tags: `edge` (rolling, rebuilt on every merge to main), `latest` (newest stable release), and `X.Y.Z` (pinned to a release).

```sh
docker run -d --name stemdeck -p 8000:8000 \
  -v /path/to/jobs:/app/jobs \
  -v /path/to/cache:/cache \
  -e STEMDECK_PERSIST_LIBRARY=1 \
  ghcr.io/stemdeckapp/stemdeck:edge
```

On a Linux host with an NVIDIA GPU (driver + NVIDIA Container Toolkit installed), add `--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all` and StemDeck auto-detects CUDA. The image already bundles CUDA-enabled torch, so no separate CUDA install is needed.

#### Unraid

StemDeck is available in Unraid Community Applications: open **Apps**, search "StemDeck", and install. Map the two volumes to persistent appdata paths:

- `/app/jobs` -> `/mnt/user/appdata/stemdeck/jobs` (library + stems)
- `/cache` -> `/mnt/user/appdata/stemdeck/cache` (model weights)

The library is persistent by default (`STEMDECK_PERSIST_LIBRARY=1`), so tracks are never auto-deleted. For GPU acceleration, install the **Nvidia Driver** plugin, then set the container's Extra Parameters to `--runtime=nvidia` (the `NVIDIA_VISIBLE_DEVICES` and `NVIDIA_DRIVER_CAPABILITIES` variables are already in the template). CPU-only works with no extra configuration.

#### `run.sh` control script

```sh
./run.sh setup      # one-shot: install ffmpeg + uv, then uv sync
./run.sh start      # boots uvicorn in the background
./run.sh stop       # graceful shutdown
./run.sh restart    # stop + start
./run.sh status     # is it running?
```
