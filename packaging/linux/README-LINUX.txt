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

Nothing needs installing: this folder can be moved or deleted at will.

Optional: add a desktop launcher
--------------------------------

If you would rather launch StemDeck from your applications menu than from a
terminal, run the installer that ships in this folder:

    ./install.sh

It copies this folder to a fixed location and adds an icon and a menu entry.
It never downloads anything; the version and the CPU/NVIDIA variant come from
the package you already extracted.

    ./install.sh --local        just me     (~/.local/opt/stemdeck)
    ./install.sh --global       all users   (/opt/stemdeck, needs sudo)
    ./install.sh --prefix DIR   somewhere else
    ./install.sh --uninstall    remove it again
    ./install.sh --help         all options

To upgrade, extract the new tarball and run ./install.sh from it. It reuses the
location you chose and replaces the old copy only once the new one is verified,
so a failed upgrade leaves the working version alone.

Uninstalling removes the application only. Your stem library and settings live
outside the install directory and are never touched.

Prerequisites
-------------

This portable package bundles its own Python runtime (torch + demucs), and
StemDeck downloads FFmpeg automatically on first launch (or uses a system
`ffmpeg` if one is already on your PATH). The only system libraries you need
are your distro's WebKitGTK + GTK, which the desktop shell links against.

  Debian / Ubuntu:
    sudo apt update
    sudo apt install libwebkit2gtk-4.1-0 libgtk-3-0

  Fedora:
    sudo dnf install webkit2gtk4.1 gtk3

  Arch:
    sudo pacman -S webkit2gtk-4.1 gtk3

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

- This is a portable folder, not a system package. Running it in place creates
  no .desktop entry, service, or package-manager integration; ./install.sh adds
  a launcher and icon if you want them.
- User data lives under $XDG_DATA_HOME/stemdeck (or ~/.local/share/stemdeck).
- Your stem library is written to ~/Documents/StemDeck/.
- Demucs model weights download from the backend on first use into the data
  directory under models/.

Troubleshooting
---------------

- "./StemDeck: error while loading shared libraries" — install the WebKitGTK
  and GTK packages listed above.
- A job failing immediately with an FFmpeg error — first-run setup downloads
  FFmpeg automatically; check internet access and retry, or install a system
  `ffmpeg` so `ffmpeg -version` works in your shell.
- If setup fails, check internet access and retry.
- Inspect logs under the data directory's logs/ folder.
- Deleting the data directory forces first-run setup to recreate runtime state.
