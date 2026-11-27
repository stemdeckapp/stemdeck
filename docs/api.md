# HTTP API

<sub>[Back to the README](../README.md)</sub>

The endpoints the web app itself uses. They are not a stable public interface and can change between releases.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Server health and version info |
| POST | `/api/jobs` | JSON `{url, stems?}` or multipart `file + stems` → `{job_id}` |
| GET | `/api/jobs` | List completed (library) jobs |
| GET | `/api/jobs/{id}` | Job state snapshot |
| GET | `/api/jobs/{id}/events` | SSE stream of job state |
| POST | `/api/jobs/{id}/cancel` | Terminate active subprocess and cancel job |
| POST | `/api/jobs/{id}/reextract` | Separate one stem again with another model, from the song's original audio, and replace it in place: JSON `{stem, model}` → `202 {state}`. See [Re-extract one stem](#re-extract-one-stem) |
| POST | `/api/jobs/{id}/reextract/undo` | Put back the stem the last re-extract of it replaced: JSON `{stem}` → `200 {state}` |
| POST | `/api/jobs/{id}/reextract/keep` | Keep the re-extracted stem and delete its undo copy: JSON `{stem}` → `200 {state}` |
| GET | `/api/transcribe/status` | Whether MIDI can be made on this install, with which engines, from which stems. See [MIDI from a stem](#midi-from-a-stem) |
| POST | `/api/jobs/{id}/midi` | Make MIDI from one of the song's stems: JSON `{stem, detail?}` → `202 {state: "running"}` |
| POST | `/api/jobs/{id}/midi/{stem}/cancel` | Stop that run; the file the stem had stays: `200 {state}` |
| DELETE | `/api/jobs/{id}/midi/{stem}` | Remove the stem's MIDI file and its record: `200 {deleted: stem}` |
| GET | `/api/jobs/{id}/stems/{stem}.mid` | The stem's MIDI file (`audio/midi`), named `<song>_<stem>.mid` like a stem download |
| GET | `/api/jobs/{id}/midi/{stem}/notes` | The notes of the stem's MIDI file for the piano roll and playback: `200 {notes: [[start_sec, end_sec, pitch, velocity], ...], tempo_source, bpm, stem_rms}`; `stem_rms` is the RMS of the stem's own file (channel 0), which the MIDI sound is matched to on either audio engine, `null` when the stem cannot be read (kept per file in the song's `stem_rms`; a song without it is measured on the first request); `404 no_midi` when there is none or it cannot be read |
| PATCH | `/api/jobs/{id}/sections` | Save waveform section markers for a job |
| PATCH | `/api/jobs/{id}/timeline-tags` | Save the timeline markers for a job: JSON `{timeline_tags: [{id, name, time}]}`, the whole list, stored in time order (#1075) |
| POST | `/api/jobs/{id}/lyrics/import` | Keep the lyrics from a `.lrc` or `.txt` the user picked, decoded by the page: JSON `{text}`, at most 100,000 characters → `lyrics.json`, source `file`, answered as `GET .../lyrics` then serves it, plus `dropped_lines` (a count) when untimed lines with words were dropped. Timed when the text has `[mm:ss.xx]` stamps (tags, `[offset:]`, multi-stamp lines and `<mm:ss.xx>` word stamps kept, other lines dropped), plain otherwise (LRC ID tags dropped; plain lyrics cannot be aligned or synced line by line). Replaces the old lyrics with their Align offset, own timing and vocal timing, and LRCLIB's other versions; the page asks first when any of those three is set. The page reads a file as UTF-8 (or UTF-16 with a byte order mark), else GB18030, so a Windows-1252, Shift-JIS or Big5 file can come out as garbled letters: save it as UTF-8 first. 409 while the job is not finished, 422 for no words, too long text or text UTF-8 cannot hold. An Align offset (`.../lyrics/offset`, Auto-detect) answers 409 when an import replaced the lyrics it was set for (#1083) |
| GET | `/api/jobs/{id}/stems/{name}.wav` | Stream a single stem as the song stores it: WAV, or MP3 (`audio/mpeg`) for a song stored as MP3 (#783) |
| GET | `/api/jobs/{id}/stems/{name}.mp3` | A stem as MP3: the stored file for a song stored as MP3, else a cached transcode |
| GET | `/api/jobs/{id}/stems/all.zip` | The chosen stems (and takes) as one ZIP, named after the song. Query: `format`, `stems`, and the click options. `reaper=1` (default off) adds `<song>.rpp`, a REAPER project with one track per file in the zip (the click too), at the stems' sample rate and the beat grid's median tempo (120 without a grid), each file named relative to the project; the download is then named `<song>_reaper.zip`. Without it the zip is unchanged (#1071). `midi=1` (default off) adds the MIDI file of each stem in the zip (only the asked stems when `stems` is given), `<song>_<stem>.mid` beside `<song>_<stem>.<format>`; a song with none gets the zip it would have had |
| GET | `/api/jobs/{id}/video.mp4` | Mux the current mix with the source video (MP4 upload or YouTube) into an MP4 |
| DELETE | `/api/jobs/{id}` | Remove job dir from disk (terminal jobs only) |
| GET | `/api/jobs/{id}/takes/{take_id}/sync` | A recorded take's picture sync: `{video_sync, camera_start_sec}`. See [Take picture sync](#take-picture-sync) |
| POST | `/api/jobs/{id}/takes/{take_id}/sync` | Set the picture's manual nudge or measure it again: JSON `{nudgeMs?, remeasure?}` → the take entry. See [Take picture sync](#take-picture-sync) |
| GET | `/api/settings` | Every setting, plus what the machine can do. Separation fields below |
| POST | `/api/settings` | JSON with any of the settings; answers the same body as the GET. `{"stem_models": {...}}` below |
| POST | `/api/speed-test` | Start the benchmark (Settings > Separation): `202 {"state": "running"}`; `409` with `code: "busy"` and a fixed `detail` while a song is separating, a song waits in a queue that is not paused, a stem fill or a Lead + Backing split holds the pipeline, a benchmark is already running, the server is stopping, or a stems folder move is in progress. A running benchmark holds the pipeline, so a song imported meanwhile waits for it |
| POST | `/api/speed-test/cancel` | Cancel a running benchmark; answers the state as the GET does |
| GET | `/api/models` | Every built-in model (Settings > Models): its purpose, licence, source, and state and size on disk, with the Download button's run. See [Built-in models](#built-in-models) |
| POST | `/api/models/{id}/download` | Download one built-in model now: `202 {state: "running"}`, or `404`/`409` `{detail, code}` |
| GET | `/api/models/download` | The Download button's run: state, model, phase, bytes, total, error code |
| POST | `/api/models/download/cancel` | Stop the download: `200` and its state |
| GET | `/api/my-models` | The models you added (Settings > Models > My models). See [My models](#my-models) |
| POST | `/api/my-models/link` | Add a model from a Hugging Face link: `202` and the new entry |
| POST | `/api/my-models/upload` | Add a model from two files (multipart): `202` and the new entry |
| POST | `/api/my-models/{id}/check` | Check a model again: `202` and the entry |
| POST | `/api/my-models/{id}/cancel` | Stop a download or a check: `200 {id, model}` |
| DELETE | `/api/my-models/{id}` | Remove a model and its files: `200 {removed: id}` |
| GET | `/api/speed-test` | `{state, step, pct, error, error_code, result}`: `state` is `idle`, `running`, `done`, `failed` or `cancelled`; `step` is `disk`, `cpu`, `cuda`, `mps` or null; `pct` is 0 to 100; `error` is null or a fixed sentence; `error_code` is null or why a failed run failed: `torch-missing`, `model-missing` (`htdemucs_6s` is not downloaded yet), `timeout`, `stalled` or `failed`; `result` is the saved benchmark result below in every state, or null when no benchmark was ever run |

## Separation settings

`GET /api/settings` carries these beside the settings that were already there.
Stems are `vocals`, `drums`, `bass`, `guitar` and `piano`; Other and lead and
backing are not chosen.

| Field | Meaning |
|---|---|
| `stem_models` | The saved choice: `{stem: "auto" or a model id}`, all five stems, `"auto"` where nothing was saved |
| `stem_models_resolved` | The model the next song uses on this machine, per stem |
| `stem_models_recommended` | What `"auto"` would pick per stem |
| `stem_models_reason` | Why the vocals recommendation is what it is: `gpu`, `cpu`, `mps`, `low-vram`, `unavailable`, or `over-budget` when a benchmark that still matches this machine moved the vocals on a CUDA GPU from Kim to `htdemucs_6s` |
| `stem_model_catalog` | `{stems, models, min_vram_mb, deployment}`. `stems` lists a row per stem: `choices` for a stem you can choose, or `fixed` for one you cannot (`lead_backing` with `available`, `other`, and every stem when a deployment pins the model). `models` maps a model id to `name`, `user`, `engine` (`demucs`, `mel_roformer`, or `user_roformer` for a model you added), `stems` (the stems it can supply, sorted), `license`, `source_url`, `download_bytes`, `cpu_rtf`, `gpu_rtf`, `available`, `downloaded` and `note_key`. A model you added also has `size` and `estimated` (see [My models](#my-models)). `deployment` is the pinned model id, or null |
| `speed_test` | The last benchmark result, or null when it was never run. `stale: true` when this machine no longer matches the one it ran on |

If the registry cannot be read, the endpoint still answers, with `htdemucs_6s` recommended and the reason `unavailable`.

`POST /api/settings` takes a partial `stem_models`, for example
`{"stem_models": {"vocals": "kim_mel_roformer", "drums": "auto"}}`. Stems left
out keep their value. The whole request is checked before anything is written,
so one bad value changes nothing. A refusal is a 422 with one of these fixed
messages (the value you sent is never repeated):

- `stem_models must be an object of stem to model`
- `stem_models: unknown stem; choose from vocals, drums, bass, guitar, piano`
- `stem_models.<stem> must be one of: auto, <the ids that stem allows>`
- `stem_models.<stem>: <id> is not available on this install`

## Re-extract one stem

Separate one stem of a finished song again with the model chosen for it, and
replace that stem in place for this song. The stems are `vocals`, `drums`,
`bass`, `guitar` and `piano`; `other` is never re-extracted, because it is the
residual and is rebuilt by every re-extract and undo.

All three routes take JSON and are refused from another site like every other
write. Every refusal is `{"detail": <fixed sentence>, "code": <code>}`; a body
of the wrong shape is FastAPI's own `422`, and a malformed or unknown job id is
`404 {"detail": "job not found"}`.

`POST /api/jobs/{id}/reextract` takes `{"stem": "guitar", "model": "<model id>"}`
and answers `202 {"state": <job state>}` at once. The model must be one of the
stem's choices in `stem_model_catalog`, or a model you added that declares the
stem. Refusals:

| Status | `code` | When |
|---|---|---|
| 409 | `not_ready` | The song is not `done` |
| 422 | `stem_not_selectable` | `stem` is not one of the five stems |
| 409 | `stems_missing` | The song lacks that stem or `other` (Extract them first) |
| 409 | `model_not_allowed` | The model cannot separate that stem |
| 409 | `model_removed` | A model you added that was removed |
| 409 | `model_unavailable` | A model this install cannot run now |
| 409 | `no_source` | No upload kept beside the song and no link to fetch again |
| 403 | `online_sources_off` | The link must be fetched again while online sources are off |
| 409 | `busy` | Anything holds the pipeline: a song separating, a fill, a Lead + Backing split, a lyrics timing, the benchmark, another re-extract or undo, a library move, the server stopping |

The run then holds the pipeline until it is over, and is stopped by
`POST /api/jobs/{id}/cancel`. Its progress and outcome are in the job state:
poll `GET /api/jobs/{id}` about once a second while `state` is `running` (the
job's event stream ends at once for a finished song, so it does not carry
them):

```
"stem_reextract": {
  "stem", "model", "action": "reextract" | "undo",
  "state": "running" | "done" | "error" | "cancelled",
  "phase": "waiting" | "downloading" | "separating" | "finishing" | null,
  "progress": 0..1, "code": null | <code>,
  "used_model": <the model that made the stem> | null,
  "fallback": {"model", "name", "code"} | null,
  "gain_db": <negative dB> | null,
  "started_at", "finished_at"
}
"stem_undo": {"<stem>": {"model": <the model the undo copy was made with>, "saved_at"}}
```

`stem_reextract` is null until the first run. `code` on an `error` is one of
`failed`, `source_changed` (the audio fetched again is no longer this song's,
so its stems would not add up), `no_source`, `online_sources_off`,
`model_unavailable`, `model_removed`, `stems_missing`, or `interrupted` (the
server stopped during the run). An error or a cancel leaves every file as it
was. A model you added that fails at run time is stood in for by
`htdemucs_6s` on the same device: `used_model` is then `htdemucs_6s`,
`fallback` says which model failed and why, and so does
`stem_model_fallbacks` for that stem. `stem_models` always says which model
made each stem now.

`POST .../reextract/undo` takes `{"stem"}`, puts the undo copy back and answers
`200 {"state"}` once it is done (no separation runs; a few seconds). `409`
`no_undo` when that stem has none, plus `not_ready`, `stem_not_selectable`,
`stems_missing` and `busy` as above, and `500` `failed` when the files could not be written (the
song is then as it was). `POST .../reextract/keep` takes `{"stem"}` and deletes
the undo copy: `200 {"state"}`, or `409` `no_undo` or `busy`.

What a re-extract of a stem changes:

- **The stem**, separated from the song's original audio: the upload kept
  beside it, or its link fetched again, never the stems it has. The run takes
  only that stem: the chosen model, and for the other stems whatever needs the
  fewest passes, keeping the song's own RoFormer passes when the model is a
  Demucs one, since a Demucs pass hears the mix minus those stems in an
  import too. It is stored as the song keeps its stems, WAV or MP3.
- **Its undo copy**: the file it replaced, one per stem, until you keep the new
  one or re-extract that stem again. An undo of a stem after another stem was
  re-extracted keeps the other change.
- **Other**: rebuilt as old other + old stem - new stem, so the six stems still
  add up to the song. The other stems are not touched. Only when the new stem
  or the new Other would clip is every stem turned down by the same small
  factor (`gain_db`), the rule every separation writes its stems by.
- **Original** (the backing track) and **mix** (the selected-stems mix), when
  the song has them and holds one of the two.
- **Lead + Backing**, after a vocals re-extract: the pair was split from the
  old vocals, so it moves into the undo copy and the song can be split again.
  An undo brings it back.
- **Peaks and presence** of what changed, and the stem URLs: a replaced file's
  URL ends in `?v=<n>`, so a browser never plays the copy it cached before.
  Take the URLs from the state; do not build them.

Not done again: the beat grid (also after drums: the beats are the song's, and
a grid you edited or practised with does not move), the key, the sections and
the loudness, since the stems still add up to the same song.

## MIDI from a stem

The notes of one of a finished song's stems as a standard MIDI file, made on
request (see [models.md](models.md#midi-from-a-stem)), one run at a time across
the server. Every stem of `htdemucs_6s` can be transcribed: `vocals`, `drums`,
`bass`, `guitar`, `piano`, `other`. Each with the engine its
`transcribe_<stem>_engine` setting picks, among those that can take it:

| Engine | Stems | Runs on | First run downloads |
|---|---|---|---|
| `basic_pitch`, Spotify's Basic Pitch | every stem but the drums | the CPU, a few seconds a song | nothing (built in) |
| `piano_hr`, ByteDance's high-resolution piano transcription | piano | the GPU when the separation device resolves to CUDA, else the CPU | its checkpoint, 172 MB |
| `ymt3`, YourMT3+ | drums, bass, guitar, piano, other | the same | its checkpoint, 562 MB, then made into its 188 MB weights |

Each stem's engine until one is picked: `ymt3` for the drums, bass, piano and
other, `basic_pitch` for the vocals and the guitar (config.MIDI_DEFAULT_ENGINES
says why, with the measurements). `ymt3` keeps only the notes of the stem's own
instrument class; the drums' file is General MIDI percussion on channel 10.

`GET /api/transcribe/status` never loads an engine:

```
{"available": true | false,
 "reason": null | "no_runtime" | "unsupported_platform" | "engine_error",
 "engines": [
   {"id": "basic_pitch", "label": "Basic Pitch", "version": "0.4.0", "licence": "Apache-2.0",
    "model_licence": "Apache-2.0", "stems": ["vocals", "bass", "guitar", "piano", "other"],
    "available": true, "reason": null, "source": "built_in", "model": "built_in",
    "download_bytes": null, "disk_bytes": null, "device": "cpu",
    "seconds_per_minute": null, "uses_detail": true},
   {"id": "piano_hr", "label": "ByteDance Piano Transcription", "version": "0.0.6",
    "licence": "MIT", "model_licence": "CC BY 4.0", "stems": ["piano"], ...,
    "model": "not_downloaded" | "partial" | "downloaded",
    "download_bytes": 171966578, "disk_bytes": 171966578, "device": "cpu" | "cuda", ...},
   {"id": "ymt3", "label": "YourMT3+", "version": "5e66c1e", "licence": "Apache-2.0",
    "model_licence": "Apache-2.0", "stems": ["drums", "bass", "guitar", "piano", "other"], ...,
    "download_bytes": 561544628, "disk_bytes": 188412704, "device": "cpu" | "cuda",
    "seconds_per_minute": {"cuda": 20, "cpu": 90}, "uses_detail": false}],
 "stem_engines": {"vocals": ["basic_pitch"], "drums": ["ymt3"], "bass": ["basic_pitch", "ymt3"], ...},
 "default_engines": {"vocals": "basic_pitch", "drums": "ymt3", ...},
 "stems": ["vocals", "drums", "bass", "guitar", "piano", "other"], "max_seconds": 1200,
 "details": ["balanced", "detailed", "clean"]}
```

`available` and `reason` at the top are Basic Pitch's, whose vendored mido
writes every engine's file; each engine has its own. `model` is the weights by
their size on disk only: a run checks their SHA-256 before it uses them (for
`ymt3`, `partial` is also a checkpoint downloaded but not yet converted).
`device` is what a run would ask for now; `seconds_per_minute` the measured
seconds of work per minute of audio on each device (null when not measured);
`uses_detail` whether `transcribe_detail` changes the engine's notes.
`source` is `built_in`; an engine the person brings would say `user`.

`unsupported_platform` is Intel macOS, which has no onnxruntime; `no_runtime`
is a module the engine needs that is not installed; `engine_error` is a
vendored engine that is incomplete, or one that failed to import in a run
since the server started.

`POST /api/jobs/{id}/midi` takes `{"stem": <one of the six>, "detail"?}` and
answers `202 {"state": "running"}` at once. Without `detail` the
`transcribe_detail` setting is used. A body of the wrong shape (a missing or
non-string `stem`, more than 32 characters) is FastAPI's own `422`. Every
refusal is `{"detail": <fixed sentence>, "code": <code>}`, checked in this
order:

| Status | `code` | When |
|---|---|---|
| 404 | `job_not_found` | Not a job id, or no such job |
| 400 | `stem_not_supported` | `stem` is not one of the six |
| 400 | `bad_detail` | `detail` is not `balanced`, `detailed` or `clean` |
| 503 | `engine_unavailable` | The stem's engine cannot run here: the status above says `available: false`, or that engine's own `available` is false |
| 403 | `disabled` | `transcribe_<stem>` is `off` |
| 409 | `busy` | The song is queued or separating, or a fill, a Lead + Backing split, a re-extract or a MIDI run is working on it; or a MIDI run is going on any song; or the server is stopping or the stems folder is moving |
| 409 | `no_stem` | The song is not `done`, or has no such stem file |
| 413 | `too_long` | The song, or the stem file's header, is longer than `max_seconds` |

The run's progress and outcome are in the job state, per stem: poll
`GET /api/jobs/{id}` about once a second while `state` is `running` (the job's
event stream ends at once for a finished song, so it does not carry them):

```
"midi": {
  "piano": {
    "state": "running" | "done" | "error" | "cancelled",
    "pct": 0..100, "phase": "download" | "prepare" | "transcribe" | null,
    "notes": <int> | null, "engine": "basic_pitch" | "piano_hr" | "ymt3",
    "detail": "balanced" | "detailed" | "clean",
    "tempo_source": "song" | "default" | null,
    "at": "<ISO 8601, UTC>", "error_code": <code> | null,
    "has_file": true | false
  },
  "drums": {...}
}
```

`midi` is `{}` until the first run, and a stem is there only once it had one.
`state`, `pct`, `phase`, `detail`, `at` and `error_code` describe the latest
run; `notes`, `tempo_source` and `has_file` describe the file on disk. A run
that fails or is cancelled never touches the file the stem had: the new one is
written under a temp name and renamed into place only when it is complete, so
`has_file` stays `true` with the old file's notes. `error_code` is one of
`too_long`, `no_audio` (the stem cannot be read), `engine`, `disk` (no space
left), `io` (any other write error), `timeout`, `stalled` (no output for two
minutes), `interrupted` (the server stopped during the run; a run found
`running` at startup becomes this), `download` (an engine's weights could not
be fetched, or what arrived was not them) or `prepare` (YourMT3+'s weights
could not be made from its checkpoint). The state survives a restart: it is
kept in `registry.json` and in the song's `metadata.json`.

`phase` is set while a run works: `"download"` while an engine's checkpoint
downloads (its percent), `"prepare"` while YourMT3+'s weights are made from
its checkpoint (once; the checkpoint is then deleted), then `"transcribe"`.
The file the song had stays through a failed download or preparation. A
`piano_hr` or `ymt3` run that fails on the GPU is run again, whole, on the CPU
(`gpu_fallback` in `midi.json`).

`tempo_source` is `song` when the song's beat grid is trusted (edited by you,
or steady with at least 70% of its beats on an onset): the file then carries
the grid's tempo and first meter, and one lead-in bar from 0 s to a bar line of
the song, at the tempo that makes the song's bar lines the file's. `default`
is 120 BPM in 4/4 from 0 s. Either way the notes keep the times they were
heard at, so the file lines up with the stems from their first sample; they
are never quantised.

`POST /api/jobs/{id}/midi/{stem}/cancel` stops the run of that stem: the
worker and every process it started are killed, and the answer waits for the
run to record it (at most 15 s): `200 {"state": "cancelled"}`. With no run of
that stem going on it changes nothing and answers the stem's state, or
`{"state": null}` when it never had one. `404 job_not_found`, `400
stem_not_supported`.

`DELETE /api/jobs/{id}/midi/{stem}` removes the file, its `midi.json` record
and its entry: `200 {"deleted": "<stem>"}`. `409 busy` while a run of that
stem is going on, `404 no_midi` when there is none of the three, `500 failed`
when the file could not be removed.

`GET /api/jobs/{id}/stems/{stem}.mid` serves the file as `audio/midi` with
`Content-Disposition: attachment; filename="<song>_<stem>.mid"` (the same
name rule as a stem download), and `Cache-Control: no-cache`, since a run
replaces it under the same URL. `404 no_midi` when there is none or `stem` is
not one of the six.

On disk, per song: `midi/<stem>.mid`, and `midi.json`:

```
{"v": 1, "stems": {"piano": {
  "engine": "basic_pitch" | "piano_hr" | "ymt3", "engine_version": "0.4.0" | "0.0.6" | "5e66c1e",
  "vendor_id": "<16 hex>", "model_sha256": null | "<the weights' sha256>",
  "device": "cpu" | "cuda", "gpu_fallback": false,
  "stem", "detail", "params": {Basic Pitch's thresholds, note length and frequency range;
                               piano_hr's thresholds; ymt3's {"class_filter": <stem>}},
  "notes", "tempo_source", "bpm", "beats_per_bar", "bar_start",
  "created_at", "seconds_audio", "seconds_run"}}}
```

A song being deleted, moved with the stems folder or reset while a MIDI run
works on it is refused with `409 job_busy`, as for every other run on a
finished song. Deleting a song, emptying the Trash and the factory reset
remove its `midi/` folder and `midi.json` with the rest of it.

## Built-in models

`GET /api/models` lists every model StemDeck comes with, for Settings > Models:

```
{
  "models": [
    {"id": "htdemucs_6s", "group": "separation", "use": "stems",
     "name": "HT Demucs 6 stems", "stems": ["vocals", ...], "license": "MIT",
     "source": {"author": "Meta AI (Rouard, Massa, Défossez)",
                "url": "https://github.com/facebookresearch/demucs"},
     "state": "downloaded", "size_bytes": 54996327, "download_bytes": 54996327,
     "available": true},
    ...
  ],
  "remove": false
}
```

`group` is `separation` (the three separation models and the Karaoke 2 split),
`transcription` (Basic Pitch, ByteDance Piano Transcription, YourMT3+) or
`analysis` (Whisper for lyrics, the beat model). `use` is `stems`,
`lead_backing`, `midi` (Basic Pitch), `midi_piano` (ByteDance Piano
Transcription), `midi_multi` (YourMT3+, its state from the weights file it
keeps, `download_bytes` the checkpoint it downloads first), `lyrics`,
`lyrics_gpu`, `lyrics_cpu` or `beats`. `state` is `downloaded`, `partial` (some files, or a
pinned file at the wrong size), `not_downloaded`, `built_in` (Basic Pitch ships
inside StemDeck) or `unknown` (the folder could not be worked out, for example
when torch does not load). `size_bytes` is what is on disk now, or null;
`download_bytes` is a full download, or null when unknown. `license` is null
when the code does not know it. `source` is who made the model and the
original author's own page (never a re-upload), or null when it is not
verified (a model a deployment swapped in); the list is in
[Models](models.md#where-each-model-comes-from). No path is ever in the answer. What a model is
used for comes from `stem_models_resolved` and `transcribe_<stem>` /
`transcribe_<stem>_engine` in `GET /api/settings`, not from here. The song-sections
model is not listed: the Hugging Face hub caches it under a layout only its
package names. Built-in models are never removed through the API (`remove` is
false): a running separation may be reading the file.

`downloadable` says the Download button can fetch the model here: one this
install can run, whose files the code has pinned (every default model except
Basic Pitch, which is built in; not a Karaoke, beat or Whisper model a
deployment swapped in). `download` is the button's run, the same as
`GET /api/models/download`:

```
{"state": "idle" | "running" | "done" | "failed" | "cancelled",
 "model": "kim_mel_roformer" | null, "phase": "download" | "prepare" | null,
 "bytes": 120400000, "total": 913106900, "error_code": null}
```

`POST /api/models/{id}/download` starts one, the way the first job that needs
the model would fetch it, into the same file (app/core/model_get.py has each
family's path). One at a time. Every file is written under a temporary name
in a folder of the run's own beside its destination, checked against its
pinned size and SHA-256, flushed, and only then renamed into place, so a
download that is stopped or cut off never leaves a file that looks complete.
`phase` is `prepare` while YourMT3+'s weights are made from its checkpoint.
`error_code` is `network`, `disk`, `checksum`, `io`, `prepare` or `failed`;
the page says each in its own words, and no path is ever in an answer.

Refusals are `{detail, code}`: `404 not_found` (not a model the button can
download here), `409 downloaded` (its files are there already), `409 busy`
(another download runs; for a separation, Karaoke, Whisper or beat model, a
song separating or waiting, a split, a fill, a re-extract or a speed test; for
ByteDance Piano Transcription or YourMT3+, a MIDI run). A separation, Karaoke,
Whisper or beat model's download holds the pipeline lock, so a song added
meanwhile waits for it, and a MIDI run of an engine whose weights are
downloading is refused with `409 model_downloading`.
`POST /api/models/download/cancel` stops the process, removes what it left,
and answers with the state, `cancelled` once it has settled.

## My models

Models you add yourself (Settings > Models > My models). StemDeck ships
none and checks no licence: see [Models](models.md#my-models-bring-your-own).
The routes are refused from another site like every other write, and a link
add makes network calls, so it is refused while online sources are off.

`GET /api/my-models` answers

```
{
  "models": [<entry>, ...],
  "limits": {"max_bytes": 2147483648, "max_config_bytes": 262144, "max_models": 12, "max_params": 500000000},
  "registry_reset": false,
  "available": true
}
```

`registry_reset` is true when the list could not be read and was started again
(the old file is kept beside it). `available` is false on an install that
cannot run a model you add (no torch, or no audio-separator, as on Intel
macOS).

An entry:

| Field | Meaning |
|---|---|
| `id` | `user_` and 12 hex digits |
| `name` | The display name, at most 60 characters |
| `status` | `staging` (an upload being stored), `downloading`, `checking`, `ready`, `refused`, or `missing` (a ready model whose file is gone or a different size) |
| `pct`, `step` | Progress while it works. A link add downloads in 0 to 80 and checks in 80 to 100; an upload checks in 0 to 100. `step` is `info`, `config`, `model`, `read`, `build`, `run` or null |
| `code` | Why it was refused, or null; `changed` for a ready model whose file changed since its check |
| `size`, `sha256` | The model file's size and SHA-256, null until it is stored |
| `stems` | The stems you declared, in the order vocals, drums, bass, guitar, piano |
| `arch`, `params` | `mel_band_roformer` or `bs_roformer`, and its parameter count |
| `source` | `{kind: "hf" or "upload", label, license_declared, link}`. `label` is `owner/repo@<12 hex of the commit>` or the file name. `license_declared` is what the Hugging Face card says, shown as given and never checked. `link` is the Hugging Face address it was added from (an `https://huggingface.co/` address), or null for a model from files |
| `added_at`, `checked_at` | Epoch seconds |
| `cpu_rtf` | Seconds of work per second of audio on the CPU, from the check's test run |
| `usable`, `missing` | Whether separation can use it now, and whether its file is gone |

Poll `GET` about once a second while an entry is `staging`, `downloading` or
`checking`; only one add or check runs at a time.

`POST /api/my-models/link` takes JSON `{"link", "stems", "licence_confirmed": true, "name"?}`.
The link must be `https://huggingface.co/<owner>/<repo>`, optionally followed
by `/tree/<rev>`, `/blob/<rev>/<file>` or `/resolve/<rev>/<file>`; it is parsed,
never fetched as sent. `POST /api/my-models/upload` takes multipart `model`
(`.safetensors`, `.ckpt`, `.pth` or `.pt`), `config` (`.yaml` or `.yml`,
required), `stems` (a JSON list), `name` and `licence` (`"true"`).

Every refusal is `{"detail": <fixed sentence>, "code": <code>}` and never
carries a path, an exception or the link that was sent: `422` for the request
(`bad_link`, `stem_not_in_model`, `licence`, `too_large`, `empty`,
`config_missing`, `bad_format`, `bad_config`, `disk_full`), `409` for the state
(`busy`, `limit`, `duplicate`, `in_use`, `changed`), `403` `online_sources_off`
for a link while online sources are off, `404` for an unknown or malformed id.
A model that was added and then refused has one of `too_large`, `empty`,
`bad_format`, `bad_config`, `config_missing`, `unsupported_arch`,
`unsupported_audio`, `stem_not_in_model`, `too_big_model`, `key_mismatch`,
`gates_missing` (the checkpoint matches except for the attention gates),
`non_finite`, `load_failed`, `forward_failed`, `timeout`, `torch_missing`,
`torch_too_old`, `unavailable`, `duplicate`, `disk_full`, `network`,
`not_found`, `gated`, `ambiguous`, `bad_download`, `interrupted` or `changed`
as its `code`.

`POST .../check` on a link entry that kept no files (its download failed)
downloads it again, so it is refused with `403 online_sources_off` while
online sources are off; the page says so on that row.

`DELETE` refuses `409 in_use` while the song being separated uses the model.
Removing it sets every stem whose saved choice was that model back to `"auto"`.

In the settings answer, a usable model you added is in
`stem_model_catalog.models` with `"user": true`, `"engine": "user_roformer"`,
its declared `stems`, `license` (the declared one, or null), `size`, the
measured `cpu_rtf`, a `gpu_rtf` estimated from it and `"estimated": true`, and
its id is in `choices` of each stem it declares. One that is checking or
refused is in the My models list only. `stem_models.<stem>` may then be its id.

A song separated with a model that failed at run time is separated again with
`htdemucs_6s` for that model's stems; the job state's `stem_model_fallbacks`
says so: `{stem: {"model": id, "name", "code"}}`, or null. A Fill stems request
for a song whose plan names a model that was removed is refused with `409`
`model_removed`.

### Benchmark result

```
{
  "version": 1, "ts": <epoch seconds>, "stale": false,
  "hw": {"cpu", "cores", "gpu", "vram_mb", "devices", "torch"},
  "disk": {"write_mb_s", "skipped", "slow"},
  "devices": {"<device>": {"<model id>": {"rtf", "measured", "peak_mb"?, "estimated_from"?}}},
  "failed": {"<device>": "<code>"},
  "recommendation": {"device", "stems", "estimate_minutes_4min", "budget_minutes", "reason", "reasons"}
}
```

`rtf` is seconds of work per second of audio for a 4-minute song: the clip's
timing is scaled by the number of 7.8 s segments Demucs runs on each (3 for
the 12 s clip, 42 for 240 s). `measured: false` marks an estimate scaled from
the `htdemucs_6s` timing on that device, and `estimated_from` names it.
`peak_mb` is null on Windows CPU runs. `disk.skipped` is null or `low-space`,
`unavailable`, `name-taken`, `failed` or `timeout`.

`failed` lists the devices that could not be measured, each with `timeout`,
`stalled`, `device-unavailable`, `model-missing`, `torch-missing` or `failed`.
They are left out of `devices`, and until the benchmark runs again "auto" on such a
device uses `htdemucs_6s` for every stem.

`recommendation.reason` is one code, the first that applies: `gpu-failed`
(the recommendation is the CPU and a GPU failed to be measured),
`cpu-over-budget` (Kim cannot run on the chosen device and `htdemucs_6s` alone
is still over the budget), `no-gpu`, `gpu-slower-than-cpu` (the GPU is at least
10% slower than the CPU), `over-budget`, else `fits`. A slow songs folder
changes no plan: `disk-slow` is always in `reasons`, and is `reason` only in
place of `fits`. "Apply recommendation" has no
route of its own: the page posts `demucs_device` and all five `stem_models` as
`"auto"` to `POST /api/settings`.

## Take picture sync

A take with a camera records its picture in a MediaRecorder and its sound in
the capture graph, two recordings that start at different moments the browser
can only estimate (on a Logitech StreamCam the estimate was 430 to 530 ms
wrong). So the picture's recorder also records the camera's own microphone,
when the camera has one, and `POST .../finish` compares that sound with the
take's `audio.raw.wav` (GCC-PHAT on both at 8 kHz, within 2 s of the
estimate). Where they match is where the camera file starts on the take's
audio timeline. Without a clear match the estimate stands, and the take says
why.

One rule places the picture, for the in-app review (`camera_start_sec` in the
take entry) and every export (`POST .../render`, any `fit`):

```
pictureOffsetMs  = offsetMs - drawAgeMs + nudgeMs - cameraOffsetMs
camera_start_sec = first_sample_song_sec - user_nudge_ms / 1000 + pictureOffsetMs / 1000
```

`camera_start_sec` is song time as heard. Takes finished before this was added
have no stored block: they read as `source: "estimated"`, `reason: "legacy"`,
placed by their estimate as the review always placed them. Their Performance
export (`fit: "fill"`) no longer adds the take's round trip to the picture.

`video_sync`, in every take entry with a camera (null without one), and
stored in `take.json` as `video.sync` (without `pictureOffsetMs`):

| Field | Meaning |
|---|---|
| `source` | `measured`, `estimated`, or `manual` when `nudgeMs` is not 0 |
| `base` | `measured` or `estimated`: where `offsetMs` came from |
| `offsetMs` | The camera file's first frame on the take's audio timeline, ms after raw audio frame 0 (positive: the picture starts after the sound) |
| `estimateMs` | The browser's estimate (`video.firstFrameAtMs`) |
| `nudgeMs` | The manual correction, -2000 to 2000 ms, positive: the picture later |
| `confidence` | 0 to 1, 0.5 or more for a measured base; null when nothing was compared |
| `reason` | `ok`, `no-camera-audio`, `no-camera-mic`, `camera-mic-denied`, `camera-mic-not-asked` (microphone access not granted yet; a take never stops to ask), `camera-mic-failed`, `silent`, `low-confidence`, `measure-failed` or `legacy` |
| `measuredAt` | Epoch seconds of the measurement, or null |
| `metrics` | Diagnostics of the best window (`pmr`, `peakRatio`, `widthMs`, `windowSec`, `windowStartSec`); not a stable shape |
| `drawAgeMs` | Performance only (entry only, from `video.drawAgeMs`): how old the camera frame was when the canvas drew it, median over the take, 0 to 1000; the recorded frames carry the draw time, so the picture is placed that much earlier. 0 when the engine did not report it |
| `pictureOffsetMs` | The sum above (entry only) |

`POST .../sync` takes `{"nudgeMs": 40}` (absolute, a JSON number), `{"remeasure":
true}`, or both. A changed `pictureOffsetMs` deletes `render.mp4` and
`resolve.otioz`: `GET .../render` and `GET .../resolve` then answer `status:
"none"` and the export runs again.
Refusals: 404 for an unknown or malformed job or take, 409 `take not finished`
or `video render running`, 503 `measurement could not run: nothing changed` (a
`remeasure` whose ffmpeg or file read failed: the stored sync, the nudge and
the render are kept, nothing is written), 422 `nothing to change`, `take has no camera`, or a
body out of range, not a number or with another key.

The camera's microphone is opened by `static/js/video/cameraAudio.js` (the
audio input with the camera's group id, echo cancellation, noise suppression
and gain control off; the take's own input is cloned when it is that same
microphone). It is never played and never mixed: the render maps only the
camera file's picture. `video.cameraAudio.state` in the finish body says
whether it was recorded: `recorded`, `no-mic`, `denied`, `not-asked`, `failed`
(also when the sound failed part way through the take) or `unsupported`.
Clear windows that disagree by more than 40 ms give no measurement; otherwise
the earliest clear window gives the offset.

## DaVinci Resolve package of a take

Performance's Export Performance menu makes either the MP4 (`POST
.../takes/{take_id}/render`) or an OpenTimelineIO bundle for DaVinci Resolve
18.5 or newer (File > Import > Timeline), `resolve.otioz` beside `render.mp4`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/jobs/{id}/takes/{take_id}/resolve` | Start it: the render's body (lanes, gains, pitches, `lanes`, `fx`, `take_gain`, `take_print`, `canvas`, `fit`, `size`, `fps`, `span`, `range_in`, `range_out`, `click`, `click_sound`, `remove_stem`) plus `backing` (`mix`: one backing track; `stems`: one track per lane as heard, a muted lane left out) and `name` (the timeline's name). Never `master`. `202` with the status |
| GET | `/api/jobs/{id}/takes/{take_id}/resolve` | `{status, progress, error, url, estimate_bytes, large}`; `large` when the estimate is over 4 GiB (ZIP64) |
| POST | `/api/jobs/{id}/takes/{take_id}/resolve/cancel` | Stop it; the MP4 render has its own cancel |
| GET | `/api/jobs/{id}/takes/{take_id}/resolve.otioz?name=` | The file (`application/zip`); `name` must end in `.otioz` |

The bundle holds `version.txt` (`1.0.0`), `content.otio` (a `Timeline.1` at
01:00:00:00 with one video track, then the take, the backing or one track per
lane, and the click when asked) and `media/<song>_video.mp4`,
`<song>_take_<take_id>.wav`, `<song>_backing.wav` or `<song>_<lane>.wav`,
`<song>_click.wav`. Every file starts at the span's start and is exactly N
frames long: H.264 at the take's rate (24, 30 or 60), no sound, no B-frames,
start 0; WAV 48 kHz, 24-bit, stereo, N x (48000 / fps) samples. Refusals as the
render's (404, 422 `take has no camera` or no libx264), plus 422 `this song has
no beat grid` for a click, 409 while an export of either kind runs for the take,
and 507 `not enough free disk space for this export` (under 2.2 times the
estimate).
