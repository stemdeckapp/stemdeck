StemDeck Windows Portable Alpha
===============================

Run
---

1. Extract the zip folder.
2. Double-click StemDeck.exe.
3. Let first-run setup prepare local runtime assets.

Code signing
------------

StemDeck.exe is Authenticode-signed. Free code signing provided by SignPath.io,
certificate by SignPath Foundation. The zip itself is not signed; verify it with
the .sha256 file published next to it on the release page.

Notes
-----

- This is a portable folder, not an installer.
- No Start Menu shortcut, service, or registry integration is created.
- Generated files stay under data/.
- FFmpeg is downloaded during first-run setup into data/ffmpeg/.
- Demucs model weights are downloaded by the backend on first use into data/models/.

Troubleshooting
---------------

- If setup fails, check internet access and retry.
- If a job fails, inspect data/jobs/ and data/logs/ when logs are added.
- Deleting data/ forces first-run setup to recreate runtime state.
