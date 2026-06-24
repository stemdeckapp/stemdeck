StemDeck Linux Portable Alpha (CPU)
===================================

Run
---

1. Extract the tarball:
     tar -xzf StemDeck-Linux-x64.tar.gz
2. Install the runtime prerequisites (see below).
3. Run the launcher:
     cd StemDeck-Linux-x64
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

Notes
-----

- This is the CPU-only build. Stem separation runs on the CPU and is slower
  than a GPU build; an NVIDIA/CUDA variant may ship later.
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
