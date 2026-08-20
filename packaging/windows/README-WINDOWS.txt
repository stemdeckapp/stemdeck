StemDeck Windows Portable Alpha
===============================

Run
---

1. Extract the zip folder.
2. Double-click StemDeck.exe.
3. Let first-run setup prepare local runtime assets.

Notes
-----

- This is a portable folder, not an installer.
- No Start Menu shortcut, service, or registry integration is created.
- Runtime, config, and logs stay under data/, next to StemDeck.exe, not in AppData.
- FFmpeg is downloaded during first-run setup into data/ffmpeg/.
- Demucs model weights are downloaded by the backend on first use into data/models/.
- Your job history and library are stored separately (Documents/StemDeck by default,
  same as before), not inside this folder -- relocate them anytime from
  Settings -> StemData location if you'd rather keep them elsewhere.
- The empty portable.txt file next to StemDeck.exe is what tells the app to use
  this data/ folder instead of AppData -- don't delete it.

Troubleshooting
---------------

- If setup fails, check internet access and retry.
- If a job fails, inspect data/logs/ when logs are added.
- Deleting data/ forces first-run setup to recreate runtime state (ffmpeg and the
  Demucs model re-download; your job history and library are unaffected).
