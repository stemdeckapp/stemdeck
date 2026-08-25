# StemDeck roadmap

Where StemDeck has been, and what is next. Dates are the first tag in each
line, taken from the repository rather than reconstructed.

Day to day work is tracked in
[issues](https://github.com/stemdeckapp/stemdeck/issues).

---

## Shipped

### 0.1.0 · 2026-05-04 · it exists

First commit and first tag. A URL in, six stems out, a browser to play them in.
Everything after this is consequence.

### 0.2.0 · 2026-05-07 · it survives being an app

Windows portable in dual CPU and NVIDIA builds, and the first collision with
what "desktop app" actually costs: WebView2 drag and drop, stem silence in
WKWebView, certifi certificates missing inside the Demucs subprocess, a setup
hang after a data reset. The registry began persisting finished jobs across
restarts, so a library outlived the process that made it.

### 0.3.0 · 2026-05-10 · not only YouTube

Local MP3 and WAV import, so the tool stopped being tied to one source. First
macOS build.

### 0.4.0 · 2026-05-12 · the runtime problem

Almost entirely portable-Python layout and `PYTHONHOME` on Windows and macOS.
Unglamorous, and the reason later releases could ship a runtime at all.

### 0.5.0 · 2026-05-18 · the DAW

The redesign that set the shape still in use: library, sections, analysis and
transport. Export Mix, the footer waveform, tag search. Uploads normalised
through ffmpeg, after non-standard WAVs were found separating into silence.

### 0.6.0 · 2026-05-23 · exports people can find

Loop-region export, native save dialogs on Windows and macOS, collapsible
library. Runtime auto-update on version mismatch, and a setup window that
explains itself when the network is not cooperating.

### 0.7.0 · 2026-05-28 · the long one

A month, forty substantive changes, and three threads at once.

**Audio.** SoundCloud support, pre-computed peaks so waveforms appear instead
of being computed, the Web Audio decode-and-mix engine, and then several
rounds of making that engine survive WebView2. FLAC, MP4 and M4A import. MP4
karaoke export.

**Security.** The security pass that closed XSS (#170) and SSRF (#173), added
the webview Content-Security-Policy (#171), and pinned the macOS FFmpeg
download to a verified SHA256 (#172). Those decisions still constrain the
design today: every feature since has had to work inside that CSP and that
extractor allowlist.

**Platforms.** Linux portable builds, RTX 50-series CUDA, and the first
encounter with GitHub's 2 GiB release asset cap, which has shaped packaging
ever since.

### 0.8.0 · 2026-06-27 · the health report

Six weeks and the largest single body of work in the project. Two halves.

**Reach.** The mobile web UI with a chunked audio engine, network access with
QR codes, Docker and Unraid via GHCR, playback speed with pitch preservation,
the click track and beat grid, background import queue and playlist import.

**Health.** A codebase health report, worked in four phases:

| Phase | What it was for |
|---|---|
| 1 — Evidence | Rotating logs, failed-job quarantine, error classification, per-stage timings, and no more internal exception text in the UI |
| 2 — Resilience | GPU to CPU fallback, retried metadata probes, the Windows registry persist race, graceful watchdog shutdown, tear-proof SSE serialization |
| 3 — Performance | Single-pass streamed peaks, a persistent Demucs worker instead of reloading weights per job, SSE dirty flags, cached mixdowns |
| 4 — Separation SOTA | The RoFormer and lead/backing vocal split investigation |

The persistent worker alone removed 35 to 42 percent of the separate stage on
an RTX 3080.

### 0.9.0 · 2026-08-12 · the library is yours

Choose where stems are stored. The WAV header parser stopped giving up after
1 KB, and a track that cannot load now says so instead of failing quietly.
First browser tests.

### 0.10.0 · 2026-08-16 · count-in, and reporting back

Count-in, a transport footer rebuilt on the studio's column grid, and reporting
a failure straight from the notification centre.

### 0.11.x · 2026-08-17 · keeping the door open

YouTube import gaps, macOS FFmpeg download reliability, and a yt-dlp relock to
fix a 403 on download. The first clear sign that YouTube extraction is a moving
target rather than a solved problem.

### 0.12.x · 2026-08-21 · lead and backing vocals

Phase 4 landing: the lead/backing vocal split. Plus portable Windows data
directories and the stems relocation fixes that followed.

### 0.13.0 · 2026-08-22 · seven languages

German, Portuguese and Indonesian, taking the UI to seven languages. A system
FFmpeg is now preferred on desktop when it verifies as compatible.

### 0.14.x · 2026-08-23 · updating in place

The leaner Windows package and the opt-in in-app updater, then the three
corrections it needed: settings surviving a new install, updates only offered
once a release is promoted, and a second StemDeck no longer adopting the
first one's backend. Most recently, YouTube import diagnostics, an opt-in
cookies file, and 0.75x playback.

---

## In flight

Search, preview, and the fixes found alongside them.

| Issue | |
|---|---|
| [#441](https://github.com/stemdeckapp/stemdeck/issues/441) | Search YouTube and SoundCloud from the topbar |
| [#442](https://github.com/stemdeckapp/stemdeck/issues/442) | Audition a result before extracting it |
| [#440](https://github.com/stemdeckapp/stemdeck/issues/440) | An undecodable byte kills the whole separation |
| [#443](https://github.com/stemdeckapp/stemdeck/issues/443) | Max track length clamps to 20 when the ceiling is 60 |
| [#444](https://github.com/stemdeckapp/stemdeck/issues/444) | Portuguese ships only the Brazilian variant |

## Next

**Keep YouTube import working.** [#432](https://github.com/stemdeckapp/stemdeck/issues/432)
and [#438](https://github.com/stemdeckapp/stemdeck/issues/438). Extraction
currently works through a fallback client that yt-dlp has deprecated. The
solver and a bundled JS runtime are both proven to work and are held on a
packaging decision, not a technical one. This is not urgent and it is not
optional.

**Save and restore a workspace.** [#425](https://github.com/stemdeckapp/stemdeck/issues/425).
Volume, mute and solo persist per song. Loop region, zoom, playhead, speed and
click settings do not, so a reloaded track does not come back as it was left.

**Pitch and key transposition.** [#245](https://github.com/stemdeckapp/stemdeck/issues/245).
Requested for vocal practice and karaoke.

**A VST plugin.** [#404](https://github.com/stemdeckapp/stemdeck/issues/404).
Exploratory.

---

## How releases work

The version comes from the git tag. `pyproject.toml` is `dynamic`, and the
desktop manifests hold `0.0.0` placeholders the release workflow stamps, so
there is no version number to hand-edit anywhere.

Every release ships as a pre-release and is promoted by hand once it has been
verified. That promotion is what the in-app updater watches: it takes the
newest release that is neither a draft nor a pre-release, so an unpromoted
build is invisible to it. That is the point, not an oversight.

Desktop and Docker split CPU and GPU differently on purpose. Docker publishes
one `linux/amd64` image that works on CPU and activates GPU through
`--runtime=nvidia`. The desktop builds two bundles, and the NVIDIA one installs
the CUDA torch wheel at first run rather than shipping it, because bundling it
once put the release past GitHub's 2 GiB asset cap (#318, reverted by #320).
