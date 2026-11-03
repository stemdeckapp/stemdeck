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
- Runtime, config, and logs stay under data/, next to StemDeck.exe, not in AppData.
  So does the window's browser storage, in data/webview/.
- FFmpeg is downloaded during first-run setup into data/ffmpeg/.
- Demucs model weights are downloaded by the backend on first use into data/models/.
- Your library is stored in data/jobs/, unless Documents/StemDeck/jobs already
  held one from an older install. Move it anytime from Settings -> StemData
  location if you would rather keep it elsewhere.
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
