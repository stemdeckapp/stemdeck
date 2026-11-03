StemDeck Windows Portable Alpha
===============================

Run
---

1. Extract the zip folder to a folder you can write to (for example C:\StemDeck),
   not Program Files.
2. Double-click StemDeck.exe.
3. Let first-run setup prepare local runtime assets.

Notes
-----

- This is a portable folder, not an installer.
- No Start Menu shortcut, service, or registry integration is created.
- StemDeck keeps what it writes inside this folder, next to StemDeck.exe, not
  in AppData, your user folder or the Windows temp folder:
    data/            settings, setup state and update downloads
    data/jobs/       your library (see below)
    data/logs/       logs
    data/ffmpeg/     FFmpeg, downloaded during first-run setup
    data/models/     AI model weights, downloaded during setup or on first use
    data/cache/      caches, also those of the tools StemDeck runs
                     (pip, Hugging Face, yt-dlp, matplotlib)
    data/tmp/        temporary files
    data/webview/    the window's browser storage
    data/certs/      the certificate phones use on your network
    python/          the NVIDIA setup installs CUDA support here
- The exceptions are places you choose: your library, if you move it with
  Settings -> StemData location, and files you export or save to a folder you
  pick. If Documents/StemDeck/jobs already held a library from an older
  install, that library is used where it is.
- Windows and your graphics driver still write a few things for any program
  that runs, which StemDeck cannot keep in this folder: a shell cache in
  AppData/Local/Microsoft/Windows/Caches, the graphics driver's shader cache
  (for example AppData/Local/NVIDIA/DXCache), that the program was run
  (Prefetch), folders you picked in its file dialogs, and the firewall rule if
  you let it reach your network. The WebView2 runtime that draws the window is
  part of Windows, not of this folder.
- A new portable folder starts with its own default settings and keeps its
  library in data/jobs/. It never reads the settings or the library of an
  installed StemDeck on this PC. To use a library you already have, choose its
  folder in Settings, StemData location. That includes a library an older
  version opened in Documents/StemDeck/jobs without you choosing it: choose
  that folder once, before you import a new song.
- To install a new version by hand, extract it over this folder. The zip has no
  files inside data/, so your library, models and settings stay. Extracted into a
  new folder, StemDeck starts without the songs in this folder's data/jobs/.
- The empty portable.txt file next to StemDeck.exe is what tells the app to use
  this data/ folder instead of AppData -- don't delete it.

Troubleshooting
---------------

- If setup fails, check internet access and retry.
- If a job fails, inspect data/logs/ when logs are added.
- Deleting data/ forces first-run setup to recreate runtime state (ffmpeg and the
  Demucs model re-download). It also deletes a library stored in data/jobs/.
