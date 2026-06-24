StemDeck Linux Portable Alpha
=============================

This comes in two variants. Pick one:

- StemDeck-Linux-x64.tar.gz         CPU-only (smaller; runs anywhere)
- StemDeck-Linux-x64.NVIDIA.tar.gz  NVIDIA/CUDA (larger; much faster on an
                                    NVIDIA GPU, falls back to CPU if no GPU)

Run
---

1. Extract the tarball, e.g.:
     tar -xzf StemDeck-Linux-x64.tar.gz
2. Install the runtime prerequisites (see below).
3. Run the launcher:
     cd StemDeck-Linux-x64        # or StemDeck-Linux-x64.NVIDIA
     ./StemDeck
4. Let first-run setup prepare local runtime assets.

Prerequisites
-------------

This portable package bundles its own Python runtime (torch + demucs), but the
desktop shell links against your system's WebKitGTK libraries, and StemDeck
expects FFmpeg on your PATH. Install both with your package manager.

  Debian / Ubuntu:
    sudo apt update
    sudo apt install libwebkit2gtk-4.1-0 libgtk-3-0 ffmpeg

  Fedora:
    sudo dnf install webkit2gtk4.1 gtk3 ffmpeg

  Arch:
    sudo pacman -S webkit2gtk-4.1 gtk3 ffmpeg

NVIDIA variant
--------------

To use the GPU you need a working NVIDIA driver on the host such that
`nvidia-smi` runs and reports your GPU.

  Check your driver:
    nvidia-smi

On first launch, the NVIDIA build detects your GPU and downloads the matching
CUDA-enabled PyTorch (a few GB) into your data directory — so the first run
needs an internet connection and some disk space. You do NOT need a separate
CUDA toolkit install, only the driver.

If no usable GPU is detected, the NVIDIA build still runs and falls back to CPU.
If you do not have an NVIDIA GPU, use the CPU-only tarball instead — it skips
the CUDA download entirely.

Notes
-----

- This is a portable folder, not a system package. No .desktop entry, service,
  or package-manager integration is created.
- User data lives under $XDG_DATA_HOME/stemdeck (or ~/.local/share/stemdeck).
- Your stem library is written to ~/Documents/StemDeck/.
- Demucs model weights download from the backend on first use into the data
  directory under models/.

Troubleshooting
---------------

- "./StemDeck: error while loading shared libraries" — install the WebKitGTK
  and GTK packages listed above.
- "ffmpeg not found" or a job failing immediately — install ffmpeg and ensure
  `ffmpeg -version` works in your shell.
- If setup fails, check internet access and retry.
- Inspect logs under the data directory's logs/ folder.
- Deleting the data directory forces first-run setup to recreate runtime state.
