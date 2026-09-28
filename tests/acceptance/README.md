# Acceptance run

The manual test checklist for a release build, automated. It drives the real
desktop build (`StemDeck.exe`) and the real internet (YouTube, MusicBrainz,
LRCLIB, Wikidata, Wikipedia, AcoustID), one test per check, with the same ids
as the manual page (`S1` ... `V4`), so each result maps onto a manual one.

It runs only when asked. Never in CI: `npx playwright test` and the CI
workflows use the root `playwright.config.mjs`, whose `testDir` is `tests/e2e`,
and the setup here refuses to start when `CI` is set.

## Run it

1. Build the Windows portable package (default location:
   `dist/StemDeck-Windows-x64.NVIDIA`).
2. From the repo root, in PowerShell or Git Bash:

   ```
   npm run test:acceptance
   ```

3. Read `acceptance-results/report.md`.

It opens the app's window and plays a few seconds of several songs out loud.

## How long it takes

- First run of a fresh package: 5 to 10 minutes more, for the NVIDIA build of
  PyTorch (2.5 GB), FFmpeg, the models and, later, the Whisper model (1.6 GB).
  These are kept between runs.
- A run on an RTX 3080 with those already downloaded: about 20 minutes, most
  of it separating the twelve imports while the checks run. YouTube refuses a
  download now and then (a 403); that import is split once more, at the back
  of the queue, which can add 10 minutes. The 59 minute compilation (C4, T1)
  is the longest import; `STEMDECK_ACCEPTANCE_SKIP_LONG=1` leaves it out.

## Environment

| Variable | What it does |
|---|---|
| `STEMDECK_ACCEPTANCE_ACOUSTID_KEY` | A real AcoustID application key, for A2 and N6. Without it both are skipped. |
| `STEMDECK_ACCEPTANCE_SKIP_LONG=1` | Skip the 59 minute import (C4 skipped, T1 checks only a normal song). |
| `STEMDECK_ACCEPTANCE_PACKAGE` | Another portable package folder to test. |
| `STEMDECK_ACCEPTANCE_VERSION` | The version S2 expects, when not the package's own `version.json`. |
| `STEMDECK_ACCEPTANCE_TARGET` | `desktop` (default), or `server http://127.0.0.1:8000` to drive a running dev server in Playwright's Chromium. |
| `STEMDECK_ACCEPTANCE_NO_BROWSER=1` | N4 records the link handed to `open_url` without opening the real browser. |
| `STEMDECK_ACCEPTANCE_JOB_TIMEOUT_MIN` | How long a check waits for its import (default 90). |
| `STEMDECK_ACCEPTANCE_CDP_PORT` | Fix the DevTools port instead of taking a free one. |

The key is typed into the masked field and read nowhere else: it is not
passed to the app's environment, not logged, not written to a file, and the
report replaces it with `[key]` should it ever appear.

## What it does to the machine

- Resets the package's own `data` folder before each run: removes the jobs
  folder (the library, its registry and the desktop library store),
  `settings.json`, the run logs and the MusicBrainz answer cache, then writes a
  `settings.json` whose `jobs_dir` is the package's own `data\jobs`. It keeps
  `data\ffmpeg`, the package's python with CUDA torch, `data\models` and the
  downloaded runtime archives, so later runs are fast.
- Starts `StemDeck.exe` with `LOCALAPPDATA` pointed at
  `%TEMP%\stemdeck-acceptance\LocalAppData` (emptied every run), which also
  holds the WebView2 profile. A portable build is not isolated by default: it
  restores settings from `%LOCALAPPDATA%\StemDeck` and mirrors changes back
  there. With the redirect, the real `%LOCALAPPDATA%\StemDeck` and the real
  library are never read or written; the report says whether the real
  settings file stayed byte for byte the same.
- Refuses to run if `Documents\StemDeck\user-data.json` exists, since the
  build would copy that library layout into the test run.
- Closes the app it started, and its backend, at the end, also after a
  failure. It never looks at any other StemDeck process.

## How the app is driven

WebView2 honours `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`, so the build is
started with `--remote-debugging-port` and Playwright attaches with
`chromium.connectOverCDP`: the checks click in the real window, and the
desktop-only paths (Tauri's `open_url`, the library store in the jobs folder)
are the real ones. Three switches that stop Chromium throttling a covered
window are added, so the karaoke checks do not depend on the window being on
top. The window is resized with the Win32 API, because the now-playing card
only shows in a window wider than 1460 px.

E4 blocks `lrclib.net` for the page alone. E6 starts the app a second time
with every outside request sent to a proxy that nobody listens on
(`HTTP(S)_PROXY` for the backend, `--proxy-server` for WebView2), loopback
excepted: offline for this app, while the machine keeps its network.

## Results

`acceptance-results/` (gitignored, emptied at the start of each run):

- `report.md` and `report.json`: every check with pass, partly, fail, service
  unavailable, skipped or not run, the reason, and its screenshots.
- `copy-results.txt`: the same summary the manual page's Copy results gives.
- `screenshots/`: at least one per check, and one at the moment of a failure.
- `files/`: the audio files the local checks upload, made with the package's
  FFmpeg.
- `app-stdout.log`: the desktop shell's own output.

"partly" means everything a program can measure passed, and names what is
still for eyes or ears (does the wipe keep time with the voice, does the
Chinese read naturally). "service unavailable" means a check failed while an
outside service was down or refusing, with the status codes or log lines as
evidence; it is not a StemDeck result.

Not here: `tests/e2e` (Playwright against a seeded backend, run in CI) and
`tests/js` (node logic tests).
