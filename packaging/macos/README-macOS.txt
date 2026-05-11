StemDeck for macOS
==================

Install:

1. Open the StemDeck DMG.
2. Drag StemDeck.app to Applications.
3. Open StemDeck from Applications.

First launch:

- StemDeck is a thin native app. It downloads a pinned, checksummed StemDeck
  runtime pack on first launch.
- The runtime installs to:
  ~/Library/Application Support/StemDeck/runtime
- FFmpeg and ffprobe install to:
  ~/Library/Application Support/StemDeck/ffmpeg
- Demucs model weights download on first use and are cached under:
  ~/Library/Application Support/StemDeck/models

Uninstall:

1. Delete /Applications/StemDeck.app.
2. To remove runtime files, jobs, caches, models, and logs, delete:
   ~/Library/Application Support/StemDeck

Notes:

- Internet access is required for first-run setup.
- Public releases should be signed and notarized.
- Unsigned local builds are for development and internal testing only.
